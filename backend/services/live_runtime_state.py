from __future__ import annotations

import copy
import threading
import time
from typing import Any, Callable, TypeVar


T = TypeVar("T")


def default_live_state() -> dict[str, Any]:
    return {
        "broker": "ctrader",
        "loop_running": False,
        "loop_strategy": None,
        "loop_started_at": None,
        "loop_shutdown": None,
        "accepting_new_risk": False,
        "account": None,
        "account_updated_at": None,
        "positions": [],
        "positions_updated_at": None,
        "spot_price": None,
        "spot_quote": None,
        "spot_quote_changed_at": 0.0,
        "market_session": None,
        "session_pnl": 0.0,
        "session_trades": 0,
        "session_winning": 0,
        "session_losing": 0,
        "session_trade_pnls": [],
        "session_consecutive_loss": 0,
        "session_max_drawdown_pct": 0.0,
        "session_peak_equity": 0.0,
        "session_start_balance": 0.0,
        "session_last_trade_ts": 0.0,
        "session_state_source": "runtime_incremental",
        "circuit_breaker": False,
        "circuit_reason": "",
        "trade_equity_history": [],
        "risk": {
            "var": {},
            "kelly": {},
            "stress": {},
            "concentration": {},
        },
    }


def state_get(
    state: dict[str, Any],
    lock: threading.Lock,
    key: str,
    default: Any = None,
    *,
    clone: bool = False,
) -> Any:
    with lock:
        value = state.get(key, default)
    if clone and isinstance(value, (dict, list, set)):
        return copy.deepcopy(value)
    return value


def state_set(state: dict[str, Any], lock: threading.Lock, key: str, value: Any) -> None:
    with lock:
        state[key] = value


def state_update(state: dict[str, Any], lock: threading.Lock, **kwargs: Any) -> None:
    with lock:
        state.update(kwargs)


def cache_get_or_refresh(
    cache: dict[str, tuple[float, T]],
    ttl: float,
    fetcher: Callable[[], T],
    lock: threading.Lock,
) -> T:
    """Read a tiny legacy cache with single-flight refresh and stale fallback."""
    now = time.time()
    cached = cache.get("_data")
    if cached and (now - cached[0]) < ttl:
        return cached[1]
    with lock:
        cached = cache.get("_data")
        if cached and (time.time() - cached[0]) < ttl:
            return cached[1]
        try:
            data = fetcher()
            cache["_data"] = (time.time(), data)
            return data
        except Exception:
            if cached:
                return cached[1]
            raise
