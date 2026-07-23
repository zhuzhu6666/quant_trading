"""Atomic activation of learned entry-quality admission controls."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, is_state_db_path, state_table_exists
from backend.services._brain_helpers import connect, dumps, execute, loads
from backend.services.autonomous_learning import ensure_autonomous_learning_tables
from backend.services.governance_eligibility import GOVERNANCE_ELIGIBILITY_VERSION
from backend.services.governance_mutation_coordinator import (
    GovernanceMutationCoordinator,
    GovernanceMutationPlan,
)
from config.runtime_config import shared as runtime_config
from risk.policy_service import RiskPolicyService


DEMO_AUTONOMY_MODES = frozenset({"demo_nursery", "demo_autonomous"})
SUPPORTED_ACTION = "raise_weak_signal_threshold"


def _stable_id(prefix: str, payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(dumps(payload).encode("utf-8")).hexdigest()[:20]
    return f"{prefix}_{digest}"


def _verdict_payload(verdict: Any) -> dict[str, Any]:
    if hasattr(verdict, "to_dict"):
        return dict(verdict.to_dict())
    return dict(verdict or {})


class EntryQualityGovernanceService:
    """Apply one current, eligible weak-signal control in demo autonomy."""

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = Path(db_path)

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {
            "schema_version": "entry_quality_governance_boundary.v1",
            "demo_only_auto_apply": True,
            "single_control_per_call": True,
            "supported_scope_key": "weak_signal",
            "supported_action": SUPPORTED_ACTION,
            "domain_only_governance_mutation": True,
            "does_not_change_global_factor_signal_threshold": True,
            "does_not_submit_orders": True,
        }

    def apply_next_weak_signal(
        self,
        *,
        run_id: str,
        actor: str = "system:entry_quality_governance",
    ) -> dict[str, Any]:
        ensure_autonomous_learning_tables(self.db_path)
        from research.learning.governor import RuleEvolutionGovernor

        RuleEvolutionGovernor(str(self.db_path))
        mode = str(getattr(runtime_config(), "autonomy_mode", "manual") or "manual")
        if mode not in DEMO_AUTONOMY_MODES:
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
                "status": "skipped_no_eligible_weak_signal_suggestion",
                "mode": mode,
                "boundary": self.boundary(),
            }
        active = self._active_application()
        if active:
            return {
                "ok": True,
                "status": "skipped_active_entry_quality_experiment",
                "active_application": active,
                "boundary": self.boundary(),
            }

        evidence = loads(suggestion.get("evidence_json"), {})
        controls = dict(evidence.get("recommended_controls") or {})
        threshold = float(controls.get("min_abs_signal_score") or 0.0)
        strong_override = float(controls.get("strong_signal_override") or 0.0)
        if not (0.5 <= threshold <= 0.68 and 0.75 <= strong_override <= 1.0):
            return {
                "ok": False,
                "status": "rejected_invalid_entry_quality_controls",
                "suggestion_id": suggestion["suggestion_id"],
                "controls": controls,
                "boundary": self.boundary(),
            }

        risk_verdict = _verdict_payload(
            RiskPolicyService.shared().evaluate(
                "activate_entry_quality_control",
                {
                    "source": "autonomous_learning",
                    "required_mode": "autonomous_governance",
                    "autonomy_mode": mode,
                    "suggestion_id": suggestion["suggestion_id"],
                    "suggestion_status": suggestion["status"],
                    "governance_eligible": bool(suggestion["governance_eligible"]),
                    "controls": controls,
                },
            )
        )
        if not risk_verdict.get("allowed"):
            return {
                "ok": True,
                "status": "blocked_by_risk",
                "risk_verdict": risk_verdict,
                "suggestion_id": suggestion["suggestion_id"],
                "boundary": self.boundary(),
            }

        suggestion_id = str(suggestion["suggestion_id"])
        fingerprint = str(suggestion["governance_eligibility_fingerprint"])
        application_id = _stable_id(
            "lapp",
            {
                "scope_type": "entry_quality",
                "scope_key": "weak_signal",
                "action": "activate_entry_quality_control",
                "suggestion_id": suggestion_id,
                "eligibility_fingerprint": fingerprint,
            },
        )
        now = time.time()
        details = {
            "schema_version": "entry_quality_application.v1",
            "source_agent": "autonomous_learning",
            "run_id": run_id,
            "suggestion_id": suggestion_id,
            "controls": controls,
            "risk_verdict": risk_verdict,
            "observation_contract": {
                "min_independent_closed_positions": 5,
                "continue_observing_after_seconds": 86400,
                "inconclusive_after_seconds": 604800,
            },
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
                raise RuntimeError("entry_quality_suggestion_changed")
            competing = execute(
                conn,
                """
                SELECT application_id
                FROM learning_application_log
                WHERE scope_type='entry_quality' AND scope_key='weak_signal'
                  AND status IN ('prepared','applied','observing','effective')
                LIMIT 1
                """,
            ).fetchone()
            if competing and str(competing["application_id"] or "") != application_id:
                raise RuntimeError("entry_quality_experiment_already_active")

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
            execute(
                conn,
                """
                INSERT INTO learning_application_log
                (application_id, cycle_ts, scope_type, scope_key, action,
                 bias_multiplier, old_weight, new_weight, suggestion_ids_json,
                 status, details_json, mutation_id,
                 governance_eligibility_version, created_at)
                VALUES (?, ?, 'entry_quality', 'weak_signal',
                        'activate_entry_quality_control', 1.0, 0.0, ?, ?,
                        'observing', ?, ?, ?, ?)
                ON CONFLICT(application_id) DO UPDATE SET
                    status='observing', details_json=excluded.details_json,
                    mutation_id=excluded.mutation_id
                """,
                (
                    application_id,
                    now,
                    threshold,
                    json.dumps([suggestion_id], ensure_ascii=False),
                    dumps(details),
                    mutation_id,
                    GOVERNANCE_ELIGIBILITY_VERSION,
                    now,
                ),
            )
            execute(
                conn,
                """
                INSERT INTO learning_application_effect
                (application_id, scope_type, scope_key, action, status,
                 observed_trade_count, baseline_trade_count, decision_json,
                 mutation_id, governance_eligibility_version,
                 last_review_at, updated_at, created_at)
                VALUES (?, 'entry_quality', 'weak_signal',
                        'activate_entry_quality_control', 'observing',
                        0, 0, ?, ?, ?, 0.0, ?, ?)
                ON CONFLICT(application_id) DO UPDATE SET
                    status='observing', decision_json=excluded.decision_json,
                    mutation_id=excluded.mutation_id,
                    governance_eligibility_version=excluded.governance_eligibility_version,
                    updated_at=excluded.updated_at
                """,
                (
                    application_id,
                    dumps({"details": details, "effect_status": "awaiting_post_trades"}),
                    mutation_id,
                    GOVERNANCE_ELIGIBILITY_VERSION,
                    now,
                    now,
                ),
            )
            return {
                "suggestion_id": suggestion_id,
                "application_id": application_id,
                "scope_key": "weak_signal",
                "threshold": threshold,
                "strong_signal_override": strong_override,
            }

        mutation = GovernanceMutationCoordinator(self.db_path).execute(
            GovernanceMutationPlan(
                patch={},
                source="entry_quality_governance",
                actor=actor,
                action="activate_entry_quality_control",
                run_id=run_id,
                reason=str(suggestion.get("reason") or "weak_signal_entry_quality"),
                control_surface="entry_quality",
                scope_type="entry_quality",
                scope_key="weak_signal",
                rollback={
                    "min_abs_signal_score": 0.0,
                    "strong_signal_override": 0.0,
                },
                evidence_refs={
                    "suggestion_id": suggestion_id,
                    "eligibility_fingerprint": fingerprint,
                    "controls": controls,
                },
                evidence_fingerprint=fingerprint,
                idempotency_key=f"entry-quality:{suggestion_id}:{fingerprint}",
                v16_target_agent="autonomous_learning",
                domain_only=True,
                domain_before={
                    "min_abs_signal_score": 0.0,
                    "strong_signal_override": 0.0,
                },
                domain_target={
                    "min_abs_signal_score": threshold,
                    "strong_signal_override": strong_override,
                },
            ),
            transaction_writer=transaction_writer,
        )
        return {
            "ok": bool(mutation.get("ok")),
            "status": str(mutation.get("status") or "mutation_failed"),
            "suggestion_id": suggestion_id,
            "application_id": application_id,
            "controls": controls,
            "risk_verdict": risk_verdict,
            "mutation": mutation,
            "boundary": self.boundary(),
        }

    def status(self) -> dict[str, Any]:
        """Return the review-to-control funnel and active observation window."""
        ensure_autonomous_learning_tables(self.db_path)
        conn = connect(self.db_path, read_only=True)
        try:
            raw = execute(
                conn,
                """
                SELECT COUNT(*) AS raw_rows,
                       COUNT(DISTINCT NULLIF(position_id, '')) AS raw_positions
                FROM autonomous_learning_sample
                WHERE sample_type='trade_review_outcome'
                """,
            ).fetchone()
            eligible = execute(
                conn,
                """
                SELECT COUNT(*) AS eligible_rows,
                       COUNT(DISTINCT NULLIF(position_id, '')) AS eligible_positions,
                       COALESCE(SUM(governance_effective_weight), 0.0) AS effective_sample_size
                FROM autonomous_learning_sample
                WHERE sample_type='trade_review_outcome'
                  AND label_status='matured'
                  AND governance_eligible=1
                  AND governance_effective_weight>0
                  AND governance_eligibility_version=?
                  AND governance_eligibility_fingerprint<>''
                """,
                (GOVERNANCE_ELIGIBILITY_VERSION,),
            ).fetchone()
            suggestion = execute(
                conn,
                """
                SELECT suggestion_id, status, applied_mutation_id,
                       governance_eligibility_fingerprint, evidence_json, created_at
                FROM policy_suggestion
                WHERE scope_type='entry_quality' AND scope_key='weak_signal'
                  AND action=?
                  AND governance_eligible=1
                  AND governance_eligibility_version=?
                ORDER BY created_at DESC LIMIT 1
                """,
                (SUPPORTED_ACTION, GOVERNANCE_ELIGIBILITY_VERSION),
            ).fetchone()
            application = None
            if state_table_exists(conn, "learning_application_log"):
                has_effect = state_table_exists(conn, "learning_application_effect")
                effect_columns = (
                    "e.observed_trade_count, e.baseline_trade_count, "
                    "e.delta_avg_reward, e.decision_json, e.updated_at"
                    if has_effect
                    else (
                        "0 AS observed_trade_count, 0 AS baseline_trade_count, "
                        "0.0 AS delta_avg_reward, '{}' AS decision_json, "
                        "0.0 AS updated_at"
                    )
                )
                effect_join = (
                    "LEFT JOIN learning_application_effect e "
                    "ON e.application_id=l.application_id"
                    if has_effect
                    else ""
                )
                application = execute(
                    conn,
                    f"""
                    SELECT l.application_id, l.status, l.mutation_id, l.cycle_ts,
                           l.details_json, {effect_columns}
                    FROM learning_application_log l
                    {effect_join}
                    WHERE l.scope_type='entry_quality' AND l.scope_key='weak_signal'
                    ORDER BY l.cycle_ts DESC LIMIT 1
                    """,
                ).fetchone()
            suggestion_item = dict(suggestion) if suggestion else {}
            if suggestion_item:
                suggestion_item["evidence"] = loads(
                    suggestion_item.pop("evidence_json", "{}"), {}
                )
            application_item = dict(application) if application else {}
            if application_item:
                application_item["details"] = loads(
                    application_item.pop("details_json", "{}"), {}
                )
                application_item["effect"] = loads(
                    application_item.pop("decision_json", "{}"), {}
                )
            return {
                "ok": True,
                "schema_version": "entry_quality_governance_status.v1",
                "status": (
                    "observing"
                    if application_item.get("status") in {"applied", "observing"}
                    else ("control_available" if suggestion_item else "collecting_evidence")
                ),
                "funnel": {
                    "raw_rows": int((raw["raw_rows"] if raw else 0) or 0),
                    "raw_positions": int((raw["raw_positions"] if raw else 0) or 0),
                    "eligible_rows": int((eligible["eligible_rows"] if eligible else 0) or 0),
                    "distinct_eligible_positions": int(
                        (eligible["eligible_positions"] if eligible else 0) or 0
                    ),
                    "effective_sample_size": round(
                        float((eligible["effective_sample_size"] if eligible else 0.0) or 0.0),
                        6,
                    ),
                },
                "current_suggestion": suggestion_item,
                "active_control": application_item,
                "application_post_trade_progress": {
                    "observed": int(application_item.get("observed_trade_count") or 0),
                    "required": 5,
                    "complete": int(application_item.get("observed_trade_count") or 0) >= 5,
                },
                "boundary": self.boundary(),
            }
        finally:
            conn.close()

    def _next_suggestion(self) -> dict[str, Any]:
        conn = connect(self.db_path, read_only=True)
        try:
            row = execute(
                conn,
                """
                SELECT *
                FROM policy_suggestion
                WHERE scope_type='entry_quality'
                  AND scope_key='weak_signal'
                  AND action=?
                  AND status='approved'
                  AND governance_eligible=1
                  AND governance_eligibility_version=?
                  AND COALESCE(governance_eligibility_fingerprint, '')<>''
                  AND COALESCE(applied_mutation_id, '')=''
                ORDER BY confidence DESC, created_at ASC
                LIMIT 1
                """,
                (SUPPORTED_ACTION, GOVERNANCE_ELIGIBILITY_VERSION),
            ).fetchone()
            return dict(row) if row else {}
        finally:
            conn.close()

    def _active_application(self) -> dict[str, Any]:
        conn = connect(self.db_path, read_only=True)
        try:
            row = execute(
                conn,
                """
                SELECT application_id, status, mutation_id, cycle_ts
                FROM learning_application_log
                WHERE scope_type='entry_quality' AND scope_key='weak_signal'
                  AND status IN ('prepared','applied','observing','effective')
                ORDER BY cycle_ts DESC
                LIMIT 1
                """,
            ).fetchone()
            return dict(row) if row else {}
        finally:
            conn.close()
