from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, connect_sqlite, get_state_pg_conn, is_state_db_path, state_table_exists
from backend.services.factor_counter_evidence import FactorCounterEvidenceService
from backend.services.learning_application_store import LearningApplicationStore

from backend.core.db_helpers import (
    load_json as _loads,
    conn_is_pg as _conn_is_pg,
    pg_sql as _sql,
    execute as _execute,
)



def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _use_pg(db_path: str | Path = STATE_DB) -> bool:
    return is_state_db_path(db_path)


def _connect(db_path: str | Path = STATE_DB, *, read_only: bool = False):
    conn = get_state_pg_conn(read_only=read_only) if _use_pg(db_path) else connect_sqlite(db_path, read_only=read_only)
    if not _use_pg(db_path):
        conn.row_factory = __import__("sqlite3").Row
    return conn


class FactorGovernanceEffectTrackerService:
    """Read pruning governance outcomes from existing application-effect facts."""

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = db_path

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {
            "schema_version": "factor_governance_effect_tracker_boundary.v1",
            "read_status_is_read_only": True,
            "reconcile_uses_existing_governor": True,
            "does_not_apply_factor_weights": True,
            "does_not_submit_orders": True,
            "does_not_create_new_tables": True,
            "rollback_source": "RuleEvolutionGovernor.reconcile_application_effects",
        }

    def status(self, *, limit: int = 50) -> dict[str, Any]:
        limit = max(1, min(int(limit or 50), 200))
        conn = _connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, "policy_suggestion"):
                return self._empty("missing_policy_suggestion")
            rows = _execute(
                conn,
                """
                SELECT suggestion_id, scope_type, scope_key, action, confidence,
                       reason, evidence_json, status, reviewed_at, review_note, created_at
                FROM policy_suggestion
                WHERE scope_type='factor'
                  AND action='downweight'
                  AND evidence_json LIKE '%factor_pruning_governance%'
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            items = [self._item_from_suggestion(conn, row) for row in rows]
        finally:
            conn.close()
        summary: dict[str, int] = {}
        effect_statuses: dict[str, int] = {}
        recommendations: dict[str, int] = {}
        for item in items:
            summary[str(item.get("stage") or "unknown")] = summary.get(str(item.get("stage") or "unknown"), 0) + 1
            effect = dict(item.get("effect") or {})
            effect_status = str(effect.get("status") or "missing")
            effect_statuses[effect_status] = effect_statuses.get(effect_status, 0) + 1
            rec = str(item.get("recommended_action") or "watch")
            recommendations[rec] = recommendations.get(rec, 0) + 1
        return {
            "ok": True,
            "schema_version": "factor_governance_effect_tracker.v1",
            "status": "available" if items else "missing_pruning_effects",
            "item_count": len(items),
            "summary": dict(sorted(summary.items())),
            "effect_statuses": dict(sorted(effect_statuses.items())),
            "recommendations": dict(sorted(recommendations.items())),
            "items": items,
            "boundary": self.boundary(),
        }

    def reconcile(self, *, limit: int = 50) -> dict[str, Any]:
        from research.learning.governor import RuleEvolutionGovernor

        governor_result = RuleEvolutionGovernor(str(self.db_path)).reconcile_application_effects(
            application_limit=max(1, min(int(limit or 50), 200)),
        )
        status = self.status(limit=limit)
        return {
            "ok": True,
            "schema_version": "factor_governance_effect_reconcile.v1",
            "governor_result": governor_result,
            "effect_status": status,
            "boundary": self.boundary(),
        }

    def _item_from_suggestion(self, conn: Any, row: Any) -> dict[str, Any]:
        suggestion_id = str(row["suggestion_id"] or "")
        factor = str(row["scope_key"] or "")
        evidence = _loads(row["evidence_json"], {})
        application = self._application_for_suggestion(conn, suggestion_id)
        effect = self._effect_for_application(conn, str(application.get("application_id") or ""))
        counter = FactorCounterEvidenceService(self.db_path).build_for_factor(factor)
        stage, recommended_action = self._classify(
            suggestion_status=str(row["status"] or ""),
            application=application,
            effect=effect,
            counter=counter,
        )
        return {
            "suggestion_id": suggestion_id,
            "schema_version": "factor_governance_effect_item.v1",
            "factor": factor,
            "action": str(row["action"] or ""),
            "suggestion_status": str(row["status"] or ""),
            "confidence": _safe_float(row["confidence"]),
            "stage": stage,
            "recommended_action": recommended_action,
            "reason": str(row["reason"] or ""),
            "review_note": str(row["review_note"] or ""),
            "created_at": _safe_float(row["created_at"]),
            "application": application,
            "effect": effect,
            "counter_evidence": {
                "recommended_stage": counter.get("recommended_stage", ""),
                "keep_score": counter.get("keep_score", 0.0),
                "prune_score": counter.get("prune_score", 0.0),
                "regime_exception": counter.get("regime_exception", {}),
            },
            "evidence_contract": {
                "source_agent": evidence.get("source_agent", ""),
                "source_kind": evidence.get("source_kind", ""),
                "has_risk_verdict": bool(evidence.get("risk_verdict")),
                "has_decision_policy_preview": bool(evidence.get("decision_policy_preview")),
                "has_rollback_plan": bool(evidence.get("rollback_plan")),
            },
        }

    def _application_for_suggestion(self, conn: Any, suggestion_id: str) -> dict[str, Any]:
        if not suggestion_id or not state_table_exists(conn, "learning_application_log"):
            return {}
        store = LearningApplicationStore(str(self.db_path))
        best: dict[str, Any] | None = None
        best_key: tuple[float, float] | None = None
        for app in store.iter_applications():
            ids = [str(item) for item in (app.get("suggestion_ids") or [])]
            if suggestion_id not in ids:
                continue
            created = _safe_float(app.get("created_at"))
            key = (created, created)
            if best_key is None or key > best_key:
                best_key = key
                best = app
        if best is None:
            return {}
        return {
            "application_id": str(best.get("application_id") or ""),
            "cycle_ts": _safe_float(best.get("created_at")),
            "scope_type": str(best.get("scope_type") or ""),
            "scope_key": str(best.get("scope_key") or ""),
            "action": str(best.get("action") or ""),
            "bias_multiplier": _safe_float(best.get("bias_multiplier"), 1.0),
            "old_weight": _safe_float(best.get("old_weight")),
            "new_weight": _safe_float(best.get("new_weight")),
            "status": str(best.get("status") or ""),
            "details": dict(best.get("details") or {}),
            "created_at": _safe_float(best.get("created_at")),
        }

    def _effect_for_application(self, conn: Any, application_id: str) -> dict[str, Any]:
        if not application_id or not state_table_exists(conn, "learning_application_effect"):
            return {}
        store = LearningApplicationStore(str(self.db_path))
        for eff in store.iter_effects():
            if str(eff.get("application_id") or "") != str(application_id or ""):
                continue
            return {
                "application_id": str(eff.get("application_id") or ""),
                "scope_type": str(eff.get("scope_type") or ""),
                "scope_key": str(eff.get("scope_key") or ""),
                "action": str(eff.get("action") or ""),
                "status": str(eff.get("status") or ""),
                "observed_trade_count": int(_safe_float(eff.get("observed_trade_count"))),
                "baseline_trade_count": int(_safe_float(eff.get("baseline_trade_count"))),
                "post_avg_reward": _safe_float(eff.get("post_avg_reward")),
                "baseline_avg_reward": _safe_float(eff.get("baseline_avg_reward")),
                "delta_avg_reward": _safe_float(eff.get("delta_avg_reward")),
                "post_win_rate": _safe_float(eff.get("post_win_rate")),
                "baseline_win_rate": _safe_float(eff.get("baseline_win_rate")),
                "decision": dict(eff.get("decision") or {}),
                "last_review_at": _safe_float(eff.get("last_review_at")),
                "updated_at": _safe_float(eff.get("updated_at")),
                "created_at": _safe_float(eff.get("created_at")),
            }
        return {}

    @staticmethod
    def _classify(
        *,
        suggestion_status: str,
        application: dict[str, Any],
        effect: dict[str, Any],
        counter: dict[str, Any],
    ) -> tuple[str, str]:
        if suggestion_status == "rolled_back":
            return "rolled_back", "watch_after_rollback"
        if not application:
            return "approved_waiting_application", "wait_for_weight_sync"
        effect_status = str(effect.get("status") or "")
        if not effect or effect_status == "observing":
            if str(counter.get("recommended_stage") or "") == "block_pruning":
                return "observing_with_keep_signal", "pause_more_pruning"
            return "observing", "collect_more_trades"
        if effect_status == "effective":
            return "validated_effective", "allow_next_limited_batch"
        if effect_status == "ineffective":
            return "ineffective", "rollback_or_block_more_pruning"
        if effect_status == "mixed":
            return "mixed", "continue_observation"
        if effect_status == "inconclusive":
            return "inconclusive", "retry_only_with_new_application"
        return effect_status or "unknown", "watch"

    def _empty(self, status: str) -> dict[str, Any]:
        return {
            "ok": False,
            "schema_version": "factor_governance_effect_tracker.v1",
            "status": status,
            "item_count": 0,
            "items": [],
            "boundary": self.boundary(),
        }
