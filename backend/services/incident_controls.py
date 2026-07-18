from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, connect_sqlite, get_state_pg_conn, is_state_db_path, state_table_exists
from backend.services.governance_control_plans import IncidentControlPlan
from backend.services.live_safety_state import (
    SafetyStatePersistenceError,
    activate_no_new_risk_latch,
    append_safety_outbox,
    clear_no_new_risk_latch,
    no_new_risk_latch_status,
)
from config.runtime_config import shared as runtime_config
from risk.policy_service import INCIDENT_MODE_RANK, INCIDENT_MODES, RiskPolicyService


def _dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, default=str)


def _loads(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except Exception:
        return default


def _use_pg(db_path: str | Path = STATE_DB) -> bool:
    return is_state_db_path(db_path)


def _connect(db_path: str | Path = STATE_DB, *, read_only: bool = False):
    conn = get_state_pg_conn(read_only=read_only) if _use_pg(db_path) else connect_sqlite(db_path, read_only=read_only)
    if not _use_pg(db_path):
        conn.row_factory = __import__("sqlite3").Row
    return conn


def _conn_is_pg(conn) -> bool:
    return conn.__class__.__module__.split(".", 1)[0] == "psycopg"


def _sql(conn, sql: str) -> str:
    return sql.replace("%", "%%").replace("?", "%s") if _conn_is_pg(conn) else sql


def _execute(conn, sql: str, params: Any = None):
    if params is None:
        return conn.execute(_sql(conn, sql))
    return conn.execute(_sql(conn, sql), params)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def ensure_incident_playbook_run_table(db_path: str | Path = STATE_DB) -> None:
    conn = _connect(db_path)
    try:
        _execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS incident_playbook_run (
                playbook_id TEXT PRIMARY KEY,
                scenario TEXT DEFAULT '',
                severity TEXT DEFAULT '',
                current_mode TEXT DEFAULT '',
                target_mode TEXT DEFAULT '',
                status TEXT DEFAULT '',
                steps_json TEXT NOT NULL DEFAULT '[]',
                risk_precheck_json TEXT NOT NULL DEFAULT '{}',
                release_ref_json TEXT NOT NULL DEFAULT '{}',
                boundary_json TEXT NOT NULL DEFAULT '{}',
                created_by TEXT DEFAULT '',
                created_at REAL NOT NULL DEFAULT 0.0
            )
            """,
        )
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_incident_playbook_created ON incident_playbook_run(created_at)")
        _execute(
            conn,
            "CREATE INDEX IF NOT EXISTS idx_incident_playbook_scenario ON incident_playbook_run(scenario, created_at)",
        )
        _execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS incident_playbook_event (
                event_id TEXT PRIMARY KEY,
                playbook_id TEXT NOT NULL,
                event_type TEXT DEFAULT '',
                actor TEXT DEFAULT '',
                status TEXT DEFAULT '',
                evidence_refs_json TEXT NOT NULL DEFAULT '{}',
                notes TEXT DEFAULT '',
                boundary_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL DEFAULT 0.0
            )
            """,
        )
        _execute(
            conn,
            """
            CREATE INDEX IF NOT EXISTS idx_incident_playbook_event_playbook
            ON incident_playbook_event(playbook_id, created_at)
            """,
        )
        _execute(
            conn,
            """
            CREATE INDEX IF NOT EXISTS idx_incident_playbook_event_type
            ON incident_playbook_event(event_type, created_at)
            """,
        )
        conn.commit()
    finally:
        conn.close()


class RuntimeIncidentControlService:
    """V15 runtime incident controls backed by RuntimeConfig overlay.

    Setting a mode is itself a governed action: callers must pass through
    RiskPolicyService before RuntimeConfigMutationService persists the overlay
    and snapshot.
    """

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = db_path

    def status(self) -> dict[str, Any]:
        cfg = runtime_config()
        configured_mode = str(
            getattr(cfg, "runtime_incident_mode", "normal") or "normal"
        ).strip().lower()
        if configured_mode not in INCIDENT_MODES:
            configured_mode = "frozen"
        local_safety = (
            no_new_risk_latch_status(fail_closed=True)
            if is_state_db_path(self.db_path)
            else {
                "schema_version": "live_no_new_risk_latch.v1",
                "active": False,
                "state": "isolated_state",
                "reason": "",
                "causes": [],
            }
        )
        effective_mode = configured_mode
        if bool(local_safety.get("active")) and INCIDENT_MODE_RANK.get(
            effective_mode, -1
        ) < INCIDENT_MODE_RANK["no_new_risk"]:
            effective_mode = "no_new_risk"
        return {
            "schema_version": "runtime_incident_control.v1",
            "mode": effective_mode,
            "configured_mode": configured_mode,
            "effective_mode": effective_mode,
            "local_latch_overrode_mode": effective_mode != configured_mode,
            "valid_modes": sorted(INCIDENT_MODES),
            "readiness_effect": self._readiness_effect(effective_mode),
            "local_safety_latch": local_safety,
            "boundary": {
                "schema_version": "runtime_incident_control_boundary.v2",
                "typed_plan_required": True,
                "risk_direction_from_before_after": True,
                "thaw_requires_recent_step_up_and_v16": True,
                "tightening_v16_exempt": True,
                "tightening_activates_local_latch_before_governance": True,
                "broker_risk_reduction_does_not_depend_on_governance": True,
            },
            "updated_at": time.time(),
        }

    def set_mode(
        self,
        mode: str,
        *,
        reason: str = "",
        actor: str = "system:v15_incident_control",
        confirm_thaw: bool = False,
        v16_command_id: str = "",
        v16_claim_token: str = "",
    ) -> dict[str, Any]:
        current_status = self.status()
        current = str(current_status["mode"])
        configured_current = str(current_status.get("configured_mode") or current)
        current_latch = dict(current_status.get("local_safety_latch") or {})
        active_causes = {
            str(item.get("cause") or "")
            for item in list(current_latch.get("causes") or [])
            if isinstance(item, dict) and str(item.get("cause") or "")
        }
        target = str(mode or "").strip().lower()
        if target not in INCIDENT_MODES:
            return {
                "ok": False,
                "status": "invalid_incident_mode",
                "reason": f"expected one of {sorted(INCIDENT_MODES)}",
                "current_mode": current,
                "target_mode": target,
            }
        if (
            target == "normal"
            and bool(current_latch.get("active"))
            and configured_current == "normal"
        ):
            # Empty cause metadata denotes a historical/monkeypatched v1
            # latch, which must retain the conservative projection-recovery
            # behavior.  A known non-incident cause is never projectable or
            # clearable through incident thaw.
            incident_projection_pending = (
                not active_causes or "incident_control" in active_causes
            )
            return {
                "ok": False,
                "status": (
                    "governance_projection_recovery_required"
                    if incident_projection_pending
                    else "independent_safety_blockers_active"
                ),
                "reason": (
                    "project the active incident-control cause as no_new_risk "
                    "before a governed V16 thaw can release it"
                    if incident_projection_pending
                    else "incident thaw cannot release independent local safety causes"
                ),
                "current_mode": current,
                "configured_mode": configured_current,
                "target_mode": target,
                "local_safety_latch": current_status.get("local_safety_latch") or {},
                "local_safety_effective": True,
                "governance_projection_pending": incident_projection_pending,
                "remaining_latch_causes": sorted(active_causes),
                "readiness_effect": self._readiness_effect(current),
            }
        run_id = f"incident_control_{uuid.uuid4().hex[:16]}"
        blocks_new_risk = target in {
            "shadow_only",
            "no_new_risk",
            "only_close",
            "frozen",
        }
        local_latch: dict[str, Any] = {}
        latch_error = ""
        if is_state_db_path(self.db_path) and blocks_new_risk:
            try:
                local_latch = activate_no_new_risk_latch(
                    reason=reason or f"runtime_incident_mode={target}",
                    actor=actor,
                    correlation_id=run_id,
                    metadata={"current_mode": current, "target_mode": target},
                    cause="incident_control",
                    cause_id="runtime_incident_mode",
                )
            except SafetyStatePersistenceError as exc:
                latch_error = str(exc)
                local_latch = no_new_risk_latch_status(fail_closed=True)

        verdict = None
        policy_error = ""
        try:
            verdict = RiskPolicyService.shared().evaluate(
                "set_incident_control",
                {
                    "current_mode": current,
                    "target_mode": target,
                    "reason": reason,
                    "confirm_thaw": confirm_thaw,
                    "local_latch_causes": sorted(active_causes),
                    "release_cause": "incident_control" if target == "normal" else "",
                },
            )
            verdict_payload = verdict.to_dict()
        except Exception as exc:
            policy_error = f"{type(exc).__name__}: {exc}"
            verdict_payload = {
                "allowed": False,
                "reason": "risk_policy_unavailable",
                "error": policy_error,
            }
        if verdict is None or not verdict.allowed:
            safety_effective = bool(
                blocks_new_risk
                and is_state_db_path(self.db_path)
                and no_new_risk_latch_status(fail_closed=True).get("active")
            )
            outbox: dict[str, Any] = {}
            if safety_effective:
                try:
                    outbox = append_safety_outbox(
                        event_type="incident_control_policy_projection_pending",
                        correlation_id=run_id,
                        payload={
                            "current_mode": current,
                            "target_mode": target,
                            "risk_verdict": verdict_payload,
                            "local_latch": local_latch,
                        },
                        error=policy_error
                        or str(verdict_payload.get("reason") or "blocked_by_risk"),
                    )
                except Exception as exc:
                    outbox = {
                        "status": "outbox_append_failed",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
            return {
                "ok": safety_effective,
                "status": (
                    "local_safety_latched_policy_projection_pending"
                    if safety_effective
                    else "blocked_by_risk"
                ),
                "risk_verdict": verdict_payload,
                "current_mode": current,
                "target_mode": target,
                "local_safety_latch": local_latch,
                "local_safety_effective": safety_effective,
                "latch_persistence_error": latch_error,
                "safety_outbox": outbox,
                "governance_projection_pending": safety_effective,
                "readiness_effect": self._readiness_effect(target),
            }

        plan = IncidentControlPlan(
            patch={"runtime_incident_mode": target},
            source="v15_incident_control",
            run_id=run_id,
            actor=actor,
            action="set_incident_control",
            reason=reason or f"runtime_incident_mode={target}",
            scope_type="incident_control",
            scope_key="runtime_incident_mode",
            target_agent="governance_control",
            rollback={"runtime_incident_mode": configured_current},
            evidence_refs={"risk_verdict": verdict_payload},
            v16_command_id=str(v16_command_id or ""),
            v16_claim_token=str(v16_claim_token or ""),
            current_mode=current,
            target_mode=target,
        )
        try:
            result = plan.execute(self.db_path)
        except Exception as exc:
            result = {
                "ok": False,
                "status": "governance_mutation_unavailable",
                "reason": f"{type(exc).__name__}: {exc}",
            }

        outbox: dict[str, Any] = {}
        if is_state_db_path(self.db_path) and blocks_new_risk and not result.get("ok"):
            try:
                outbox = append_safety_outbox(
                    event_type="incident_control_projection_pending",
                    correlation_id=run_id,
                    payload={
                        "current_mode": current,
                        "target_mode": target,
                        "mutation": result,
                        "local_latch": local_latch,
                    },
                    error=str(
                        result.get("reason")
                        or result.get("status")
                        or "mutation_failed"
                    ),
                )
            except Exception as exc:
                outbox = {
                    "status": "outbox_append_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }

        latch_clear: dict[str, Any] = {}
        if is_state_db_path(self.db_path) and target == "normal" and result.get("ok"):
            try:
                latch_clear = clear_no_new_risk_latch(
                    reason=reason or "governed incident thaw committed",
                    actor=actor,
                    correlation_id=run_id,
                    cause="incident_control",
                    cause_id="runtime_incident_mode",
                )
            except SafetyStatePersistenceError as exc:
                latch_clear = {
                    "active": True,
                    "status": "latch_clear_failed",
                    "error": str(exc),
                }

        post_latch = (
            no_new_risk_latch_status(fail_closed=True)
            if is_state_db_path(self.db_path)
            else {}
        )
        safety_effective = bool(post_latch.get("active"))
        ok = bool(result.get("ok"))
        status = str(result.get("status") or "applied")
        if not ok and blocks_new_risk and safety_effective:
            ok = True
            status = "local_safety_latched_projection_pending"
        if target == "normal" and result.get("ok") and safety_effective:
            ok = False
            status = "governance_committed_safety_latch_retained"
        effective_mode_after = target
        if safety_effective and INCIDENT_MODE_RANK.get(
            effective_mode_after, -1
        ) < INCIDENT_MODE_RANK["no_new_risk"]:
            effective_mode_after = "no_new_risk"
        return {
            "ok": ok,
            "status": status,
            "risk_verdict": verdict_payload,
            "current_mode": current,
            "configured_mode_before": configured_current,
            "target_mode": target,
            "effective_mode": effective_mode_after,
            "mutation": result,
            "local_safety_latch": post_latch,
            "local_safety_effective": safety_effective,
            "latch_persistence_error": latch_error,
            "latch_clear": latch_clear,
            "safety_outbox": outbox,
            "governance_projection_pending": bool(
                blocks_new_risk and safety_effective and not result.get("ok")
            ),
            "resume_required": bool(target == "normal" and safety_effective),
            "remaining_latch_causes": [
                str(item.get("cause") or "")
                for item in list(post_latch.get("causes") or [])
                if isinstance(item, dict) and str(item.get("cause") or "")
            ],
            "readiness_effect": self._readiness_effect(effective_mode_after),
        }

    def build_playbook(
        self,
        *,
        scenario: str,
        severity: str = "medium",
        release_run_id: str = "",
        created_by: str = "system:v15_incident_playbook",
        persist: bool = True,
        playbook_id: str = "",
    ) -> dict[str, Any]:
        current = self.status()["mode"]
        normalized_scenario = str(scenario or "unknown").strip().lower()
        normalized_severity = str(severity or "medium").strip().lower()
        target = self._target_mode_for_playbook(normalized_scenario, normalized_severity, current)
        reason = f"incident_playbook:{normalized_scenario}:{normalized_severity}"
        risk_precheck = RiskPolicyService.shared().evaluate(
            "set_incident_control",
            {
                "current_mode": current,
                "target_mode": target,
                "reason": reason,
                "confirm_thaw": False,
            },
        ).to_dict()
        playbook = {
            "ok": True,
            "schema_version": "incident_playbook_run.v1",
            "playbook_id": str(playbook_id or f"incident_playbook_{uuid.uuid4().hex[:16]}"),
            "scenario": normalized_scenario,
            "severity": normalized_severity,
            "current_mode": current,
            "target_mode": target,
            "status": "planned",
            "steps": self._playbook_steps(
                scenario=normalized_scenario,
                target_mode=target,
                release_run_id=release_run_id,
                risk_precheck=risk_precheck,
            ),
            "risk_precheck": risk_precheck,
            "release_ref": {"release_run_id": str(release_run_id or "")},
            "boundary": self._playbook_boundary(),
            "created_by": str(created_by or ""),
            "created_at": time.time(),
        }
        if persist:
            self._persist_playbook(playbook)
        return playbook

    def latest_playbook(self) -> dict[str, Any]:
        conn = _connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, "incident_playbook_run"):
                return {"ok": False, "status": "missing_table"}
            row = _execute(
                conn,
                """
                SELECT *
                FROM incident_playbook_run
                ORDER BY created_at DESC
                LIMIT 1
                """,
            ).fetchone()
            if not row:
                return {"ok": False, "status": "missing_playbook"}
            return self._row_to_playbook(dict(row))
        finally:
            conn.close()

    def get_playbook(self, playbook_id: str) -> dict[str, Any]:
        conn = _connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, "incident_playbook_run"):
                return {"ok": False, "status": "missing_table", "playbook_id": str(playbook_id or "")}
            row = _execute(
                conn,
                "SELECT * FROM incident_playbook_run WHERE playbook_id = ? LIMIT 1",
                (str(playbook_id or ""),),
            ).fetchone()
            if not row:
                return {"ok": False, "status": "missing_playbook", "playbook_id": str(playbook_id or "")}
            return self._row_to_playbook(dict(row))
        finally:
            conn.close()

    def record_playbook_event(
        self,
        playbook_id: str,
        *,
        event_type: str = "evidence_linked",
        actor: str = "system:v15_incident_playbook",
        status: str = "recorded",
        evidence_refs: dict[str, Any] | list[Any] | None = None,
        notes: str = "",
        event_id: str = "",
        created_at: float | None = None,
    ) -> dict[str, Any]:
        playbook = self.get_playbook(playbook_id)
        if not playbook.get("ok"):
            return {"ok": False, "status": "missing_playbook", "playbook_id": str(playbook_id or "")}
        event = {
            "ok": True,
            "schema_version": "incident_playbook_event.v1",
            "event_id": str(event_id or f"incident_event_{uuid.uuid4().hex[:16]}"),
            "playbook_id": str(playbook.get("playbook_id") or ""),
            "event_type": str(event_type or "evidence_linked"),
            "actor": str(actor or "system:v15_incident_playbook"),
            "status": str(status or "recorded"),
            "evidence_refs": evidence_refs if evidence_refs is not None else {},
            "notes": str(notes or ""),
            "boundary": self._playbook_event_boundary(),
            "created_at": _safe_float(created_at, time.time()),
        }
        ensure_incident_playbook_run_table(self.db_path)
        conn = _connect(self.db_path)
        try:
            _execute(
                conn,
                """
                INSERT INTO incident_playbook_event
                (event_id, playbook_id, event_type, actor, status,
                 evidence_refs_json, notes, boundary_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["event_id"],
                    event["playbook_id"],
                    event["event_type"],
                    event["actor"],
                    event["status"],
                    _dumps(event["evidence_refs"]),
                    event["notes"],
                    _dumps(event["boundary"]),
                    event["created_at"],
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return event

    def playbook_events(self, playbook_id: str, *, limit: int = 100) -> dict[str, Any]:
        playbook = self.get_playbook(playbook_id)
        if not playbook.get("ok"):
            return {"ok": False, "status": "missing_playbook", "playbook_id": str(playbook_id or "")}
        conn = _connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, "incident_playbook_event"):
                events: list[dict[str, Any]] = []
            else:
                rows = _execute(
                    conn,
                    """
                    SELECT *
                    FROM incident_playbook_event
                    WHERE playbook_id = ?
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    (str(playbook_id or ""), max(1, min(int(limit or 100), 500))),
                ).fetchall()
                events = [self._row_to_playbook_event(dict(row)) for row in rows]
        finally:
            conn.close()
        return {
            "ok": True,
            "schema_version": "incident_playbook_event_trail.v1",
            "playbook_id": str(playbook.get("playbook_id") or ""),
            "playbook_status": str(playbook.get("status") or ""),
            "event_count": len(events),
            "events": events,
            "boundary": self._playbook_event_boundary(),
        }

    @staticmethod
    def _readiness_effect(mode: str) -> dict[str, Any]:
        return {
            "blocks_new_risk": mode in {"shadow_only", "no_new_risk", "only_close", "frozen"},
            "allows_only_close": mode == "only_close",
            "allows_shadow_only": mode == "shadow_only",
            "allows_rollbacks": mode in {"normal", "shadow_only", "no_new_risk", "frozen"},
        }

    @staticmethod
    def _target_mode_for_playbook(scenario: str, severity: str, current_mode: str) -> str:
        severe = severity in {"high", "critical", "severe"}
        if scenario in {"broker_disconnect", "execution_anomaly", "order_reject_spike", "position_mismatch"}:
            suggested = "only_close" if severe else "no_new_risk"
        elif scenario in {"drawdown", "risk_breach", "latency_spike"}:
            suggested = "only_close" if severe else "no_new_risk"
        elif scenario in {"data_gap", "replay_failed", "factor_regression", "model_regression", "governance_failure"}:
            suggested = "frozen" if severe else "shadow_only"
        else:
            suggested = "no_new_risk"
        # Invalid runtime state must never be interpreted as the least strict
        # mode.  Config loading rejects it; defensive runtime callers fail
        # closed to the strictest governance posture.
        current = current_mode if current_mode in INCIDENT_MODES else "frozen"
        if INCIDENT_MODE_RANK[current] > INCIDENT_MODE_RANK[suggested]:
            return current
        return suggested

    @staticmethod
    def _playbook_steps(
        *,
        scenario: str,
        target_mode: str,
        release_run_id: str,
        risk_precheck: dict[str, Any],
    ) -> list[dict[str, Any]]:
        return [
            {
                "schema_version": "incident_playbook_step.v1",
                "step": "capture_readiness",
                "endpoint": "GET /api/ops/backend-readiness",
                "required": True,
                "applies_runtime_change": False,
            },
            {
                "schema_version": "incident_playbook_step.v1",
                "step": "record_release_approval_event",
                "endpoint": f"POST /api/ops/release/{release_run_id or '<run_id>'}/approvals",
                "required": bool(release_run_id),
                "applies_runtime_change": False,
            },
            {
                "schema_version": "incident_playbook_step.v1",
                "step": "set_incident_control",
                "endpoint": "POST /api/ops/incident-control",
                "target_mode": target_mode,
                "requires_risk_policy": True,
                "risk_precheck_allowed": bool(risk_precheck.get("allowed")),
                "applies_runtime_change": False,
                "execution_note": "operator_must_call_incident_control_endpoint_to_apply",
            },
            {
                "schema_version": "incident_playbook_step.v1",
                "step": "run_replay_evidence",
                "endpoint": "POST /api/ops/replay/bar-run",
                "required": scenario in {"replay_failed", "factor_regression", "model_regression", "governance_failure"},
                "applies_runtime_change": False,
            },
            {
                "schema_version": "incident_playbook_step.v1",
                "step": "record_release_finish_or_followup",
                "endpoint": f"POST /api/ops/release/{release_run_id or '<run_id>'}/finish",
                "required": bool(release_run_id),
                "applies_runtime_change": False,
            },
        ]

    @staticmethod
    def _playbook_boundary() -> dict[str, Any]:
        return {
            "schema_version": "incident_playbook_boundary.v1",
            "audit_and_plan_only": True,
            "does_not_apply_incident_mode": True,
            "does_not_change_runtime_overlay": True,
            "does_not_change_runtime_snapshot": True,
            "does_not_change_orders_or_positions": True,
            "incident_mode_change_requires_risk_policy": True,
            "incident_mode_change_requires_runtime_overlay_snapshot": True,
            "approval_events_are_audit_only": True,
        }

    @staticmethod
    def _playbook_event_boundary() -> dict[str, Any]:
        return {
            "schema_version": "incident_playbook_event_boundary.v1",
            "audit_only": True,
            "binds_evidence_to_playbook": True,
            "does_not_apply_incident_mode": True,
            "does_not_change_runtime_overlay": True,
            "does_not_change_runtime_snapshot": True,
            "does_not_change_orders_or_positions": True,
            "incident_mode_change_requires_incident_control_endpoint": True,
            "incident_mode_change_requires_risk_policy": True,
            "release_status_change_requires_release_endpoint": True,
            "replay_execution_requires_replay_endpoint": True,
        }

    def _persist_playbook(self, playbook: dict[str, Any]) -> None:
        ensure_incident_playbook_run_table(self.db_path)
        conn = _connect(self.db_path)
        try:
            _execute(
                conn,
                """
                INSERT INTO incident_playbook_run
                (playbook_id, scenario, severity, current_mode, target_mode, status,
                 steps_json, risk_precheck_json, release_ref_json, boundary_json,
                 created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(playbook.get("playbook_id") or ""),
                    str(playbook.get("scenario") or ""),
                    str(playbook.get("severity") or ""),
                    str(playbook.get("current_mode") or ""),
                    str(playbook.get("target_mode") or ""),
                    str(playbook.get("status") or ""),
                    _dumps(playbook.get("steps") or []),
                    _dumps(playbook.get("risk_precheck") or {}),
                    _dumps(playbook.get("release_ref") or {}),
                    _dumps(playbook.get("boundary") or {}),
                    str(playbook.get("created_by") or ""),
                    _safe_float(playbook.get("created_at")),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _row_to_playbook(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "schema_version": "incident_playbook_run.v1",
            "playbook_id": str(row.get("playbook_id") or ""),
            "scenario": str(row.get("scenario") or ""),
            "severity": str(row.get("severity") or ""),
            "current_mode": str(row.get("current_mode") or ""),
            "target_mode": str(row.get("target_mode") or ""),
            "status": str(row.get("status") or ""),
            "steps": _loads(row.get("steps_json"), []),
            "risk_precheck": _loads(row.get("risk_precheck_json"), {}),
            "release_ref": _loads(row.get("release_ref_json"), {}),
            "boundary": _loads(row.get("boundary_json"), RuntimeIncidentControlService._playbook_boundary()),
            "created_by": str(row.get("created_by") or ""),
            "created_at": _safe_float(row.get("created_at")),
        }

    @staticmethod
    def _row_to_playbook_event(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "schema_version": "incident_playbook_event.v1",
            "event_id": str(row.get("event_id") or ""),
            "playbook_id": str(row.get("playbook_id") or ""),
            "event_type": str(row.get("event_type") or ""),
            "actor": str(row.get("actor") or ""),
            "status": str(row.get("status") or ""),
            "evidence_refs": _loads(row.get("evidence_refs_json"), {}),
            "notes": str(row.get("notes") or ""),
            "boundary": _loads(row.get("boundary_json"), RuntimeIncidentControlService._playbook_event_boundary()),
            "created_at": _safe_float(row.get("created_at")),
        }
