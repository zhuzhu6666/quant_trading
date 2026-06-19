"""Risk API endpoints: summary, VaR, Kelly, stress test, concentration."""
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Any

from backend.core.auth import RequireUser
from backend.risk import VaRCalculator, KellyCriterion, StressTest, ConcentrationChecker

router = APIRouter(prefix="/api/risk", tags=["risk"])

# Module-level singletons
_var_calc = VaRCalculator(confidence=0.95)
_kelly = KellyCriterion()
_stress = StressTest()
_conc = ConcentrationChecker(max_single_weight=0.40, max_sector_weight=0.60)


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
    }


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
