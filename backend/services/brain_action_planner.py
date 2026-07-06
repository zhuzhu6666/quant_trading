from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from backend.core.db import (
    STATE_DB,
    connect_sqlite,
    get_state_pg_conn,
    is_state_db_path,
    state_table_exists,
)


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


def ensure_brain_action_plan_table(db_path: str | Path = STATE_DB) -> None:
    conn = _connect(db_path)
    try:
        _execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS brain_action_plan (
                plan_id TEXT PRIMARY KEY,
                snapshot_id TEXT DEFAULT '',
                hypothesis_id TEXT DEFAULT '',
                action_type TEXT DEFAULT '',
                status TEXT DEFAULT 'shadow_recorded',
                scope_json TEXT NOT NULL DEFAULT '{}',
                max_impact TEXT DEFAULT 'none_shadow_only',
                risk_class TEXT DEFAULT '',
                critic_verdict TEXT DEFAULT '',
                validation_refs_json TEXT NOT NULL DEFAULT '{}',
                rollback_plan_json TEXT NOT NULL DEFAULT '{}',
                required_services_json TEXT NOT NULL DEFAULT '[]',
                shadow_eval_json TEXT NOT NULL DEFAULT '{}',
                boundary_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL DEFAULT 0.0
            )
            """,
        )
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_action_plan_created ON brain_action_plan(created_at)")
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_action_plan_snapshot ON brain_action_plan(snapshot_id, created_at)")
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_action_plan_type ON brain_action_plan(action_type, status)")
        conn.commit()
    finally:
        conn.close()


class BrainActionPlannerService:
    """V16 Phase 2 shadow action-plan ledger.

    The planner converts read-only brain hypotheses into shadow-only action
    plans. It does not execute actions, call RuntimeConfig mutation paths, write
    factor weights, alter templates, submit orders, or create learning labels.
    """

    ACTIONS = [
        {
            "action_type": "shadow_factor_weight_review",
            "scope_type": "factor_weight",
            "scope_key": "alpha_weight_policy",
            "required_services": ["ReplayHarnessService", "RiskPolicyService", "DecisionPolicy"],
            "candidate_change": "compare candidate downweight/hold in replay before any future write",
        },
        {
            "action_type": "shadow_parameter_template_review",
            "scope_type": "parameter_template",
            "scope_key": "online_light",
            "required_services": ["ReplayHarnessService", "RiskPolicyService", "ParameterTemplateService"],
            "candidate_change": "compare online_light template candidates in shadow only",
        },
        {
            "action_type": "shadow_context_policy_review",
            "scope_type": "context_policy",
            "scope_key": "threshold_and_sizing",
            "required_services": ["ReplayHarnessService", "RiskPolicyService", "ContextPolicyService"],
            "candidate_change": "compare threshold/sizing posture without mutating runtime config",
        },
        {
            "action_type": "shadow_supervisor_template_review",
            "scope_type": "supervisor_template",
            "scope_key": "position_supervisor",
            "required_services": ["ReplayHarnessService", "RiskPolicyService", "PositionSupervisor"],
            "candidate_change": "compare supervisor hold/tighten/reduce/close template outcomes in shadow",
        },
    ]

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = db_path

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {
            "phase": "v16_phase2_shadow_brain",
            "read_only": True,
            "affects_trading": False,
            "shadow_only": True,
            "does_not_execute_action_plan": True,
            "does_not_mutate_runtime_overlay": True,
            "does_not_change_factor_weights": True,
            "does_not_switch_templates": True,
            "does_not_write_learning_samples": True,
            "future_execution_requires_risk_policy": True,
            "future_weight_writes_require_decision_policy": True,
        }

    def build_plans(
        self,
        *,
        brain_state: dict[str, Any],
        persist: bool = True,
        source: str = "brain_action_planner",
    ) -> dict[str, Any]:
        now = time.time()
        snapshot_id = str(brain_state.get("snapshot_id") or "")
        hypotheses = list(brain_state.get("hypotheses") or [])
        critic = dict(brain_state.get("critic") or {})
        world_model = dict(brain_state.get("world_model") or {})
        memory = dict(brain_state.get("memory") or {})
        plans = [
            self._plan_for_action(
                action=action,
                snapshot_id=snapshot_id,
                hypotheses=hypotheses,
                critic=critic,
                world_model=world_model,
                memory=memory,
                now=now,
                source=source,
            )
            for action in self.ACTIONS
        ]
        if persist:
            self._persist(plans)
        return {
            "ok": True,
            "schema_version": "brain_action_plan_run.v1",
            "phase": "v16_phase2_shadow_brain",
            "snapshot_id": snapshot_id,
            "plan_count": len(plans),
            "plans": plans,
            "boundary": self.boundary(),
            "read_only": True,
            "affects_trading": False,
            "created_at": now,
        }

    def latest_plans(self, *, limit: int = 20) -> dict[str, Any]:
        ensure_brain_action_plan_table(self.db_path)
        limit = max(1, min(int(limit), 200))
        conn = _connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, "brain_action_plan"):
                return self._missing_status("missing_table")
            rows = _execute(
                conn,
                """
                SELECT plan_id, snapshot_id, hypothesis_id, action_type, status,
                       scope_json, max_impact, risk_class, critic_verdict,
                       validation_refs_json, rollback_plan_json, required_services_json,
                       shadow_eval_json, boundary_json, created_at
                FROM brain_action_plan
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return {
                "ok": True,
                "schema_version": "brain_action_plan_list.v1",
                "status": "available",
                "plans": [self._row_to_plan(row) for row in rows],
                "read_only": True,
                "affects_trading": False,
                "boundary": self.boundary(),
            }
        finally:
            conn.close()

    def status(self, *, limit: int = 20) -> dict[str, Any]:
        latest = self.latest_plans(limit=limit)
        plans = list(latest.get("plans") or [])
        if not plans:
            return {
                "ok": False,
                "schema_version": "brain_action_plan_readiness.v1",
                "status": latest.get("status", "missing_plans"),
                "plan_count": 0,
                "read_only": True,
                "affects_trading": False,
            }
        return {
            "ok": True,
            "schema_version": "brain_action_plan_readiness.v1",
            "status": "available",
            "plan_count": len(plans),
            "latest_created_at": max(_safe_float(plan.get("created_at")) for plan in plans),
            "critic_verdicts": sorted({str(plan.get("critic_verdict") or "") for plan in plans}),
            "action_types": sorted({str(plan.get("action_type") or "") for plan in plans}),
            "read_only": True,
            "affects_trading": False,
        }

    def _plan_for_action(
        self,
        *,
        action: dict[str, Any],
        snapshot_id: str,
        hypotheses: list[dict[str, Any]],
        critic: dict[str, Any],
        world_model: dict[str, Any],
        memory: dict[str, Any],
        now: float,
        source: str,
    ) -> dict[str, Any]:
        hypothesis = self._best_hypothesis(action["scope_type"], hypotheses)
        risk_class = self._risk_class(action["scope_type"], hypothesis, world_model)
        critic_verdict = self._critic_verdict(risk_class, critic, memory, world_model)
        status = "shadow_recorded" if critic_verdict in {"pass", "caution"} else "critic_rejected"
        validation_refs = self._validation_refs(snapshot_id, hypothesis, memory)
        plan = {
            "plan_id": f"bap_{uuid.uuid4().hex[:16]}",
            "schema_version": "brain_action_plan.v1",
            "snapshot_id": snapshot_id,
            "hypothesis_id": str(hypothesis.get("hypothesis_id") or ""),
            "action_type": action["action_type"],
            "status": status,
            "scope": {
                "scope_type": action["scope_type"],
                "scope_key": action["scope_key"],
                "candidate_change": action["candidate_change"],
                "source": source,
                "world_model": {
                    "strategy_posture": world_model.get("strategy_posture"),
                    "factor_posture": world_model.get("factor_posture"),
                    "learning_posture": world_model.get("learning_posture"),
                    "execution_posture": world_model.get("execution_posture"),
                },
            },
            "max_impact": "none_shadow_only",
            "risk_class": risk_class,
            "critic_verdict": critic_verdict,
            "validation_refs": validation_refs,
            "rollback_plan": {
                "required": False,
                "reason": "shadow_plan_does_not_mutate_runtime_state",
                "future_if_executed": {
                    "requires_runtime_config_snapshot": True,
                    "requires_rollback_json": True,
                    "requires_risk_policy_verdict": True,
                },
            },
            "required_services": list(action["required_services"]),
            "shadow_eval": {
                "schema_version": "brain_shadow_action_eval_contract.v1",
                "record_only": True,
                "compare_to_sources": [
                    "replay_report",
                    "trade_outcome_review",
                    "learning_application_effect",
                    "position_supervisor_trace",
                ],
                "success_metric": "post_action_reward_delta_or_replay_agreement",
                "minimum_observation": {
                    "replay_required_before_execution": True,
                    "live_observed_trade_count_before_governance": 3,
                },
            },
            "boundary": self.boundary(),
            "read_only": True,
            "affects_trading": False,
            "created_at": now,
        }
        return plan

    @staticmethod
    def _best_hypothesis(scope_type: str, hypotheses: list[dict[str, Any]]) -> dict[str, Any]:
        scope_aliases = {
            "factor_weight": {"factor", "simulation", "runtime"},
            "parameter_template": {"simulation", "autonomy", "runtime"},
            "context_policy": {"autonomy", "incident", "runtime", "simulation"},
            "supervisor_template": {"incident", "runtime", "simulation"},
        }
        aliases = scope_aliases.get(scope_type, {scope_type})
        candidates = [item for item in hypotheses if str(item.get("scope") or "") in aliases]
        if not candidates:
            candidates = hypotheses
        if not candidates:
            return {}
        return max(candidates, key=lambda item: _safe_float(item.get("evidence_score")))

    @staticmethod
    def _risk_class(scope_type: str, hypothesis: dict[str, Any], world_model: dict[str, Any]) -> str:
        if str(world_model.get("strategy_posture") or "") in {"no_new_risk", "observation_only"}:
            return "high"
        if str(hypothesis.get("risk_class") or "") in {"high", "medium", "low"}:
            return str(hypothesis.get("risk_class"))
        if scope_type in {"factor_weight", "parameter_template"}:
            return "medium"
        return "low"

    @staticmethod
    def _critic_verdict(
        risk_class: str,
        critic: dict[str, Any],
        memory: dict[str, Any],
        world_model: dict[str, Any],
    ) -> str:
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
        memory_items = [
            {
                "memory_id": item.get("memory_id"),
                "source_table": item.get("source_table"),
                "source_id": item.get("source_id"),
                "polarity": item.get("polarity"),
            }
            for item in list(memory.get("negative_matches") or [])[:3] + list(memory.get("counter_evidence") or [])[:3]
        ]
        return {
            "brain_snapshot_id": snapshot_id,
            "hypothesis_id": str(hypothesis.get("hypothesis_id") or ""),
            "hypothesis_evidence_refs": hypothesis.get("evidence_refs") or {},
            "hypothesis_counter_evidence_refs": hypothesis.get("counter_evidence_refs") or {},
            "memory_refs": memory_items,
            "requires_replay_before_execution": True,
        }

    def _persist(self, plans: list[dict[str, Any]]) -> None:
        ensure_brain_action_plan_table(self.db_path)
        conn = _connect(self.db_path)
        try:
            for plan in plans:
                _execute(
                    conn,
                    """
                    INSERT INTO brain_action_plan
                    (plan_id, snapshot_id, hypothesis_id, action_type, status,
                     scope_json, max_impact, risk_class, critic_verdict,
                     validation_refs_json, rollback_plan_json, required_services_json,
                     shadow_eval_json, boundary_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        plan["plan_id"],
                        plan.get("snapshot_id", ""),
                        plan.get("hypothesis_id", ""),
                        plan.get("action_type", ""),
                        plan.get("status", ""),
                        _dumps(plan.get("scope", {})),
                        plan.get("max_impact", ""),
                        plan.get("risk_class", ""),
                        plan.get("critic_verdict", ""),
                        _dumps(plan.get("validation_refs", {})),
                        _dumps(plan.get("rollback_plan", {})),
                        _dumps(plan.get("required_services", [])),
                        _dumps(plan.get("shadow_eval", {})),
                        _dumps(plan.get("boundary", {})),
                        _safe_float(plan.get("created_at")),
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _row_to_plan(row: Any) -> dict[str, Any]:
        return {
            "plan_id": str(row["plan_id"] or ""),
            "schema_version": "brain_action_plan.v1",
            "snapshot_id": str(row["snapshot_id"] or ""),
            "hypothesis_id": str(row["hypothesis_id"] or ""),
            "action_type": str(row["action_type"] or ""),
            "status": str(row["status"] or ""),
            "scope": _loads(row["scope_json"], {}),
            "max_impact": str(row["max_impact"] or ""),
            "risk_class": str(row["risk_class"] or ""),
            "critic_verdict": str(row["critic_verdict"] or ""),
            "validation_refs": _loads(row["validation_refs_json"], {}),
            "rollback_plan": _loads(row["rollback_plan_json"], {}),
            "required_services": _loads(row["required_services_json"], []),
            "shadow_eval": _loads(row["shadow_eval_json"], {}),
            "boundary": _loads(row["boundary_json"], BrainActionPlannerService.boundary()),
            "read_only": True,
            "affects_trading": False,
            "created_at": _safe_float(row["created_at"]),
        }

    @staticmethod
    def _missing_status(status: str) -> dict[str, Any]:
        return {
            "ok": False,
            "schema_version": "brain_action_plan_list.v1",
            "status": status,
            "plans": [],
            "read_only": True,
            "affects_trading": False,
            "boundary": BrainActionPlannerService.boundary(),
        }
