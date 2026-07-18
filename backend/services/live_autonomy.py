from __future__ import annotations

import math
import time
import uuid
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, is_state_db_path, state_table_exists
from backend.services._brain_helpers import connect as _connect, dumps as _dumps, execute as _execute, loads as _loads, safe_float as _safe_float
from backend.services.v16_brain_planning import BrainLiveReadyGuardrailService
from backend.services.proposal_registry import ProposalRegistryService
from backend.services.governance_control_plans import AutonomyControlPlan
from backend.services.live_safety_state import (
    SafetyStatePersistenceError,
    activate_no_new_risk_latch,
    append_safety_outbox,
    no_new_risk_latch_status,
)
from config import runtime_config
from risk.policy_service import RiskPolicyService


READINESS_MAX_AGE_SECONDS = 5 * 60.0
REPLAY_MAX_AGE_SECONDS = 24 * 60 * 60.0
RELEASE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60.0
UNLOCK_EVENT_MAX_AGE_SECONDS = 24 * 60 * 60.0


def ensure_live_autonomy_unlock_table(db_path: str | Path = STATE_DB) -> None:
    conn = _connect(db_path)
    try:
        _execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS live_autonomy_unlock_event (
                event_id TEXT PRIMARY KEY,
                action TEXT DEFAULT '',
                status TEXT DEFAULT '',
                actor TEXT DEFAULT '',
                reason TEXT DEFAULT '',
                autonomy_mode_before TEXT DEFAULT '',
                autonomy_mode_after TEXT DEFAULT '',
                readiness_json TEXT NOT NULL DEFAULT '{}',
                proposal_registry_json TEXT NOT NULL DEFAULT '{}',
                risk_verdict_json TEXT NOT NULL DEFAULT '{}',
                blockers_json TEXT NOT NULL DEFAULT '[]',
                mutation_json TEXT NOT NULL DEFAULT '{}',
                boundary_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL DEFAULT 0.0
            )
            """,
        )
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_live_autonomy_unlock_created ON live_autonomy_unlock_event(created_at)")
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_live_autonomy_unlock_status ON live_autonomy_unlock_event(status, created_at)")
        conn.commit()
    finally:
        conn.close()


class LiveAutonomyService:
    """Governed live-autonomous unlock and status service.

    Unlock is an operator-controlled capability change. The service writes an
    audit event and persists runtime overlay through RuntimeConfigMutationService;
    it does not submit orders or bypass RiskPolicyService.
    """

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = db_path

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {
            "schema_version": "live_autonomy_boundary.v1",
            "manual_one_time_unlock_required": True,
            "does_not_submit_orders": True,
            "does_not_bypass_risk_policy": True,
            "runtime_config_mutation_service_required": True,
            "typed_autonomy_control_plan_required": True,
            "risk_direction_from_before_after": True,
            "unlock_requires_v16": True,
            "overlay_snapshot_required": True,
            "llm_advisory_only": True,
            "revoke_sets_live_candidate": True,
            "revoke_activates_local_no_new_risk_first": True,
            "revoke_does_not_depend_on_postgres": True,
        }

    def status(self, *, readiness: dict[str, Any] | None = None, refresh_proposals: bool = False) -> dict[str, Any]:
        ensure_live_autonomy_unlock_table(self.db_path)
        readiness = readiness or self._build_readiness()
        evaluation = self.evaluate(readiness=readiness, refresh_proposals=refresh_proposals, persist=False)
        latest = self.latest_event()
        cfg = runtime_config.shared()
        local_safety = (
            no_new_risk_latch_status(fail_closed=True)
            if is_state_db_path(self.db_path)
            else {"active": False, "state": "isolated_state"}
        )
        unlock_freshness = self._unlock_event_freshness(latest_event=latest)
        operational_posture = self._operational_posture(
            autonomy_mode=str(getattr(cfg, "autonomy_mode", "") or "manual"),
            unlocked=bool(getattr(cfg, "live_autonomy_unlocked", False)),
            evaluation=evaluation,
            unlock_freshness=unlock_freshness,
            local_safety_latch=local_safety,
        )
        return {
            "ok": True,
            "schema_version": "live_autonomy_status.v1",
            "autonomy_mode": str(getattr(cfg, "autonomy_mode", "") or "manual"),
            "live_autonomy_unlocked": bool(getattr(cfg, "live_autonomy_unlocked", False)),
            "live_autonomy_unlock_id": str(getattr(cfg, "live_autonomy_unlock_id", "") or ""),
            "autonomy_expansion_frozen": bool(
                getattr(cfg, "autonomy_expansion_frozen", True)
            ),
            "governance_expansion_paused": bool(
                getattr(cfg, "governance_expansion_paused", False)
            ),
            "operational_posture": operational_posture,
            "evaluation": evaluation,
            "latest_event": latest,
            "unlock_event_freshness": unlock_freshness,
            "local_safety_latch": local_safety,
            "boundary": self.boundary(),
        }

    def evaluate(
        self,
        *,
        readiness: dict[str, Any] | None = None,
        refresh_proposals: bool = True,
        persist: bool = True,
        actor: str = "api:ops.autonomy.live_unlock",
        reason: str = "",
    ) -> dict[str, Any]:
        ensure_live_autonomy_unlock_table(self.db_path)
        readiness = readiness or self._build_readiness()
        proposal_service = ProposalRegistryService(self.db_path)
        proposal_summary = proposal_service.refresh().get("summary", {}) if refresh_proposals else proposal_service.status()
        guardrail = BrainLiveReadyGuardrailService(self.db_path).evaluate(
            readiness=readiness,
            persist=False,
            source="system:live_autonomy_unlock.evaluate",
        )
        evidence_freshness = self._evidence_freshness(readiness=readiness)
        blockers = self._blockers(
            readiness=readiness,
            proposal_summary=proposal_summary,
            guardrail=guardrail,
            evidence_freshness=evidence_freshness,
        )
        risk_verdict = RiskPolicyService.shared().evaluate(
            "live_autonomy_budget",
            self._risk_context(readiness=readiness),
        ).to_dict()
        if not bool(risk_verdict.get("allowed")):
            blockers.append({"component": "risk_policy_budget", "status": "blocked", "reason": risk_verdict.get("reason", "")})
        budget_response = self._budget_breach_response(risk_verdict)
        status = "unlock_ready" if not blockers else "blocked"
        payload = {
            "ok": not blockers,
            "schema_version": "live_autonomy_unlock_evaluation.v1",
            "status": status,
            "blockers": blockers,
            "evidence_freshness": evidence_freshness,
            "readiness_generated_at": readiness.get("generated_at"),
            "proposal_registry": proposal_summary,
            "guardrail": {
                "status": guardrail.get("status", ""),
                "live_capability_lock": guardrail.get("live_capability_lock") or {},
                "broker_local_divergence": guardrail.get("broker_local_divergence") or {},
            },
            "risk_verdict": risk_verdict,
            "budget_breach_response": budget_response,
            "boundary": self.boundary(),
        }
        if persist:
            self._record_event(
                action="evaluate",
                status=status,
                actor=actor,
                reason=reason,
                readiness=readiness,
                proposal_summary=proposal_summary,
                risk_verdict=risk_verdict,
                blockers=blockers,
                mutation={},
            )
        return payload

    def unlock(
        self,
        *,
        actor: str = "api:ops.autonomy.live_unlock",
        reason: str = "",
        confirm: bool = False,
        readiness: dict[str, Any] | None = None,
        v16_command_id: str = "",
        v16_claim_token: str = "",
    ) -> dict[str, Any]:
        readiness = readiness or self._build_readiness()
        evaluation = self.evaluate(readiness=readiness, refresh_proposals=True, persist=False, actor=actor, reason=reason)
        if not confirm:
            return {
                "ok": False,
                "schema_version": "live_autonomy_unlock.v1",
                "status": "confirm_required",
                "evaluation": evaluation,
                "boundary": self.boundary(),
            }
        if not bool(evaluation.get("ok")):
            event = self._record_event(
                action="unlock",
                status="blocked",
                actor=actor,
                reason=reason,
                readiness=readiness,
                proposal_summary=evaluation.get("proposal_registry") or {},
                risk_verdict=evaluation.get("risk_verdict") or {},
                blockers=evaluation.get("blockers") or [],
                mutation={},
            )
            return {
                "ok": False,
                "schema_version": "live_autonomy_unlock.v1",
                "status": "blocked",
                "evaluation": evaluation,
                "event": event,
                "boundary": self.boundary(),
            }
        before_mode = str(getattr(runtime_config.shared(), "autonomy_mode", "") or "manual")
        event_id = f"live_unlock_{uuid.uuid4().hex[:16]}"
        plan = AutonomyControlPlan(
            patch={
                "autonomy_mode": "live_autonomous",
                "live_autonomy_unlocked": True,
                "live_autonomy_unlock_id": event_id,
            },
            source="live_autonomy_unlock",
            run_id=event_id,
            actor=actor,
            action="live_autonomy_unlock",
            reason=reason or "manual one-time live autonomy unlock",
            scope_type="autonomy_control",
            scope_key="live_autonomy",
            target_agent="governance_control",
            rollback={
                "autonomy_mode": before_mode,
                "live_autonomy_unlocked": bool(
                    getattr(runtime_config.shared(), "live_autonomy_unlocked", False)
                ),
                "live_autonomy_unlock_id": str(
                    getattr(runtime_config.shared(), "live_autonomy_unlock_id", "") or ""
                ),
            },
            evidence_refs={"evaluation": evaluation},
            v16_command_id=str(v16_command_id or ""),
            v16_claim_token=str(v16_claim_token or ""),
            current_mode=before_mode,
            target_mode="live_autonomous",
            unlock_event_id=event_id,
        )
        try:
            mutation = plan.execute(self.db_path)
        except Exception as exc:
            mutation = {
                "ok": False,
                "status": "governance_mutation_unavailable",
                "reason": f"{type(exc).__name__}: {exc}",
            }
        event = self._record_event(
            action="unlock",
            status="unlocked" if mutation.get("ok") else "mutation_failed",
            actor=actor,
            reason=reason,
            readiness=readiness,
            proposal_summary=evaluation.get("proposal_registry") or {},
            risk_verdict=evaluation.get("risk_verdict") or {},
            blockers=[] if mutation.get("ok") else [{"component": "runtime_config_mutation", "status": "failed"}],
            mutation=mutation,
            event_id=event_id,
            before_mode=before_mode,
            after_mode="live_autonomous" if mutation.get("ok") else before_mode,
        )
        return {
            "ok": bool(mutation.get("ok")),
            "schema_version": "live_autonomy_unlock.v1",
            "status": "unlocked" if mutation.get("ok") else "mutation_failed",
            "evaluation": evaluation,
            "mutation": mutation,
            "event": event,
            "boundary": self.boundary(),
        }

    def revoke(
        self,
        *,
        actor: str = "api:ops.autonomy.live_unlock",
        reason: str = "",
    ) -> dict[str, Any]:
        before_mode = str(getattr(runtime_config.shared(), "autonomy_mode", "") or "manual")
        event_id = f"live_revoke_{uuid.uuid4().hex[:16]}"
        latch: dict[str, Any] = {}
        latch_error = ""
        if is_state_db_path(self.db_path):
            try:
                latch = activate_no_new_risk_latch(
                    reason=reason or "operator revoked live autonomous mode",
                    actor=actor,
                    correlation_id=event_id,
                    metadata={"current_mode": before_mode, "target_mode": "live_candidate"},
                )
            except SafetyStatePersistenceError as exc:
                latch_error = str(exc)
                latch = no_new_risk_latch_status(fail_closed=True)

        plan = AutonomyControlPlan(
            patch={
                "autonomy_mode": "live_candidate",
                "live_autonomy_unlocked": False,
                "live_autonomy_unlock_id": "",
            },
            source="live_autonomy_revoke",
            run_id=event_id,
            actor=actor,
            action="live_autonomy_revoke",
            reason=reason or "operator revoked live autonomous mode",
            scope_type="autonomy_control",
            scope_key="live_autonomy",
            target_agent="governance_control",
            rollback={
                "autonomy_mode": before_mode,
                "live_autonomy_unlocked": bool(
                    getattr(runtime_config.shared(), "live_autonomy_unlocked", False)
                ),
                "live_autonomy_unlock_id": str(
                    getattr(runtime_config.shared(), "live_autonomy_unlock_id", "") or ""
                ),
            },
            evidence_refs={"operator_reason": reason},
            current_mode=before_mode,
            target_mode="live_candidate",
            unlock_event_id=event_id,
        )
        try:
            mutation = plan.execute(self.db_path)
        except Exception as exc:
            mutation = {
                "ok": False,
                "status": "governance_mutation_unavailable",
                "reason": f"{type(exc).__name__}: {exc}",
            }
        event: dict[str, Any] = {}
        event_error = ""
        try:
            event = self._record_event(
                action="revoke",
                status="revoked" if mutation.get("ok") else "projection_pending",
                actor=actor,
                reason=reason,
                readiness=self._build_readiness(),
                proposal_summary=ProposalRegistryService(self.db_path).status(),
                risk_verdict={},
                blockers=[] if mutation.get("ok") else [{"component": "runtime_config_mutation", "status": "failed"}],
                mutation=mutation,
                event_id=event_id,
                before_mode=before_mode,
                after_mode="live_candidate" if mutation.get("ok") else before_mode,
            )
        except Exception as exc:
            event_error = f"{type(exc).__name__}: {exc}"

        safety_effective = bool(
            is_state_db_path(self.db_path)
            and no_new_risk_latch_status(fail_closed=True).get("active")
        )
        outbox: dict[str, Any] = {}
        if is_state_db_path(self.db_path) and (not mutation.get("ok") or event_error):
            try:
                outbox = append_safety_outbox(
                    event_type="live_autonomy_revoke_projection_pending",
                    correlation_id=event_id,
                    payload={
                        "before_mode": before_mode,
                        "target_mode": "live_candidate",
                        "mutation": mutation,
                        "event": event,
                    },
                    error=event_error or str(mutation.get("status") or "mutation_failed"),
                )
            except Exception as exc:
                outbox = {
                    "status": "outbox_append_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
        ok = bool(mutation.get("ok")) or safety_effective
        status = "revoked" if mutation.get("ok") else (
            "local_safety_latched_projection_pending"
            if safety_effective
            else "mutation_failed"
        )
        return {
            "ok": ok,
            "schema_version": "live_autonomy_revoke.v1",
            "status": status,
            "mutation": mutation,
            "event": event,
            "event_error": event_error,
            "local_safety_latch": latch,
            "local_safety_effective": safety_effective,
            "latch_persistence_error": latch_error,
            "safety_outbox": outbox,
            "governance_projection_pending": bool(safety_effective and not mutation.get("ok")),
            "boundary": self.boundary(),
        }

    def latest_event(self) -> dict[str, Any]:
        ensure_live_autonomy_unlock_table(self.db_path)
        conn = _connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, "live_autonomy_unlock_event"):
                return {"ok": False, "status": "missing_table"}
            row = _execute(
                conn,
                """
                SELECT *
                FROM live_autonomy_unlock_event
                ORDER BY created_at DESC
                LIMIT 1
                """,
            ).fetchone()
            if not row:
                return {"ok": False, "status": "none"}
            return self._row_to_event(row)
        finally:
            conn.close()

    def _blockers(
        self,
        *,
        readiness: dict[str, Any],
        proposal_summary: dict[str, Any],
        guardrail: dict[str, Any],
        evidence_freshness: dict[str, Any],
    ) -> list[dict[str, Any]]:
        blockers: list[dict[str, Any]] = []
        live = dict(readiness.get("live") or {})
        ctrader = dict(live.get("ctrader") or {})
        loop = dict(live.get("loop") or {})
        ready_for_live_alpha = readiness.get("ready_for_live_alpha")
        if ready_for_live_alpha is None:
            # Compatibility for stored/read-only v1 readiness fixtures.  This
            # derives from live facts and never from ready_for_frontend.
            ready_for_live_alpha = bool(
                str(ctrader.get("status") or "").lower() == "connected"
                and loop.get("running")
                and not readiness.get("blockers")
            )
        if not bool(ready_for_live_alpha):
            blockers.append(
                {
                    "component": "live_alpha_readiness",
                    "status": "blocked",
                    "details": (
                        (readiness.get("readiness_dimensions") or {})
                        .get("blockers", {})
                        .get("live_alpha", [])
                    ),
                }
            )
        ready_for_mutation = readiness.get("ready_for_autonomous_mutation")
        if ready_for_mutation is None:
            ready_for_mutation = not readiness.get("blockers")
        if not bool(ready_for_mutation):
            blockers.append(
                {
                    "component": "autonomous_mutation_readiness",
                    "status": "blocked",
                    "details": (
                        (readiness.get("readiness_dimensions") or {})
                        .get("blockers", {})
                        .get("autonomous_mutation", [])
                    ),
                }
            )
        for key, payload in (evidence_freshness.get("items") or {}).items():
            if isinstance(payload, dict) and bool(payload.get("stale")):
                blockers.append({
                    "component": str(key),
                    "status": "stale_evidence",
                    "reason": str(payload.get("reason") or payload.get("status") or "stale"),
                    "age_seconds": payload.get("age_seconds"),
                    "stale_after_seconds": payload.get("stale_after_seconds"),
                })
        if str(ctrader.get("status") or "").lower() != "connected":
            blockers.append({"component": "ctrader", "status": str(ctrader.get("status") or "unknown")})
        if not bool(loop.get("running")):
            blockers.append({"component": "live_loop", "status": "not_running"})
        incident = dict(readiness.get("incident_control") or {})
        if str(incident.get("mode") or "normal") != "normal":
            blockers.append({"component": "incident_control", "status": str(incident.get("mode") or "unknown")})
        release = dict(readiness.get("release") or {})
        latest_release = dict(release.get("latest_release") or release.get("release") or {})
        rollback_ref = latest_release.get("rollback_ref") or latest_release.get("rollback_ref_json") or release.get("rollback_ref") or {}
        if not bool(release.get("ok")):
            blockers.append({"component": "release", "status": "missing_release"})
        if isinstance(rollback_ref, str):
            rollback_ref = _loads(rollback_ref, {})
        if not (isinstance(rollback_ref, dict) and rollback_ref.get("snapshot_hash")):
            blockers.append({"component": "release", "status": "missing_snapshot_rollback_ref"})
        replay = dict(readiness.get("replay") or {})
        if not bool(replay.get("ok")):
            blockers.append({"component": "replay", "status": "missing_or_failed"})
        divergence = dict(guardrail.get("broker_local_divergence") or {})
        if bool(divergence.get("divergence_detected")) or str(divergence.get("status") or "") == "divergent":
            blockers.append({"component": "broker_local_divergence", "status": str(divergence.get("status") or "divergent")})
        if int(proposal_summary.get("high_unresolved_conflict_count") or 0) > 0:
            blockers.append({
                "component": "proposal_registry",
                "status": "high_unresolved_conflicts",
                "count": int(proposal_summary.get("high_unresolved_conflict_count") or 0),
            })
        return blockers

    def _evidence_freshness(self, *, readiness: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        items = {
            "readiness": self._freshness_item(
                timestamp=_safe_float(readiness.get("generated_at")),
                now=now,
                stale_after=READINESS_MAX_AGE_SECONDS,
                required=True,
            ),
            "replay": self._freshness_from_payload(
                readiness.get("replay") or {},
                now=now,
                stale_after=REPLAY_MAX_AGE_SECONDS,
                required=True,
            ),
            "release": self._freshness_from_payload(
                (readiness.get("release") or {}).get("latest_release") or readiness.get("release") or {},
                now=now,
                stale_after=RELEASE_MAX_AGE_SECONDS,
                required=True,
            ),
        }
        stale_keys = [key for key, item in items.items() if bool(item.get("stale"))]
        return {
            "schema_version": "live_autonomy_evidence_freshness.v1",
            "ok": not stale_keys,
            "status": "fresh" if not stale_keys else "stale",
            "stale_keys": stale_keys,
            "items": items,
        }

    @staticmethod
    def _freshness_from_payload(payload: dict[str, Any], *, now: float, stale_after: float, required: bool) -> dict[str, Any]:
        if not isinstance(payload, dict):
            payload = {}
        timestamp = (
            _safe_float(payload.get("created_at"))
            or _safe_float(payload.get("updated_at"))
            or _safe_float(payload.get("finished_at"))
            or _safe_float(payload.get("generated_at"))
        )
        if timestamp <= 0 and "age_seconds" in payload:
            raw_age = payload.get("age_seconds")
            try:
                age = float(raw_age)
            except (TypeError, ValueError):
                age = float("nan")
            if not math.isfinite(age) or age < 0.0:
                return LiveAutonomyService._freshness_item(
                    timestamp=0.0,
                    now=now,
                    stale_after=stale_after,
                    required=required,
                )
            stale_after = _safe_float(payload.get("stale_after_seconds"), stale_after)
            stale = age > stale_after or bool(payload.get("stale", False))
            return {
                "schema_version": "live_autonomy_freshness_item.v1",
                "status": "stale" if stale else "fresh",
                "stale": stale,
                "age_seconds": round(age, 3),
                "stale_after_seconds": stale_after,
                "timestamp": None,
                "reason": "age_seconds",
            }
        return LiveAutonomyService._freshness_item(
            timestamp=timestamp,
            now=now,
            stale_after=stale_after,
            required=required,
        )

    @staticmethod
    def _freshness_item(*, timestamp: float, now: float, stale_after: float, required: bool) -> dict[str, Any]:
        if timestamp <= 0:
            return {
                "schema_version": "live_autonomy_freshness_item.v1",
                "status": "missing_timestamp",
                "stale": bool(required),
                "age_seconds": None,
                "stale_after_seconds": stale_after,
                "timestamp": None,
                "reason": "missing_timestamp",
            }
        age = max(0.0, now - timestamp)
        stale = age > stale_after
        return {
            "schema_version": "live_autonomy_freshness_item.v1",
            "status": "stale" if stale else "fresh",
            "stale": stale,
            "age_seconds": round(age, 3),
            "stale_after_seconds": stale_after,
            "timestamp": timestamp,
        }

    def _unlock_event_freshness(self, *, latest_event: dict[str, Any]) -> dict[str, Any]:
        if not latest_event or not bool(latest_event.get("ok")):
            return {
                "schema_version": "live_autonomy_unlock_event_freshness.v1",
                "status": "missing",
                "stale": True,
                "age_seconds": None,
                "stale_after_seconds": UNLOCK_EVENT_MAX_AGE_SECONDS,
            }
        item = self._freshness_item(
            timestamp=_safe_float(latest_event.get("created_at")),
            now=time.time(),
            stale_after=UNLOCK_EVENT_MAX_AGE_SECONDS,
            required=True,
        )
        item["schema_version"] = "live_autonomy_unlock_event_freshness.v1"
        return item

    @staticmethod
    def _operational_posture(
        *,
        autonomy_mode: str,
        unlocked: bool,
        evaluation: dict[str, Any],
        unlock_freshness: dict[str, Any],
        local_safety_latch: dict[str, Any],
    ) -> dict[str, Any]:
        live_mode = str(autonomy_mode or "").lower() == "live_autonomous"
        local_safety_active = bool(local_safety_latch.get("active"))
        degraded = live_mode and (
            not unlocked
            or not bool(evaluation.get("ok"))
            or bool(unlock_freshness.get("stale"))
            or local_safety_active
        )
        reasons = []
        if live_mode and not unlocked:
            reasons.append("live_autonomy_not_unlocked")
        if live_mode and not bool(evaluation.get("ok")):
            reasons.append("unlock_evidence_not_current")
        if live_mode and bool(unlock_freshness.get("stale")):
            reasons.append("unlock_event_stale")
        if local_safety_active:
            reasons.append("local_no_new_risk_latched")
        return {
            "schema_version": "live_autonomy_operational_posture.v1",
            "status": (
                "safety_latched"
                if local_safety_active
                else "degraded"
                if degraded
                else "ok"
                if live_mode and unlocked
                else "locked"
            ),
            "degraded": degraded,
            "reasons": reasons,
            "recommended_incident_mode": (
                "no_new_risk" if degraded or local_safety_active else "normal"
            ),
            "blocks_new_risk": degraded or local_safety_active,
            "allows_risk_reducing_actions": True,
        }

    @staticmethod
    def _budget_breach_response(risk_verdict: dict[str, Any]) -> dict[str, Any]:
        breached = str(risk_verdict.get("reason") or "") == "live_autonomy_budget_breach"
        return {
            "schema_version": "live_autonomy_budget_response.v1",
            "breached": breached,
            "recommended_incident_mode": "no_new_risk" if breached else "normal",
            "blocks_new_risk": breached,
            "allows_risk_reducing_actions": True,
            "proposal_registry_source": "live_autonomy_unlock_event" if breached else "",
            "incident_audit_required": breached,
        }

    def _risk_context(self, *, readiness: dict[str, Any]) -> dict[str, Any]:
        live = dict(readiness.get("live") or {})
        return {
            "runtime_incident_mode": str((readiness.get("incident_control") or {}).get("mode") or "normal"),
            "loop_running": bool((live.get("loop") or {}).get("running", True)),
            "bridge_connected": str((live.get("ctrader") or {}).get("status") or "").lower() == "connected",
            "session": (live.get("session") or readiness.get("session") or {}),
            "account": (live.get("account") or readiness.get("account") or {}),
            "risk_limits": (live.get("risk_limits") or readiness.get("risk_limits") or {}),
        }

    def _record_event(
        self,
        *,
        action: str,
        status: str,
        actor: str,
        reason: str,
        readiness: dict[str, Any],
        proposal_summary: dict[str, Any],
        risk_verdict: dict[str, Any],
        blockers: list[dict[str, Any]],
        mutation: dict[str, Any],
        event_id: str | None = None,
        before_mode: str | None = None,
        after_mode: str | None = None,
    ) -> dict[str, Any]:
        ensure_live_autonomy_unlock_table(self.db_path)
        event_id = event_id or f"live_autonomy_{uuid.uuid4().hex[:16]}"
        before_mode = before_mode if before_mode is not None else str(getattr(runtime_config.shared(), "autonomy_mode", "") or "manual")
        after_mode = after_mode if after_mode is not None else before_mode
        now = time.time()
        conn = _connect(self.db_path)
        try:
            _execute(
                conn,
                """
                INSERT INTO live_autonomy_unlock_event
                (event_id, action, status, actor, reason, autonomy_mode_before,
                 autonomy_mode_after, readiness_json, proposal_registry_json,
                 risk_verdict_json, blockers_json, mutation_json, boundary_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    action,
                    status,
                    actor,
                    reason,
                    before_mode,
                    after_mode,
                    _dumps({
                        "generated_at": readiness.get("generated_at"),
                        "ready_for_live_alpha": readiness.get("ready_for_live_alpha"),
                        "ready_for_autonomous_mutation": readiness.get("ready_for_autonomous_mutation"),
                        "readiness_dimensions": readiness.get("readiness_dimensions") or {},
                        "blockers": readiness.get("blockers") or [],
                    }),
                    _dumps(proposal_summary),
                    _dumps(risk_verdict),
                    _dumps(blockers),
                    _dumps(mutation),
                    _dumps(self.boundary()),
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return {
            "ok": status not in {"blocked", "mutation_failed"},
            "event_id": event_id,
            "action": action,
            "status": status,
            "actor": actor,
            "reason": reason,
            "autonomy_mode_before": before_mode,
            "autonomy_mode_after": after_mode,
            "created_at": now,
            "boundary": self.boundary(),
        }

    def _row_to_event(self, row: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "schema_version": "live_autonomy_unlock_event.v1",
            "event_id": str(row["event_id"] or ""),
            "action": str(row["action"] or ""),
            "status": str(row["status"] or ""),
            "actor": str(row["actor"] or ""),
            "reason": str(row["reason"] or ""),
            "autonomy_mode_before": str(row["autonomy_mode_before"] or ""),
            "autonomy_mode_after": str(row["autonomy_mode_after"] or ""),
            "readiness": _loads(row["readiness_json"], {}),
            "proposal_registry": _loads(row["proposal_registry_json"], {}),
            "risk_verdict": _loads(row["risk_verdict_json"], {}),
            "blockers": _loads(row["blockers_json"], []),
            "mutation": _loads(row["mutation_json"], {}),
            "boundary": _loads(row["boundary_json"], {}),
            "created_at": _safe_float(row["created_at"]),
        }

    @staticmethod
    def _build_readiness() -> dict[str, Any]:
        from backend.services.backend_readiness import BackendReadinessService

        return BackendReadinessService().build()
