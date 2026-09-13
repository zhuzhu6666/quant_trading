"""Atomic activation of learned entry-cluster cooldown controls.

The entry-cluster surface had a live consumer but no writer: an approved
``increase_same_direction_cooldown`` suggestion was never applied, so the
control never reached ``risk.policy_service`` (which blocks clustered
re-entries with ``learning_same_direction_cooldown`` when
``min_same_direction_open_count`` is active) and the row stayed
approved-but-unapplied forever, which the learning workload gate then read as
pending governance.

This service is that writer.  It mirrors ``EntryQualityGovernanceService``:
RiskPolicy verdict -> ``GovernanceMutationCoordinator`` domain-only mutation ->
``policy_suggestion`` marked applied with the committed mutation id -> the
learning application/effect row that carries the observation window.  Live
still only consumes committed controls (``load_live_policy_controls``).
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, is_state_db_path
from backend.services._brain_helpers import connect, dumps, execute, loads
from backend.services.autonomous_learning import ensure_autonomous_learning_tables
from backend.services.brain_governance_candidates import sync_candidate_suggestion_lifecycle
from backend.services.governance_eligibility import GOVERNANCE_ELIGIBILITY_VERSION
from backend.services.governance_mutation_coordinator import (
    GovernanceMutationCoordinator,
    GovernanceMutationPlan,
)
from backend.services.learning_application_store import LearningApplicationStore
from backend.services.live_learning_policy import entry_cluster_threshold
from config import runtime_config as runtime_config_module
from risk.policy_service import RiskPolicyService

SUPPORTED_SCOPE_TYPE = "entry_cluster"
SUPPORTED_ACTION = "increase_same_direction_cooldown"
GOVERNANCE_ACTION = "activate_entry_cluster_control"
# Compatibility seam retained for focused tests and callers that inject a
# caller-owned RuntimeConfig snapshot.
runtime_config = runtime_config_module.shared

_ACTIVE_STATES = frozenset({"prepared", "applied", "observing", "effective", "mixed"})


def _stable_id(prefix: str, payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(dumps(payload).encode("utf-8")).hexdigest()[:20]
    return f"{prefix}_{digest}"


def _verdict_payload(verdict: Any) -> dict[str, Any]:
    if hasattr(verdict, "to_dict"):
        return dict(verdict.to_dict())
    return dict(verdict or {})


class EntryClusterGovernanceService:
    """Apply one current, eligible same-direction cooldown control in demo autonomy."""

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = Path(db_path)

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {
            "schema_version": "entry_cluster_governance_boundary.v1",
            "demo_only_auto_apply": True,
            "single_control_per_call": True,
            "supported_scope_type": SUPPORTED_SCOPE_TYPE,
            "supported_action": SUPPORTED_ACTION,
            "risk_tightening_only": True,
            "domain_only_governance_mutation": True,
            "does_not_change_same_direction_cooldown_seconds": True,
            "does_not_submit_orders": True,
        }

    def apply_next_cooldown(
        self,
        *,
        run_id: str,
        actor: str = "system:entry_cluster_governance",
    ) -> dict[str, Any]:
        ensure_autonomous_learning_tables(self.db_path)
        from research.learning.governor import RuleEvolutionGovernor

        RuleEvolutionGovernor(str(self.db_path))
        cfg = runtime_config()
        mode = str(getattr(cfg, "autonomy_mode", "manual") or "manual")
        if not runtime_config_module.bounded_demo_mode_active(cfg):
            return {
                "ok": True,
                "status": "skipped_non_demo_mode",
                "mode": mode,
                "boundary": self.boundary(),
            }

        suggestion = self._next_suggestion()
        if not suggestion:
            return {
                "ok": True,
                "status": "skipped_no_eligible_entry_cluster_suggestion",
                "mode": mode,
                "boundary": self.boundary(),
            }

        suggestion_id = str(suggestion["suggestion_id"])
        scope_key = str(suggestion["scope_key"])
        fingerprint = str(suggestion["governance_eligibility_fingerprint"])
        evidence = loads(suggestion.get("evidence_json"), {})
        controls = dict(evidence.get("recommended_controls") or {})
        if not bool(controls.get(SUPPORTED_ACTION)):
            return {
                "ok": False,
                "status": "rejected_entry_cluster_control_not_recommended",
                "suggestion_id": suggestion_id,
                "controls": controls,
                "boundary": self.boundary(),
            }

        active = self._active_application(scope_key)
        if active:
            return {
                "ok": True,
                "status": "skipped_active_entry_cluster_experiment",
                "active_application": active,
                "boundary": self.boundary(),
            }

        min_same_direction_open_count = entry_cluster_threshold(scope_key)
        risk_verdict = _verdict_payload(
            RiskPolicyService.shared().evaluate(
                GOVERNANCE_ACTION,
                {
                    "source": "autonomous_learning",
                    "required_mode": "autonomous_governance",
                    "autonomy_mode": mode,
                    "suggestion_id": suggestion_id,
                    "suggestion_status": str(suggestion["status"]),
                    "governance_eligible": bool(suggestion["governance_eligible"]),
                    "cluster_key": scope_key,
                    "min_same_direction_open_count": min_same_direction_open_count,
                    "controls": controls,
                },
            )
        )
        if not risk_verdict.get("allowed"):
            return {
                "ok": True,
                "status": "blocked_by_risk",
                "risk_verdict": risk_verdict,
                "suggestion_id": suggestion_id,
                "boundary": self.boundary(),
            }

        now = time.time()
        application_id = _stable_id(
            "lapp",
            {
                "scope_type": SUPPORTED_SCOPE_TYPE,
                "scope_key": scope_key,
                "action": GOVERNANCE_ACTION,
                "suggestion_id": suggestion_id,
                "eligibility_fingerprint": fingerprint,
            },
        )
        details = {
            "schema_version": "entry_cluster_application.v1",
            "source_agent": "autonomous_learning",
            "run_id": run_id,
            "suggestion_id": suggestion_id,
            "cluster_key": scope_key,
            "min_same_direction_open_count": min_same_direction_open_count,
            "controls": controls,
            "risk_verdict": risk_verdict,
            "observation_contract": {
                "min_independent_closed_positions": 5,
                "continue_observing_after_seconds": 86400,
                "inconclusive_after_seconds": 604800,
            },
        }
        # Activating the cluster cooldown only ever removes entries, so the
        # domain diff must classify as risk_tightening: an increase on a
        # ``same_direction_cooldown`` leaf is tightening by construction
        # (see classify_governance_risk).  Nothing here relaxes a limit.
        domain_before = {
            "entry_cluster_control": {"same_direction_cooldown": {scope_key: 0.0}}
        }
        domain_target = {
            "entry_cluster_control": {"same_direction_cooldown": {scope_key: 1.0}}
        }

        def transaction_writer(conn: Any, mutation_id: str, effective_config: Any):
            lock = " FOR UPDATE" if is_state_db_path(self.db_path) else ""
            row = execute(
                conn,
                f"""
                SELECT status, governance_eligible, governance_eligibility_version,
                       governance_eligibility_fingerprint, applied_mutation_id
                FROM policy_suggestion
                WHERE suggestion_id=?{lock}
                """,
                (suggestion_id,),
            ).fetchone()
            current = dict(row) if row else {}
            if (
                str(current.get("status") or "") != "approved"
                or not bool(current.get("governance_eligible"))
                or str(current.get("governance_eligibility_version") or "")
                != GOVERNANCE_ELIGIBILITY_VERSION
                or str(current.get("governance_eligibility_fingerprint") or "")
                != fingerprint
                or str(current.get("applied_mutation_id") or "")
            ):
                raise RuntimeError("entry_cluster_suggestion_changed")
            execute(
                conn,
                """
                UPDATE policy_suggestion
                SET status='applied', reviewed_at=?,
                    review_note='bounded_demo_auto_apply',
                    applied_mutation_id=?
                WHERE suggestion_id=?
                """,
                (now, mutation_id, suggestion_id),
            )
            sync_candidate_suggestion_lifecycle(
                conn,
                suggestion_id=suggestion_id,
                suggestion_status="applied",
                applied_mutation_id=mutation_id,
                now=now,
            )
            return {
                "suggestion_id": suggestion_id,
                "scope_key": scope_key,
                "min_same_direction_open_count": min_same_direction_open_count,
            }

        mutation = GovernanceMutationCoordinator(self.db_path).execute(
            GovernanceMutationPlan(
                patch={},
                source="entry_cluster_governance",
                actor=actor,
                action=GOVERNANCE_ACTION,
                run_id=run_id,
                reason=str(suggestion.get("reason") or "same_direction_cluster_cooldown"),
                control_surface=SUPPORTED_SCOPE_TYPE,
                scope_type=SUPPORTED_SCOPE_TYPE,
                scope_key=scope_key,
                rollback={"same_direction_cooldown_control_active": False},
                evidence_refs={
                    "suggestion_id": suggestion_id,
                    "eligibility_fingerprint": fingerprint,
                    "cluster_key": scope_key,
                    "min_same_direction_open_count": min_same_direction_open_count,
                    "controls": controls,
                },
                evidence_fingerprint=fingerprint,
                idempotency_key=f"entry-cluster:{suggestion_id}:{fingerprint}:{run_id}",
                v16_target_agent="autonomous_learning",
                domain_only=True,
                domain_before=domain_before,
                domain_target=domain_target,
            ),
            transaction_writer=transaction_writer,
        )
        committed = bool(mutation.get("ok"))
        if committed:
            # The store owns its own connection and cannot write inside the
            # coordinator's open transaction, so the application/effect rows are
            # recorded after the commit.
            store = LearningApplicationStore(str(self.db_path))
            application_id = store.prepare_application(
                scope_type=SUPPORTED_SCOPE_TYPE,
                scope_key=scope_key,
                action=GOVERNANCE_ACTION,
                status="observing",
                run_id=run_id,
                source="autonomous_learning",
                bias_multiplier=1.0,
                old_weight=0.0,
                new_weight=float(min_same_direction_open_count),
                suggestion_ids=[suggestion_id],
                mutation_id=str(mutation.get("mutation_id") or ""),
                governance_eligibility_version=GOVERNANCE_ELIGIBILITY_VERSION,
                cycle_ts=now,
                details=details,
            )
            store.write_effect(
                application_id=application_id,
                scope_key=scope_key,
                scope_type=SUPPORTED_SCOPE_TYPE,
                action=GOVERNANCE_ACTION,
                status="observing",
                observed_trade_count=0,
                baseline_trade_count=0,
                decision={"details": details, "effect_status": "awaiting_cluster_entries"},
                mutation_id=str(mutation.get("mutation_id") or ""),
                governance_eligibility_version=GOVERNANCE_ELIGIBILITY_VERSION,
                last_review_at=0.0,
                updated_at=now,
            )
        return {
            "ok": committed,
            "status": str(mutation.get("status") or "mutation_failed"),
            "suggestion_id": suggestion_id,
            "application_id": application_id,
            "cluster_key": scope_key,
            "min_same_direction_open_count": min_same_direction_open_count,
            "risk_verdict": risk_verdict,
            "mutation": mutation,
            "boundary": self.boundary(),
        }

    def _next_suggestion(self) -> dict[str, Any]:
        conn = connect(self.db_path, read_only=True)
        try:
            rows = execute(
                conn,
                """
                SELECT suggestion_id, scope_key, action, status, evidence_json,
                       governance_eligible, governance_eligibility_fingerprint
                FROM policy_suggestion
                WHERE scope_type=?
                  AND action=?
                  AND status='approved'
                  AND governance_eligible=1
                  AND governance_eligibility_version=?
                  AND COALESCE(governance_eligibility_fingerprint, '')<>''
                  AND COALESCE(applied_mutation_id, '')=''
                ORDER BY reviewed_at DESC, created_at DESC
                LIMIT 5
                """,
                (SUPPORTED_SCOPE_TYPE, SUPPORTED_ACTION, GOVERNANCE_ELIGIBILITY_VERSION),
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            item = dict(row)
            scope_key = str(item.get("scope_key") or "")
            if entry_cluster_threshold(scope_key) <= 0:
                continue
            if self._active_application(scope_key):
                continue
            return item
        return {}

    def _active_application(self, scope_key: str) -> dict[str, Any]:
        try:
            store = LearningApplicationStore(str(self.db_path))
            application = store.latest_application(
                scope_type=SUPPORTED_SCOPE_TYPE, scope_key=scope_key
            )
        except Exception:
            return {}
        if not application:
            return {}
        if str(application.get("status") or "") not in _ACTIVE_STATES:
            return {}
        return application
