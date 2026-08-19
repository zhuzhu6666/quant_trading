"""V16 Brain Planning & Governance — merged shadow-planning layer.

Combines 5 V16 read-only services that form a natural pipeline:
  BrainActionPlannerService       (shadow action plans from brain state)
  BrainActionPlanEvaluatorService (compare plans to posterior evidence)
  BrainLowImpactExecutorService   (read-only replay job)
  BrainMediumImpactGovernanceService (candidate materialization)
  BrainLiveReadyGuardrailService  (live readiness evaluation)

All are read-only or write-only-audit-tables. They do not submit orders,
change weights, mutate runtime config, or bypass risk policy.

Previously: 5 files, ~2,317 lines (+ 592 + 579 from snapshot = ~3,488 total V16)
Now:        v16_brain_snapshot.py (~690 lines) + this file (~1,500 lines)
"""
from __future__ import annotations

import time
import uuid
import hashlib
from pathlib import Path
from typing import Any

from alpha.decision_policy import DecisionPolicy
from backend.core.db import STATE_DB, state_table_columns, state_table_exists
from backend.services._brain_helpers import (
    connect,
    dumps,
    execute,
    loads,
    safe_float,
    text,
)
from backend.services.canonical_v2_reader import (
    canonical_ready,
    iter_position_rows,
    iter_review_rows_desc,
)
from backend.services.agent_authority import control_surface, execution_owner
from backend.services.review_contract import review_has_system_contamination
from backend.services.v16_brain_snapshot import (
    BrainMemoryService,
    BrainStateService,
    build_posterior_arbitration,
)
from backend.services.brain_governance_candidates import (
    BrainGovernanceCandidateService,
    ensure_brain_governance_candidate_table,
)
from backend.services.state_payloads import (
    ensure_state_payload_schema,
    payload_hash,
    put_brain_action_plan_eval_payload,
)
from risk.policy_service import INCIDENT_MODE_RANK, INCIDENT_MODES, RiskPolicyService


# ---------------------------------------------------------------------------
# Table helpers
# ---------------------------------------------------------------------------

def _ensure_column(conn, db_path, table, column, col_def) -> None:
    cols = state_table_columns(conn, table)
    if column not in cols:
        execute(conn, f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")


def ensure_brain_action_plan_table(db_path: str | Path = STATE_DB) -> None:
    conn = connect(db_path)
    try:
        execute(conn, """CREATE TABLE IF NOT EXISTS brain_action_plan (
            plan_id TEXT PRIMARY KEY, snapshot_id TEXT DEFAULT '', hypothesis_id TEXT DEFAULT '',
            action_type TEXT DEFAULT '', status TEXT DEFAULT 'shadow_recorded',
            scope_json TEXT NOT NULL DEFAULT '{}', max_impact TEXT DEFAULT 'none_shadow_only',
            risk_class TEXT DEFAULT '', critic_verdict TEXT DEFAULT '',
            validation_refs_json TEXT NOT NULL DEFAULT '{}', rollback_plan_json TEXT NOT NULL DEFAULT '{}',
            required_services_json TEXT NOT NULL DEFAULT '[]', shadow_eval_json TEXT NOT NULL DEFAULT '{}',
            boundary_json TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL DEFAULT 0.0)""")
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_action_plan_created ON brain_action_plan(created_at)")
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_action_plan_snapshot ON brain_action_plan(snapshot_id, created_at)")
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_action_plan_type ON brain_action_plan(action_type, status)")
        conn.commit()
    finally:
        conn.close()


def ensure_brain_action_plan_eval_table(db_path: str | Path = STATE_DB) -> None:
    ensure_brain_action_plan_table(db_path)
    conn = connect(db_path)
    try:
        execute(conn, """CREATE TABLE IF NOT EXISTS brain_action_plan_eval (
            eval_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, snapshot_id TEXT DEFAULT '',
            action_type TEXT DEFAULT '', scope_type TEXT DEFAULT '', status TEXT DEFAULT 'needs_evidence',
            comparison_verdict TEXT DEFAULT 'needs_more_evidence', coverage_score REAL NOT NULL DEFAULT 0.0,
            comparison_json TEXT NOT NULL DEFAULT '{}', evidence_refs_json TEXT NOT NULL DEFAULT '{}',
            boundary_json TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL DEFAULT 0.0)""")
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_action_plan_eval_created ON brain_action_plan_eval(created_at)")
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_action_plan_eval_plan ON brain_action_plan_eval(plan_id, created_at)")
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_action_plan_eval_scope ON brain_action_plan_eval(scope_type, status, created_at)")
        conn.commit()
    finally:
        conn.close()
    ensure_state_payload_schema(db_path)


def ensure_brain_low_impact_execution_table(db_path: str | Path = STATE_DB) -> None:
    ensure_brain_action_plan_eval_table(db_path)
    conn = connect(db_path)
    try:
        execute(conn, """CREATE TABLE IF NOT EXISTS brain_low_impact_execution (
            execution_id TEXT PRIMARY KEY, plan_id TEXT DEFAULT '', eval_id TEXT DEFAULT '',
            action_type TEXT DEFAULT '', execution_action TEXT DEFAULT '', status TEXT DEFAULT '',
            evidence_score REAL NOT NULL DEFAULT 0.0, critic_verdict TEXT DEFAULT '',
            comparison_verdict TEXT DEFAULT '', risk_verdict_json TEXT NOT NULL DEFAULT '{}',
            rollback_plan_json TEXT NOT NULL DEFAULT '{}', result_json TEXT NOT NULL DEFAULT '{}',
            posterior_monitor_json TEXT NOT NULL DEFAULT '{}', boundary_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL DEFAULT 0.0, updated_at REAL NOT NULL DEFAULT 0.0)""")
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_low_impact_execution_created ON brain_low_impact_execution(created_at)")
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_low_impact_execution_plan ON brain_low_impact_execution(plan_id, eval_id)")
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_low_impact_execution_status ON brain_low_impact_execution(status, created_at)")
        conn.commit()
    finally:
        conn.close()


def ensure_brain_medium_impact_governance_table(db_path: str | Path = STATE_DB) -> None:
    ensure_brain_action_plan_eval_table(db_path)
    ensure_brain_governance_candidate_table(db_path)
    conn = connect(db_path)
    try:
        execute(conn, """CREATE TABLE IF NOT EXISTS brain_medium_impact_governance (
            governance_id TEXT PRIMARY KEY, plan_id TEXT DEFAULT '', eval_id TEXT DEFAULT '',
            governance_action TEXT DEFAULT '', scope_type TEXT DEFAULT '', scope_key TEXT DEFAULT '',
            status TEXT DEFAULT '', candidate_id TEXT DEFAULT '', suggestion_id TEXT DEFAULT '',
            evidence_score REAL NOT NULL DEFAULT 0.0, critic_verdict TEXT DEFAULT '',
            comparison_verdict TEXT DEFAULT '', risk_verdict_json TEXT NOT NULL DEFAULT '{}',
            decision_policy_json TEXT NOT NULL DEFAULT '{}', rollback_plan_json TEXT NOT NULL DEFAULT '{}',
            posterior_refs_json TEXT NOT NULL DEFAULT '{}', autonomy_guard_json TEXT NOT NULL DEFAULT '{}',
            boundary_json TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL DEFAULT 0.0,
            updated_at REAL NOT NULL DEFAULT 0.0)""")
        columns = state_table_columns(conn, "brain_medium_impact_governance")
        if "candidate_id" not in columns:
            execute(conn, "ALTER TABLE brain_medium_impact_governance ADD COLUMN candidate_id TEXT DEFAULT ''")
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_medium_governance_created ON brain_medium_impact_governance(created_at)")
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_medium_governance_plan ON brain_medium_impact_governance(plan_id, eval_id)")
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_medium_governance_scope ON brain_medium_impact_governance(scope_type, status, created_at)")
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_medium_governance_candidate ON brain_medium_impact_governance(candidate_id)")
        conn.commit()
    finally:
        conn.close()


def ensure_brain_live_ready_guardrail_table(db_path: str | Path = STATE_DB) -> None:
    conn = connect(db_path)
    try:
        execute(conn, """CREATE TABLE IF NOT EXISTS brain_live_ready_guardrail (
            guardrail_id TEXT PRIMARY KEY, status TEXT DEFAULT '',
            live_capability_lock_json TEXT NOT NULL DEFAULT '{}',
            broker_local_divergence_json TEXT NOT NULL DEFAULT '{}',
            incident_control_json TEXT NOT NULL DEFAULT '{}',
            incident_memory_json TEXT NOT NULL DEFAULT '{}',
            release_rollback_json TEXT NOT NULL DEFAULT '{}',
            p3_p4_evidence_json TEXT NOT NULL DEFAULT '{}',
            action_recommendation_json TEXT NOT NULL DEFAULT '{}',
            risk_precheck_json TEXT NOT NULL DEFAULT '{}',
            boundary_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL DEFAULT 0.0, updated_at REAL NOT NULL DEFAULT 0.0)""")
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_live_ready_guardrail_created ON brain_live_ready_guardrail(created_at)")
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_live_ready_guardrail_status ON brain_live_ready_guardrail(status, created_at)")
        conn.commit()
    finally:
        conn.close()


# ===================================================================
# 1. BrainActionPlannerService (V16 Phase 2 — shadow plans)
# ===================================================================

class BrainActionPlannerService:
    """V16 Phase 2 shadow action-plan ledger.

    Converts read-only brain hypotheses into shadow-only action plans.
    Does not execute, mutate runtime, change weights, or submit orders.
    """

    ACTIONS = [
        {"action_type": "shadow_factor_weight_review", "scope_type": "factor_weight",
         "scope_key": "alpha_weight_policy",
         "required_services": ["ReplayHarnessService", "RiskPolicyService", "DecisionPolicy"],
         "candidate_change": "compare candidate downweight/hold in replay before any future write"},
        {"action_type": "shadow_parameter_template_review", "scope_type": "parameter_template",
         "scope_key": "online_light",
         "required_services": ["ReplayHarnessService", "RiskPolicyService", "ParameterTemplateService"],
         "candidate_change": "compare online_light template candidates in shadow only"},
        {"action_type": "shadow_context_policy_review", "scope_type": "context_policy",
         "scope_key": "threshold_and_sizing",
         "required_services": ["ReplayHarnessService", "RiskPolicyService", "ContextPolicyService"],
         "candidate_change": "compare threshold/sizing posture without mutating runtime config"},
        {"action_type": "shadow_supervisor_template_review", "scope_type": "supervisor_template",
         "scope_key": "position_supervisor",
         "required_services": ["ReplayHarnessService", "RiskPolicyService", "PositionSupervisor"],
         "candidate_change": "compare supervisor hold/tighten/reduce/close template outcomes in shadow"},
    ]

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = db_path

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {"phase": "v16_phase2_shadow_brain", "read_only": True, "affects_trading": False,
                "shadow_only": True, "does_not_execute_action_plan": True,
                "does_not_mutate_runtime_overlay": True, "does_not_change_factor_weights": True,
                "does_not_switch_templates": True, "does_not_write_learning_samples": True,
                "future_execution_requires_risk_policy": True,
                "future_weight_writes_require_decision_policy": True}

    def build_plans(self, *, brain_state: dict[str, Any], persist: bool = True,
                    source: str = "brain_action_planner") -> dict[str, Any]:
        now = time.time()
        snapshot_id = str(brain_state.get("snapshot_id") or "")
        hypotheses = list(brain_state.get("hypotheses") or [])
        critic = dict(brain_state.get("critic") or {})
        world_model = dict(brain_state.get("world_model") or {})
        memory = dict(brain_state.get("memory") or {})
        plans = [self._plan_for_action(action=action, snapshot_id=snapshot_id,
                 hypotheses=hypotheses, critic=critic, world_model=world_model,
                 memory=memory, now=now, source=source) for action in self.ACTIONS]
        if persist:
            self._persist(plans)
        return {"ok": True, "schema_version": "brain_action_plan_run.v1",
                "phase": "v16_phase2_shadow_brain", "snapshot_id": snapshot_id,
                "plan_count": len(plans), "plans": plans, "boundary": self.boundary(),
                "read_only": True, "affects_trading": False, "created_at": now}

    def latest_plans(self, *, limit: int = 20) -> dict[str, Any]:
        ensure_brain_action_plan_table(self.db_path)
        limit = max(1, min(int(limit), 200))
        conn = connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, "brain_action_plan"):
                return self._missing_status("missing_table")
            rows = execute(conn, """SELECT plan_id, snapshot_id, hypothesis_id, action_type, status,
                scope_json, max_impact, risk_class, critic_verdict, validation_refs_json,
                rollback_plan_json, required_services_json, shadow_eval_json, boundary_json, created_at
                FROM brain_action_plan ORDER BY created_at DESC LIMIT ?""", (limit,)).fetchall()
            return {"ok": True, "schema_version": "brain_action_plan_list.v1", "status": "available",
                    "plans": [self._row_to_plan(row) for row in rows],
                    "read_only": True, "affects_trading": False, "boundary": self.boundary()}
        finally:
            conn.close()

    def status(self, *, limit: int = 20) -> dict[str, Any]:
        latest = self.latest_plans(limit=limit)
        plans = list(latest.get("plans") or [])
        if not plans:
            return {"ok": False, "schema_version": "brain_action_plan_readiness.v1",
                    "status": latest.get("status", "missing_plans"), "plan_count": 0,
                    "read_only": True, "affects_trading": False}
        return {"ok": True, "schema_version": "brain_action_plan_readiness.v1", "status": "available",
                "plan_count": len(plans), "latest_created_at": max(safe_float(p.get("created_at")) for p in plans),
                "critic_verdicts": sorted({str(p.get("critic_verdict") or "") for p in plans}),
                "action_types": sorted({str(p.get("action_type") or "") for p in plans}),
                "read_only": True, "affects_trading": False}

    def _plan_for_action(self, *, action: dict[str, Any], snapshot_id: str,
                         hypotheses: list[dict[str, Any]], critic: dict[str, Any],
                         world_model: dict[str, Any], memory: dict[str, Any],
                         now: float, source: str) -> dict[str, Any]:
        hypothesis = self._best_hypothesis(action["scope_type"], hypotheses)
        risk_class = self._risk_class(action["scope_type"], hypothesis, world_model)
        critic_verdict = self._critic_verdict(risk_class, critic, memory, world_model)
        status = "shadow_recorded" if critic_verdict in {"pass", "caution"} else "critic_rejected"
        validation_refs = self._validation_refs(snapshot_id, hypothesis, memory)
        posterior = dict(memory.get("posterior_arbitration") or {})
        selected = dict(posterior.get("selected_conclusion") or {})
        delegated_agent = execution_owner(
            control_surface(action["scope_type"], action["action_type"])
        )
        delegation = {
            "schema_version": "v16_agent_delegation.v1",
            "target_agent": delegated_agent,
            "command_owner": "v16_brain",
            "execution_owner": delegated_agent,
            "v16_may": ["select", "prioritize", "set_evidence_requirements", "route_to_governor"],
            "v16_may_not": ["write_policy_suggestion", "mutate_runtime_overlay", "change_factor_weight", "submit_order"],
        }
        scope = {"scope_type": action["scope_type"], "scope_key": action["scope_key"],
                 "candidate_change": action["candidate_change"], "source": source,
                 "world_model": {k: world_model.get(k) for k in
                                 ("strategy_posture", "factor_posture", "learning_posture", "execution_posture")},
                 "posterior_arbitration": posterior,
                 "delegation": delegation}
        if action["scope_type"] == "supervisor_template" and selected:
            scope["candidate_change"] = f"delegate {selected.get('recommended_action') or 'hold'} to {delegated_agent}"
            scope["selected_posterior_conclusion"] = selected
        return {
            "plan_id": f"bap_{uuid.uuid4().hex[:16]}", "schema_version": "brain_action_plan.v1",
            "snapshot_id": snapshot_id, "hypothesis_id": str(hypothesis.get("hypothesis_id") or ""),
            "action_type": action["action_type"], "status": status,
            "scope": scope,
            "max_impact": "none_shadow_only", "risk_class": risk_class,
            "critic_verdict": critic_verdict, "validation_refs": validation_refs,
            "rollback_plan": {"required": False, "reason": "shadow_plan_does_not_mutate_runtime_state",
                              "future_if_executed": {"requires_runtime_config_snapshot": True,
                                                     "requires_rollback_json": True,
                                                     "requires_risk_policy_verdict": True}},
            "required_services": list(action["required_services"]),
            "shadow_eval": {"schema_version": "brain_shadow_action_eval_contract.v1", "record_only": True,
                            "compare_to_sources": ["replay_report", "trade_outcome_review",
                                                    "learning_application_effect", "position_supervisor_trace",
                                                    "supervisor_counterfactual_review"],
                            "success_metric": "post_action_reward_delta_or_replay_agreement",
                            "minimum_observation": {"replay_required_before_execution": True,
                                                    "live_observed_trade_count_before_governance": 3}},
            "boundary": self.boundary(), "read_only": True, "affects_trading": False, "created_at": now,
        }

    @staticmethod
    def _best_hypothesis(scope_type: str, hypotheses: list[dict[str, Any]]) -> dict[str, Any]:
        aliases = {"factor_weight": {"factor", "simulation", "runtime"},
                    "parameter_template": {"simulation", "autonomy", "runtime"},
                    "context_policy": {"autonomy", "incident", "runtime", "simulation"},
                    "supervisor_template": {"incident", "runtime", "simulation"}}
        candidates = [h for h in hypotheses if str(h.get("scope") or "") in aliases.get(scope_type, {scope_type})]
        if not candidates:
            candidates = hypotheses
        return max(candidates, key=lambda h: safe_float(h.get("evidence_score"))) if candidates else {}

    @staticmethod
    def _risk_class(scope_type: str, hypothesis: dict[str, Any], world_model: dict[str, Any]) -> str:
        if str(world_model.get("strategy_posture") or "") in {"no_new_risk", "observation_only"}:
            return "high"
        if str(hypothesis.get("risk_class") or "") in {"high", "medium", "low"}:
            return str(hypothesis.get("risk_class"))
        return "medium" if scope_type in {"factor_weight", "parameter_template"} else "low"

    @staticmethod
    def _critic_verdict(risk_class: str, critic: dict[str, Any], memory: dict[str, Any],
                        world_model: dict[str, Any]) -> str:
        if str(world_model.get("execution_posture") or "") == "unsafe":
            return "reject"
        if risk_class == "high" and str(world_model.get("incident_mode") or "normal") != "normal":
            return "reject"
        if str(critic.get("verdict") or "") in {"reject"}:
            return "reject"
        if memory.get("negative_matches") or str(critic.get("verdict") or "") in {"shadow_only", "caution"}:
            return "caution"
        return "pass"

    @staticmethod
    def _validation_refs(snapshot_id: str, hypothesis: dict[str, Any], memory: dict[str, Any]) -> dict[str, Any]:
        memory_items = [{"memory_id": m.get("memory_id"), "source_table": m.get("source_table"),
                         "source_id": m.get("source_id"), "polarity": m.get("polarity")}
                        for m in (list(memory.get("negative_matches") or [])[:3] +
                                  list(memory.get("counter_evidence") or [])[:3])]
        return {"brain_snapshot_id": snapshot_id, "hypothesis_id": str(hypothesis.get("hypothesis_id") or ""),
                "hypothesis_evidence_refs": hypothesis.get("evidence_refs") or {},
                "hypothesis_counter_evidence_refs": hypothesis.get("counter_evidence_refs") or {},
                "memory_refs": memory_items,
                "posterior_arbitration": memory.get("posterior_arbitration") or {},
                "requires_replay_before_execution": True}

    def _persist(self, plans: list[dict[str, Any]]) -> None:
        ensure_brain_action_plan_table(self.db_path)
        conn = connect(self.db_path)
        try:
            for p in plans:
                execute(conn, """INSERT INTO brain_action_plan (plan_id, snapshot_id, hypothesis_id, action_type,
                    status, scope_json, max_impact, risk_class, critic_verdict, validation_refs_json,
                    rollback_plan_json, required_services_json, shadow_eval_json, boundary_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (p["plan_id"], p.get("snapshot_id", ""), p.get("hypothesis_id", ""), p.get("action_type", ""),
                     p.get("status", ""), dumps(p.get("scope", {})), p.get("max_impact", ""),
                     p.get("risk_class", ""), p.get("critic_verdict", ""),
                     dumps(p.get("validation_refs", {})), dumps(p.get("rollback_plan", {})),
                     dumps(p.get("required_services", [])), dumps(p.get("shadow_eval", {})),
                     dumps(p.get("boundary", {})), safe_float(p.get("created_at"))))
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _row_to_plan(row: Any) -> dict[str, Any]:
        return {"plan_id": str(row["plan_id"] or ""), "schema_version": "brain_action_plan.v1",
                "snapshot_id": str(row["snapshot_id"] or ""), "hypothesis_id": str(row["hypothesis_id"] or ""),
                "action_type": str(row["action_type"] or ""), "status": str(row["status"] or ""),
                "scope": loads(row["scope_json"], {}), "max_impact": str(row["max_impact"] or ""),
                "risk_class": str(row["risk_class"] or ""), "critic_verdict": str(row["critic_verdict"] or ""),
                "validation_refs": loads(row["validation_refs_json"], {}),
                "rollback_plan": loads(row["rollback_plan_json"], {}),
                "required_services": loads(row["required_services_json"], []),
                "shadow_eval": loads(row["shadow_eval_json"], {}),
                "boundary": loads(row["boundary_json"], BrainActionPlannerService.boundary()),
                "read_only": True, "affects_trading": False, "created_at": safe_float(row["created_at"])}

    @staticmethod
    def _missing_status(status: str) -> dict[str, Any]:
        return {"ok": False, "schema_version": "brain_action_plan_list.v1", "status": status, "plans": [],
                "read_only": True, "affects_trading": False, "boundary": BrainActionPlannerService.boundary()}


# ===================================================================
# 2. BrainActionPlanEvaluatorService (V16 Phase 2 — posterior comparison)
# ===================================================================

class BrainActionPlanEvaluatorService:
    """Compare V16 shadow plans with already-recorded posterior evidence."""

    REQUIRED_SOURCES = ["replay_report", "trade_outcome_review",
                        "learning_application_effect", "position_supervisor_trace"]
    OPTIONAL_POSTERIOR_SOURCES = ["supervisor_counterfactual_review"]

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = db_path

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {"phase": "v16_phase2_shadow_brain_eval", "read_only": True, "affects_trading": False,
                "record_only": True, "does_not_execute_action_plan": True,
                "does_not_mutate_runtime_overlay": True, "does_not_change_factor_weights": True,
                "does_not_switch_templates": True, "does_not_write_learning_samples": True,
                "comparison_sources_only": True}

    def evaluate_latest_plans(
        self,
        *,
        limit: int = 20,
        persist: bool = True,
        evaluation_run_id: str = "",
    ) -> dict[str, Any]:
        ensure_brain_action_plan_eval_table(self.db_path)
        limit = max(1, min(int(limit), 200))
        plans = list(BrainActionPlannerService(self.db_path).latest_plans(limit=limit).get("plans") or [])
        if not plans:
            return {"ok": False, "schema_version": "brain_action_plan_eval_run.v1",
                    "status": "missing_action_plans", "evals": [], "read_only": True,
                    "affects_trading": False, "boundary": self.boundary()}
        now = time.time()
        evidence = self._load_evidence(limit=100)
        evals = [self._evaluate_plan(plan=p, evidence=evidence, now=now) for p in plans]
        if persist:
            self._persist(evals, evaluation_run_id=str(evaluation_run_id or ""))
        return {"ok": True, "schema_version": "brain_action_plan_eval_run.v1", "status": "evaluated",
                "eval_count": len(evals), "evals": evals, "source_gaps": evidence.get("source_gaps", []),
                "read_only": True, "affects_trading": False, "boundary": self.boundary(), "created_at": now}

    def latest_evals(self, *, limit: int = 20) -> dict[str, Any]:
        ensure_brain_action_plan_eval_table(self.db_path)
        limit = max(1, min(int(limit), 200))
        conn = connect(self.db_path, read_only=True)
        try:
            rows = execute(conn, """SELECT e.eval_id, e.plan_id, e.snapshot_id, e.action_type, e.scope_type,
                e.status, e.comparison_verdict, e.coverage_score,
                COALESCE(p.comparison_json, e.comparison_json) AS comparison_json,
                COALESCE(p.evidence_refs_json, e.evidence_refs_json) AS evidence_refs_json,
                COALESCE(p.boundary_json, e.boundary_json) AS boundary_json,
                e.payload_hash, e.evaluation_run_id, e.created_at
                FROM brain_action_plan_eval e
                LEFT JOIN brain_action_plan_eval_payload p ON p.payload_hash=e.payload_hash
                ORDER BY e.created_at DESC LIMIT ?""", (limit,)).fetchall()
            return {"ok": bool(rows), "schema_version": "brain_action_plan_eval_list.v1",
                    "status": "available" if rows else "missing_evals",
                    "evals": [self._row_to_eval(r) for r in rows],
                    "read_only": True, "affects_trading": False, "boundary": self.boundary()}
        finally:
            conn.close()

    def status(self, *, limit: int = 20) -> dict[str, Any]:
        latest = self.latest_evals(limit=limit)
        evals = list(latest.get("evals") or [])
        if not evals:
            return {"ok": False, "schema_version": "brain_action_plan_eval_readiness.v1",
                    "status": latest.get("status", "missing_evals"), "eval_count": 0,
                    "read_only": True, "affects_trading": False}
        return {"ok": True, "schema_version": "brain_action_plan_eval_readiness.v1", "status": "available",
                "eval_count": len(evals), "latest_created_at": max(safe_float(e.get("created_at")) for e in evals),
                "coverage_avg": round(sum(safe_float(e.get("coverage_score")) for e in evals) / len(evals), 6),
                "verdicts": sorted({str(e.get("comparison_verdict") or "") for e in evals}),
                "read_only": True, "affects_trading": False}

    def _load_evidence(self, *, limit: int) -> dict[str, Any]:
        conn = connect(self.db_path, read_only=True)
        source_gaps: list[str] = []
        try:
            replay = {}
            if state_table_exists(conn, "replay_report"):
                row = execute(conn, """SELECT replay_run_id, decision_count, matched_live_count,
                    mismatch_count, metric_summary_json, replay_error, evidence_grade,
                    artifact_hash, status, created_at FROM replay_report ORDER BY created_at DESC LIMIT 1""").fetchone()
                replay = dict(row) if row else {}
            else:
                source_gaps.append("missing_replay_report")
            trade_reviews = iter_review_rows_desc(conn, limit=limit)
            trade_reviews = [
                row
                for row in trade_reviews
                if not review_has_system_contamination(loads(row.get("review_json"), {}))
            ][:limit]
            clean_review_ids = {
                str(row.get("review_id") or "") for row in trade_reviews
            }
            # Effects are partitioned by scope in _evaluate_plan.  A global
            # LIMIT here can fill the page with factor rows and hide the
            # older supervisor effect that the selected plan needs.
            learning_effects = []
            try:
                from backend.services.learning_application_store import (
                    LearningApplicationStore,
                )

                learning_effects = list(
                    LearningApplicationStore(self.db_path).iter_effects()
                )
            except Exception:
                learning_effects = []
            supervisor_traces = self._fetch_table(conn, "position_supervisor_trace", limit,
                                                   cols=["trace_id", "decision_id", "position_id",
                                                         "trade_id", "action", "outcome", "risk_allowed",
                                                         "risk_reason", "execution_status",
                                                         "trace_integrity", "event_ts", "created_at"],
                                                   order_col="event_ts")
            counterfactuals = self._fetch_table(conn, "supervisor_counterfactual_review", None,
                                                 cols=["counterfactual_id", "review_id", "trade_id", "position_id",
                                                       "close_ts", "label", "confidence", "horizons_json",
                                                       "evidence_json", "updated_at", "created_at"],
                                                 order_col="updated_at")
            counterfactuals = [
                row
                for row in counterfactuals
                if str(row.get("review_id") or "") in clean_review_ids
                and not bool(loads(row.get("evidence_json"), {}).get("evidence_invalidated"))
                and bool(
                    (loads(row.get("evidence_json"), {}).get("maturity") or {}).get(
                        "governance_eligible"
                    )
                )
            ][:limit]
            return {"replay_report": replay, "trade_outcome_review": trade_reviews,
                    "learning_application_effect": learning_effects,
                    "position_supervisor_trace": supervisor_traces,
                    "supervisor_counterfactual_review": counterfactuals,
                    "source_gaps": source_gaps}
        finally:
            conn.close()

    def _fetch_table(self, conn, table: str, limit: int | None, *, cols: list[str],
                     order_col: str | None = None) -> list[dict[str, Any]]:
        if not state_table_exists(conn, table):
            return []
        available = state_table_columns(conn, table)
        select = [c for c in cols if c in available] or [sorted(available)[0]]
        orc = order_col if order_col in available else ("created_at" if "created_at" in available else select[0])
        sql = f"SELECT {', '.join(select)} FROM {table} ORDER BY {orc} DESC"
        rows = execute(conn, sql if limit is None else f"{sql} LIMIT ?", None if limit is None else (limit,)).fetchall()
        return [dict(r) for r in rows]

    def _evaluate_plan(self, *, plan: dict[str, Any], evidence: dict[str, Any], now: float) -> dict[str, Any]:
        scope = dict(plan.get("scope") or {})
        scope_type = str(scope.get("scope_type") or "")
        replay = dict(evidence.get("replay_report") or {})
        trade_reviews = list(evidence.get("trade_outcome_review") or [])
        learning_effects = [e for e in list(evidence.get("learning_application_effect") or [])
                            if self._matches_scope(scope_type, e)]
        supervisor_traces = list(evidence.get("position_supervisor_trace") or [])
        counterfactuals = [self._counterfactual_item(item) for item in list(evidence.get("supervisor_counterfactual_review") or [])]
        source_presence = {"replay_report": bool(replay.get("replay_run_id")),
                           "trade_outcome_review": bool(trade_reviews),
                           "learning_application_effect": bool(learning_effects),
                           "position_supervisor_trace": bool(supervisor_traces),
                           "supervisor_counterfactual_review": bool(counterfactuals)}
        # The counterfactual table is an optional posterior enrichment.  It
        # must improve routing quality, not inflate the four-source coverage
        # contract above 1.0.
        coverage_score = min(
            1.0,
            round(sum(1 for name in self.REQUIRED_SOURCES if source_presence.get(name)) / len(self.REQUIRED_SOURCES), 6),
        )
        comparison = self._comparison_summary(replay=replay, trade_reviews=trade_reviews,
                                               learning_effects=learning_effects,
                                               supervisor_traces=supervisor_traces,
                                               counterfactuals=counterfactuals,
                                               source_presence=source_presence)
        verdict = self._comparison_verdict(scope_type=scope_type, coverage_score=coverage_score, comparison=comparison)
        return {"eval_id": f"bape_{uuid.uuid4().hex[:16]}", "schema_version": "brain_action_plan_eval.v1",
                "plan_id": str(plan.get("plan_id") or ""), "snapshot_id": str(plan.get("snapshot_id") or ""),
                "action_type": str(plan.get("action_type") or ""), "scope_type": scope_type,
                "status": "comparable" if coverage_score >= 0.5 else "needs_evidence",
                "comparison_verdict": verdict, "coverage_score": coverage_score,
                "comparison": comparison,
                "evidence_refs": {"replay_report": replay.get("replay_run_id") or "",
                                  "trade_outcome_review": [t.get("review_id") for t in trade_reviews[:5]],
                                  "learning_application_effect": [e.get("application_id") for e in learning_effects[:5]],
                                  "position_supervisor_trace": [t.get("trace_id") for t in supervisor_traces[:5]],
                                  "supervisor_counterfactual_review": [t.get("counterfactual_id") for t in counterfactuals[:5]]},
                "boundary": self.boundary(), "read_only": True, "affects_trading": False, "created_at": now}

    @staticmethod
    def _matches_scope(scope_type: str, effect: dict[str, Any]) -> bool:
        effect_scope = str(effect.get("scope_type") or "")
        aliases = {
            "factor_weight": {"factor", "factor_weight", "alpha_weight_policy"},
            "parameter_template": {"parameter_template", "template", "online_light"},
            "context_policy": {"context", "context_policy", "threshold_and_sizing"},
            "supervisor_template": {
                "supervisor",
                "supervisor_template",
                "position_supervisor",
                "position_supervisor_template",
            },
        }
        allowed = aliases.get(scope_type, {scope_type})
        return effect_scope in allowed or str(effect.get("scope_key") or "") in allowed

    @staticmethod
    def _comparison_summary(*, replay: dict[str, Any], trade_reviews: list[dict[str, Any]],
                            learning_effects: list[dict[str, Any]], supervisor_traces: list[dict[str, Any]],
                            counterfactuals: list[dict[str, Any]], source_presence: dict[str, bool]) -> dict[str, Any]:
        dc = safe_float(replay.get("decision_count"))
        replay_agreement = safe_float(replay.get("matched_live_count")) / dc if dc > 0 else 0.0
        pnls = [safe_float(t.get("pnl")) for t in trade_reviews]
        avg_pnl = sum(pnls) / len(pnls) if pnls else 0.0
        deltas = [safe_float(e.get("delta_avg_reward")) for e in learning_effects]
        avg_delta = sum(deltas) / len(deltas) if deltas else 0.0
        risk_allowed = sum(1 for t in supervisor_traces if int(t.get("risk_allowed") or 0) == 1)
        outcomes: dict[str, int] = {}
        for t in trade_reviews:
            label = str(t.get("outcome_label") or "unknown")
            outcomes[label] = outcomes.get(label, 0) + 1
        return {
            "schema_version": "brain_action_plan_comparison.v1",
            "source_presence": source_presence,
            "replay": {"replay_run_id": replay.get("replay_run_id") or "",
                       "status": replay.get("status") or "", "evidence_grade": replay.get("evidence_grade") or "",
                       "decision_count": int(dc), "mismatch_count": int(safe_float(replay.get("mismatch_count"))),
                       "agreement": round(replay_agreement, 6), "has_error": bool(replay.get("replay_error"))},
            "trade_outcomes": {"review_count": len(trade_reviews), "avg_pnl": round(avg_pnl, 6),
                               "outcomes": dict(sorted(outcomes.items()))},
            "learning_effects": {"effect_count": len(learning_effects), "avg_delta_reward": round(avg_delta, 6),
                                 "statuses": sorted({str(e.get("status") or "") for e in learning_effects})},
            "supervisor": {"trace_count": len(supervisor_traces),
                           "risk_allowed_coverage": round(risk_allowed / len(supervisor_traces), 6) if supervisor_traces else 0.0,
                           "integrity_issues": sum(1 for t in supervisor_traces if str(t.get("trace_integrity") or "full") != "full")},
            "counterfactual": {"count": len(counterfactuals),
                               "labels": dict(sorted({str(item.get("label") or "unknown"): sum(1 for x in counterfactuals if str(x.get("label") or "unknown") == str(item.get("label") or "unknown")) for item in counterfactuals}.items())),
                               "items": counterfactuals[:5]},
            "posterior_arbitration": build_posterior_arbitration(
                trade_reviews=trade_reviews,
                counterfactuals=counterfactuals,
            ),
        }

    @staticmethod
    def _comparison_verdict(*, scope_type: str, coverage_score: float, comparison: dict[str, Any]) -> str:
        if coverage_score < 0.5:
            return "needs_more_evidence"
        arbitration = dict(comparison.get("posterior_arbitration") or {})
        selected_scope = str(arbitration.get("selected_scope") or "")
        supervisor = dict(arbitration.get("supervisor_conclusion") or {})
        if scope_type == "supervisor_template" and selected_scope == "supervisor":
            if supervisor.get("recommended_action") == "less_tighten" and safe_float(supervisor.get("confidence")) >= 0.6:
                return "supportive"
            if supervisor.get("recommended_action") == "tighten" and safe_float(supervisor.get("confidence")) >= 0.6:
                return "caution"
        delta = safe_float((comparison.get("learning_effects") or {}).get("avg_delta_reward"))
        avg_pnl = safe_float((comparison.get("trade_outcomes") or {}).get("avg_pnl"))
        replay_err = bool((comparison.get("replay") or {}).get("has_error"))
        if replay_err or delta < -0.05 or avg_pnl < 0:
            return "caution"
        if delta > 0.05 or avg_pnl > 0:
            return "supportive"
        return "inconclusive"

    @staticmethod
    def _counterfactual_item(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "counterfactual_id": str(item.get("counterfactual_id") or ""),
            "review_id": str(item.get("review_id") or ""),
            "trade_id": str(item.get("trade_id") or ""),
            "position_id": str(item.get("position_id") or ""),
            "label": str(item.get("label") or ""),
            "confidence": safe_float(item.get("confidence")),
            "horizons": loads(item.get("horizons_json"), []),
            "evidence": loads(item.get("evidence_json"), {}),
        }

    def _persist(self, evals: list[dict[str, Any]], *, evaluation_run_id: str = "") -> None:
        ensure_brain_action_plan_eval_table(self.db_path)
        conn = connect(self.db_path)
        try:
            for e in evals:
                comparison_json = dumps(e.get("comparison", {}))
                evidence_refs_json = dumps(e.get("evidence_refs", {}))
                boundary_json = dumps(e.get("boundary", {}))
                eval_payload_hash = payload_hash(
                    "\x00".join((comparison_json, evidence_refs_json, boundary_json)),
                    namespace="brain_action_plan_eval_payload.v1",
                )
                if evaluation_run_id:
                    existing = execute(
                        conn,
                        """SELECT eval_id FROM brain_action_plan_eval
                           WHERE evaluation_run_id=? AND plan_id=? LIMIT 1""",
                        (str(evaluation_run_id), str(e.get("plan_id", ""))),
                    ).fetchone()
                    if existing:
                        e["eval_id"] = str(
                            existing["eval_id"] if hasattr(existing, "keys") else existing[0]
                        )
                        continue
                put_brain_action_plan_eval_payload(
                    conn,
                    eval_payload_hash,
                    comparison_json,
                    evidence_refs_json,
                    boundary_json,
                    created_at=safe_float(e.get("created_at")),
                )
                inserted = execute(conn, """INSERT INTO brain_action_plan_eval (eval_id, plan_id, snapshot_id,
                    action_type, scope_type, status, comparison_verdict, coverage_score,
                    comparison_json, evidence_refs_json, boundary_json, payload_hash,
                    evaluation_run_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}', '{}', '{}', ?, ?, ?)
                    ON CONFLICT DO NOTHING""",
                    (e["eval_id"], e.get("plan_id", ""), e.get("snapshot_id", ""),
                     e.get("action_type", ""), e.get("scope_type", ""), e.get("status", ""),
                     e.get("comparison_verdict", ""), safe_float(e.get("coverage_score")),
                     eval_payload_hash, str(evaluation_run_id or ""), safe_float(e.get("created_at"))))
                if evaluation_run_id and int(getattr(inserted, "rowcount", 1) or 0) == 0:
                    existing = execute(
                        conn,
                        """SELECT eval_id FROM brain_action_plan_eval
                           WHERE evaluation_run_id=? AND plan_id=? LIMIT 1""",
                        (str(evaluation_run_id), str(e.get("plan_id", ""))),
                    ).fetchone()
                    if existing:
                        e["eval_id"] = str(
                            existing["eval_id"] if hasattr(existing, "keys") else existing[0]
                        )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _row_to_eval(row: Any) -> dict[str, Any]:
        return {"eval_id": str(row["eval_id"] or ""), "schema_version": "brain_action_plan_eval.v1",
                "plan_id": str(row["plan_id"] or ""), "snapshot_id": str(row["snapshot_id"] or ""),
                "action_type": str(row["action_type"] or ""), "scope_type": str(row["scope_type"] or ""),
                "status": str(row["status"] or ""), "comparison_verdict": str(row["comparison_verdict"] or ""),
                "coverage_score": safe_float(row["coverage_score"]),
                "comparison": loads(row["comparison_json"], {}),
                "evidence_refs": loads(row["evidence_refs_json"], {}),
                "boundary": loads(row["boundary_json"], BrainActionPlanEvaluatorService.boundary()),
                "read_only": True, "affects_trading": False, "created_at": safe_float(row["created_at"])}


# ===================================================================
# 3. BrainLowImpactExecutorService (V16 Phase 3 — read-only replay job)
# ===================================================================

class BrainLowImpactExecutorService:
    """V16 Phase 3 low-impact executor. Only runs read-only replay jobs."""

    ALLOWED_ACTIONS = {"run_replay_job"}

    def __init__(self, db_path: str | Path = STATE_DB, *, replay_artifact_dir: str | Path | None = None):
        self.db_path = db_path
        self.replay_artifact_dir = replay_artifact_dir

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {"phase": "v16_phase3_low_impact_autonomous_brain", "low_impact_only": True,
                "allowed_actions": sorted(BrainLowImpactExecutorService.ALLOWED_ACTIONS),
                "does_not_change_factor_weights": True, "does_not_switch_templates": True,
                "does_not_submit_orders": True, "does_not_write_learning_samples": True,
                "replay_job_is_read_only": True, "autonomy_tighten_uses_incident_control": True,
                "risk_policy_service_required": True,
                "rollback_or_downgrade_required_for_bad_posterior": True}

    def execute_latest(self, *, limit: int = 1, allow_tighten: bool = False,
                       replay_lookback_days: float = 1.0, replay_limit: int = 100,
                       persist: bool = True) -> dict[str, Any]:
        ensure_brain_low_impact_execution_table(self.db_path)
        limit = max(1, min(int(limit), 20))
        evals = list(BrainActionPlanEvaluatorService(self.db_path).latest_evals(limit=limit).get("evals") or [])
        if not evals:
            return {"ok": False, "schema_version": "brain_low_impact_execution_run.v1",
                    "status": "missing_action_plan_evals", "executions": [], "boundary": self.boundary()}
        executions = [self._execute_eval(e, allow_tighten=allow_tighten,
                      replay_lookback_days=replay_lookback_days, replay_limit=replay_limit) for e in evals[:limit]]
        if persist:
            self._persist(executions)
        return {"ok": any(e.get("status") in {"executed", "executed_and_downgraded"} for e in executions),
                "schema_version": "brain_low_impact_execution_run.v1",
                "status": "executed" if executions else "empty",
                "execution_count": len(executions), "executions": executions,
                "boundary": self.boundary(), "created_at": time.time()}

    def latest_executions(self, *, limit: int = 20) -> dict[str, Any]:
        ensure_brain_low_impact_execution_table(self.db_path)
        limit = max(1, min(int(limit), 200))
        conn = connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, "brain_low_impact_execution"):
                return self._missing_status("missing_table")
            rows = execute(conn, """SELECT execution_id, plan_id, eval_id, action_type, execution_action,
                status, evidence_score, critic_verdict, comparison_verdict, risk_verdict_json,
                rollback_plan_json, result_json, posterior_monitor_json, boundary_json,
                created_at, updated_at FROM brain_low_impact_execution ORDER BY created_at DESC LIMIT ?""", (limit,)).fetchall()
            return {"ok": bool(rows), "schema_version": "brain_low_impact_execution_list.v1",
                    "status": "available" if rows else "missing_executions",
                    "executions": [self._row_to_execution(r) for r in rows], "boundary": self.boundary()}
        finally:
            conn.close()

    def status(self, *, limit: int = 20) -> dict[str, Any]:
        latest = self.latest_executions(limit=limit)
        executions = list(latest.get("executions") or [])
        if not executions:
            return {"ok": False, "schema_version": "brain_low_impact_execution_readiness.v1",
                    "status": latest.get("status", "missing_executions"), "execution_count": 0, "low_impact_only": True}
        return {"ok": True, "schema_version": "brain_low_impact_execution_readiness.v1", "status": "available",
                "execution_count": len(executions), "latest_created_at": max(safe_float(e.get("created_at")) for e in executions),
                "statuses": sorted({str(e.get("status") or "") for e in executions}), "low_impact_only": True}

    def _execute_eval(self, evaluation: dict[str, Any], *, allow_tighten: bool,
                      replay_lookback_days: float, replay_limit: int) -> dict[str, Any]:
        now = time.time()
        plan = self._load_plan(str(evaluation.get("plan_id") or ""))
        critic_verdict = str(plan.get("critic_verdict") or "")
        evidence_score = safe_float(evaluation.get("coverage_score"))
        comparison_verdict = str(evaluation.get("comparison_verdict") or "")
        risk_verdict = RiskPolicyService.shared().evaluate("run_replay_job", {
            "plan_id": evaluation.get("plan_id", ""), "eval_id": evaluation.get("eval_id", ""),
            "evidence_score": evidence_score, "critic_verdict": critic_verdict,
            "comparison_verdict": comparison_verdict, "mutates_runtime": False,
        }).to_dict()
        posterior_monitor = {"schema_version": "brain_low_impact_posterior_monitor.v1",
                             "comparison_verdict": comparison_verdict,
                             "bad_posterior": comparison_verdict == "caution",
                             "allow_tighten": bool(allow_tighten),
                             "downgrade": {"status": "not_required"}}
        status = "blocked_by_risk"
        result: dict[str, Any] = {}
        if critic_verdict == "reject":
            status = "blocked_by_critic"
        elif not bool(risk_verdict.get("allowed")):
            status = "blocked_by_risk"
        else:
            from backend.services.replay_harness import ReplayHarnessService
            kwargs = {"artifact_dir": self.replay_artifact_dir} if self.replay_artifact_dir is not None else {}
            replay = ReplayHarnessService(self.db_path, **kwargs).run_factor_gate_risk_replay(
                lookback_days=max(0.0, min(float(replay_lookback_days), 7.0)),
                limit=max(1, min(int(replay_limit), 500)),
                replay_run_id=f"brain_p3_replay_{uuid.uuid4().hex[:12]}",
            )
            result = {"schema_version": "brain_low_impact_replay_result.v1",
                      "replay_run_id": replay.get("replay_run_id", ""),
                      "replay_error": replay.get("replay_error", ""),
                      "decision_count": replay.get("decision_count", 0),
                      "evidence_grade": replay.get("evidence_grade", ""),
                      "artifact_hash": replay.get("artifact_hash", "")}
            bad_posterior = bool(replay.get("replay_error")) or comparison_verdict == "caution"
            posterior_monitor["bad_posterior"] = bad_posterior
            if bad_posterior and allow_tighten:
                downgrade = self._tighten_to_shadow_only(reason=f"v16_phase3:{evaluation.get('eval_id') or ''}")
                posterior_monitor["downgrade"] = downgrade
                status = "executed_and_downgraded" if downgrade.get("ok") else "executed_downgrade_blocked"
            else:
                posterior_monitor["downgrade"]["status"] = "pending_operator_or_future_cycle" if bad_posterior else "not_required"
                status = "executed"
        return {"execution_id": f"brain_p3_exec_{uuid.uuid4().hex[:16]}",
                "schema_version": "brain_low_impact_execution.v1",
                "plan_id": str(evaluation.get("plan_id") or ""), "eval_id": str(evaluation.get("eval_id") or ""),
                "action_type": str(evaluation.get("action_type") or ""), "execution_action": "run_replay_job",
                "status": status, "evidence_score": evidence_score, "critic_verdict": critic_verdict,
                "comparison_verdict": comparison_verdict, "risk_verdict": risk_verdict,
                "rollback_plan": {"schema_version": "brain_low_impact_rollback_plan.v1", "runtime_mutation": False,
                                  "primary_action": "run_replay_job", "rollback_required": False,
                                  "bad_posterior_action": "tighten_to_shadow_only" if allow_tighten else "record_pending_downgrade",
                                  "uses_risk_policy_for_tighten": True},
                "result": result, "posterior_monitor": posterior_monitor, "boundary": self.boundary(),
                "created_at": now, "updated_at": time.time()}

    def _tighten_to_shadow_only(self, *, reason: str) -> dict[str, Any]:
        from backend.services.incident_controls import RuntimeIncidentControlService
        return RuntimeIncidentControlService(self.db_path).set_mode("shadow_only", reason=reason,
                                                                     actor="system:v16_brain_low_impact_executor",
                                                                     confirm_thaw=False)

    def _load_plan(self, plan_id: str) -> dict[str, Any]:
        if not plan_id:
            return {}
        conn = connect(self.db_path, read_only=True)
        try:
            row = execute(conn, "SELECT plan_id, critic_verdict, scope_json, boundary_json FROM brain_action_plan WHERE plan_id = ? LIMIT 1",
                          (plan_id,)).fetchone()
            return {"plan_id": str(row["plan_id"] or ""), "critic_verdict": str(row["critic_verdict"] or ""),
                    "scope": loads(row["scope_json"], {}), "boundary": loads(row["boundary_json"], {})} if row else {}
        finally:
            conn.close()

    def _persist(self, executions: list[dict[str, Any]]) -> None:
        ensure_brain_low_impact_execution_table(self.db_path)
        conn = connect(self.db_path)
        try:
            for e in executions:
                execute(conn, """INSERT INTO brain_low_impact_execution (execution_id, plan_id, eval_id,
                    action_type, execution_action, status, evidence_score, critic_verdict,
                    comparison_verdict, risk_verdict_json, rollback_plan_json, result_json,
                    posterior_monitor_json, boundary_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (e["execution_id"], e.get("plan_id", ""), e.get("eval_id", ""), e.get("action_type", ""),
                     e.get("execution_action", ""), e.get("status", ""), safe_float(e.get("evidence_score")),
                     e.get("critic_verdict", ""), e.get("comparison_verdict", ""),
                     dumps(e.get("risk_verdict", {})), dumps(e.get("rollback_plan", {})),
                     dumps(e.get("result", {})), dumps(e.get("posterior_monitor", {})),
                     dumps(e.get("boundary", {})), safe_float(e.get("created_at")), safe_float(e.get("updated_at"))))
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _row_to_execution(row: Any) -> dict[str, Any]:
        return {"execution_id": str(row["execution_id"] or ""), "schema_version": "brain_low_impact_execution.v1",
                "plan_id": str(row["plan_id"] or ""), "eval_id": str(row["eval_id"] or ""),
                "action_type": str(row["action_type"] or ""), "execution_action": str(row["execution_action"] or ""),
                "status": str(row["status"] or ""), "evidence_score": safe_float(row["evidence_score"]),
                "critic_verdict": str(row["critic_verdict"] or ""),
                "comparison_verdict": str(row["comparison_verdict"] or ""),
                "risk_verdict": loads(row["risk_verdict_json"], {}),
                "rollback_plan": loads(row["rollback_plan_json"], {}),
                "result": loads(row["result_json"], {}),
                "posterior_monitor": loads(row["posterior_monitor_json"], {}),
                "boundary": loads(row["boundary_json"], BrainLowImpactExecutorService.boundary()),
                "created_at": safe_float(row["created_at"]), "updated_at": safe_float(row["updated_at"])}

    @staticmethod
    def _missing_status(status: str) -> dict[str, Any]:
        return {"ok": False, "schema_version": "brain_low_impact_execution_list.v1", "status": status,
                "executions": [], "boundary": BrainLowImpactExecutorService.boundary()}


# ===================================================================
# 4. BrainMediumImpactGovernanceService (V16 Phase 4 — candidate materialization)
# ===================================================================

class BrainMediumImpactGovernanceService:
    """V16 Phase 4 medium-impact governance candidate materializer."""

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = db_path

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {"phase": "v16_phase4_medium_impact_governance", "medium_impact_governance": True,
                "materializes_governance_candidates_only": True,
                "materializes_policy_suggestions_only": False,
                "does_not_write_policy_suggestion_directly": True,
                "policy_suggestion_bridge_manual_only": True,
                "demo_nursery_system_bridge_supported": True,
                "demo_nursery_human_approval_required": False,
                "does_not_apply_factor_weights": True, "does_not_switch_templates": True,
                "does_not_submit_orders": True, "does_not_write_learning_samples": True,
                "risk_policy_service_required": True,
                "decision_policy_preview_required_for_weight_actions": True,
                "runtime_overlay_snapshot_required_for_future_apply": True,
                "release_evidence_required_for_future_apply": True,
                "governance_candidate_service_required": True}

    def materialize_latest(self, *, limit: int = 4, allow_tighten_low_health: bool = False,
                           readiness: dict[str, Any] | None = None, persist: bool = True) -> dict[str, Any]:
        ensure_brain_medium_impact_governance_table(self.db_path)
        limit = max(1, min(int(limit), 20))
        evals = list(BrainActionPlanEvaluatorService(self.db_path).latest_evals(limit=limit).get("evals") or [])
        if not evals:
            return {"ok": False, "schema_version": "brain_medium_impact_governance_run.v1",
                    "status": "missing_action_plan_evals", "items": [], "boundary": self.boundary()}
        now = time.time()
        autonomy_guard = self._autonomy_guard(readiness=readiness or {}, allow_tighten_low_health=allow_tighten_low_health)
        runtime_targets = dict((readiness or {}).get("runtime_targets") or {})
        items = [
            self._materialize_eval(
                evaluation=e,
                now=now,
                autonomy_guard=autonomy_guard,
                persist_candidate=persist,
                runtime_targets=runtime_targets,
            )
            for e in evals[:limit]
        ]
        if persist:
            self._persist(items)
        return {"ok": any(i.get("status") == "candidate_materialized" for i in items),
                "schema_version": "brain_medium_impact_governance_run.v1", "status": "materialized",
                "item_count": len(items), "items": items, "autonomy_guard": autonomy_guard,
                "boundary": self.boundary(), "created_at": now}

    def latest_governance(self, *, limit: int = 20) -> dict[str, Any]:
        ensure_brain_medium_impact_governance_table(self.db_path)
        limit = max(1, min(int(limit), 200))
        conn = connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, "brain_medium_impact_governance"):
                return self._missing_status("missing_table")
            rows = execute(conn, """SELECT governance_id, plan_id, eval_id, governance_action, scope_type,
                scope_key, status, candidate_id, suggestion_id, evidence_score, critic_verdict,
                comparison_verdict, risk_verdict_json, decision_policy_json, rollback_plan_json,
                posterior_refs_json, autonomy_guard_json, boundary_json, created_at, updated_at
                FROM brain_medium_impact_governance ORDER BY created_at DESC LIMIT ?""", (limit,)).fetchall()
            return {"ok": bool(rows), "schema_version": "brain_medium_impact_governance_list.v1",
                    "status": "available" if rows else "missing_governance",
                    "items": [self._row_to_governance(r) for r in rows], "boundary": self.boundary()}
        finally:
            conn.close()

    def status(self, *, limit: int = 20) -> dict[str, Any]:
        latest = self.latest_governance(limit=limit)
        items = list(latest.get("items") or [])
        if not items:
            return {"ok": False, "schema_version": "brain_medium_impact_governance_readiness.v1",
                    "status": latest.get("status", "missing_governance"), "item_count": 0, "medium_impact_governance": True}
        return {"ok": True, "schema_version": "brain_medium_impact_governance_readiness.v1", "status": "available",
                "item_count": len(items), "latest_created_at": max(safe_float(i.get("created_at")) for i in items),
                "statuses": sorted({str(i.get("status") or "") for i in items}), "medium_impact_governance": True,
                "governance_candidates": BrainGovernanceCandidateService(self.db_path).status(limit=limit)}

    def _materialize_eval(
        self,
        *,
        evaluation: dict[str, Any],
        now: float,
        autonomy_guard: dict[str, Any],
        persist_candidate: bool,
        runtime_targets: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        plan = self._load_plan(str(evaluation.get("plan_id") or ""))
        mapped = self._map_action(evaluation=evaluation, plan=plan)
        comparison = dict(evaluation.get("comparison") or {})
        arbitration = dict(comparison.get("posterior_arbitration") or {})
        correction_contract = dict(arbitration.get("correction_contract") or {})
        parent_policy_decision_id = str(
            correction_contract.get("policy_decision_id") or ""
        )
        selected_scope = str(arbitration.get("selected_scope") or "")
        scope_causal = {
            "factor_weight": "factor",
            "parameter_template": "entry",
            "context_policy": "entry",
            "supervisor_template": "supervisor",
        }.get(str(evaluation.get("scope_type") or ""), "")
        if selected_scope and scope_causal and selected_scope != scope_causal:
            return {
                "governance_id": f"brain_p4_gov_{uuid.uuid4().hex[:16]}",
                "schema_version": "brain_medium_impact_governance.v1",
                "plan_id": str(evaluation.get("plan_id") or ""),
                "eval_id": str(evaluation.get("eval_id") or ""),
                "governance_action": mapped["policy_action"],
                "scope_type": mapped["scope_type"],
                "scope_key": mapped["scope_key"],
                "status": "not_selected_by_posterior",
                "candidate_id": "", "suggestion_id": "",
                "evidence_score": safe_float(evaluation.get("coverage_score")),
                "critic_verdict": str(plan.get("critic_verdict") or ""),
                "comparison_verdict": str(evaluation.get("comparison_verdict") or ""),
                "risk_verdict": {}, "decision_policy": {},
                "rollback_plan": self._rollback_plan(mapped),
                "posterior_refs": {
                    **dict(evaluation.get("evidence_refs") or {}),
                    "correction_contract": correction_contract,
                    "parent_policy_decision_id": parent_policy_decision_id,
                },
                "autonomy_guard": autonomy_guard, "boundary": self.boundary(),
                "created_at": now, "updated_at": time.time(),
            }
        no_op_reason = self._supervisor_no_op_reason(
            mapped,
            runtime_targets=dict(runtime_targets or {}),
        )
        if no_op_reason:
            return {
                "governance_id": f"brain_p4_gov_{uuid.uuid4().hex[:16]}",
                "schema_version": "brain_medium_impact_governance.v1",
                "plan_id": str(evaluation.get("plan_id") or ""),
                "eval_id": str(evaluation.get("eval_id") or ""),
                "governance_action": mapped["policy_action"],
                "scope_type": mapped["scope_type"],
                "scope_key": mapped["scope_key"],
                "status": "no_op",
                "decision_intent": "no_op",
                "no_op_reason": no_op_reason,
                "candidate_id": "",
                "suggestion_id": "",
                "evidence_score": safe_float(evaluation.get("coverage_score")),
                "critic_verdict": str(plan.get("critic_verdict") or ""),
                "comparison_verdict": str(evaluation.get("comparison_verdict") or ""),
                "risk_verdict": {"allowed": True, "status": "no_op", "reason": no_op_reason},
                "decision_policy": {
                    "schema_version": "decision_policy_preview.v1",
                    "required": False,
                    "action": "no_change",
                    "applied": False,
                    "reason": no_op_reason,
                },
                "rollback_plan": self._rollback_plan(mapped),
                "posterior_refs": {
                    **dict(evaluation.get("evidence_refs") or {}),
                    "correction_contract": correction_contract,
                    "parent_policy_decision_id": parent_policy_decision_id,
                    "decision_intent": "no_op",
                    "no_op_reason": no_op_reason,
                },
                "autonomy_guard": autonomy_guard,
                "boundary": self.boundary(),
                "created_at": now,
                "updated_at": time.time(),
            }
        evidence_score = safe_float(evaluation.get("coverage_score"))
        critic_verdict = str(plan.get("critic_verdict") or "")
        comparison_verdict = str(evaluation.get("comparison_verdict") or "")
        decision_policy = self._decision_policy_preview(
            {
                **mapped,
                "parent_policy_decision_id": parent_policy_decision_id,
            }
        )
        risk_verdict = RiskPolicyService.shared().evaluate(mapped["risk_action"], {
            "required_mode": "autonomous_governance", "session": {"drawdown_pct": 0.0},
            "evidence": {"brain_eval": evaluation.get("evidence_refs") or {},
                         "comparison": evaluation.get("comparison") or {},
                         "replay_summary": (evaluation.get("comparison") or {}).get("replay") or {},
                         "counterfactual_summary": (comparison.get("counterfactual") or {}) | {
                             "posterior_arbitration": arbitration,
                         }},
            "suggestion_status": "approved", "target_template_id": mapped.get("target_template_id", ""),
            "autonomous_apply": False,
        }).to_dict()
        status = "blocked_by_evidence"
        candidate_id = ""
        if critic_verdict == "reject":
            status = "blocked_by_critic"
        elif evidence_score < 0.5 or comparison_verdict in {"needs_more_evidence"}:
            status = "blocked_by_evidence"
        elif not bool(risk_verdict.get("allowed")):
            status = "blocked_by_risk"
        else:
            delegation = dict((plan.get("scope") or {}).get("delegation") or {})
            target_agent = execution_owner(
                control_surface(mapped["scope_type"], mapped["policy_action"])
            )
            candidate = BrainGovernanceCandidateService(self.db_path).create_candidate(
                candidate_id=self._candidate_id(evaluation=evaluation, mapped=mapped),
                source_agent="v16_brain", source_kind="brain_medium_impact_governance",
                source_ref_type=("v16_posterior_arbitration" if arbitration.get("fingerprint") else "brain_action_plan_eval"),
                source_ref_id=str(arbitration.get("fingerprint") or evaluation.get("eval_id") or ""),
                proposal_stage="governance_ready", capability_scope="medium_impact_governance",
                scope_type=mapped["scope_type"], scope_key=mapped["scope_key"],
                action=mapped["policy_action"],
                confidence=max(0.1, min(0.95, evidence_score)), evidence_score=evidence_score,
                risk_class="medium", max_impact="medium_impact",
                expected_effect=comparison,
                evidence_refs={
                    "plan_id": evaluation.get("plan_id", ""),
                    "eval_id": evaluation.get("eval_id", ""),
                    "posterior": evaluation.get("evidence_refs") or {},
                    "correction_contract": correction_contract,
                    "parent_policy_decision_id": parent_policy_decision_id,
                },
                counter_evidence_refs=dict(plan.get("counter_evidence_refs") or {}),
                risk_verdict=risk_verdict, decision_policy=decision_policy,
                rollback_plan=self._rollback_plan(mapped),
                lineage={"schema_version": "brain_medium_impact_candidate_lineage.v1",
                         "phase": "v16_phase4_medium_impact_governance",
                         "plan_id": evaluation.get("plan_id", ""), "eval_id": evaluation.get("eval_id", ""),
                         "critic_verdict": critic_verdict, "comparison_verdict": comparison_verdict,
                         "mapped_action": mapped, "posterior_arbitration": arbitration,
                         "correction_contract": correction_contract,
                         "parent_policy_decision_id": parent_policy_decision_id,
                         "delegation": {**delegation, "target_agent": target_agent,
                                        "command_owner": "v16_brain",
                                        "execution_owner": target_agent},
                         "bridge": {"policy_suggestion_direct_write": False, "governed_bridge_required": True,
                                     "demo_nursery_system_bridge": True, "non_demo_explicit_bridge": True}},
                expires_at=now + 14 * 86400, now=now, persist=persist_candidate,
            )
            candidate_id = str(candidate.get("candidate_id") or "")
            status = "candidate_materialized"
        return {"governance_id": f"brain_p4_gov_{uuid.uuid4().hex[:16]}",
                "schema_version": "brain_medium_impact_governance.v1",
                "plan_id": str(evaluation.get("plan_id") or ""), "eval_id": str(evaluation.get("eval_id") or ""),
                "governance_action": mapped["policy_action"], "scope_type": mapped["scope_type"],
                "scope_key": mapped["scope_key"], "status": status, "candidate_id": candidate_id,
                "suggestion_id": "", "evidence_score": evidence_score, "critic_verdict": critic_verdict,
                "comparison_verdict": comparison_verdict, "risk_verdict": risk_verdict,
                "decision_policy": decision_policy, "rollback_plan": self._rollback_plan(mapped),
                "posterior_refs": {
                    **dict(evaluation.get("evidence_refs") or {}),
                    "correction_contract": correction_contract,
                    "parent_policy_decision_id": parent_policy_decision_id,
                },
                "autonomy_guard": autonomy_guard, "boundary": self.boundary(),
                "created_at": now, "updated_at": time.time()}

    @staticmethod
    def _supervisor_no_op_reason(
        mapped: dict[str, Any],
        *,
        runtime_targets: dict[str, Any],
    ) -> str:
        if str(mapped.get("scope_type") or "") != "supervisor_template":
            return ""
        recommended = str(mapped.get("recommended_action") or "").strip().lower()
        if recommended in {"keep", "no_change", "hold", "observe", "watch"}:
            return f"posterior_recommended_{recommended}"
        target_template_id = str(mapped.get("target_template_id") or "")
        current_template_id = str(
            runtime_targets.get("position_supervisor_template_id") or ""
        )
        if target_template_id and current_template_id and target_template_id == current_template_id:
            return "target_template_already_active"
        return ""

    @staticmethod
    def _candidate_id(*, evaluation: dict[str, Any], mapped: dict[str, str]) -> str:
        comparison = dict(evaluation.get("comparison") or {})
        arbitration = dict(comparison.get("posterior_arbitration") or {})
        identity = "|".join(
            [
                str(arbitration.get("fingerprint") or evaluation.get("eval_id") or ""),
                str(mapped.get("scope_type") or ""),
                str(mapped.get("policy_action") or ""),
                str(mapped.get("target_template_id") or ""),
            ]
        )
        return f"brain_candidate_{hashlib.sha1(identity.encode('utf-8')).hexdigest()[:16]}"

    @staticmethod
    def _map_action(*, evaluation: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
        scope = str(evaluation.get("scope_type") or (plan.get("scope") or {}).get("scope_type") or "")
        if scope == "parameter_template":
            return {"scope_type": "parameter_template", "scope_key": "online_light:default",
                    "policy_action": "switch_parameter_template", "risk_action": "switch_parameter_template",
                    "target_template_id": ""}
        if scope == "context_policy":
            return {"scope_type": "context_policy", "scope_key": "threshold_and_sizing",
                    "policy_action": "enable_context_policy", "risk_action": "enable_context_policy",
                    "target_template_id": ""}
        if scope == "supervisor_template":
            arbitration = dict((evaluation.get("comparison") or {}).get("posterior_arbitration") or {})
            selected = dict(arbitration.get("supervisor_conclusion") or {})
            recommended = str(selected.get("recommended_action") or "")
            target = (
                "position_supervisor:conservative.v1"
                if recommended == "less_tighten"
                else "position_supervisor:profit_protection.v1"
                if recommended == "tighten"
                else "position_supervisor:conservative.v1"
            )
            return {"scope_type": "supervisor_template", "scope_key": "position_supervisor",
                    "policy_action": "switch_position_supervisor_template",
                    "risk_action": "switch_position_supervisor_template",
                    "target_template_id": target,
                    "recommended_action": recommended}
        plan_scope = dict(plan.get("scope") or {})
        arbitration = dict((evaluation.get("comparison") or {}).get("posterior_arbitration") or {})
        selected = dict(arbitration.get("selected_conclusion") or {})
        correction_contract = dict(arbitration.get("correction_contract") or {})
        factor_dimension = dict(
            (correction_contract.get("dimensions") or {}).get("factor") or {}
        )
        factor_id = str(
            evaluation.get("factor_id")
            or plan_scope.get("factor_id")
            or selected.get("factor_id")
            or factor_dimension.get("factor_id")
            or ""
        ).strip()
        if not factor_id:
            candidate_scope_key = str(plan_scope.get("scope_key") or "").strip()
            if candidate_scope_key and candidate_scope_key != "alpha_weight_policy":
                factor_id = candidate_scope_key
        factor_binding = dict(
            selected.get("factor_binding")
            or selected.get("runtime_binding")
            or plan_scope.get("factor_binding")
            or factor_dimension.get("factor_binding")
            or {}
        )
        expected_effect = dict(factor_dimension.get("expected_effect") or {})
        return {
            "scope_type": "factor",
            "scope_key": factor_id or "alpha_weight_policy",
            "factor_id": factor_id,
            "generation": factor_binding.get("generation")
            or factor_binding.get("live_generation_id")
            or factor_dimension.get("applicable_generation"),
            "selection_fingerprint": factor_binding.get("selection_fingerprint")
            or factor_dimension.get("selection_fingerprint"),
            "artifact_hash": factor_binding.get("artifact_hash")
            or factor_dimension.get("artifact_hash"),
            "evidence_refs": factor_binding.get("evidence_refs")
            or selected.get("evidence_refs")
            or factor_dimension.get("evidence_refs")
            or {},
            "target_weight": factor_binding.get("target_weight")
            or factor_dimension.get("target_weight")
            or expected_effect.get("target_weight"),
            "current_weight": factor_binding.get("current_weight")
            or factor_dimension.get("current_weight"),
            "causal_state": factor_dimension.get("causal_state"),
            "executable_allowed": bool(factor_dimension.get("executable_allowed")),
            "policy_action": "update_weight",
            "risk_action": "update_weight",
            "target_template_id": "",
        }

    @staticmethod
    def _decision_policy_preview(mapped: dict[str, Any]) -> dict[str, Any]:
        if mapped["policy_action"] != "update_weight":
            return {"schema_version": "decision_policy_preview.v1", "required": False}
        factor_id = str(mapped.get("factor_id") or "").strip()
        if not factor_id:
            return {
                "schema_version": "decision_policy_preview.v1",
                "required": True,
                "factor_id": "",
                "action": "no_change",
                "reason": "factor_id_unavailable_for_weight_patch",
                "executable_patch": False,
                "review_candidate": True,
                "applied": False,
                "parent_policy_decision_id": str(
                    mapped.get("parent_policy_decision_id") or ""
                ),
            }
        binding_fields = {
            "generation": mapped.get("generation") or mapped.get("live_generation_id"),
            "selection_fingerprint": mapped.get("selection_fingerprint"),
            "artifact_hash": mapped.get("artifact_hash"),
            "evidence_refs": mapped.get("evidence_refs"),
            "target_weight": mapped.get("target_weight"),
        }
        causal_state = str(mapped.get("causal_state") or "").lower()
        if (
            not all(binding_fields.values())
            or causal_state not in {"confirmed", "probable"}
            or not bool(mapped.get("executable_allowed"))
        ):
            return {
                "schema_version": "decision_policy_preview.v1",
                "required": True,
                "factor_id": factor_id,
                "action": "no_change",
                "reason": "factor_binding_or_governed_evidence_unavailable",
                "executable_patch": False,
                "review_candidate": True,
                "applied": False,
                "parent_policy_decision_id": str(
                    mapped.get("parent_policy_decision_id") or ""
                ),
            }
        try:
            target_weight = float(binding_fields["target_weight"])
        except (TypeError, ValueError):
            target_weight = 0.0
        if target_weight <= 0.0:
            return {
                "schema_version": "decision_policy_preview.v1",
                "required": True,
                "factor_id": factor_id,
                "action": "no_change",
                "reason": "governed_target_weight_unavailable",
                "executable_patch": False,
                "review_candidate": True,
                "applied": False,
            }
        decisions = DecisionPolicy().decide(
            awe_patches={factor_id: {"weight": target_weight, "reason": "v16_governed_factor_candidate"}},
            weight_policy_weights={factor_id: target_weight}, shadow_perfs={},
            factor_configs={factor_id: {"enabled": True, "role": "alpha"}},
            current_weights={factor_id: float(mapped.get("current_weight") or 0.0)})
        decision = decisions.get(factor_id)
        return {
            "schema_version": "decision_policy_preview.v1",
            "required": True,
            "factor_id": factor_id,
            "decision": decision.to_api() if decision else {},
            "executable_patch": False,
            "review_candidate": True,
            "applied": False,
            "parent_policy_decision_id": str(
                mapped.get("parent_policy_decision_id") or ""
            ),
        }

    @staticmethod
    def _rollback_plan(mapped: dict[str, str]) -> dict[str, Any]:
        return {"schema_version": "brain_medium_impact_rollback_plan.v1", "policy_suggestion_only": False,
                "candidate_lane_only": True, "runtime_mutation": False,
                "future_submit_requires_governed_bridge": True,
                "demo_nursery_system_bridge": True,
                "non_demo_explicit_bridge": True,
                "future_apply_requires_runtime_snapshot": True,
                "future_apply_requires_release_evidence": True,
                "future_apply_requires_rollback_json": True,
                "governance_action": mapped["policy_action"]}

    @staticmethod
    def _autonomy_guard(*, readiness: dict[str, Any], allow_tighten_low_health: bool) -> dict[str, Any]:
        posture = str((readiness or {}).get("autonomy_health", {}).get("posture") or "")
        return {"schema_version": "brain_medium_impact_autonomy_guard.v1", "posture": posture,
                "allow_tighten_low_health": bool(allow_tighten_low_health),
                "should_tighten": posture in {"constrained", "shadow_only", "frozen"},
                "tighten_applied": False, "reason": "p4_materializes_governance_candidates_only"}

    def _load_plan(self, plan_id: str) -> dict[str, Any]:
        if not plan_id:
            return {}
        conn = connect(self.db_path, read_only=True)
        try:
            row = execute(conn, "SELECT plan_id, critic_verdict, scope_json, validation_refs_json FROM brain_action_plan WHERE plan_id = ? LIMIT 1",
                          (plan_id,)).fetchone()
            if not row:
                return {}
            return {"plan_id": str(row["plan_id"] or ""), "critic_verdict": str(row["critic_verdict"] or ""),
                    "scope": loads(row["scope_json"], {}),
                    "counter_evidence_refs": (loads(row["validation_refs_json"], {}) or {}).get("counter_evidence_refs", {})}
        finally:
            conn.close()

    def _persist(self, items: list[dict[str, Any]]) -> None:
        ensure_brain_medium_impact_governance_table(self.db_path)
        conn = connect(self.db_path)
        try:
            for item in items:
                execute(conn, """INSERT INTO brain_medium_impact_governance
                    (governance_id, plan_id, eval_id, governance_action, scope_type, scope_key,
                     status, candidate_id, suggestion_id, evidence_score, critic_verdict,
                     comparison_verdict, risk_verdict_json, decision_policy_json, rollback_plan_json,
                     posterior_refs_json, autonomy_guard_json, boundary_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (item["governance_id"], item.get("plan_id", ""), item.get("eval_id", ""),
                     item.get("governance_action", ""), item.get("scope_type", ""), item.get("scope_key", ""),
                     item.get("status", ""), item.get("candidate_id", ""), item.get("suggestion_id", ""),
                     safe_float(item.get("evidence_score")), item.get("critic_verdict", ""),
                     item.get("comparison_verdict", ""), dumps(item.get("risk_verdict", {})),
                     dumps(item.get("decision_policy", {})), dumps(item.get("rollback_plan", {})),
                     dumps(item.get("posterior_refs", {})), dumps(item.get("autonomy_guard", {})),
                     dumps(item.get("boundary", {})), safe_float(item.get("created_at")), safe_float(item.get("updated_at"))))
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _row_to_governance(row: Any) -> dict[str, Any]:
        posterior_refs = loads(row["posterior_refs_json"], {})
        if not isinstance(posterior_refs, dict):
            posterior_refs = {}
        return {"governance_id": str(row["governance_id"] or ""), "schema_version": "brain_medium_impact_governance.v1",
                "plan_id": str(row["plan_id"] or ""), "eval_id": str(row["eval_id"] or ""),
                "governance_action": str(row["governance_action"] or ""),
                "scope_type": str(row["scope_type"] or ""), "scope_key": str(row["scope_key"] or ""),
                "status": str(row["status"] or ""), "candidate_id": str(row["candidate_id"] or ""),
                "suggestion_id": str(row["suggestion_id"] or ""),
                "evidence_score": safe_float(row["evidence_score"]),
                "critic_verdict": str(row["critic_verdict"] or ""),
                "comparison_verdict": str(row["comparison_verdict"] or ""),
                "risk_verdict": loads(row["risk_verdict_json"], {}),
                "decision_policy": loads(row["decision_policy_json"], {}),
                "rollback_plan": loads(row["rollback_plan_json"], {}),
                "posterior_refs": posterior_refs,
                "decision_intent": str(posterior_refs.get("decision_intent") or ""),
                "no_op_reason": str(posterior_refs.get("no_op_reason") or ""),
                "autonomy_guard": loads(row["autonomy_guard_json"], {}),
                "boundary": loads(row["boundary_json"], BrainMediumImpactGovernanceService.boundary()),
                "created_at": safe_float(row["created_at"]), "updated_at": safe_float(row["updated_at"])}

    @staticmethod
    def _missing_status(status: str) -> dict[str, Any]:
        return {"ok": False, "schema_version": "brain_medium_impact_governance_list.v1", "status": status,
                "items": [], "boundary": BrainMediumImpactGovernanceService.boundary()}


# ===================================================================
# 5. BrainLiveReadyGuardrailService (V16 Phase 5 — live readiness evaluation)
# ===================================================================

class BrainLiveReadyGuardrailService:
    """V16 Phase 5 live-ready guardrail evaluator and tightening entry."""

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = db_path

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {"phase": "v16_phase5_live_ready_guardrails", "guardrail_only": True,
                "does_not_submit_orders": True, "does_not_apply_policy_suggestions": True,
                "does_not_write_learning_samples": True, "does_not_relax_incident_mode": True,
                "tightening_requires_explicit_request": True,
                "tightening_uses_incident_control_service": True,
                "tightening_requires_risk_policy": True,
                "runtime_overlay_snapshot_managed_by_incident_control": True}

    def evaluate(self, *, readiness: dict[str, Any] | None = None, persist: bool = True,
                 source: str = "system:v16_p5_guardrail") -> dict[str, Any]:
        ensure_brain_live_ready_guardrail_table(self.db_path)
        readiness = dict(readiness or {})
        now = time.time()
        live_lock = self._live_capability_lock(readiness)
        divergence = self._broker_local_divergence(readiness)
        incident = self._incident_control(readiness)
        incident_memory = self._incident_memory()
        release_rollback = self._release_rollback(readiness)
        p3_p4 = self._p3_p4_evidence(readiness)
        recommendation = self._recommendation(live_lock=live_lock, divergence=divergence,
                                               incident=incident, incident_memory=incident_memory,
                                               release_rollback=release_rollback, p3_p4=p3_p4)
        risk_precheck = RiskPolicyService.shared().evaluate("set_incident_control", {
            "current_mode": incident.get("mode", "normal"),
            "target_mode": recommendation.get("target_mode", "no_new_risk"),
            "reason": "v16_live_ready_guardrail_precheck",
        }).to_dict()
        status = "live_ready_locked" if bool(live_lock.get("locked")) else "guardrail_attention_required"
        payload = {"guardrail_id": f"brain_p5_guard_{uuid.uuid4().hex[:16]}",
                   "schema_version": "brain_live_ready_guardrail.v1", "status": status, "source": source,
                   "live_capability_lock": live_lock, "broker_local_divergence": divergence,
                   "incident_control": incident, "incident_memory": incident_memory,
                   "release_rollback": release_rollback, "p3_p4_evidence": p3_p4,
                   "action_recommendation": recommendation, "risk_precheck": risk_precheck,
                   "boundary": self.boundary(), "created_at": now, "updated_at": now}
        if persist:
            self._persist(payload)
        return payload

    def latest_guardrails(self, *, limit: int = 20) -> dict[str, Any]:
        ensure_brain_live_ready_guardrail_table(self.db_path)
        limit = max(1, min(int(limit), 200))
        conn = connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, "brain_live_ready_guardrail"):
                return self._missing_status("missing_table")
            rows = execute(conn, """SELECT guardrail_id, status, live_capability_lock_json,
                broker_local_divergence_json, incident_control_json, incident_memory_json,
                release_rollback_json, p3_p4_evidence_json, action_recommendation_json,
                risk_precheck_json, boundary_json, created_at, updated_at
                FROM brain_live_ready_guardrail ORDER BY created_at DESC LIMIT ?""", (limit,)).fetchall()
            return {"ok": bool(rows), "schema_version": "brain_live_ready_guardrail_list.v1",
                    "status": "available" if rows else "missing_guardrail",
                    "items": [self._row_to_guardrail(r) for r in rows], "boundary": self.boundary()}
        finally:
            conn.close()

    def status(self, *, limit: int = 20) -> dict[str, Any]:
        latest = self.latest_guardrails(limit=limit)
        items = list(latest.get("items") or [])
        if not items:
            return {"ok": False, "schema_version": "brain_live_ready_guardrail_readiness.v1",
                    "status": latest.get("status", "missing_guardrail"), "item_count": 0, "live_ready_guardrails": True}
        item = items[0]
        return {"ok": bool(item.get("live_capability_lock", {}).get("locked")),
                "schema_version": "brain_live_ready_guardrail_readiness.v1",
                "status": str(item.get("status") or "available"), "item_count": len(items),
                "latest_created_at": safe_float(item.get("created_at")),
                "live_capability_locked": bool(item.get("live_capability_lock", {}).get("locked")),
                "recommended_mode": str(item.get("action_recommendation", {}).get("target_mode") or ""),
                "divergence_status": str(item.get("broker_local_divergence", {}).get("status") or ""),
                "live_ready_guardrails": True}

    def tighten(self, *, target_mode: str = "no_new_risk", reason: str = "",
                actor: str = "api:ops.brain.live_ready_guardrails",
                readiness: dict[str, Any] | None = None) -> dict[str, Any]:
        target_mode = str(target_mode or "no_new_risk").strip().lower()
        if target_mode not in INCIDENT_MODES:
            return {"ok": False, "status": "invalid_target_mode", "target_mode": target_mode, "boundary": self.boundary()}
        evaluation = self.evaluate(readiness=readiness, persist=True, source="system:v16_p5_guardrail.tighten_precheck")
        current_mode = str(evaluation.get("incident_control", {}).get("mode") or "normal")
        if INCIDENT_MODE_RANK.get(target_mode, 0) < INCIDENT_MODE_RANK.get(current_mode, 0):
            return {"ok": False, "schema_version": "brain_live_ready_guardrail_tighten.v1",
                    "status": "refused_to_relax_incident_mode", "current_mode": current_mode,
                    "target_mode": target_mode, "evaluation": evaluation, "boundary": self.boundary()}
        from backend.services.incident_controls import RuntimeIncidentControlService
        result = RuntimeIncidentControlService(self.db_path).set_mode(target_mode, reason=reason or "v16 live-ready guardrail tightening", actor=actor, confirm_thaw=False)
        return {"ok": bool(result.get("ok")), "schema_version": "brain_live_ready_guardrail_tighten.v1",
                "status": "tightened" if result.get("ok") else "tighten_blocked",
                "current_mode": current_mode, "target_mode": target_mode,
                "evaluation": evaluation, "incident_control_result": result, "boundary": self.boundary()}

    def _live_capability_lock(self, readiness: dict[str, Any]) -> dict[str, Any]:
        live = dict(readiness.get("live") or {})
        ctrader = dict(live.get("ctrader") or {})
        loop = dict(live.get("loop") or {})
        execution = dict(readiness.get("execution_semantics") or {})
        incident = dict(readiness.get("incident_control") or {})
        release = dict(readiness.get("release") or {})
        replay = dict(readiness.get("replay") or {})
        autonomy = dict(readiness.get("autonomy_health") or {})
        blockers = []
        if str(ctrader.get("status") or "").lower() not in {"connected", "warming_up"}:
            blockers.append("broker_not_connected")
        if not bool(loop.get("running")):
            blockers.append("live_loop_not_running")
        if not bool(live.get("readiness", {}).get("ok", True)):
            blockers.append("live_readiness_not_ok")
        if not bool(execution.get("effective_send_orders", True)):
            blockers.append("send_orders_disabled_or_unknown")
        if str(incident.get("mode") or "normal") != "normal":
            blockers.append("incident_mode_not_normal")
        if not bool(release.get("ok")):
            blockers.append("missing_release_run")
        latest_release = dict(release.get("latest_release") or {})
        if latest_release and not dict(latest_release.get("rollback_ref") or {}).get("snapshot_hash"):
            blockers.append("release_missing_snapshot_rollback_ref")
        if not bool(replay.get("ok")):
            blockers.append("missing_replay_evidence")
        if str(autonomy.get("posture") or "full") not in {"full", "constrained"}:
            blockers.append("autonomy_posture_not_live_ready")
        return {"schema_version": "brain_live_capability_lock.v1", "locked": not blockers, "blockers": blockers,
                "inputs": {"broker_status": str(ctrader.get("status") or ""), "loop_running": bool(loop.get("running")),
                           "effective_send_orders": bool(execution.get("effective_send_orders", True)),
                           "incident_mode": str(incident.get("mode") or "normal"),
                           "release_ok": bool(release.get("ok")), "replay_ok": bool(replay.get("ok")),
                           "autonomy_posture": str(autonomy.get("posture") or "")}}

    def _broker_local_divergence(self, readiness: dict[str, Any]) -> dict[str, Any]:
        live = dict(readiness.get("live") or {})
        positions = dict(live.get("positions") or readiness.get("positions") or {})
        bp = positions.get("broker_positions") or positions.get("positions")
        broker_count = len(bp) if isinstance(bp, list) else None
        local_count = self._local_open_position_count()
        if broker_count is None:
            return {"schema_version": "broker_local_divergence.v1", "status": "missing_broker_position_cache",
                    "broker_open_count": None, "local_open_count": local_count,
                    "divergence_count": None, "divergence_detected": False, "degraded": True}
        divergence = abs(int(broker_count) - int(local_count))
        return {"schema_version": "broker_local_divergence.v1", "status": "divergent" if divergence else "aligned",
                "broker_open_count": int(broker_count), "local_open_count": int(local_count),
                "divergence_count": divergence, "divergence_detected": divergence > 0, "degraded": False}

    def _incident_control(self, readiness: dict[str, Any]) -> dict[str, Any]:
        from backend.services.incident_controls import RuntimeIncidentControlService
        incident = dict(readiness.get("incident_control") or RuntimeIncidentControlService(self.db_path).status())
        mode = str(incident.get("mode") or "normal")
        return {"schema_version": "brain_incident_control_guardrail.v1", "mode": mode,
                "valid_modes": list(incident.get("valid_modes") or sorted(INCIDENT_MODES)),
                "only_close_available": "only_close" in INCIDENT_MODES,
                "no_new_risk_available": "no_new_risk" in INCIDENT_MODES,
                "autonomy_freeze_available": "frozen" in INCIDENT_MODES,
                "readiness_effect": incident.get("readiness_effect") or {}}

    def _incident_memory(self) -> dict[str, Any]:
        rows = self._latest_json_rows("incident_playbook_event", "event_id", "created_at",
                                      ["event_type", "status", "evidence_refs_json", "notes"], limit=5)
        return {"schema_version": "incident_memory_guardrail.v1", "available": bool(rows),
                "event_count": len(rows), "events": rows}

    def _release_rollback(self, readiness: dict[str, Any]) -> dict[str, Any]:
        release = dict(readiness.get("release") or {})
        latest = dict(release.get("latest_release") or {})
        rollback_ref = dict(latest.get("rollback_ref") or {})
        snapshot_hash = str(rollback_ref.get("snapshot_hash") or latest.get("runtime_config_hash") or "")
        return {"schema_version": "release_rollback_guardrail.v1", "release_available": bool(release.get("ok")),
                "run_id": str(latest.get("run_id") or ""), "release_status": str(latest.get("status") or ""),
                "rollback_ref": rollback_ref, "snapshot_hash": snapshot_hash, "rollback_ready": bool(snapshot_hash)}

    def _p3_p4_evidence(self, readiness: dict[str, Any]) -> dict[str, Any]:
        v16 = dict(readiness.get("v16") or {})
        p3 = dict(v16.get("low_impact_executions") or readiness.get("brain_low_impact_executions") or {})
        p4 = dict(v16.get("medium_impact_governance") or readiness.get("brain_medium_impact_governance") or {})
        return {"schema_version": "brain_p3_p4_guardrail_evidence.v1",
                "p3_available": bool(p3.get("ok")), "p3_status": str(p3.get("status") or ""),
                "p3_count": int(p3.get("execution_count") or p3.get("item_count") or 0),
                "p4_available": bool(p4.get("ok")), "p4_status": str(p4.get("status") or ""),
                "p4_count": int(p4.get("item_count") or 0)}

    @staticmethod
    def _recommendation(*, live_lock: dict[str, Any], divergence: dict[str, Any],
                        incident: dict[str, Any], incident_memory: dict[str, Any],
                        release_rollback: dict[str, Any], p3_p4: dict[str, Any]) -> dict[str, Any]:
        reasons = list(live_lock.get("blockers") or [])
        if divergence.get("divergence_detected"):
            reasons.append("broker_local_divergence")
        if divergence.get("degraded"):
            reasons.append("missing_broker_divergence_evidence")
        if not incident_memory.get("available"):
            reasons.append("missing_incident_memory")
        if not release_rollback.get("rollback_ready"):
            reasons.append("missing_release_rollback_ref")
        if not p3_p4.get("p3_available"):
            reasons.append("missing_p3_execution_evidence")
        if not p3_p4.get("p4_available"):
            reasons.append("missing_p4_governance_evidence")
        if live_lock.get("locked") and not reasons:
            target_mode = str(incident.get("mode") or "normal")
            action = "observe"
        elif divergence.get("divergence_detected") or not release_rollback.get("rollback_ready"):
            target_mode = "only_close"
            action = "tighten_to_only_close"
        elif "broker_not_connected" in reasons or "autonomy_posture_not_live_ready" in reasons:
            target_mode = "frozen"
            action = "freeze_autonomy"
        else:
            target_mode = "no_new_risk"
            action = "tighten_to_no_new_risk"
        return {"schema_version": "brain_live_ready_action_recommendation.v1", "action": action,
                "target_mode": target_mode, "reasons": sorted(set(str(r) for r in reasons if r)),
                "requires_operator_or_explicit_api": action != "observe"}

    def _local_open_position_count(self) -> int:
        conn = connect(self.db_path, read_only=True)
        try:
            if canonical_ready(conn):
                opened: set[str] = set()
                closed: set[str] = set()
                for row in iter_position_rows(conn, limit=0):
                    position_id = str(row.get("position_id") or "")
                    if not position_id:
                        continue
                    event_type = str(row.get("event_type") or "")
                    if event_type in ("opened", "open", "recovered"):
                        opened.add(position_id)
                    elif event_type in ("closed", "close", "retired", "failed"):
                        closed.add(position_id)
                return len(opened - closed)
            if not state_table_exists(conn, "position_lifecycle_event"):
                return 0
            row = execute(conn, """SELECT COUNT(DISTINCT position_id) AS cnt FROM position_lifecycle_event
                WHERE event_type IN ('opened', 'open', 'recovered')
                AND position_id NOT IN (SELECT position_id FROM position_lifecycle_event
                WHERE event_type IN ('closed', 'close', 'retired', 'failed'))""").fetchone()
            return int(row["cnt"]) if row and row["cnt"] is not None else 0
        except Exception:
            return 0
        finally:
            conn.close()

    def _latest_json_rows(self, table: str, id_col: str, ts_col: str, cols: list[str], *, limit: int = 5) -> list[dict[str, Any]]:
        conn = connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, table):
                return []
            select = f"{id_col}, {ts_col}, {', '.join(cols)}"
            rows = execute(conn, f"SELECT {select} FROM {table} ORDER BY {ts_col} DESC LIMIT ?", (limit,)).fetchall()
            out = []
            for r in rows:
                item = {id_col: str(r[id_col] or ""), ts_col: safe_float(r[ts_col])}
                for c in cols:
                    v = r[c]
                    item[c.replace("_json", "")] = loads(v, {}) if c.endswith("_json") else v
                out.append(item)
            return out
        finally:
            conn.close()

    def _persist(self, payload: dict[str, Any]) -> None:
        ensure_brain_live_ready_guardrail_table(self.db_path)
        conn = connect(self.db_path)
        try:
            execute(conn, """INSERT INTO brain_live_ready_guardrail (guardrail_id, status,
                live_capability_lock_json, broker_local_divergence_json, incident_control_json,
                incident_memory_json, release_rollback_json, p3_p4_evidence_json,
                action_recommendation_json, risk_precheck_json, boundary_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guardrail_id) DO UPDATE SET status=excluded.status,
                live_capability_lock_json=excluded.live_capability_lock_json,
                broker_local_divergence_json=excluded.broker_local_divergence_json,
                incident_control_json=excluded.incident_control_json,
                incident_memory_json=excluded.incident_memory_json,
                release_rollback_json=excluded.release_rollback_json,
                p3_p4_evidence_json=excluded.p3_p4_evidence_json,
                action_recommendation_json=excluded.action_recommendation_json,
                risk_precheck_json=excluded.risk_precheck_json, updated_at=excluded.updated_at""",
                (str(payload.get("guardrail_id") or ""), str(payload.get("status") or ""),
                 dumps(payload.get("live_capability_lock") or {}),
                 dumps(payload.get("broker_local_divergence") or {}),
                 dumps(payload.get("incident_control") or {}),
                 dumps(payload.get("incident_memory") or {}),
                 dumps(payload.get("release_rollback") or {}),
                 dumps(payload.get("p3_p4_evidence") or {}),
                 dumps(payload.get("action_recommendation") or {}),
                 dumps(payload.get("risk_precheck") or {}),
                 dumps(payload.get("boundary") or {}),
                 safe_float(payload.get("created_at")), safe_float(payload.get("updated_at"))))
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _row_to_guardrail(row: Any) -> dict[str, Any]:
        return {"guardrail_id": str(row["guardrail_id"] or ""), "schema_version": "brain_live_ready_guardrail.v1",
                "status": str(row["status"] or ""),
                "live_capability_lock": loads(row["live_capability_lock_json"], {}),
                "broker_local_divergence": loads(row["broker_local_divergence_json"], {}),
                "incident_control": loads(row["incident_control_json"], {}),
                "incident_memory": loads(row["incident_memory_json"], {}),
                "release_rollback": loads(row["release_rollback_json"], {}),
                "p3_p4_evidence": loads(row["p3_p4_evidence_json"], {}),
                "action_recommendation": loads(row["action_recommendation_json"], {}),
                "risk_precheck": loads(row["risk_precheck_json"], {}),
                "boundary": loads(row["boundary_json"], BrainLiveReadyGuardrailService.boundary()),
                "created_at": safe_float(row["created_at"]), "updated_at": safe_float(row["updated_at"])}

    @staticmethod
    def _missing_status(status: str) -> dict[str, Any]:
        return {"ok": False, "schema_version": "brain_live_ready_guardrail_list.v1", "status": status,
                "items": [], "boundary": BrainLiveReadyGuardrailService.boundary()}
