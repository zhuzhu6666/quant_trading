from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, state_table_exists
from backend.services.brain_action_evaluator import BrainActionPlanEvaluatorService, ensure_brain_action_plan_eval_table
from backend.services.brain_action_planner import _connect, _dumps, _execute, _loads, _safe_float
from backend.services.replay_harness import ReplayHarnessService
from risk.policy_service import RiskPolicyService


def ensure_brain_low_impact_execution_table(db_path: str | Path = STATE_DB) -> None:
    ensure_brain_action_plan_eval_table(db_path)
    conn = _connect(db_path)
    try:
        _execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS brain_low_impact_execution (
                execution_id TEXT PRIMARY KEY,
                plan_id TEXT DEFAULT '',
                eval_id TEXT DEFAULT '',
                action_type TEXT DEFAULT '',
                execution_action TEXT DEFAULT '',
                status TEXT DEFAULT '',
                evidence_score REAL NOT NULL DEFAULT 0.0,
                critic_verdict TEXT DEFAULT '',
                comparison_verdict TEXT DEFAULT '',
                risk_verdict_json TEXT NOT NULL DEFAULT '{}',
                rollback_plan_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT NOT NULL DEFAULT '{}',
                posterior_monitor_json TEXT NOT NULL DEFAULT '{}',
                boundary_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL DEFAULT 0.0,
                updated_at REAL NOT NULL DEFAULT 0.0
            )
            """,
        )
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_low_impact_execution_created ON brain_low_impact_execution(created_at)")
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_low_impact_execution_plan ON brain_low_impact_execution(plan_id, eval_id)")
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_low_impact_execution_status ON brain_low_impact_execution(status, created_at)")
        conn.commit()
    finally:
        conn.close()


class BrainLowImpactExecutorService:
    """V16 Phase 3 low-impact autonomous executor.

    The executor only runs explicitly whitelisted low-impact actions. The first
    action is a read-only replay job; optional downgrade uses the existing
    incident-control/RiskPolicy path and only tightens autonomy scope.
    """

    ALLOWED_ACTIONS = {"run_replay_job"}

    def __init__(
        self,
        db_path: str | Path = STATE_DB,
        *,
        replay_artifact_dir: str | Path | None = None,
    ):
        self.db_path = db_path
        self.replay_artifact_dir = replay_artifact_dir

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {
            "phase": "v16_phase3_low_impact_autonomous_brain",
            "low_impact_only": True,
            "allowed_actions": sorted(BrainLowImpactExecutorService.ALLOWED_ACTIONS),
            "does_not_change_factor_weights": True,
            "does_not_switch_templates": True,
            "does_not_submit_orders": True,
            "does_not_write_learning_samples": True,
            "replay_job_is_read_only": True,
            "autonomy_tighten_uses_incident_control": True,
            "risk_policy_service_required": True,
            "rollback_or_downgrade_required_for_bad_posterior": True,
        }

    def execute_latest(
        self,
        *,
        limit: int = 1,
        allow_tighten: bool = False,
        replay_lookback_days: float = 1.0,
        replay_limit: int = 100,
        persist: bool = True,
    ) -> dict[str, Any]:
        ensure_brain_low_impact_execution_table(self.db_path)
        limit = max(1, min(int(limit), 20))
        latest = BrainActionPlanEvaluatorService(self.db_path).latest_evals(limit=limit)
        evals = list(latest.get("evals") or [])
        if not evals:
            return {
                "ok": False,
                "schema_version": "brain_low_impact_execution_run.v1",
                "status": "missing_action_plan_evals",
                "executions": [],
                "boundary": self.boundary(),
            }
        executions = [
            self._execute_eval(
                item,
                allow_tighten=allow_tighten,
                replay_lookback_days=replay_lookback_days,
                replay_limit=replay_limit,
            )
            for item in evals[:limit]
        ]
        if persist:
            self._persist(executions)
        return {
            "ok": any(item.get("status") in {"executed", "executed_and_downgraded"} for item in executions),
            "schema_version": "brain_low_impact_execution_run.v1",
            "status": "executed" if executions else "empty",
            "execution_count": len(executions),
            "executions": executions,
            "boundary": self.boundary(),
            "created_at": time.time(),
        }

    def latest_executions(self, *, limit: int = 20) -> dict[str, Any]:
        ensure_brain_low_impact_execution_table(self.db_path)
        limit = max(1, min(int(limit), 200))
        conn = _connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, "brain_low_impact_execution"):
                return self._missing_status("missing_table")
            rows = _execute(
                conn,
                """
                SELECT execution_id, plan_id, eval_id, action_type, execution_action,
                       status, evidence_score, critic_verdict, comparison_verdict,
                       risk_verdict_json, rollback_plan_json, result_json,
                       posterior_monitor_json, boundary_json, created_at, updated_at
                FROM brain_low_impact_execution
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return {
                "ok": bool(rows),
                "schema_version": "brain_low_impact_execution_list.v1",
                "status": "available" if rows else "missing_executions",
                "executions": [self._row_to_execution(row) for row in rows],
                "boundary": self.boundary(),
            }
        finally:
            conn.close()

    def status(self, *, limit: int = 20) -> dict[str, Any]:
        latest = self.latest_executions(limit=limit)
        executions = list(latest.get("executions") or [])
        if not executions:
            return {
                "ok": False,
                "schema_version": "brain_low_impact_execution_readiness.v1",
                "status": latest.get("status", "missing_executions"),
                "execution_count": 0,
                "low_impact_only": True,
            }
        return {
            "ok": True,
            "schema_version": "brain_low_impact_execution_readiness.v1",
            "status": "available",
            "execution_count": len(executions),
            "latest_created_at": max(_safe_float(item.get("created_at")) for item in executions),
            "statuses": sorted({str(item.get("status") or "") for item in executions}),
            "low_impact_only": True,
        }

    def _execute_eval(
        self,
        evaluation: dict[str, Any],
        *,
        allow_tighten: bool,
        replay_lookback_days: float,
        replay_limit: int,
    ) -> dict[str, Any]:
        now = time.time()
        plan = self._load_plan(str(evaluation.get("plan_id") or ""))
        critic_verdict = str(plan.get("critic_verdict") or "")
        evidence_score = _safe_float(evaluation.get("coverage_score"))
        comparison_verdict = str(evaluation.get("comparison_verdict") or "")
        execution_action = "run_replay_job"
        rollback_plan = self._rollback_plan(allow_tighten=allow_tighten)
        risk_verdict = RiskPolicyService.shared().evaluate(
            execution_action,
            {
                "plan_id": evaluation.get("plan_id", ""),
                "eval_id": evaluation.get("eval_id", ""),
                "evidence_score": evidence_score,
                "critic_verdict": critic_verdict,
                "comparison_verdict": comparison_verdict,
                "mutates_runtime": False,
            },
        ).to_dict()
        status = "blocked_by_risk"
        result: dict[str, Any] = {}
        posterior_monitor = {
            "schema_version": "brain_low_impact_posterior_monitor.v1",
            "comparison_verdict": comparison_verdict,
            "bad_posterior": comparison_verdict == "caution",
            "allow_tighten": bool(allow_tighten),
            "downgrade": {"status": "not_required"},
        }
        if critic_verdict == "reject":
            status = "blocked_by_critic"
        elif not bool(risk_verdict.get("allowed")):
            status = "blocked_by_risk"
        else:
            replay = self._run_replay_job(replay_lookback_days=replay_lookback_days, replay_limit=replay_limit)
            result = {
                "schema_version": "brain_low_impact_replay_result.v1",
                "replay_run_id": replay.get("replay_run_id", ""),
                "replay_error": replay.get("replay_error", ""),
                "decision_count": replay.get("decision_count", 0),
                "evidence_grade": replay.get("evidence_grade", ""),
                "artifact_hash": replay.get("artifact_hash", ""),
            }
            bad_posterior = bool(replay.get("replay_error")) or comparison_verdict == "caution"
            posterior_monitor["bad_posterior"] = bad_posterior
            if bad_posterior and allow_tighten:
                downgrade = self._tighten_to_shadow_only(reason=f"v16_phase3:{evaluation.get('eval_id') or ''}")
                posterior_monitor["downgrade"] = downgrade
                status = "executed_and_downgraded" if downgrade.get("ok") else "executed_downgrade_blocked"
            else:
                posterior_monitor["downgrade"] = {
                    "status": "pending_operator_or_future_cycle" if bad_posterior else "not_required",
                    "reason": "bad_posterior_without_allow_tighten" if bad_posterior else "",
                }
                status = "executed"
        return {
            "execution_id": f"brain_p3_exec_{uuid.uuid4().hex[:16]}",
            "schema_version": "brain_low_impact_execution.v1",
            "plan_id": str(evaluation.get("plan_id") or ""),
            "eval_id": str(evaluation.get("eval_id") or ""),
            "action_type": str(evaluation.get("action_type") or ""),
            "execution_action": execution_action,
            "status": status,
            "evidence_score": evidence_score,
            "critic_verdict": critic_verdict,
            "comparison_verdict": comparison_verdict,
            "risk_verdict": risk_verdict,
            "rollback_plan": rollback_plan,
            "result": result,
            "posterior_monitor": posterior_monitor,
            "boundary": self.boundary(),
            "created_at": now,
            "updated_at": time.time(),
        }

    def _run_replay_job(self, *, replay_lookback_days: float, replay_limit: int) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if self.replay_artifact_dir is not None:
            kwargs["artifact_dir"] = self.replay_artifact_dir
        return ReplayHarnessService(self.db_path, **kwargs).run_factor_gate_risk_replay(
            lookback_days=max(0.0, min(float(replay_lookback_days), 7.0)),
            limit=max(1, min(int(replay_limit), 500)),
            replay_run_id=f"brain_p3_replay_{uuid.uuid4().hex[:12]}",
        )

    def _tighten_to_shadow_only(self, *, reason: str) -> dict[str, Any]:
        from backend.services.incident_controls import RuntimeIncidentControlService

        return RuntimeIncidentControlService(self.db_path).set_mode(
            "shadow_only",
            reason=reason,
            actor="system:v16_brain_low_impact_executor",
            confirm_thaw=False,
        )

    @staticmethod
    def _rollback_plan(*, allow_tighten: bool) -> dict[str, Any]:
        return {
            "schema_version": "brain_low_impact_rollback_plan.v1",
            "runtime_mutation": False,
            "primary_action": "run_replay_job",
            "rollback_required": False,
            "bad_posterior_action": "tighten_to_shadow_only" if allow_tighten else "record_pending_downgrade",
            "uses_risk_policy_for_tighten": True,
        }

    def _load_plan(self, plan_id: str) -> dict[str, Any]:
        if not plan_id:
            return {}
        conn = _connect(self.db_path, read_only=True)
        try:
            row = _execute(
                conn,
                """
                SELECT plan_id, critic_verdict, scope_json, boundary_json
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
                "boundary": _loads(row["boundary_json"], {}),
            }
        finally:
            conn.close()

    def _persist(self, executions: list[dict[str, Any]]) -> None:
        ensure_brain_low_impact_execution_table(self.db_path)
        conn = _connect(self.db_path)
        try:
            for item in executions:
                _execute(
                    conn,
                    """
                    INSERT INTO brain_low_impact_execution
                    (execution_id, plan_id, eval_id, action_type, execution_action,
                     status, evidence_score, critic_verdict, comparison_verdict,
                     risk_verdict_json, rollback_plan_json, result_json,
                     posterior_monitor_json, boundary_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["execution_id"],
                        item.get("plan_id", ""),
                        item.get("eval_id", ""),
                        item.get("action_type", ""),
                        item.get("execution_action", ""),
                        item.get("status", ""),
                        _safe_float(item.get("evidence_score")),
                        item.get("critic_verdict", ""),
                        item.get("comparison_verdict", ""),
                        _dumps(item.get("risk_verdict", {})),
                        _dumps(item.get("rollback_plan", {})),
                        _dumps(item.get("result", {})),
                        _dumps(item.get("posterior_monitor", {})),
                        _dumps(item.get("boundary", {})),
                        _safe_float(item.get("created_at")),
                        _safe_float(item.get("updated_at")),
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _row_to_execution(row: Any) -> dict[str, Any]:
        return {
            "execution_id": str(row["execution_id"] or ""),
            "schema_version": "brain_low_impact_execution.v1",
            "plan_id": str(row["plan_id"] or ""),
            "eval_id": str(row["eval_id"] or ""),
            "action_type": str(row["action_type"] or ""),
            "execution_action": str(row["execution_action"] or ""),
            "status": str(row["status"] or ""),
            "evidence_score": _safe_float(row["evidence_score"]),
            "critic_verdict": str(row["critic_verdict"] or ""),
            "comparison_verdict": str(row["comparison_verdict"] or ""),
            "risk_verdict": _loads(row["risk_verdict_json"], {}),
            "rollback_plan": _loads(row["rollback_plan_json"], {}),
            "result": _loads(row["result_json"], {}),
            "posterior_monitor": _loads(row["posterior_monitor_json"], {}),
            "boundary": _loads(row["boundary_json"], BrainLowImpactExecutorService.boundary()),
            "created_at": _safe_float(row["created_at"]),
            "updated_at": _safe_float(row["updated_at"]),
        }

    @staticmethod
    def _missing_status(status: str) -> dict[str, Any]:
        return {
            "ok": False,
            "schema_version": "brain_low_impact_execution_list.v1",
            "status": status,
            "executions": [],
            "boundary": BrainLowImpactExecutorService.boundary(),
        }
