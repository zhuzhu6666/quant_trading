from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, connect_sqlite, get_state_pg_conn, is_state_db_path, state_table_exists
from backend.ledger.service import DecisionLedger
from backend.services.model_permissions import validate_model_artifact


MODEL_TYPE = "meta_model_sidecar"


def _loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def _conn_is_pg(conn) -> bool:
    return conn.__class__.__module__.split(".", 1)[0] == "psycopg"


def _sql(conn, sql: str) -> str:
    return sql.replace("?", "%s") if _conn_is_pg(conn) else sql


def _execute(conn, sql: str, params: tuple | list | None = None):
    if params is None:
        return conn.execute(_sql(conn, sql))
    return conn.execute(_sql(conn, sql), tuple(params))


def _table_exists(conn, table: str) -> bool:
    return state_table_exists(conn, table)


def _count_by_status(conn: sqlite3.Connection, table: str, status_col: str = "status") -> dict[str, int]:
    if not _table_exists(conn, table):
        return {}
    rows = _execute(conn,
        f"""
        SELECT {status_col} AS status, COUNT(*) AS n
        FROM {table}
        GROUP BY {status_col}
        """
    ).fetchall()
    return {str(row["status"] or "unknown"): int(row["n"] or 0) for row in rows}


class MetaModelSidecar:
    """Read-only meta-model sidecar for global advisory decisions.

    This layer sees cross-system context and records advice, but it cannot place
    orders, close positions, change hard risk limits, or apply governance.
    """

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = Path(db_path)

    def _use_pg(self) -> bool:
        return is_state_db_path(self.db_path)

    def _conn(self, *, read_only: bool = True):
        conn = get_state_pg_conn(read_only=read_only) if self._use_pg() else connect_sqlite(self.db_path, read_only=read_only)
        if not self._use_pg():
            conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def artifact() -> dict[str, Any]:
        return {
            "schema_version": "meta_model_sidecar_artifact.v1",
            "model_type": MODEL_TYPE,
            "capabilities": {
                "live_trading": False,
                "advisory_only": True,
                "shadow_only": True,
                "can_place_orders": False,
                "can_close_positions": False,
                "can_change_risk_limits": False,
                "can_increase_hard_risk_limits": False,
                "can_change_factor_weights": False,
                "can_bypass_risk_policy": False,
                "can_apply_policy_without_review": False,
                "can_release_market_connection": False,
            },
            "notes": [
                "Global advisory sidecar only.",
                "Outputs must be logged before any human or governor review.",
            ],
        }

    def build_context(self, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        overrides = overrides or {}
        now = time.time()
        context: dict[str, Any] = {
            "schema_version": "meta_context.v1",
            "created_at": now,
            "symbol": str(overrides.get("symbol") or "XAUUSD+"),
            "timeframe": str(overrides.get("timeframe") or "M5"),
            "market": dict(overrides.get("market") or {}),
            "portfolio": dict(overrides.get("portfolio") or {}),
            "risk": dict(overrides.get("risk") or {}),
            "factor": dict(overrides.get("factor") or {}),
            "learning": dict(overrides.get("learning") or {}),
            "models": dict(overrides.get("models") or {}),
            "system": dict(overrides.get("system") or {}),
        }
        try:
            with self._conn(read_only=True) as conn:
                context["risk"].setdefault("recent_policy_verdicts", self._recent_policy_verdicts(conn))
                context["risk"].setdefault("blocked_verdict_count_24h", self._blocked_verdict_count(conn, now - 86400))
                context["factor"].setdefault("health", self._factor_health_snapshot(conn))
                context["learning"].setdefault("suggestions", _count_by_status(conn, "policy_suggestion"))
                context["learning"].setdefault(
                    "position_quality_shadow",
                    self._shadow_audit_snapshot(conn, "position_quality_shadow_audit"),
                )
                context["learning"].setdefault(
                    "factor_governance_shadow",
                    self._shadow_audit_snapshot(conn, "factor_governance_shadow_audit"),
                )
                context["models"].setdefault("permission_audits", _count_by_status(conn, "model_permission_audit"))
                context["portfolio"].setdefault("recent_position_events", self._recent_position_events(conn))
        except Exception as exc:
            context["system"]["context_warning"] = str(exc)
        return context

    def decide(self, context: dict[str, Any]) -> dict[str, Any]:
        risk = context.get("risk") or {}
        factor = context.get("factor") or {}
        learning = context.get("learning") or {}
        market = context.get("market") or {}
        system = context.get("system") or {}

        contract_score = 0.0
        recover_score = 0.0
        reasons: list[str] = []
        recover_reasons: list[str] = []
        blocked = _safe_int(risk.get("blocked_verdict_count_24h"))
        if blocked > 0:
            contract_score += min(0.35, blocked * 0.07)
            reasons.append(f"recent risk blocks={blocked}")
        else:
            recover_score += 0.08
            recover_reasons.append("no recent risk blocks")

        factor_health = factor.get("health") or {}
        weak_factors = _safe_int(factor_health.get("weak_count"))
        if weak_factors > 0:
            contract_score += min(0.25, weak_factors * 0.04)
            reasons.append(f"weak factor count={weak_factors}")

        pq = learning.get("position_quality_shadow") or {}
        weak_position_rate = _safe_float(pq.get("weak_rate"))
        if weak_position_rate >= 0.45:
            contract_score += min(0.25, weak_position_rate * 0.35)
            reasons.append(f"weak position-quality shadow rate={weak_position_rate:.2f}")
        elif weak_position_rate > 0.0:
            recover_score += min(0.08, (0.45 - weak_position_rate) * 0.18)

        fg = learning.get("factor_governance_shadow") or {}
        weak_factor_shadow_rate = _safe_float(fg.get("weak_rate"))
        if weak_factor_shadow_rate >= 0.45:
            contract_score += min(0.2, weak_factor_shadow_rate * 0.25)
            reasons.append(f"weak factor-governance shadow rate={weak_factor_shadow_rate:.2f}")
        elif weak_factor_shadow_rate > 0.0:
            recover_score += min(0.08, (0.45 - weak_factor_shadow_rate) * 0.16)

        rolling = learning.get("rolling") or {}
        rolling_trade_count = _safe_int(rolling.get("trade_count") or rolling.get("rolling_trade_count"))
        rolling_pnl_sum = _safe_float(rolling.get("pnl_sum") or rolling.get("rolling_pnl_sum"))
        rolling_pnl_avg = _safe_float(rolling.get("pnl_avg") or rolling.get("rolling_pnl_avg"))
        loss_rate = _safe_float(rolling.get("loss_rate") or rolling.get("rolling_loss_rate"))
        bad_loss_rate = _safe_float(rolling.get("bad_loss_rate") or rolling.get("rolling_bad_loss_rate"))
        win_rate = _safe_float(rolling.get("win_rate") or rolling.get("rolling_win_rate"))
        mfe_mae_ratio = _safe_float(rolling.get("mfe_mae_ratio") or rolling.get("rolling_mfe_mae_ratio"))
        profit_capture = _safe_float(rolling.get("profit_capture_avg") or rolling.get("rolling_profit_capture_avg"))
        giveback = _safe_float(rolling.get("giveback_avg") or rolling.get("rolling_giveback_avg"))
        thesis_broken_rate = _safe_float(rolling.get("thesis_broken_rate") or rolling.get("rolling_thesis_broken_rate"))
        broker_close_rate = _safe_float(rolling.get("broker_close_rate") or rolling.get("rolling_broker_close_rate"))
        if rolling_trade_count > 0:
            if rolling_pnl_sum <= -2.5:
                contract_score += 0.25
                reasons.append(f"rolling pnl sum weak={rolling_pnl_sum:.2f}")
            elif rolling_pnl_sum >= 2.5:
                recover_score += 0.2
                recover_reasons.append(f"rolling pnl sum strong={rolling_pnl_sum:.2f}")
            if rolling_pnl_avg <= -0.8:
                contract_score += 0.12
                reasons.append(f"rolling pnl avg weak={rolling_pnl_avg:.2f}")
            elif rolling_pnl_avg >= 0.8:
                recover_score += 0.1
                recover_reasons.append(f"rolling pnl avg strong={rolling_pnl_avg:.2f}")
            if loss_rate >= 0.67:
                contract_score += 0.2
                reasons.append(f"rolling loss rate={loss_rate:.2f}")
            elif loss_rate <= 0.34:
                recover_score += 0.16
                recover_reasons.append(f"rolling loss rate low={loss_rate:.2f}")
            if bad_loss_rate >= 0.34:
                contract_score += 0.18
                reasons.append(f"rolling bad-loss rate={bad_loss_rate:.2f}")
            if thesis_broken_rate >= 0.34:
                contract_score += 0.12
                reasons.append(f"thesis-broken close rate={thesis_broken_rate:.2f}")
            if win_rate >= 0.55:
                recover_score += 0.14
                recover_reasons.append(f"rolling win rate={win_rate:.2f}")
            if profit_capture >= 0.45:
                recover_score += 0.1
                recover_reasons.append(f"profit capture healthy={profit_capture:.2f}")
            if giveback >= 0.65:
                contract_score += 0.1
                reasons.append(f"giveback elevated={giveback:.2f}")
            if mfe_mae_ratio >= 1.35 and broker_close_rate >= 0.2:
                recover_score += 0.08
                recover_reasons.append("favorable excursion converted by broker closes")

        counterfactual = learning.get("counterfactual") or {}
        premature_rate = _safe_float(counterfactual.get("premature_rate") or counterfactual.get("premature_tighten_rate"))
        protection_tight_rate = _safe_float(counterfactual.get("protection_tight_rate") or counterfactual.get("protection_too_tight_rate"))
        correct_stop_rate = _safe_float(counterfactual.get("correct_stop_rate"))
        if premature_rate >= 0.35:
            recover_score += 0.12
            recover_reasons.append(f"counterfactual premature rate={premature_rate:.2f}")
        if protection_tight_rate >= 0.35:
            recover_score += 0.12
            recover_reasons.append(f"counterfactual tight-protection rate={protection_tight_rate:.2f}")
        if correct_stop_rate >= 0.35:
            contract_score += 0.14
            reasons.append(f"counterfactual correct-stop rate={correct_stop_rate:.2f}")

        market_state = str(market.get("session_state") or market.get("state") or "").lower()
        minutes_to_close = _safe_float(market.get("minutes_to_close"), default=999.0)
        if market_state in {"closing_soon", "close_pending"} or minutes_to_close <= 30:
            contract_score += 0.18
            reasons.append("near market close")
        if market_state in {"halted", "closed_confirmed", "offmarket_confirmed"}:
            contract_score += 0.12
            reasons.append(f"market session={market_state}")

        health = str(system.get("health") or system.get("status") or "").lower()
        if health in {"critical", "degraded"}:
            contract_score += 0.25 if health == "critical" else 0.12
            reasons.append(f"system health={health}")

        contract_score = min(1.0, contract_score)
        recover_score = min(1.0, recover_score)
        observe_score = max(0.0, min(1.0, 1.0 - abs(contract_score - recover_score)))
        if contract_score >= 0.42 and contract_score >= recover_score + 0.12:
            posture = "contract"
            risk_budget = {"direction": "reduce", "suggested_delta_pct": -20.0}
            frequency = {"direction": "reduce", "reason": "global risk pressure elevated"}
            rationale = reasons
        elif recover_score >= 0.42 and recover_score >= contract_score + 0.12:
            posture = "recover"
            risk_budget = {"direction": "hold_or_restore_review", "suggested_delta_pct": 0.0}
            frequency = {"direction": "normal", "reason": "recovery evidence stronger than contraction risk"}
            rationale = recover_reasons or ["recovery evidence stronger than contraction risk"]
        else:
            posture = "observe"
            risk_budget = {"direction": "hold", "suggested_delta_pct": 0.0}
            frequency = {"direction": "hold_or_slight_reduce", "reason": "mixed evidence requires observation"}
            rationale = (reasons + recover_reasons) or ["insufficient evidence for posture change"]

        freeze_observe = []
        if weak_factors:
            freeze_observe.append({"scope": "factor_family", "action": "observe_weak_factors", "count": weak_factors})
        if minutes_to_close <= 30:
            freeze_observe.append({"scope": "new_entries", "action": "avoid_late_session_entries"})

        return {
            "schema_version": "meta_decision.v1",
            "model_type": MODEL_TYPE,
            "created_at": time.time(),
            "advisory_only": True,
            "posture": posture,
            "rule_version": "meta_sidecar_rules.v2",
            "risk_score": round(contract_score, 4),
            "contract_score": round(contract_score, 4),
            "observe_score": round(observe_score, 4),
            "recover_score": round(recover_score, 4),
            "net_score": round(contract_score - recover_score, 4),
            "risk_budget_advice": risk_budget,
            "trade_frequency_advice": frequency,
            "factor_family_advice": {
                "freeze_or_observe": freeze_observe,
                "trusted_family_hint": "unchanged",
            },
            "rationale": rationale,
            "approval_path": "human_or_governor_review_only",
            "capabilities": self.artifact()["capabilities"],
        }

    def run(self, *, context: dict[str, Any] | None = None, materialize: bool = True) -> dict[str, Any]:
        meta_context = self.build_context(context)
        permission = validate_model_artifact(
            self.artifact(),
            model_type=MODEL_TYPE,
            db_path=self.db_path,
            context={"operation": "meta_model_sidecar_run"},
        )
        if not permission.get("ok"):
            return {
                "ok": False,
                "error": "model_permission_violation",
                "permission": permission,
                "context": meta_context,
            }
        decision = self.decide(meta_context)
        ledger_id = ""
        if materialize:
            ledger_id = DecisionLedger(str(self.db_path)).log_decision(
                event_type="meta_model_advisory",
                symbol=str(meta_context.get("symbol") or "XAUUSD+"),
                timeframe=str(meta_context.get("timeframe") or "M5"),
                decision_ts=float(decision.get("created_at") or time.time()),
                portfolio_state=meta_context.get("portfolio") or {},
                risk_state={
                    "meta_model_permission": permission,
                    "meta_context_risk": meta_context.get("risk") or {},
                },
                action_score=float(decision.get("risk_score") or 0.0),
                action_reason=str(decision.get("posture") or "observe"),
                action_json={
                    "schema_version": "meta_model_advisory_ledger.v1",
                    "context": meta_context,
                    "decision": decision,
                    "advisory_only": True,
                },
            )
        return {
            "ok": True,
            "schema_version": "meta_model_sidecar_run.v1",
            "model_type": MODEL_TYPE,
            "materialized": bool(materialize),
            "ledger_decision_id": ledger_id,
            "permission": permission,
            "context": meta_context,
            "decision": decision,
        }

    def list_advisories(self, *, limit: int = 50) -> dict[str, Any]:
        try:
            with self._conn(read_only=True) as conn:
                rows = _execute(conn,
                    """
                    SELECT decision_id, event_type, symbol, timeframe, decision_ts,
                           action_score, action_reason, action_json, risk_state_json, created_at
                    FROM decision_ledger
                    WHERE event_type='meta_model_advisory'
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (int(limit),),
                ).fetchall()
        except Exception:
            rows = []
        items = []
        for row in rows:
            action = _loads(row["action_json"], {})
            items.append(
                {
                    "decision_id": str(row["decision_id"] or ""),
                    "event_type": str(row["event_type"] or ""),
                    "symbol": str(row["symbol"] or ""),
                    "timeframe": str(row["timeframe"] or ""),
                    "decision_ts": _safe_float(row["decision_ts"]),
                    "action_score": _safe_float(row["action_score"]),
                    "action_reason": str(row["action_reason"] or ""),
                    "decision": action.get("decision") or {},
                    "context": action.get("context") or {},
                    "risk_state": _loads(row["risk_state_json"], {}),
                    "created_at": _safe_float(row["created_at"]),
                }
            )
        return {"items": items, "count": len(items)}

    @staticmethod
    def _recent_policy_verdicts(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
        if not _table_exists(conn, "decision_ledger"):
            return []
        rows = _execute(conn,
            """
            SELECT decision_id, event_type, risk_state_json, created_at
            FROM decision_ledger
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        items = []
        for row in rows:
            risk_state = _loads(row["risk_state_json"], {})
            verdict = risk_state.get("policy_verdict") or risk_state.get("risk_verdict") or {}
            if verdict:
                items.append(
                    {
                        "decision_id": str(row["decision_id"] or ""),
                        "event_type": str(row["event_type"] or ""),
                        "verdict": verdict,
                        "created_at": _safe_float(row["created_at"]),
                    }
                )
        return items

    @staticmethod
    def _blocked_verdict_count(conn: sqlite3.Connection, since_ts: float) -> int:
        if not _table_exists(conn, "decision_ledger"):
            return 0
        rows = _execute(conn,
            """
            SELECT risk_state_json
            FROM decision_ledger
            WHERE created_at >= ?
            """,
            (float(since_ts),),
        ).fetchall()
        count = 0
        for row in rows:
            risk_state = _loads(row["risk_state_json"], {})
            verdict = risk_state.get("policy_verdict") or risk_state.get("risk_verdict") or {}
            if verdict and verdict.get("allowed") is False:
                count += 1
        return count

    @staticmethod
    def _factor_health_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
        if not _table_exists(conn, "factor_health"):
            return {"count": 0, "weak_count": 0, "items": []}
        rows = _execute(conn,
            """
            SELECT factor, score, status, section, updated_at
            FROM factor_health
            ORDER BY score ASC, updated_at DESC
            LIMIT 20
            """
        ).fetchall()
        items = [
            {
                "factor": str(row["factor"] or ""),
                "score": _safe_float(row["score"]),
                "status": str(row["status"] or ""),
                "section": str(row["section"] or ""),
                "updated_at": _safe_float(row["updated_at"]),
            }
            for row in rows
        ]
        return {
            "count": len(items),
            "weak_count": sum(1 for item in items if item["score"] < 45 or item["status"].lower() in {"weak", "bad"}),
            "items": items,
        }

    @staticmethod
    def _shadow_audit_snapshot(conn: sqlite3.Connection, table: str) -> dict[str, Any]:
        if not _table_exists(conn, table):
            return {"count": 0, "weak_count": 0, "weak_rate": 0.0}
        rows = _execute(conn,
            f"""
            SELECT prediction, result_json
            FROM {table}
            ORDER BY created_at DESC
            LIMIT 100
            """
        ).fetchall()
        weak_count = 0
        for row in rows:
            result = _loads(row["result_json"], {})
            label = str(result.get("prediction_label") or "").lower()
            if "weak" in label or "bad" in label or _safe_int(row["prediction"]) == 1:
                weak_count += 1
        return {
            "count": len(rows),
            "weak_count": weak_count,
            "weak_rate": round(weak_count / max(len(rows), 1), 4),
        }

    @staticmethod
    def _recent_position_events(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
        if not _table_exists(conn, "position_lifecycle_event"):
            return []
        rows = _execute(conn,
            """
            SELECT position_id, event_type, symbol, unrealized_pnl, realized_pnl, event_ts
            FROM position_lifecycle_event
            ORDER BY event_ts DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        return [
            {
                "position_id": str(row["position_id"] or ""),
                "event_type": str(row["event_type"] or ""),
                "symbol": str(row["symbol"] or ""),
                "unrealized_pnl": _safe_float(row["unrealized_pnl"]),
                "realized_pnl": _safe_float(row["realized_pnl"]),
                "event_ts": _safe_float(row["event_ts"]),
            }
            for row in rows
        ]
