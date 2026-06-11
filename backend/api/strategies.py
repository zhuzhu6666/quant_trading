"""GET /api/strategies — 列出 strategy_registry 里所有可用策略.

audit 2026-06-08: 之前没这个端点, 前端拿不到"当前可用的 strategy 列表".
"""
from fastapi import APIRouter

from backend.core.auth import RequireUser
from strategy.registry import strategy_registry

router = APIRouter(prefix="/api/strategies", tags=["strategies"])


@router.get("")
def list_strategies(_user: RequireUser) -> dict:
    """返回所有注册过的 strategy, 附带 timeframe/active 信息.

    Returns: {strategies: [{id, timeframes, active}]}
    """
    out: list[dict] = []
    for name in strategy_registry.list():
        cls = strategy_registry._strategies.get(name)
        timeframes = getattr(cls, "_reg_timeframes", ["H1"]) if cls else ["H1"]
        out.append({
            "id": name,
            "timeframes": list(timeframes),
            "active": strategy_registry.is_active(name),
        })
    return {"strategies": out}
