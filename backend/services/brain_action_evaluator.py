from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, state_table_columns, state_table_exists
from backend.services.brain_action_planner import (
    BrainActionPlannerService,
    _connect,
    _dumps,
    _execute,
    _loads,
    _safe_float,
    ensure_brain_action_plan_table,
)


def ensure_brain_action_plan_eval_table(db_path: str | Path = STATE_DB) -> None:
    ensure_brain_action_plan_table(db_path)
    conn = _connect(db_path)
    try:
        _execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS brain_action_plan_eval (
                eval_id TEXT PRIMARY KEY,
                plan_id TEXT NOT NULL,
                snapshot_id TEXT DEFAULT '',
                action_type TEXT DEFAULT '',
                scope_type TEXT DEFAULT '',
                status TEXT DEFAULT 'needs_evidence',
                comparison_verdict TEXT DEFAULT 'needs_more_evidence',
                coverage_score REAL NOT NULL DEFAULT 0.0,
                comparison_json TEXT NOT NULL DEFAULT '{}',
                evidence_refs_json TEXT NOT NULL DEFAULT '{}',
                boundary_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL DEFAULT 0.0
            )
            """,
        )
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_action_plan_eval_created ON brain_action_plan_eval(created_at)")
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_action_plan_eval_plan ON brain_action_plan_eval(plan_id, created_at)")
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_action_plan_eval_scope ON brain_action_plan_eval(scope_type, status, created_at)")
        conn.commit()
    finally:
        conn.close()


class BrainActionPlanEvaluatorService:
    """Compare V16 Phase 2 shadow plans with already-recorded posterior evidence."""

    REQUIRED_SOURCES = [
        "replay_report",
        "trade_outcome_review",
        "learning_application_effect",
        "position_supervisor_trace",
    ]

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = db_path

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {
            "phase": "v16_phase2_shadow_brain_eval",
            "read_only": True,
            "affects_trading": False,
            "record_only": True,
            "does_not_execute_action_plan": True,
            "does_not_mutate_runtime_overlay": True,
            "does_not_change_factor_weights": True,
            "does_not_switch_templates": True,
            "does_not_write_learning_samples": True,
            "comparison_sources_only": True,
        }

    def evaluate_latest_plans(self, *, limit: int = 20, persist: bool = True) -> dict[str, Any]:
        ensure_brain_action_plan_eval_table(self.db_path)
        limit = max(1, min(int(limit), 200))
        plan_result = BrainActionPlannerService(self.db_path).latest_plans(limit=limit)
        plans = list(plan_result.get("plans") or [])
        if not plans:
            return {
                "ok": False,
                "schema_version": "brain_action_plan_eval_run.v1",
                "status": "missing_action_plans",
                "evals": [],
                "read_only": True,
                "affects_trading": False,
                "boundary": self.boundary(),
            }
        now = time.time()
        evidence = self._load_evidence(limit=100)
        evals = [self._evaluate_plan(plan=plan, evidence=evidence, now=now) for plan in plans]
        if persist:
            self._persist(evals)
        return {
            "ok": True,
            "schema_version": "brain_action_plan_eval_run.v1",
            "status": "evaluated",
            "eval_count": len(evals),
            "evals": evals,
            "source_gaps": evidence.get("source_gaps", []),
            "read_only": True,
            "affects_trading": False,
            "boundary": self.boundary(),
            "created_at": now,
        }

    def latest_evals(self, *, limit: int = 20) -> dict[str, Any]:
        ensure_brain_action_plan_eval_table(self.db_path)
        limit = max(1, min(int(limit), 200))
        conn = _connect(self.db_path, read_only=True)
        try:
            rows = _execute(
                conn,
                """
                SELECT eval_id, plan_id, snapshot_id, action_type, scope_type,
                       status, comparison_verdict, coverage_score,
                       comparison_json, evidence_refs_json, boundary_json, created_at
                FROM brain_action_plan_eval
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return {
                "ok": bool(rows),
                "schema_version": "brain_action_plan_eval_list.v1",
                "status": "available" if rows else "missing_evals",
                "evals": [self._row_to_eval(row) for row in rows],
                "read_only": True,
                "affects_trading": False,
                "boundary": self.boundary(),
            }
        finally:
            conn.close()

    def status(self, *, limit: int = 20) -> dict[str, Any]:
        latest = self.latest_evals(limit=limit)
        evals = list(latest.get("evals") or [])
        if not evals:
            return {
                "ok": False,
                "schema_version": "brain_action_plan_eval_readiness.v1",
                "status": latest.get("status", "missing_evals"),
                "eval_count": 0,
                "read_only": True,
                "affects_trading": False,
            }
        return {
            "ok": True,
            "schema_version": "brain_action_plan_eval_readiness.v1",
            "status": "available",
            "eval_count": len(evals),
            "latest_created_at": max(_safe_float(item.get("created_at")) for item in evals),
            "coverage_avg": round(sum(_safe_float(item.get("coverage_score")) for item in evals) / len(evals), 6),
            "verdicts": sorted({str(item.get("comparison_verdict") or "") for item in evals}),
            "read_only": True,
            "affects_trading": False,
        }

    def _load_evidence(self, *, limit: int) -> dict[str, Any]:
        conn = _connect(self.db_path, read_only=True)
        source_gaps: list[str] = []
        try:
            replay = {}
            if state_table_exists(conn, "replay_report"):
                row = _execute(
                    conn,
                    """
                    SELECT replay_run_id, decision_count, matched_live_count,
                           mismatch_count, metric_summary_json, replay_error,
                           evidence_grade, artifact_hash, status, created_at
                    FROM replay_report
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                ).fetchone()
                replay = dict(row) if row else {}
            else:
                source_gaps.append("missing_replay_report")

            trade_reviews: list[dict[str, Any]] = []
            if state_table_exists(conn, "trade_outcome_review"):
                columns = state_table_columns(conn, "trade_outcome_review")
                select_columns = self._select_columns(
                    columns,
                    [
                        "review_id",
                        "trade_id",
                        "position_id",
                        "pnl",
                        "outcome_label",
                        "failure_tags_json",
                        "summary_text",
                        "created_at",
                    ],
                )
                order_column = "created_at" if "created_at" in columns else "review_id"
                rows = _execute(
                    conn,
                    f"""
                    SELECT {", ".join(select_columns)}
                    FROM trade_outcome_review
                    ORDER BY {order_column} DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                trade_reviews = [dict(row) for row in rows]
            else:
                source_gaps.append("missing_trade_outcome_review")

            learning_effects: list[dict[str, Any]] = []
            if state_table_exists(conn, "learning_application_effect"):
                columns = state_table_columns(conn, "learning_application_effect")
                select_columns = self._select_columns(
                    columns,
                    [
                        "application_id",
                        "scope_type",
                        "scope_key",
                        "action",
                        "status",
                        "observed_trade_count",
                        "baseline_trade_count",
                        "post_avg_reward",
                        "baseline_avg_reward",
                        "delta_avg_reward",
                        "post_win_rate",
                        "baseline_win_rate",
                        "decision_json",
                        "last_review_at",
                        "updated_at",
                        "created_at",
                    ],
                )
                if "updated_at" in columns:
                    order_expr = "updated_at DESC"
                elif "created_at" in columns:
                    order_expr = "created_at DESC"
                else:
                    order_expr = "application_id DESC"
                rows = _execute(
                    conn,
                    f"""
                    SELECT {", ".join(select_columns)}
                    FROM learning_application_effect
                    ORDER BY {order_expr}
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                learning_effects = [dict(row) for row in rows]
            else:
                source_gaps.append("missing_learning_application_effect")

            supervisor_traces: list[dict[str, Any]] = []
            if state_table_exists(conn, "position_supervisor_trace"):
                columns = state_table_columns(conn, "position_supervisor_trace")
                select_columns = self._select_columns(
                    columns,
                    [
                        "trace_id",
                        "decision_id",
                        "position_id",
                        "trade_id",
                        "action",
                        "outcome",
                        "risk_allowed",
                        "risk_reason",
                        "execution_status",
                        "trace_integrity",
                        "event_ts",
                        "created_at",
                    ],
                )
                if "event_ts" in columns:
                    order_expr = "event_ts DESC"
                elif "created_at" in columns:
                    order_expr = "created_at DESC"
                else:
                    order_expr = "trace_id DESC"
                rows = _execute(
                    conn,
                    f"""
                    SELECT {", ".join(select_columns)}
                    FROM position_supervisor_trace
                    ORDER BY {order_expr}
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                supervisor_traces = [dict(row) for row in rows]
            else:
                source_gaps.append("missing_position_supervisor_trace")

            return {
                "replay_report": replay,
                "trade_outcome_review": trade_reviews,
                "learning_application_effect": learning_effects,
                "position_supervisor_trace": supervisor_traces,
                "source_gaps": source_gaps,
            }
        finally:
            conn.close()

    @staticmethod
    def _select_columns(available: set[str], preferred: list[str]) -> list[str]:
        columns = [column for column in preferred if column in available]
        return columns or sorted(available)[:1]

    def _evaluate_plan(self, *, plan: dict[str, Any], evidence: dict[str, Any], now: float) -> dict[str, Any]:
        scope = dict(plan.get("scope") or {})
        scope_type = str(scope.get("scope_type") or "")
        replay = dict(evidence.get("replay_report") or {})
        trade_reviews = list(evidence.get("trade_outcome_review") or [])
        learning_effects = [item for item in list(evidence.get("learning_application_effect") or []) if self._matches_scope(scope_type, item)]
        supervisor_traces = list(evidence.get("position_supervisor_trace") or [])
        source_presence = {
            "replay_report": bool(replay.get("replay_run_id")),
            "trade_outcome_review": bool(trade_reviews),
            "learning_application_effect": bool(learning_effects),
            "position_supervisor_trace": bool(supervisor_traces),
        }
        coverage_score = round(sum(1 for present in source_presence.values() if present) / len(self.REQUIRED_SOURCES), 6)
        comparison = self._comparison_summary(
            replay=replay,
            trade_reviews=trade_reviews,
            learning_effects=learning_effects,
            supervisor_traces=supervisor_traces,
            source_presence=source_presence,
        )
        verdict = self._comparison_verdict(coverage_score=coverage_score, comparison=comparison)
        status = "comparable" if coverage_score >= 0.5 else "needs_evidence"
        return {
            "eval_id": f"bape_{uuid.uuid4().hex[:16]}",
            "schema_version": "brain_action_plan_eval.v1",
            "plan_id": str(plan.get("plan_id") or ""),
            "snapshot_id": str(plan.get("snapshot_id") or ""),
            "action_type": str(plan.get("action_type") or ""),
            "scope_type": scope_type,
            "status": status,
            "comparison_verdict": verdict,
            "coverage_score": coverage_score,
            "comparison": comparison,
            "evidence_refs": self._evidence_refs(replay, trade_reviews, learning_effects, supervisor_traces),
            "boundary": self.boundary(),
            "read_only": True,
            "affects_trading": False,
            "created_at": now,
        }

    @staticmethod
    def _matches_scope(scope_type: str, effect: dict[str, Any]) -> bool:
        effect_scope = str(effect.get("scope_type") or "")
        aliases = {
            "factor_weight": {"factor", "factor_weight", "alpha_weight_policy"},
            "parameter_template": {"parameter_template", "template", "online_light"},
            "context_policy": {"context", "context_policy", "threshold_and_sizing"},
            "supervisor_template": {"supervisor", "supervisor_template", "position_supervisor"},
        }
        allowed = aliases.get(scope_type, {scope_type})
        return effect_scope in allowed or str(effect.get("scope_key") or "") in allowed

    @staticmethod
    def _comparison_summary(
        *,
        replay: dict[str, Any],
        trade_reviews: list[dict[str, Any]],
        learning_effects: list[dict[str, Any]],
        supervisor_traces: list[dict[str, Any]],
        source_presence: dict[str, bool],
    ) -> dict[str, Any]:
        decision_count = _safe_float(replay.get("decision_count"))
        matched_count = _safe_float(replay.get("matched_live_count"))
        replay_agreement = matched_count / decision_count if decision_count > 0 else 0.0
        pnls = [_safe_float(item.get("pnl")) for item in trade_reviews]
        avg_pnl = sum(pnls) / len(pnls) if pnls else 0.0
        deltas = [_safe_float(item.get("delta_avg_reward")) for item in learning_effects]
        avg_delta = sum(deltas) / len(deltas) if deltas else 0.0
        risk_allowed_count = sum(1 for item in supervisor_traces if int(item.get("risk_allowed") or 0) == 1)
        supervisor_risk_coverage = risk_allowed_count / len(supervisor_traces) if supervisor_traces else 0.0
        outcomes: dict[str, int] = {}
        for item in trade_reviews:
            label = str(item.get("outcome_label") or "unknown")
            outcomes[label] = outcomes.get(label, 0) + 1
        return {
            "schema_version": "brain_action_plan_comparison.v1",
            "source_presence": source_presence,
            "replay": {
                "replay_run_id": replay.get("replay_run_id") or "",
                "status": replay.get("status") or "",
                "evidence_grade": replay.get("evidence_grade") or "",
                "decision_count": int(decision_count),
                "mismatch_count": int(_safe_float(replay.get("mismatch_count"))),
                "agreement": round(replay_agreement, 6),
                "has_error": bool(replay.get("replay_error")),
            },
            "trade_outcomes": {
                "review_count": len(trade_reviews),
                "avg_pnl": round(avg_pnl, 6),
                "outcomes": dict(sorted(outcomes.items())),
            },
            "learning_effects": {
                "effect_count": len(learning_effects),
                "avg_delta_reward": round(avg_delta, 6),
                "statuses": sorted({str(item.get("status") or "") for item in learning_effects}),
            },
            "supervisor": {
                "trace_count": len(supervisor_traces),
                "risk_allowed_coverage": round(supervisor_risk_coverage, 6),
                "integrity_issues": sum(1 for item in supervisor_traces if str(item.get("trace_integrity") or "full") != "full"),
            },
        }

    @staticmethod
    def _comparison_verdict(*, coverage_score: float, comparison: dict[str, Any]) -> str:
        if coverage_score < 0.5:
            return "needs_more_evidence"
        learning_delta = _safe_float((comparison.get("learning_effects") or {}).get("avg_delta_reward"))
        avg_pnl = _safe_float((comparison.get("trade_outcomes") or {}).get("avg_pnl"))
        replay_has_error = bool((comparison.get("replay") or {}).get("has_error"))
        if replay_has_error or learning_delta < -0.05 or avg_pnl < 0:
            return "caution"
        if learning_delta > 0.05 or avg_pnl > 0:
            return "supportive"
        return "inconclusive"

    @staticmethod
    def _evidence_refs(
        replay: dict[str, Any],
        trade_reviews: list[dict[str, Any]],
        learning_effects: list[dict[str, Any]],
        supervisor_traces: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "replay_report": replay.get("replay_run_id") or "",
            "trade_outcome_review": [item.get("review_id") for item in trade_reviews[:5]],
            "learning_application_effect": [item.get("application_id") for item in learning_effects[:5]],
            "position_supervisor_trace": [item.get("trace_id") for item in supervisor_traces[:5]],
        }

    def _persist(self, evals: list[dict[str, Any]]) -> None:
        ensure_brain_action_plan_eval_table(self.db_path)
        conn = _connect(self.db_path)
        try:
            for item in evals:
                _execute(
                    conn,
                    """
                    INSERT INTO brain_action_plan_eval
                    (eval_id, plan_id, snapshot_id, action_type, scope_type,
                     status, comparison_verdict, coverage_score, comparison_json,
                     evidence_refs_json, boundary_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["eval_id"],
                        item.get("plan_id", ""),
                        item.get("snapshot_id", ""),
                        item.get("action_type", ""),
                        item.get("scope_type", ""),
                        item.get("status", ""),
                        item.get("comparison_verdict", ""),
                        _safe_float(item.get("coverage_score")),
                        _dumps(item.get("comparison", {})),
                        _dumps(item.get("evidence_refs", {})),
                        _dumps(item.get("boundary", {})),
                        _safe_float(item.get("created_at")),
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _row_to_eval(row: Any) -> dict[str, Any]:
        return {
            "eval_id": str(row["eval_id"] or ""),
            "schema_version": "brain_action_plan_eval.v1",
            "plan_id": str(row["plan_id"] or ""),
            "snapshot_id": str(row["snapshot_id"] or ""),
            "action_type": str(row["action_type"] or ""),
            "scope_type": str(row["scope_type"] or ""),
            "status": str(row["status"] or ""),
            "comparison_verdict": str(row["comparison_verdict"] or ""),
            "coverage_score": _safe_float(row["coverage_score"]),
            "comparison": _loads(row["comparison_json"], {}),
            "evidence_refs": _loads(row["evidence_refs_json"], {}),
            "boundary": _loads(row["boundary_json"], BrainActionPlanEvaluatorService.boundary()),
            "read_only": True,
            "affects_trading": False,
            "created_at": _safe_float(row["created_at"]),
        }
