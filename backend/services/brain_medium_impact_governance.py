from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from alpha.decision_policy import DecisionPolicy
from backend.core.db import STATE_DB, state_table_columns, state_table_exists
from backend.services.brain_action_evaluator import BrainActionPlanEvaluatorService, ensure_brain_action_plan_eval_table
from backend.services.brain_action_planner import _connect, _dumps, _execute, _loads, _safe_float
from backend.services.brain_governance_candidates import (
    BrainGovernanceCandidateService,
    ensure_brain_governance_candidate_table,
)
from risk.policy_service import RiskPolicyService


def ensure_brain_medium_impact_governance_table(db_path: str | Path = STATE_DB) -> None:
    ensure_brain_action_plan_eval_table(db_path)
    ensure_brain_governance_candidate_table(db_path)
    conn = _connect(db_path)
    try:
        _execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS brain_medium_impact_governance (
                governance_id TEXT PRIMARY KEY,
                plan_id TEXT DEFAULT '',
                eval_id TEXT DEFAULT '',
                governance_action TEXT DEFAULT '',
                scope_type TEXT DEFAULT '',
                scope_key TEXT DEFAULT '',
                status TEXT DEFAULT '',
                candidate_id TEXT DEFAULT '',
                suggestion_id TEXT DEFAULT '',
                evidence_score REAL NOT NULL DEFAULT 0.0,
                critic_verdict TEXT DEFAULT '',
                comparison_verdict TEXT DEFAULT '',
                risk_verdict_json TEXT NOT NULL DEFAULT '{}',
                decision_policy_json TEXT NOT NULL DEFAULT '{}',
                rollback_plan_json TEXT NOT NULL DEFAULT '{}',
                posterior_refs_json TEXT NOT NULL DEFAULT '{}',
                autonomy_guard_json TEXT NOT NULL DEFAULT '{}',
                boundary_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL DEFAULT 0.0,
                updated_at REAL NOT NULL DEFAULT 0.0
            )
            """,
        )
        columns = state_table_columns(conn, "brain_medium_impact_governance")
        if "candidate_id" not in columns:
            _execute(conn, "ALTER TABLE brain_medium_impact_governance ADD COLUMN candidate_id TEXT DEFAULT ''")
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_medium_governance_created ON brain_medium_impact_governance(created_at)")
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_medium_governance_plan ON brain_medium_impact_governance(plan_id, eval_id)")
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_medium_governance_scope ON brain_medium_impact_governance(scope_type, status, created_at)")
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_medium_governance_candidate ON brain_medium_impact_governance(candidate_id)")
        conn.commit()
    finally:
        conn.close()


class BrainMediumImpactGovernanceService:
    """V16 Phase 4 medium-impact governance candidate materializer."""

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = db_path

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {
            "phase": "v16_phase4_medium_impact_governance",
            "medium_impact_governance": True,
            "materializes_governance_candidates_only": True,
            "materializes_policy_suggestions_only": False,
            "does_not_write_policy_suggestion_directly": True,
            "policy_suggestion_bridge_manual_only": True,
            "does_not_apply_factor_weights": True,
            "does_not_switch_templates": True,
            "does_not_submit_orders": True,
            "does_not_write_learning_samples": True,
            "risk_policy_service_required": True,
            "decision_policy_preview_required_for_weight_actions": True,
            "runtime_overlay_snapshot_required_for_future_apply": True,
            "release_evidence_required_for_future_apply": True,
            "governance_candidate_service_required": True,
        }

    def materialize_latest(
        self,
        *,
        limit: int = 4,
        allow_tighten_low_health: bool = False,
        readiness: dict[str, Any] | None = None,
        persist: bool = True,
    ) -> dict[str, Any]:
        ensure_brain_medium_impact_governance_table(self.db_path)
        limit = max(1, min(int(limit), 20))
        evals = list(BrainActionPlanEvaluatorService(self.db_path).latest_evals(limit=limit).get("evals") or [])
        if not evals:
            return {
                "ok": False,
                "schema_version": "brain_medium_impact_governance_run.v1",
                "status": "missing_action_plan_evals",
                "items": [],
                "boundary": self.boundary(),
            }
        now = time.time()
        autonomy_guard = self._autonomy_guard(readiness=readiness or {}, allow_tighten_low_health=allow_tighten_low_health)
        items = [self._materialize_eval(evaluation=item, now=now, autonomy_guard=autonomy_guard, persist_candidate=persist) for item in evals[:limit]]
        if persist:
            self._persist(items)
        return {
            "ok": any(item.get("status") == "candidate_materialized" for item in items),
            "schema_version": "brain_medium_impact_governance_run.v1",
            "status": "materialized",
            "item_count": len(items),
            "items": items,
            "autonomy_guard": autonomy_guard,
            "boundary": self.boundary(),
            "created_at": now,
        }

    def latest_governance(self, *, limit: int = 20) -> dict[str, Any]:
        ensure_brain_medium_impact_governance_table(self.db_path)
        limit = max(1, min(int(limit), 200))
        conn = _connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, "brain_medium_impact_governance"):
                return self._missing_status("missing_table")
            rows = _execute(
                conn,
                """
                SELECT governance_id, plan_id, eval_id, governance_action, scope_type,
                       scope_key, status, candidate_id, suggestion_id, evidence_score,
                       critic_verdict, comparison_verdict, risk_verdict_json,
                       decision_policy_json, rollback_plan_json, posterior_refs_json,
                       autonomy_guard_json, boundary_json, created_at, updated_at
                FROM brain_medium_impact_governance
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return {
                "ok": bool(rows),
                "schema_version": "brain_medium_impact_governance_list.v1",
                "status": "available" if rows else "missing_governance",
                "items": [self._row_to_governance(row) for row in rows],
                "boundary": self.boundary(),
            }
        finally:
            conn.close()

    def status(self, *, limit: int = 20) -> dict[str, Any]:
        latest = self.latest_governance(limit=limit)
        items = list(latest.get("items") or [])
        if not items:
            return {
                "ok": False,
                "schema_version": "brain_medium_impact_governance_readiness.v1",
                "status": latest.get("status", "missing_governance"),
                "item_count": 0,
                "medium_impact_governance": True,
            }
        return {
            "ok": True,
            "schema_version": "brain_medium_impact_governance_readiness.v1",
            "status": "available",
            "item_count": len(items),
            "latest_created_at": max(_safe_float(item.get("created_at")) for item in items),
            "statuses": sorted({str(item.get("status") or "") for item in items}),
            "medium_impact_governance": True,
            "governance_candidates": BrainGovernanceCandidateService(self.db_path).status(limit=limit),
        }

    def _materialize_eval(
        self,
        *,
        evaluation: dict[str, Any],
        now: float,
        autonomy_guard: dict[str, Any],
        persist_candidate: bool,
    ) -> dict[str, Any]:
        plan = self._load_plan(str(evaluation.get("plan_id") or ""))
        mapped = self._map_action(evaluation=evaluation, plan=plan)
        evidence_score = _safe_float(evaluation.get("coverage_score"))
        critic_verdict = str(plan.get("critic_verdict") or "")
        comparison_verdict = str(evaluation.get("comparison_verdict") or "")
        decision_policy = self._decision_policy_preview(mapped)
        risk_verdict = RiskPolicyService.shared().evaluate(
            mapped["risk_action"],
            {
                "required_mode": "autonomous_governance",
                "session": {"drawdown_pct": 0.0},
                "evidence": {
                    "brain_eval": evaluation.get("evidence_refs") or {},
                    "comparison": evaluation.get("comparison") or {},
                    "replay_summary": (evaluation.get("comparison") or {}).get("replay") or {},
                    "counterfactual_summary": (evaluation.get("comparison") or {}).get("supervisor") or {},
                },
                "suggestion_status": "approved",
                "target_template_id": mapped.get("target_template_id", ""),
                "autonomous_apply": False,
            },
        ).to_dict()
        status = "blocked_by_evidence"
        candidate_id = ""
        suggestion_id = ""
        if critic_verdict == "reject":
            status = "blocked_by_critic"
        elif evidence_score < 0.5 or comparison_verdict in {"needs_more_evidence"}:
            status = "blocked_by_evidence"
        elif not bool(risk_verdict.get("allowed")):
            status = "blocked_by_risk"
        else:
            candidate = BrainGovernanceCandidateService(self.db_path).create_candidate(
                candidate_id=f"brain_candidate_{uuid.uuid4().hex[:16]}",
                source_agent="v16_brain",
                source_kind="brain_medium_impact_governance",
                source_ref_type="brain_action_plan_eval",
                source_ref_id=str(evaluation.get("eval_id") or ""),
                proposal_stage="governance_ready",
                capability_scope="medium_impact_governance",
                scope_type=mapped["scope_type"],
                scope_key=mapped["scope_key"],
                action=mapped["policy_action"],
                confidence=max(0.1, min(0.95, evidence_score)),
                evidence_score=evidence_score,
                risk_class="medium",
                max_impact="medium_impact",
                expected_effect=evaluation.get("comparison") or {},
                evidence_refs={
                    "plan_id": evaluation.get("plan_id", ""),
                    "eval_id": evaluation.get("eval_id", ""),
                    "posterior": evaluation.get("evidence_refs") or {},
                },
                counter_evidence_refs=dict(plan.get("counter_evidence_refs") or {}),
                risk_verdict=risk_verdict,
                decision_policy=decision_policy,
                rollback_plan=self._rollback_plan(mapped),
                lineage={
                    "schema_version": "brain_medium_impact_candidate_lineage.v1",
                    "phase": "v16_phase4_medium_impact_governance",
                    "plan_id": evaluation.get("plan_id", ""),
                    "eval_id": evaluation.get("eval_id", ""),
                    "critic_verdict": critic_verdict,
                    "comparison_verdict": comparison_verdict,
                    "mapped_action": mapped,
                    "bridge": {
                        "policy_suggestion_direct_write": False,
                        "manual_bridge_required": True,
                    },
                },
                expires_at=now + 14 * 86400,
                now=now,
                persist=persist_candidate,
            )
            candidate_id = str(candidate.get("candidate_id") or "")
            status = "candidate_materialized"
        return {
            "governance_id": f"brain_p4_gov_{uuid.uuid4().hex[:16]}",
            "schema_version": "brain_medium_impact_governance.v1",
            "plan_id": str(evaluation.get("plan_id") or ""),
            "eval_id": str(evaluation.get("eval_id") or ""),
            "governance_action": mapped["policy_action"],
            "scope_type": mapped["scope_type"],
            "scope_key": mapped["scope_key"],
            "status": status,
            "candidate_id": candidate_id,
            "suggestion_id": suggestion_id,
            "evidence_score": evidence_score,
            "critic_verdict": critic_verdict,
            "comparison_verdict": comparison_verdict,
            "risk_verdict": risk_verdict,
            "decision_policy": decision_policy,
            "rollback_plan": self._rollback_plan(mapped),
            "posterior_refs": evaluation.get("evidence_refs") or {},
            "autonomy_guard": autonomy_guard,
            "boundary": self.boundary(),
            "created_at": now,
            "updated_at": time.time(),
        }

    @staticmethod
    def _map_action(*, evaluation: dict[str, Any], plan: dict[str, Any]) -> dict[str, str]:
        scope = str(evaluation.get("scope_type") or (plan.get("scope") or {}).get("scope_type") or "")
        if scope == "parameter_template":
            return {
                "scope_type": "parameter_template",
                "scope_key": "online_light:default",
                "policy_action": "switch_parameter_template",
                "risk_action": "switch_parameter_template",
                "target_template_id": "",
            }
        if scope == "context_policy":
            return {
                "scope_type": "context_policy",
                "scope_key": "threshold_and_sizing",
                "policy_action": "enable_context_policy",
                "risk_action": "enable_context_policy",
                "target_template_id": "",
            }
        if scope == "supervisor_template":
            return {
                "scope_type": "supervisor_template",
                "scope_key": "position_supervisor",
                "policy_action": "switch_position_supervisor_template",
                "risk_action": "switch_position_supervisor_template",
                "target_template_id": "position_supervisor:conservative.v1",
            }
        return {
            "scope_type": "factor",
            "scope_key": "alpha_weight_policy",
            "policy_action": "update_weight",
            "risk_action": "update_weight",
            "target_template_id": "",
        }

    @staticmethod
    def _decision_policy_preview(mapped: dict[str, str]) -> dict[str, Any]:
        if mapped["policy_action"] != "update_weight":
            return {"schema_version": "decision_policy_preview.v1", "required": False}
        decisions = DecisionPolicy().decide(
            awe_patches={mapped["scope_key"]: {"weight": 0.1, "reason": "v16_p4_downweight_candidate"}},
            weight_policy_weights={mapped["scope_key"]: 0.1},
            shadow_perfs={},
            factor_configs={mapped["scope_key"]: {"enabled": True, "role": "alpha"}},
            current_weights={mapped["scope_key"]: 0.2},
        )
        decision = decisions.get(mapped["scope_key"])
        return {
            "schema_version": "decision_policy_preview.v1",
            "required": True,
            "decision": decision.to_api() if decision else {},
            "applied": False,
        }

    @staticmethod
    def _rollback_plan(mapped: dict[str, str]) -> dict[str, Any]:
        return {
            "schema_version": "brain_medium_impact_rollback_plan.v1",
            "policy_suggestion_only": False,
            "candidate_lane_only": True,
            "runtime_mutation": False,
            "future_submit_requires_manual_bridge": True,
            "future_apply_requires_runtime_snapshot": True,
            "future_apply_requires_release_evidence": True,
            "future_apply_requires_rollback_json": True,
            "governance_action": mapped["policy_action"],
        }

    @staticmethod
    def _autonomy_guard(*, readiness: dict[str, Any], allow_tighten_low_health: bool) -> dict[str, Any]:
        health = dict((readiness or {}).get("autonomy_health") or {})
        posture = str(health.get("posture") or "")
        should_tighten = posture in {"constrained", "shadow_only", "frozen"}
        return {
            "schema_version": "brain_medium_impact_autonomy_guard.v1",
            "posture": posture,
            "allow_tighten_low_health": bool(allow_tighten_low_health),
            "should_tighten": should_tighten,
            "tighten_applied": False,
            "reason": "p4_materializes_governance_candidates_only",
        }

    def _load_plan(self, plan_id: str) -> dict[str, Any]:
        if not plan_id:
            return {}
        conn = _connect(self.db_path, read_only=True)
        try:
            row = _execute(
                conn,
                """
                SELECT plan_id, critic_verdict, scope_json, validation_refs_json
                FROM brain_action_plan
                WHERE plan_id = ?
                LIMIT 1
                """,
                (plan_id,),
            ).fetchone()
            if not row:
                return {}
            return {
                "plan_id": str(row["plan_id"] or ""),
                "critic_verdict": str(row["critic_verdict"] or ""),
                "scope": _loads(row["scope_json"], {}),
                "counter_evidence_refs": (_loads(row["validation_refs_json"], {}) or {}).get("counter_evidence_refs", {}),
            }
        finally:
            conn.close()

    def _persist(self, items: list[dict[str, Any]]) -> None:
        ensure_brain_medium_impact_governance_table(self.db_path)
        conn = _connect(self.db_path)
        try:
            for item in items:
                _execute(
                    conn,
                    """
                    INSERT INTO brain_medium_impact_governance
                    (governance_id, plan_id, eval_id, governance_action,
                     scope_type, scope_key, status, candidate_id, suggestion_id, evidence_score,
                     critic_verdict, comparison_verdict, risk_verdict_json,
                     decision_policy_json, rollback_plan_json, posterior_refs_json,
                     autonomy_guard_json, boundary_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["governance_id"],
                        item.get("plan_id", ""),
                        item.get("eval_id", ""),
                        item.get("governance_action", ""),
                        item.get("scope_type", ""),
                        item.get("scope_key", ""),
                        item.get("status", ""),
                        item.get("candidate_id", ""),
                        item.get("suggestion_id", ""),
                        _safe_float(item.get("evidence_score")),
                        item.get("critic_verdict", ""),
                        item.get("comparison_verdict", ""),
                        _dumps(item.get("risk_verdict", {})),
                        _dumps(item.get("decision_policy", {})),
                        _dumps(item.get("rollback_plan", {})),
                        _dumps(item.get("posterior_refs", {})),
                        _dumps(item.get("autonomy_guard", {})),
                        _dumps(item.get("boundary", {})),
                        _safe_float(item.get("created_at")),
                        _safe_float(item.get("updated_at")),
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _row_to_governance(row: Any) -> dict[str, Any]:
        return {
            "governance_id": str(row["governance_id"] or ""),
            "schema_version": "brain_medium_impact_governance.v1",
            "plan_id": str(row["plan_id"] or ""),
            "eval_id": str(row["eval_id"] or ""),
            "governance_action": str(row["governance_action"] or ""),
            "scope_type": str(row["scope_type"] or ""),
            "scope_key": str(row["scope_key"] or ""),
            "status": str(row["status"] or ""),
            "candidate_id": str(row["candidate_id"] or ""),
            "suggestion_id": str(row["suggestion_id"] or ""),
            "evidence_score": _safe_float(row["evidence_score"]),
            "critic_verdict": str(row["critic_verdict"] or ""),
            "comparison_verdict": str(row["comparison_verdict"] or ""),
            "risk_verdict": _loads(row["risk_verdict_json"], {}),
            "decision_policy": _loads(row["decision_policy_json"], {}),
            "rollback_plan": _loads(row["rollback_plan_json"], {}),
            "posterior_refs": _loads(row["posterior_refs_json"], {}),
            "autonomy_guard": _loads(row["autonomy_guard_json"], {}),
            "boundary": _loads(row["boundary_json"], BrainMediumImpactGovernanceService.boundary()),
            "created_at": _safe_float(row["created_at"]),
            "updated_at": _safe_float(row["updated_at"]),
        }

    @staticmethod
    def _missing_status(status: str) -> dict[str, Any]:
        return {
            "ok": False,
            "schema_version": "brain_medium_impact_governance_list.v1",
            "status": status,
            "items": [],
            "boundary": BrainMediumImpactGovernanceService.boundary(),
        }
