"""Risk API endpoints: summary, VaR, Kelly, stress test, concentration."""
import json
import sqlite3

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Any

from backend.core.auth import RequireUser
from backend.core.db import get_state_conn
from backend.risk import VaRCalculator, KellyCriterion, StressTest, ConcentrationChecker

router = APIRouter(prefix="/api/risk", tags=["risk"])

# Module-level singletons
_var_calc = VaRCalculator(confidence=0.95)
_kelly = KellyCriterion()
_stress = StressTest()
_conc = ConcentrationChecker(max_single_weight=0.40, max_sector_weight=0.60)


def _loads_json(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _get_system_health_report():
    try:
        from monitor.system_health import shared as _system_health_shared

        return _system_health_shared().get_last_report()
    except Exception:
        return None


def _runtime_risk_policy() -> dict[str, bool]:
    try:
        from config.runtime_config import shared as _runtime_cfg

        cfg = _runtime_cfg()
        return {
            "require_l2_depth": bool(getattr(cfg, "risk_require_l2_depth", False)),
            "block_on_disk_critical": bool(getattr(cfg, "risk_block_on_disk_critical", True)),
        }
    except Exception:
        return {
            "require_l2_depth": False,
            "block_on_disk_critical": True,
        }


def _recent_policy_verdicts(limit: int = 50) -> dict[str, Any]:
    limit = max(1, min(int(limit or 50), 200))
    conn = get_state_conn()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT decision_id, event_type, symbol, timeframe, decision_ts,
                   action_reason, action_json, risk_state_json
            FROM decision_ledger
            WHERE risk_state_json LIKE '%policy_verdict%'
               OR action_json LIKE '%risk_verdict%'
            ORDER BY decision_ts DESC, created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()

    items: list[dict[str, Any]] = []
    counts: dict[str, int] = {"allowed": 0, "blocked": 0}
    by_reason: dict[str, int] = {}
    by_action: dict[str, int] = {}

    for row in rows:
        risk_state = _loads_json(row["risk_state_json"], {})
        action_json = _loads_json(row["action_json"], {})
        if not isinstance(risk_state, dict):
            risk_state = {}
        if not isinstance(action_json, dict):
            action_json = {}
        verdict = risk_state.get("policy_verdict") or action_json.get("risk_verdict") or {}
        allowed = bool(verdict.get("allowed", False))
        reason = str(verdict.get("reason") or row["action_reason"] or "unknown")
        action = str((verdict.get("audit_payload") or {}).get("action") or action_json.get("skip_stage") or row["event_type"])
        counts["allowed" if allowed else "blocked"] += 1
        by_reason[reason] = by_reason.get(reason, 0) + 1
        by_action[action] = by_action.get(action, 0) + 1
        items.append({
            "decision_id": row["decision_id"],
            "event_type": row["event_type"],
            "symbol": row["symbol"],
            "timeframe": row["timeframe"],
            "decision_ts": row["decision_ts"],
            "allowed": allowed,
            "reason": reason,
            "action": action,
            "risk_verdict": verdict,
        })

    return {
        "limit": limit,
        "total": len(items),
        "counts": counts,
        "by_reason": by_reason,
        "by_action": by_action,
        "items": items,
    }


def _parse_review_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["failure_tags"] = _loads_json(item.pop("failure_tags_json", None), [])
    item["review"] = _loads_json(item.pop("review_json", None), {})
    return item


def _safe_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except Exception:
        return None


def _trade_trace(position_id: str | None = None, decision_id: str | None = None) -> dict[str, Any]:
    resolved_position_id = str(position_id or "").strip()
    resolved_decision_id = str(decision_id or "").strip()
    if not resolved_position_id and not resolved_decision_id:
        raise ValueError("position_id or decision_id is required")

    conn = get_state_conn()
    conn.row_factory = sqlite3.Row
    try:
        anchor = None
        if resolved_decision_id:
            anchor = conn.execute(
                """
                SELECT decision_id, trade_id, position_id, event_type, symbol, timeframe, decision_ts
                FROM decision_ledger
                WHERE decision_id = ?
                LIMIT 1
                """,
                (resolved_decision_id,),
            ).fetchone()
            if anchor and not resolved_position_id:
                resolved_position_id = str(anchor["position_id"] or anchor["trade_id"] or "").strip()

        ledger_rows = []
        if resolved_position_id:
            ledger_rows = conn.execute(
                """
                SELECT decision_id, trade_id, position_id, event_type, symbol, timeframe, decision_ts,
                       regime_id, regime_confidence, portfolio_state_json, risk_state_json,
                       policy_version, factor_set_version, action_score, action_reason, action_json, created_at
                FROM decision_ledger
                WHERE position_id = ? OR trade_id = ?
                ORDER BY decision_ts ASC, created_at ASC
                """,
                (resolved_position_id, resolved_position_id),
            ).fetchall()
        elif anchor:
            ledger_rows = [anchor]

        if not anchor and ledger_rows:
            anchor = ledger_rows[0]
        if not anchor and resolved_decision_id:
            raise LookupError(f"decision_id not found: {resolved_decision_id}")

        trade_id = ""
        symbol = ""
        timeframe = ""
        for row in ledger_rows:
            trade_id = trade_id or str(row["trade_id"] or "")
            symbol = symbol or str(row["symbol"] or "")
            timeframe = timeframe or str(row["timeframe"] or "")

        position_events = []
        recovery_state = None
        pos_int = _safe_int(resolved_position_id)
        if pos_int is not None:
            position_events = conn.execute(
                """
                SELECT event_id, position_id, trade_id, symbol, event_type, event_ts,
                       net_volume, avg_price, unrealized_pnl, realized_pnl, details_json
                FROM position_lifecycle_event
                WHERE position_id = ?
                ORDER BY event_ts ASC, event_id ASC
                """,
                (str(pos_int),),
            ).fetchall()
            recovery_state = conn.execute(
                """
                SELECT position_id, broker, symbol, direction, open_price, volume, first_seen_at,
                       last_seen_at, status, strategy_name, entry_decision_id, context_integrity,
                       recovery_meta_json, closed_at, close_reason, close_pnl
                FROM recovery_position_state
                WHERE position_id = ?
                LIMIT 1
                """,
                (pos_int,),
            ).fetchone()

        order_events = []
        if trade_id:
            order_events = conn.execute(
                """
                SELECT event_id, decision_id, trade_id, order_id, broker_order_id, event_type,
                       event_ts, price, volume, status, details_json
                FROM order_lifecycle_event
                WHERE trade_id = ?
                ORDER BY event_ts ASC, event_id ASC
                """,
                (trade_id,),
            ).fetchall()

        review_row = None
        if resolved_position_id:
            review_row = conn.execute(
                """
                SELECT review_id, trade_id, position_id, entry_decision_id, exit_decision_id,
                       entry_quality, hold_quality, exit_quality, regime_fit_score,
                       execution_quality, pnl, mae, mfe, outcome_label, failure_tags_json,
                       summary_text, review_json, created_at
                FROM trade_outcome_review
                WHERE position_id = ? OR trade_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (resolved_position_id, resolved_position_id),
            ).fetchone()
        if review_row is None and resolved_decision_id:
            review_row = conn.execute(
                """
                SELECT review_id, trade_id, position_id, entry_decision_id, exit_decision_id,
                       entry_quality, hold_quality, exit_quality, regime_fit_score,
                       execution_quality, pnl, mae, mfe, outcome_label, failure_tags_json,
                       summary_text, review_json, created_at
                FROM trade_outcome_review
                WHERE entry_decision_id = ? OR exit_decision_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (resolved_decision_id, resolved_decision_id),
            ).fetchone()

        factor_rows = []
        if review_row is not None:
            factor_rows = conn.execute(
                """
                SELECT id, review_id, trade_id, factor, entry_contribution, hold_contribution,
                       exit_contribution, net_contribution, confidence, notes
                FROM factor_contribution_review
                WHERE review_id = ?
                ORDER BY ABS(net_contribution) DESC, id ASC
                """,
                (review_row["review_id"],),
            ).fetchall()

        def _parse_ledger(row: sqlite3.Row) -> dict[str, Any]:
            item = dict(row)
            item["portfolio_state"] = _loads_json(item.pop("portfolio_state_json", None), {})
            item["risk_state"] = _loads_json(item.pop("risk_state_json", None), {})
            item["action"] = _loads_json(item.pop("action_json", None), {})
            return item

        def _parse_event(row: sqlite3.Row) -> dict[str, Any]:
            item = dict(row)
            item["details"] = _loads_json(item.pop("details_json", None), {})
            return item

        review = _parse_review_row(review_row) if review_row is not None else None
        factor_contributions = [dict(row) for row in factor_rows]
        if not ledger_rows and not position_events and not order_events and review is None and recovery_state is None:
            locator = resolved_position_id or resolved_decision_id
            raise LookupError(f"trade trace not found: {locator}")
        summary = {
            "position_id": resolved_position_id or (str(review["position_id"]) if review else ""),
            "decision_id": resolved_decision_id or (str(anchor["decision_id"]) if anchor else ""),
            "trade_id": trade_id or (str(review["trade_id"]) if review else ""),
            "symbol": symbol or (str(review["review"].get("symbol") or "") if review else ""),
            "timeframe": timeframe,
            "ledger_events": len(ledger_rows),
            "position_events": len(position_events),
            "order_events": len(order_events),
            "has_review": review is not None,
            "factor_count": len(factor_contributions),
            "latest_outcome": str(review["outcome_label"] or "") if review else "",
            "latest_close_reason": str((review.get("review") or {}).get("close_reason") or "") if review else "",
        }
        return {
            "summary": summary,
            "anchor": dict(anchor) if anchor is not None else None,
            "decision_ledger": [_parse_ledger(row) for row in ledger_rows],
            "position_lifecycle": [_parse_event(row) for row in position_events],
            "order_lifecycle": [_parse_event(row) for row in order_events],
            "review": review,
            "factor_contributions": factor_contributions,
            "recovery_state": {
                **dict(recovery_state),
                "recovery_meta": _loads_json(recovery_state["recovery_meta_json"], {}),
            } if recovery_state is not None else None,
        }
    finally:
        conn.close()


def _system_health_summary() -> dict[str, Any]:
    report = _get_system_health_report()
    if report is None:
        return {
            "overall": "unknown",
            "overall_score": 0.0,
            "critical_components": [],
            "degraded_components": [],
            "blocking_components": [],
            "advisory_critical_components": [],
            "trading_blocked": False,
            "impact_status": "unknown",
            "impact_summary": "还没有拿到运行环境快照，暂时无法判断是否会影响交易。",
            "policy_flags": _runtime_risk_policy(),
            "components": {},
            "errors": [],
        }

    policy_flags = _runtime_risk_policy()
    components = getattr(report, "components", {}) or {}
    component_status = {
        str(name): {
            "status": str(getattr(component, "status", "") or ""),
            "detail": str(getattr(component, "detail", "") or ""),
            "score": float(getattr(component, "score", 0.0) or 0.0),
        }
        for name, component in components.items()
    }
    critical_components = [name for name, item in component_status.items() if item["status"] == "critical"]
    degraded_components = [name for name, item in component_status.items() if item["status"] == "degraded"]

    blocking_components: list[str] = []
    advisory_critical_components: list[str] = []
    for name in critical_components:
        if name == "l2_depth" and not policy_flags["require_l2_depth"]:
            advisory_critical_components.append(name)
        elif name == "disk_space" and not policy_flags["block_on_disk_critical"]:
            advisory_critical_components.append(name)
        else:
            blocking_components.append(name)

    trading_blocked = bool(blocking_components)
    if trading_blocked:
        impact_status = "blocked"
        impact_summary = (
            f"当前有 {len(blocking_components)} 个运行风险会直接阻断新开仓："
            + " / ".join(blocking_components)
        )
        if advisory_critical_components or degraded_components:
            advisory_parts = advisory_critical_components + degraded_components
            impact_summary += "；同时还有需要盯住的观察项：" + " / ".join(advisory_parts)
    elif advisory_critical_components or degraded_components:
        impact_status = "observe"
        focus_items = advisory_critical_components or degraded_components
        impact_summary = (
            "当前有运行观察项，但按现有风控配置不会直接阻断交易："
            + " / ".join(focus_items)
        )
        if advisory_critical_components and degraded_components:
            impact_summary += "；一般观察项：" + " / ".join(degraded_components)
    else:
        impact_status = "ok"
        impact_summary = "运行环境目前没有明显风险项，暂时不会额外拖累交易执行。"

    return {
        "overall": str(getattr(report, "overall", "unknown") or "unknown"),
        "overall_score": float(getattr(report, "overall_score", 0.0) or 0.0),
        "critical_components": critical_components,
        "degraded_components": degraded_components,
        "blocking_components": blocking_components,
        "advisory_critical_components": advisory_critical_components,
        "trading_blocked": trading_blocked,
        "impact_status": impact_status,
        "impact_summary": impact_summary,
        "policy_flags": policy_flags,
        "components": component_status,
        "errors": list(getattr(report, "errors", []) or []),
        "ts": float(getattr(report, "ts", 0.0) or 0.0),
    }


class VarRequest(BaseModel):
    equity_series: list[float]


class KellyRequest(BaseModel):
    win_rate: float
    avg_win: float
    avg_loss: float


class StressRequest(BaseModel):
    equity_series: list[float]
    initial_equity: float | None = None


class ConcentrationRequest(BaseModel):
    weights: list[float]


@router.get("/summary")
def get_risk_summary(_user: RequireUser) -> dict[str, Any]:
    """
    获取风控指标概览: VaR, Kelly, stress, concentration.
    """
    var = _var_calc.get_status()
    kelly = _kelly.get_status()
    stress = _stress.get_status()
    conc = _conc.get_status()
    return {
        "var": var,
        "kelly": kelly,
        "stress": stress,
        "concentration": conc,
        "policy": _recent_policy_verdicts(limit=25),
        "system_health": _system_health_summary(),
    }


@router.get("/policy/verdicts")
def get_policy_verdicts(_user: RequireUser, limit: int = 50) -> dict[str, Any]:
    """最近的统一风控裁决，用于 Phase B 风控面板与审计."""
    return _recent_policy_verdicts(limit=limit)


@router.get("/trade-trace")
def get_trade_trace(
    _user: RequireUser,
    position_id: str | None = None,
    decision_id: str | None = None,
) -> dict[str, Any]:
    """按 position_id / decision_id 查询一笔交易的风控、生命周期与复盘证据链。"""
    try:
        return _trade_trace(position_id=position_id, decision_id=decision_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/var")
def calc_var(_user: RequireUser, req: VarRequest) -> dict[str, Any]:
    """
    计算并返回 VaR / CVaR.
    """
    return _var_calc.calculate(req.equity_series)


@router.get("/var")
def get_var_status(_user: RequireUser) -> dict[str, Any]:
    """获取当前 VaR 状态 (无权益数据时返回空结构)。"""
    return _var_calc.get_status()


@router.post("/kelly")
def calc_kelly(_user: RequireUser, req: KellyRequest) -> dict[str, Any]:
    """
    计算 Kelly 最优下注比例。
    """
    return _kelly.calculate(req.win_rate, req.avg_win, req.avg_loss)


@router.get("/kelly")
def get_kelly_status(_user: RequireUser) -> dict[str, Any]:
    """获取 Kelly 状态概览 (无数据时)。"""
    return _kelly.get_status()


@router.post("/stress/run")
def run_stress(_user: RequireUser, req: StressRequest) -> dict[str, Any]:
    """
    运行压力测试场景。
    """
    return _stress.run(req.equity_series, req.initial_equity)


@router.get("/stress")
def get_stress_status(_user: RequireUser) -> dict[str, Any]:
    """获取压力测试状态 (无数据时)。"""
    return _stress.get_status()


@router.post("/concentration")
def check_concentration(
    _user: RequireUser,
    weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    """
    检查因子/仓位集中度。
    weights: {因子名: 权重百分比}
    """
    return _conc.check(weights)


@router.get("/concentration")
def get_concentration_status(_user: RequireUser) -> dict[str, Any]:
    """获取集中度状态 (无数据时)。"""
    return _conc.get_status()
