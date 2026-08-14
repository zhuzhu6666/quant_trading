from __future__ import annotations

import dataclasses
import enum
import threading
import time
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any, Callable, TypeVar


T = TypeVar("T")


def safe_container_snapshot(value: Any, *, _active: set[int] | None = None) -> Any:
    """Copy projection containers while cutting only true recursive edges.

    Live state is assembled from several compatibility projections.  A
    malformed nested mapping must not make an API response impossible to
    serialize, but repeated references that are not recursive should remain
    valid copies.  Non-container domain values are intentionally left alone.
    """

    active = _active if _active is not None else set()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, enum.Enum):
        return safe_container_snapshot(value.value, _active=active)
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            return None
        active.add(identity)
        try:
            return {
                key: safe_container_snapshot(item, _active=active)
                for key, item in value.items()
            }
        finally:
            active.remove(identity)
    if isinstance(value, list):
        identity = id(value)
        if identity in active:
            return None
        active.add(identity)
        try:
            return [
                safe_container_snapshot(item, _active=active)
                for item in value
            ]
        finally:
            active.remove(identity)
    if isinstance(value, tuple):
        identity = id(value)
        if identity in active:
            return None
        active.add(identity)
        try:
            return tuple(
                safe_container_snapshot(item, _active=active)
                for item in value
            )
        finally:
            active.remove(identity)
    if isinstance(value, (set, frozenset)):
        identity = id(value)
        if identity in active:
            return None
        active.add(identity)
        try:
            return [
                safe_container_snapshot(item, _active=active)
                for item in value
            ]
        finally:
            active.remove(identity)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        identity = id(value)
        if identity in active:
            return None
        active.add(identity)
        try:
            return {
                field.name: safe_container_snapshot(
                    getattr(value, field.name),
                    _active=active,
                )
                for field in dataclasses.fields(value)
            }
        finally:
            active.remove(identity)
    # Compatibility adapters may place a small domain object in an otherwise
    # JSON-shaped projection.  Copy its attributes here so the public API
    # serializer cannot walk an object graph after this cycle guard.
    attrs = getattr(value, "__dict__", None)
    if isinstance(attrs, dict):
        identity = id(value)
        if identity in active:
            return None
        active.add(identity)
        try:
            return {
                key: safe_container_snapshot(item, _active=active)
                for key, item in attrs.items()
                if not str(key).startswith("__")
            }
        finally:
            active.remove(identity)
    # Normalize numpy scalar-like values when available without adding a
    # dependency to this low-level state module.
    item = getattr(value, "item", None)
    if callable(item):
        try:
            normalized = item()
        except Exception:
            normalized = value
        if normalized is not value:
            return safe_container_snapshot(normalized, _active=active)
    return value


def default_live_state() -> dict[str, Any]:
    return {
        "broker": "ctrader",
        "loop_running": False,
        "loop_strategy": None,
        "loop_started_at": None,
        "loop_shutdown": None,
        "accepting_new_risk": False,
        # The completed Safety heartbeat remains the admission authority.
        # These two fields only let the watchdog tell an active serial safety
        # cycle from a stalled one; progress must never authorize new risk.
        "safety_cycle_active": False,
        "safety_cycle_progress_at": None,
        # ``account``/``positions`` remain the compatibility projections used
        # by existing API/WS consumers.  Only an explicit fresh broker
        # reconcile may populate the corresponding ``*_reconciled`` snapshot
        # and advance ``*_updated_at``.  Event-cache projections are kept in
        # their own fields so an execution/trader event (or estimated equity)
        # can never manufacture an authoritative freshness timestamp.
        "account": None,
        "account_reconciled": None,
        "account_updated_at": None,
        "account_reconcile_id": None,
        "account_reconcile_failed_at": None,
        "account_reconcile_error": None,
        "account_event": None,
        "account_event_updated_at": None,
        "account_event_reason": None,
        "positions": [],
        "positions_reconciled": [],
        "positions_updated_at": None,
        "positions_reconcile_id": None,
        "positions_reconcile_failed_at": None,
        "positions_reconcile_error": None,
        "positions_component_facts": {},
        "new_risk_reconcile_blockers": [],
        "positions_event": [],
        "positions_event_updated_at": None,
        "positions_event_reason": None,
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
        "session_state_status": "unknown",
        "session_risk_blockers": [],
        # Observation time belongs to the session-risk projection itself.
        # It must never be borrowed from account/position refresh timestamps.
        "session_observed_at": 0.0,
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
    if clone and isinstance(value, (dict, list, tuple, set, frozenset)):
        # Runtime projections can contain compatibility objects with a
        # recursive edge.  Keep clone reads isolated from the live state while
        # cutting only the recursive edge instead of failing the whole read.
        return safe_container_snapshot(value)
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
