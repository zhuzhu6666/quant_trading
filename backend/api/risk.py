"""Risk API endpoints: summary, VaR, Kelly, stress test, concentration."""
import json
import sqlite3

from fastapi import APIRouter
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
    }


@router.get("/policy/verdicts")
def get_policy_verdicts(_user: RequireUser, limit: int = 50) -> dict[str, Any]:
    """最近的统一风控裁决，用于 Phase B 风控面板与审计."""
    return _recent_policy_verdicts(limit=limit)


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
