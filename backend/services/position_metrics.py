from __future__ import annotations

from typing import Any


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return float(default)


def _safe_str(value: Any) -> str:
    return str(value or "")


def normalize_path_state(raw: dict[str, Any] | None) -> dict[str, Any]:
    state = raw or {}
    return {
        "mfe": max(0.0, _safe_float(state.get("mfe"))),
        "mae": max(0.0, _safe_float(state.get("mae"))),
        "time_in_profit_seconds": max(0.0, _safe_float(state.get("time_in_profit_seconds"))),
        "last_observed_ts": max(0.0, _safe_float(state.get("last_observed_ts"))),
        "last_unrealized_pnl": _safe_float(state.get("last_unrealized_pnl")),
        "entry_regime": _safe_str(state.get("entry_regime")),
        "current_regime": _safe_str(state.get("current_regime")),
        "thesis_status": _safe_str(state.get("thesis_status")) or "intact",
        "regime_shift": _safe_str(state.get("regime_shift")) or "none",
    }


def update_position_path_metrics(
    *,
    previous_state: dict[str, Any] | None,
    current_pnl: float,
    now_ts: float,
    holding_seconds: float,
    max_holding_seconds: float = 0.0,
    entry_regime: str = "",
    current_regime: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = normalize_path_state(previous_state)
    current_pnl = _safe_float(current_pnl)
    now_ts = max(0.0, _safe_float(now_ts))
    holding_seconds = max(0.0, _safe_float(holding_seconds))
    max_holding_seconds = max(0.0, _safe_float(max_holding_seconds))
    entry_regime = _safe_str(entry_regime) or state["entry_regime"]
    current_regime = _safe_str(current_regime) or state["current_regime"]

    last_ts = state["last_observed_ts"]
    delta_seconds = 0.0
    if now_ts > 0 and last_ts > 0:
        delta_seconds = max(0.0, min(now_ts - last_ts, 3600.0))

    time_in_profit_seconds = state["time_in_profit_seconds"]
    if current_pnl > 0:
        time_in_profit_seconds += delta_seconds
    time_in_profit_seconds = min(time_in_profit_seconds, holding_seconds) if holding_seconds > 0 else 0.0

    mfe = max(state["mfe"], max(current_pnl, 0.0))
    mae = max(state["mae"], max(-current_pnl, 0.0))
    giveback_ratio = _clamp((mfe - max(current_pnl, 0.0)) / mfe) if mfe > 0 else 0.0
    profit_capture_ratio = _clamp(max(current_pnl, 0.0) / mfe) if mfe > 0 else 0.0
    time_in_profit_ratio = _clamp(time_in_profit_seconds / holding_seconds) if holding_seconds > 0 else 0.0
    holding_efficiency = _clamp((profit_capture_ratio * 0.6) + (time_in_profit_ratio * 0.4))
    timeout_ratio = (holding_seconds / max_holding_seconds) if max_holding_seconds > 0 else 0.0
    timeout_ratio = max(0.0, timeout_ratio)

    time_decay_score = _clamp(
        1.0
        - max(0.0, timeout_ratio - 0.5) * 0.9
        - giveback_ratio * 0.35
        + profit_capture_ratio * 0.15
    )

    if current_regime and entry_regime and current_regime != entry_regime:
        regime_shift = "confirmed"
    else:
        regime_shift = "none"

    if current_pnl < 0 and (timeout_ratio >= 1.0 or giveback_ratio >= 0.85 or holding_efficiency < 0.2):
        thesis_status = "broken"
    elif giveback_ratio >= 0.5 or timeout_ratio >= 0.8 or holding_efficiency < 0.45:
        thesis_status = "weakening"
    else:
        thesis_status = "intact"

    next_state = {
        "mfe": round(mfe, 6),
        "mae": round(mae, 6),
        "time_in_profit_seconds": round(time_in_profit_seconds, 6),
        "last_observed_ts": round(now_ts, 6),
        "last_unrealized_pnl": round(current_pnl, 6),
        "entry_regime": entry_regime,
        "current_regime": current_regime,
        "thesis_status": thesis_status,
        "regime_shift": regime_shift,
    }
    metrics = {
        "mfe": round(mfe, 6),
        "mae": round(mae, 6),
        "giveback_ratio": round(giveback_ratio, 6),
        "profit_capture_ratio": round(profit_capture_ratio, 6),
        "time_in_profit_seconds": round(time_in_profit_seconds, 6),
        "time_in_profit_ratio": round(time_in_profit_ratio, 6),
        "holding_efficiency": round(holding_efficiency, 6),
        "time_decay_score": round(time_decay_score, 6),
        "thesis_status": thesis_status,
        "regime_shift": regime_shift,
    }
    return next_state, metrics
