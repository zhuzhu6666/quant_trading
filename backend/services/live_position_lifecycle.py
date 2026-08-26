"""Helpers for live position lifecycle bookkeeping.

The real close/replay/supervisor actions still live in ``live_service``.  These
helpers only manage pending close intent state so the large service can shrink
without changing execution behavior.
"""

from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, MutableMapping

from backend.services.market_regime import resolve_market_regime
from backend.services.market_session import (
    market_open_seconds_between,
    market_open_seconds_between_with_source,
)
from backend.services.review_contract import (
    build_execution_quality_event_details,
    trusted_broker_close_price,
)
from backend.services.supervisor_payload_contract import compact_supervisor_mapping
from risk.runtime_policy import RiskLimitSnapshot

# Compatibility export for older read/projection callers; the implementation
# is now owned by the shared supervisor payload contract above.
_compact_supervisor_mapping = compact_supervisor_mapping


MergeRecoveryMeta = Callable[[int, dict[str, Any]], Any]
LoadRecoveryRow = Callable[[int], dict[str, Any] | None]
BuildCloseContext = Callable[..., dict[str, Any]]
RiskEvaluate = Callable[[str, dict[str, Any]], Any]


def payload_get(payload: Any, key: str, default: Any = None) -> Any:
    if isinstance(payload, dict):
        return payload.get(key, default)
    return getattr(payload, key, default)


def position_symbol_value(position: Any, default: str = "XAUUSD") -> str:
    raw = str(payload_get(position, "symbol", "") or payload_get(position, "symbol_name", "") or default)
    raw = raw.strip() or default
    return raw.replace("+", "").upper()


def float_payload_value(payload: Any, *keys: str) -> float:
    for key in keys:
        try:
            value = payload_get(payload, key)
        except Exception:
            value = None
        if value in (None, ""):
            continue
        try:
            return float(value)
        except Exception:
            continue
    return 0.0


def position_api_volume(pos: Any) -> float:
    if pos is None:
        return 0.0
    if hasattr(pos, "get"):
        for key in ("volume", "api_volume"):
            try:
                value = pos.get(key)
            except Exception:
                value = None
            if value is not None:
                return float(value)
        return 0.0
    for key in ("volume", "api_volume"):
        value = getattr(pos, key, None)
        if value is not None:
            return float(value)
    return 0.0


def position_direction_sign(position: dict[str, Any]) -> int:
    direction = int(position.get("direction") or 0)
    if direction != 0:
        return 1 if direction > 0 else -1
    ptype = str(position.get("type") or "").lower()
    return -1 if ptype == "sell" else 1


def position_direction_from_payload(position: Any) -> int:
    try:
        direction = int(payload_get(position, "direction", 0) or 0)
    except Exception:
        direction = 0
    if direction:
        return 1 if direction > 0 else -1
    side = str(payload_get(position, "type", "") or payload_get(position, "side", "") or "").lower()
    if side in {"buy", "long"}:
        return 1
    if side in {"sell", "short"}:
        return -1
    return 0


def side_name(direction: int) -> str:
    if int(direction or 0) > 0:
        return "long"
    if int(direction or 0) < 0:
        return "short"
    return "unknown"


def position_unrealized_pnl(position: Any) -> float:
    if isinstance(position, dict):
        candidates = (
            position.get("profit"),
            position.get("pnl"),
            position.get("unrealized_pnl"),
        )
    else:
        candidates = (
            getattr(position, "profit", None),
            getattr(position, "pnl", None),
            getattr(position, "unrealized_pnl", None),
        )
    for value in candidates:
        try:
            return float(value or 0.0)
        except Exception:
            continue
    return 0.0


def position_component_state(position: Any, component: str) -> str:
    """Return the broker-published component state, defaulting to unknown."""

    normalized = str(component or "").strip().lower()
    keys = (
        ("pnl_state", "unrealized_pnl_state")
        if normalized == "pnl"
        else ("current_price_state", "price_state")
        if normalized in {"price", "current_price"}
        else (f"{normalized}_state",)
    )
    for key in keys:
        value = payload_get(position, key, "")
        if value not in (None, ""):
            return str(value).strip().lower()
    return "unknown"


def position_component_is_known(position: Any, component: str) -> bool:
    """Return true only for an explicitly broker-confirmed component."""

    return position_component_state(position, component) == "known"


def apply_unrealized_pnl_fields(
    positions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not positions:
        return []
    out = [dict(item) for item in positions]
    component_states = [position_component_state(item, "pnl") for item in out]
    for item, state in zip(out, component_states):
        if state != "known":
            # Never convert an unavailable PnL component into a numeric zero.
            # Risk-reducing actions can continue using position identity, but
            # metric-driven supervisor/path logic must see the unknown state.
            item["pnl_state"] = state
            item["profit"] = None
            item["pnl"] = None
            item["unrealized"] = None
            item["unrealized_pnl"] = None
            item["netUnrealizedPnL"] = None
            item["pnl_source"] = "unknown"
            item["pnl_reason_code"] = str(
                item.get("pnl_reason_code") or "position_pnl_component_unknown"
            )
            continue
        pnl = round(float(position_unrealized_pnl(item) or 0.0), 6)
        item["profit"] = pnl
        item["pnl"] = pnl
        item["unrealized"] = pnl
        item["unrealized_pnl"] = pnl
        item["netUnrealizedPnL"] = pnl
        item["pnl_source"] = "broker"
    return out


def estimate_close_pnl_from_state(
    *,
    position_id: int,
    current_price: float,
    recovery_row: dict[str, Any] | None,
    open_prices: dict[int, float],
    open_api_volumes: dict[int, float],
) -> float:
    pid = int(position_id)
    row = dict(recovery_row or {})
    open_price = float(open_prices.get(pid) or row.get("open_price") or current_price)
    direction = int(row.get("direction") or 0)
    pos_type = str(row.get("type") or "").lower()
    dir_sign = -1 if direction < 0 or pos_type == "sell" else 1
    api_volume = float(open_api_volumes.get(pid, 0.0) or 0.0)
    if api_volume <= 0:
        api_volume = float(row.get("volume") or 0.0)
    if api_volume <= 0:
        api_volume = 0.01 * 100.0
    return (float(current_price) - open_price) * dir_sign * api_volume


def position_open_price(pos: Any) -> float:
    if pos is None:
        return 0.0
    if isinstance(pos, dict):
        candidates = (
            pos.get("open_price"),
            pos.get("entry_price"),
            pos.get("price"),
        )
    else:
        candidates = (
            getattr(pos, "open_price", None),
            getattr(pos, "entry_price", None),
            getattr(pos, "price", None),
        )
    for value in candidates:
        try:
            price = float(value or 0.0)
        except Exception:
            continue
        if price > 0:
            return price
    return 0.0


def position_open_timestamp(pos: Any) -> float:
    if pos is None:
        return 0.0
    if isinstance(pos, dict):
        candidates = (
            pos.get("open_time"),
            pos.get("open_timestamp"),
            pos.get("open_ts"),
        )
    else:
        candidates = (
            getattr(pos, "open_time", None),
            getattr(pos, "open_timestamp", None),
            getattr(pos, "open_ts", None),
        )
    for value in candidates:
        try:
            ts = float(value or 0.0)
        except Exception:
            continue
        if ts <= 0:
            continue
        if ts > 10_000_000_000:
            ts /= 1000.0
        if ts > 0:
            return ts
    return 0.0


def position_id_value(pos: Any) -> int:
    try:
        return int(payload_get(pos, "position_id", None) or payload_get(pos, "ticket", None) or 0)
    except Exception:
        return 0


def normalize_position_snapshot(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        data = dict(raw)
    else:
        data = {
            "position_id": getattr(raw, "position_id", 0) or getattr(raw, "ticket", 0),
            "ticket": getattr(raw, "ticket", 0) or getattr(raw, "position_id", 0),
            "symbol": getattr(raw, "symbol", ""),
            "type": "buy" if int(getattr(raw, "direction", 0) or 0) >= 0 else "sell",
            "direction": int(getattr(raw, "direction", 0) or 0),
            "open_price": float(getattr(raw, "open_price", 0.0) or getattr(raw, "entry_price", 0.0) or 0.0),
            "entry_price": float(getattr(raw, "entry_price", 0.0) or getattr(raw, "open_price", 0.0) or 0.0),
            "volume": float(getattr(raw, "volume", 0.0) or getattr(raw, "api_volume", 0.0) or 0.0),
            "entry_decision_id": str(getattr(raw, "entry_decision_id", "") or ""),
        }
    direction = int(data.get("direction") or 0)
    if direction == 0:
        ptype = str(data.get("type") or "").lower()
        direction = 1 if ptype == "buy" else -1 if ptype == "sell" else 0
    position_id = int(data.get("position_id") or data.get("ticket") or 0)
    return {
        "position_id": position_id,
        "symbol": str(data.get("symbol") or ""),
        "direction": direction,
        "open_price": float(data.get("open_price") or data.get("entry_price") or 0.0),
        "volume": float(data.get("api_volume") or data.get("volume") or 0.0),
        "type": str(data.get("type") or ("buy" if direction >= 0 else "sell")),
        # Caller-supplied entry lineage (the live open-ledger decision id) must
        # survive normalization: the recovery store persists it and its purge
        # path treats rows without it as orphans.  Broker snapshots never carry
        # it; only filled-open/repaired-open payloads do, so the default "".
        "entry_decision_id": str(data.get("entry_decision_id") or ""),
        "raw": data,
    }


def tracked_total_api_volume(
    positions: list[Any] | None,
    *,
    open_api_volumes: dict[int, float],
) -> float:
    total_api_volume = 0.0
    for item in positions or []:
        if hasattr(item, "get"):
            pid = item.get("position_id") or item.get("ticket")
        else:
            pid = getattr(item, "position_id", None) or getattr(item, "ticket", None)
        if pid is not None and int(pid) in open_api_volumes:
            total_api_volume += float(open_api_volumes[int(pid)])
            continue
        total_api_volume += position_api_volume(item)
    return float(total_api_volume)


def max_abs_entry_score_for_positions(
    positions: list[Any] | None,
    *,
    entry_scores: dict[int, float],
) -> float:
    max_entry = 0.0
    for item in positions or []:
        if hasattr(item, "get"):
            pid = item.get("position_id") or item.get("ticket")
        else:
            pid = getattr(item, "position_id", None) or getattr(item, "ticket", None)
        if pid is None:
            continue
        entry_score = entry_scores.get(int(pid))
        if entry_score is not None and abs(entry_score) > abs(max_entry):
            max_entry = float(entry_score)
    return abs(float(max_entry))


def same_symbol_position(symbol: str, pos: Any, *, default_symbol: str | None = None) -> bool:
    wanted = str(symbol or default_symbol or "XAUUSD").replace("+", "").upper()
    actual = str(payload_get(pos, "symbol", "") or payload_get(pos, "symbol_name", "") or wanted)
    actual = actual.strip() or wanted
    actual = actual.replace("+", "").upper()
    return actual == wanted


def build_entry_cluster_context(
    *,
    positions_before: list[Any] | None,
    direction: int,
    symbol: str,
    now_ts: float,
    new_position_id: int = 0,
    new_api_volume: float = 0.0,
) -> dict[str, Any]:
    direction = 1 if int(direction or 0) > 0 else -1 if int(direction or 0) < 0 else 0
    rows: list[dict[str, Any]] = []
    same_rows: list[dict[str, Any]] = []
    opposite_rows: list[dict[str, Any]] = []
    net_api_volume = 0.0
    for pos in positions_before or []:
        if not same_symbol_position(symbol, pos):
            continue
        pos_direction = position_direction_from_payload(pos)
        api_volume = position_api_volume(pos)
        open_ts = position_open_timestamp(pos)
        open_timestamp_known = open_ts > 0
        item = {
            "position_id": position_id_value(pos),
            "direction": pos_direction,
            "api_volume": api_volume,
            "open_price": position_open_price(pos),
            "open_ts": open_ts,
            "age_seconds": (
                max(0.0, float(now_ts) - open_ts)
                if open_timestamp_known
                else None
            ),
            "open_timestamp_state": "known" if open_timestamp_known else "unknown",
        }
        rows.append(item)
        net_api_volume += api_volume * (1 if pos_direction > 0 else -1 if pos_direction < 0 else 0)
        if direction != 0 and pos_direction == direction:
            same_rows.append(item)
        elif direction != 0 and pos_direction == -direction:
            opposite_rows.append(item)

    same_ages = [
        float(item["age_seconds"])
        for item in same_rows
        if item.get("age_seconds") is not None
    ]
    unknown_open_timestamp_ids = [
        int(item.get("position_id") or 0)
        for item in same_rows
        if item.get("open_timestamp_state") != "known"
    ]
    same_timestamp_state = (
        "unknown"
        if unknown_open_timestamp_ids
        else "known"
        if same_rows
        else "not_applicable"
    )
    same_api_volume = sum(float(item.get("api_volume") or 0.0) for item in same_rows)
    opposite_api_volume = sum(float(item.get("api_volume") or 0.0) for item in opposite_rows)
    recent_same = {
        "5m": sum(1 for age in same_ages if age <= 300.0),
        "15m": sum(1 for age in same_ages if age <= 900.0),
        "30m": sum(1 for age in same_ages if age <= 1800.0),
    }
    after_same_count = len(same_rows) + (1 if direction != 0 and new_position_id > 0 else 0)
    after_same_volume = same_api_volume + (float(new_api_volume or 0.0) if direction != 0 else 0.0)
    return {
        "schema_version": "entry_cluster_context.v1",
        "symbol": str(symbol or ""),
        "direction": direction,
        "open_position_count_before": len(rows),
        "open_position_count_after": len(rows) + (1 if new_position_id > 0 else 0),
        "same_direction_open_count_before": len(same_rows),
        "same_direction_open_count_after": after_same_count,
        "opposite_direction_open_count_before": len(opposite_rows),
        "same_direction_api_volume_before": same_api_volume,
        "same_direction_api_volume_after": after_same_volume,
        "opposite_direction_api_volume_before": opposite_api_volume,
        "net_direction_api_volume_before": net_api_volume,
        "net_direction_api_volume_after": net_api_volume + float(new_api_volume or 0.0) * direction,
        "seconds_since_last_same_direction_open": (
            None
            if same_timestamp_state == "unknown"
            else min(same_ages)
            if same_ages
            else 0.0
        ),
        "same_direction_open_timestamp_state": same_timestamp_state,
        "unknown_open_timestamp_position_ids": unknown_open_timestamp_ids,
        "recent_same_direction_entries": recent_same,
        "same_direction_position_ids": [item["position_id"] for item in same_rows if item["position_id"]],
        "new_position_id": int(new_position_id or 0),
        "position_slot_index": after_same_count,
        "is_pyramid": bool(len(same_rows) > 0 and direction != 0),
        "pyramid_depth": max(0, after_same_count - 1),
    }


def build_market_micro_context_payload(
    *,
    quote: dict[str, Any] | None,
    current_price: float,
    fill_price: float = 0.0,
    direction: int = 0,
    quote_age_seconds: float = 0.0,
    quote_fresh: bool = False,
) -> dict[str, Any]:
    payload = dict(quote or {})
    bid = float(payload.get("bid") or 0.0)
    ask = float(payload.get("ask") or 0.0)
    mid = float(payload.get("mid") or 0.0)
    spread = float((ask - bid) if ask > 0 and bid > 0 else 0.0)
    signal_price = float(current_price or 0.0)
    actual_fill = float(fill_price or 0.0)
    direction = 1 if int(direction or 0) > 0 else -1 if int(direction or 0) < 0 else 0
    raw_delta = actual_fill - signal_price if actual_fill > 0 and signal_price > 0 else 0.0
    adverse_delta = raw_delta * direction if direction else 0.0
    return {
        "schema_version": "market_micro_context.v1",
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "spread": spread,
        "quote_ts": float(payload.get("ts") or 0.0),
        "quote_age_seconds": quote_age_seconds,
        "quote_fresh": bool(quote_fresh),
        "signal_price": signal_price,
        "fill_price": actual_fill,
        "fill_delta_points": raw_delta,
        "adverse_slippage_points": adverse_delta,
    }


def build_bar_context_snapshot(bar: dict[str, Any] | None) -> dict[str, Any]:
    item = dict(bar or {})
    high = float(item.get("high") or 0.0)
    low = float(item.get("low") or 0.0)
    open_price = float(item.get("open") or 0.0)
    close = float(item.get("close") or 0.0)
    span = high - low
    body = abs(close - open_price)
    return {
        "schema_version": "entry_bar_context.v1",
        "bar_ts": float(item.get("time") or 0.0),
        "timeframe": str(item.get("timeframe") or ""),
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": float(item.get("volume") or 0.0),
        "range_points": span if span > 0 else 0.0,
        "body_points": body,
        "body_ratio": body / span if span > 0 else 0.0,
        "close_location": (close - low) / span if span > 0 else 0.0,
        "upper_wick_ratio": (high - max(open_price, close)) / span if span > 0 else 0.0,
        "lower_wick_ratio": (min(open_price, close) - low) / span if span > 0 else 0.0,
        "complete": bool(item.get("complete", False)),
    }


def build_decision_quality_context(composite: Any) -> dict[str, Any]:
    signals = getattr(composite, "factor_signals", {}) or {}
    weights = getattr(composite, "active_weights", {}) or {}
    roles = getattr(composite, "factor_roles", {}) or {}
    contributions = []
    for factor, signal in signals.items():
        if signal is None:
            continue
        weight = float(weights.get(factor, 0.0) or 0.0)
        contribution = float(signal or 0.0) * weight
        contributions.append((str(factor), contribution, float(signal or 0.0), weight, str(roles.get(factor) or "alpha")))
    top_abs = sorted(contributions, key=lambda item: abs(item[1]), reverse=True)[:8]
    pos_abs = sum(abs(item[1]) for item in contributions if item[1] > 0)
    neg_abs = sum(abs(item[1]) for item in contributions if item[1] < 0)
    total_abs = pos_abs + neg_abs
    return {
        "schema_version": "decision_quality_context.v1",
        "score": float(getattr(composite, "score", 0.0) or 0.0),
        "composer_version": str(getattr(composite, "composer_version", "") or ""),
        "alpha_score": float(getattr(composite, "alpha_score", getattr(composite, "score", 0.0)) or 0.0),
        "tactical_score": float(getattr(composite, "tactical_score", 0.0) or 0.0),
        "macro_score": float(getattr(composite, "macro_score", 0.0) or 0.0),
        "calibrated_confidence": dict(getattr(composite, "calibrated_confidence", {}) or {}),
        "n_active_factors": int(getattr(composite, "n_active_factors", 0) or 0),
        "n_active_alpha_factors": int(getattr(composite, "n_active_alpha_factors", 0) or 0),
        "effective_alpha_factor_count": int(getattr(composite, "effective_alpha_factor_count", getattr(composite, "n_active_alpha_factors", 0)) or 0),
        "n_abstain_factors": int(getattr(composite, "n_abstain_factors", 0) or 0),
        "factor_roles": dict(roles or {}),
        "context_signals": dict(getattr(composite, "context_signals", {}) or {}),
        "context_state": dict(getattr(composite, "context_state", {}) or {}),
        "context_policy": dict(getattr(composite, "context_policy", {}) or {}),
        "redundancy_groups": dict(getattr(composite, "redundancy_groups", {}) or {}),
        "positive_contribution_abs": pos_abs,
        "negative_contribution_abs": neg_abs,
        "factor_conflict_ratio": min(pos_abs, neg_abs) / total_abs if total_abs > 0 else 0.0,
        "top_contributors": [
            {"factor": factor, "contribution_score": contribution, "signal": signal, "weight": weight, "role": role}
            for factor, contribution, signal, weight, role in top_abs
        ],
    }


def build_portfolio_exposure_context(
    *,
    entry_cluster: dict[str, Any],
    total_api_volume_before: float,
    actual_api_volume: float,
) -> dict[str, Any]:
    return {
        "schema_version": "portfolio_exposure_context.v1",
        "open_position_count_before": entry_cluster["open_position_count_before"],
        "open_position_count_after": entry_cluster["open_position_count_after"],
        "same_direction_open_count_before": entry_cluster["same_direction_open_count_before"],
        "same_direction_open_count_after": entry_cluster["same_direction_open_count_after"],
        "same_direction_api_volume_before": entry_cluster["same_direction_api_volume_before"],
        "same_direction_api_volume_after": entry_cluster["same_direction_api_volume_after"],
        "total_api_volume_before": float(total_api_volume_before or 0.0),
        "total_api_volume_after": float(total_api_volume_before or 0.0) + float(actual_api_volume or 0.0),
    }


def build_entry_execution_context(
    *,
    requested_volume: float,
    base_requested_volume: float,
    actual_api_volume: float,
    current_price: float,
    fill_price: float,
    sl_price: float,
    tp_price: float,
    sl_dist: float,
    tp_dist: float,
) -> dict[str, Any]:
    return {
        "schema_version": "entry_execution_context.v1",
        "requested_volume": float(requested_volume or 0.0),
        "base_requested_volume": float(base_requested_volume or 0.0),
        "actual_api_volume": float(actual_api_volume or 0.0),
        "signal_price": float(current_price or 0.0),
        "fill_price": float(fill_price or 0.0),
        "sl": float(sl_price or 0.0),
        "tp": float(tp_price or 0.0),
        "sl_distance_points": float(sl_dist or 0.0),
        "tp_distance_points": float(tp_dist or 0.0),
        "entry_protection_expected": bool(sl_price or tp_price),
    }


def build_entry_data_quality_context(
    *,
    market_micro: dict[str, Any],
    runtime_health: dict[str, Any] | None,
    decision_freshness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "schema_version": "entry_data_quality_context.v1",
        "quote_fresh": market_micro.get("quote_fresh"),
        "quote_age_seconds": market_micro.get("quote_age_seconds"),
        "runtime_health": runtime_health or {},
    }
    if decision_freshness:
        payload["decision_freshness"] = dict(decision_freshness or {})
    return payload


OPEN_LEARNING_CONTEXT_SCHEMA_VERSION = "open_learning_context.v2"
OPEN_LEARNING_CONTEXT_REQUIRED_FIELDS = (
    "entry_cluster",
    "market_micro_context",
    "bar_context",
    "execution_context",
    "decision_quality_context",
    "event_context",
    "data_quality_context",
    "market_session",
)


def _positive_finite(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(number) and number > 0.0


def validate_open_learning_context(
    payload: Mapping[str, Any] | None,
    *,
    require_fill: bool = True,
) -> dict[str, Any]:
    """Validate the single producer contract used by future open decisions.

    ``require_fill=False`` is used immediately before broker submission: all
    entry facts must exist, while the broker fill is not known yet.  Filled
    ledger/recovery payloads use the default and require the actual fill.
    """

    context = dict(payload or {})
    missing: list[str] = []
    invalid: list[str] = []

    contexts: dict[str, dict[str, Any]] = {}
    for field in OPEN_LEARNING_CONTEXT_REQUIRED_FIELDS:
        value = context.get(field)
        if not isinstance(value, dict) or not value:
            missing.append(field)
            continue
        contexts[field] = value

    cluster = contexts.get("entry_cluster")
    if cluster is not None:
        if str(cluster.get("schema_version") or "") != "entry_cluster_context.v1":
            invalid.append("entry_cluster.schema_version")
        try:
            direction = int(cluster.get("direction") or 0)
        except (TypeError, ValueError, OverflowError):
            direction = 0
        if direction == 0:
            invalid.append("entry_cluster.direction")

    micro = contexts.get("market_micro_context")
    if micro is not None:
        for field in ("bid", "ask", "mid", "spread", "quote_ts", "signal_price"):
            if not _positive_finite(micro.get(field)):
                invalid.append(f"market_micro_context.{field}")
        if micro.get("quote_fresh") is not True:
            invalid.append("market_micro_context.quote_fresh")

    bar = contexts.get("bar_context")
    if bar is not None:
        if bar.get("complete") is not True:
            invalid.append("bar_context.complete")
        for field in ("bar_ts", "open", "high", "low", "close"):
            if not _positive_finite(bar.get(field)):
                invalid.append(f"bar_context.{field}")

    execution = contexts.get("execution_context")
    if execution is not None:
        for field in ("requested_volume", "actual_api_volume", "signal_price"):
            if not _positive_finite(execution.get(field)):
                invalid.append(f"execution_context.{field}")
        if require_fill and not _positive_finite(execution.get("fill_price")):
            invalid.append("execution_context.fill_price")

    decision_quality = contexts.get("decision_quality_context")
    if decision_quality is not None:
        if str(decision_quality.get("schema_version") or "") != "decision_quality_context.v1":
            invalid.append("decision_quality_context.schema_version")
        if not str(decision_quality.get("composer_version") or ""):
            invalid.append("decision_quality_context.composer_version")
        if not isinstance(decision_quality.get("factor_roles"), dict) or not decision_quality.get("factor_roles"):
            invalid.append("decision_quality_context.factor_roles")
        if not _positive_finite(decision_quality.get("n_active_alpha_factors")):
            invalid.append("decision_quality_context.n_active_alpha_factors")

    event = contexts.get("event_context")
    if event is not None:
        try:
            multiplier = float(event.get("multiplier"))
        except (TypeError, ValueError, OverflowError):
            multiplier = -1.0
        if not math.isfinite(multiplier) or multiplier < 0.0:
            invalid.append("event_context.multiplier")

    data_quality = contexts.get("data_quality_context")
    if data_quality is not None:
        if str(data_quality.get("schema_version") or "") != "entry_data_quality_context.v1":
            invalid.append("data_quality_context.schema_version")
        if data_quality.get("quote_fresh") is not True:
            invalid.append("data_quality_context.quote_fresh")

    session = contexts.get("market_session")
    if session is not None and not str(session.get("status") or ""):
        invalid.append("market_session.status")

    return {
        "schema_version": OPEN_LEARNING_CONTEXT_SCHEMA_VERSION,
        "ready": not missing and not invalid,
        "required_fields": list(OPEN_LEARNING_CONTEXT_REQUIRED_FIELDS),
        "missing_fields": list(dict.fromkeys(missing)),
        "invalid_fields": list(dict.fromkeys(invalid)),
        "require_fill": bool(require_fill),
    }


def build_open_learning_context_payload(
    *,
    entry_cluster: dict[str, Any],
    market_micro: dict[str, Any],
    bar: dict[str, Any] | None,
    composite: Any,
    total_api_volume_before: float,
    actual_api_volume: float,
    requested_volume: float,
    base_requested_volume: float,
    current_price: float,
    fill_price: float,
    sl_price: float,
    tp_price: float,
    sl_dist: float,
    tp_dist: float,
    sizing_trace: dict[str, Any] | None,
    event_sizing_context: dict[str, Any] | None,
    runtime_health: dict[str, Any] | None,
    market_session: dict[str, Any] | None,
    decision_freshness: dict[str, Any] | None = None,
    entry_timing_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    portfolio_exposure = build_portfolio_exposure_context(
        entry_cluster=entry_cluster,
        total_api_volume_before=total_api_volume_before,
        actual_api_volume=actual_api_volume,
    )
    execution_context = build_entry_execution_context(
        requested_volume=requested_volume,
        base_requested_volume=base_requested_volume,
        actual_api_volume=actual_api_volume,
        current_price=current_price,
        fill_price=fill_price,
        sl_price=sl_price,
        tp_price=tp_price,
        sl_dist=sl_dist,
        tp_dist=tp_dist,
    )
    payload = {
        "entry_cluster": entry_cluster,
        "same_direction_open_count": entry_cluster["same_direction_open_count_before"],
        "recent_same_direction_entries": entry_cluster["recent_same_direction_entries"],
        "portfolio_exposure": portfolio_exposure,
        "market_micro_context": market_micro,
        "spread": market_micro.get("spread", 0.0),
        "bid": market_micro.get("bid", 0.0),
        "ask": market_micro.get("ask", 0.0),
        "bar_context": build_bar_context_snapshot(bar),
        "execution_context": execution_context,
        "sizing_trace": sizing_trace or {},
        "decision_quality_context": build_decision_quality_context(composite),
        "event_context": event_sizing_context or {},
        "data_quality_context": build_entry_data_quality_context(
            market_micro=market_micro,
            runtime_health=runtime_health,
            decision_freshness=decision_freshness,
        ),
        "market_session": market_session or {},
        "decision_freshness": dict(decision_freshness or {}),
        "entry_timing_context": dict(entry_timing_context or {}),
    }
    capture_quality = validate_open_learning_context(payload, require_fill=True)
    if capture_quality["ready"]:
        payload["open_context_quality"] = capture_quality
    return payload


def build_open_trade_risk_context_payload(
    *,
    cfg: Any,
    acct: dict[str, Any] | None,
    positions: list[Any] | None,
    requested_api_volume: float,
    signal_score: float,
    symbol: str,
    direction: int,
    current_price: float,
    atr_price: float,
    risk_snapshot: dict[str, Any] | None,
    session_state: dict[str, Any],
    total_api_volume: float,
    event_sizing_context: dict[str, Any] | None,
    event_window_learning_policy: dict[str, Any] | None,
    entry_quality_gate: dict[str, Any] | None,
    entry_cluster_context: dict[str, Any],
    entry_cluster_learning_policy: dict[str, Any] | None,
    same_direction_cooldown_seconds: float,
    max_abs_entry_score: float,
    loop_running: bool,
    bridge_connected: bool,
    data_lag_seconds: float,
    runtime_health: dict[str, Any],
    temporal_context: dict[str, Any],
    decision_freshness: dict[str, Any] | None,
    supervisor_reentry_block: dict[str, Any] | None,
    event_filter_context: dict[str, Any] | None = None,
    loss_streak_ladder: dict[str, Any] | None = None,
) -> dict[str, Any]:
    risk_limits = RiskLimitSnapshot.from_runtime_config(cfg)
    var_enabled = bool(getattr(cfg, "var_enabled", False))
    projected_risk_snapshot = dict(risk_snapshot or {})
    if var_enabled:
        from backend.risk.metrics_snapshot import (
            project_candidate_risk_snapshot,
        )

        contract_sizes = {
            str(name): dict(settings or {}).get("contract_size")
            for name, settings in dict(
                getattr(cfg, "multi_symbol_config", {}) or {}
            ).items()
        }
        projected_risk_snapshot = project_candidate_risk_snapshot(
            projected_risk_snapshot,
            positions=positions,
            account=acct,
            candidate={
                "symbol": symbol,
                "direction": direction,
                "current_price": current_price,
                "requested_api_volume": requested_api_volume,
            },
            contract_sizes=contract_sizes,
            var_confidence=float(getattr(cfg, "var_alpha", 0.95) or 0.95),
            var_lookback=max(
                2,
                int(getattr(cfg, "var_window", 500) or 500),
            ),
        )
    else:
        projected_risk_snapshot.pop("_forward_var_input", None)
    return {
        "trade": {
            "symbol": str(symbol or "XAUUSD"),
            "direction": int(direction or 0),
            "current_price": float(current_price or 0.0),
            "atr_price": float(atr_price or 0.0),
        },
        "account": acct or {},
        "session": {
            "pnl": float(session_state.get("pnl", 0.0) or 0.0),
            "start_balance": float(session_state.get("start_balance", 0.0) or 0.0),
            "trades": int(session_state.get("trades", 0) or 0),
            "consecutive_losses": int(session_state.get("consecutive_losses", 0) or 0),
            "drawdown_pct": float(session_state.get("drawdown_pct", 0.0) or 0.0),
            "circuit_breaker": bool(session_state.get("circuit_breaker", False)),
        },
        "risk_snapshot": projected_risk_snapshot,
        "risk_limits": risk_limits.to_dict(),
        "var": {
            "enabled": var_enabled,
            "threshold_pct": risk_limits.var_threshold_pct,
            "cvar_threshold_pct": risk_limits.cvar_threshold_pct,
        },
        "open_position_count": len(positions or []),
        "max_position_count": int(getattr(cfg, "max_position_count", 3) or 0),
        "total_api_volume": float(total_api_volume or 0.0),
        "requested_api_volume": float(requested_api_volume or 0.0),
        "max_position_api_volume": float(getattr(cfg, "max_position_api_volume", 1000.0) or 0.0),
        "event_sizing": dict(event_sizing_context or {"enabled": False, "multiplier": 1.0}),
        "event_filter": event_filter_context or {},
        "event_window_learning_policy": event_window_learning_policy or {},
        "entry_quality_gate": entry_quality_gate or {},
        "entry_cluster": entry_cluster_context,
        "entry_cluster_learning_policy": entry_cluster_learning_policy or {},
        "same_direction_cooldown_seconds": float(same_direction_cooldown_seconds or 0.0),
        "pyramid_enabled": bool(getattr(cfg, "pyramid_enabled", True)),
        "max_abs_entry_score": float(max_abs_entry_score or 0.0),
        "signal_score": float(signal_score or 0.0),
        "loop_running": bool(loop_running),
        "bridge_connected": bool(bridge_connected),
        "data_lag_seconds": float(data_lag_seconds or 0.0),
        "runtime_health": runtime_health or {},
        "decision_freshness": decision_freshness or {},
        "loss_cooldown_after_losses": risk_limits.loss_cooldown_after_losses,
        "loss_cooldown_bars": risk_limits.loss_cooldown_bars,
        "block_on_disk_critical": risk_limits.block_on_disk_critical,
        "loss_streak_ladder": dict(loss_streak_ladder or {}),
        "temporal_context": temporal_context,
        "supervisor_reentry_block": supervisor_reentry_block or {},
    }


def build_filled_open_ledger_payloads(
    *,
    cfg: Any,
    bar: dict[str, Any],
    tick: int,
    pid: int,
    actual_api_volume: float,
    requested_volume: float,
    fill_price: float,
    current_price: float,
    sl_price: float,
    tp_price: float,
    acct: dict[str, Any] | None,
    positions_before: list[Any] | None,
    composite: Any,
    gate_result: Any,
    learning_context: dict[str, Any],
    risk_state: dict[str, Any],
    session_pnl: float = 0.0,
    risk_verdict: Any = None,
    decision_ts_fallback: float = 0.0,
    event_ts: float = 0.0,
) -> dict[str, dict[str, Any]]:
    pid_str = str(int(pid))
    direction = int(getattr(composite, "direction", 0) or 0)
    risk_payload = risk_verdict.to_dict() if hasattr(risk_verdict, "to_dict") else (risk_verdict or {})
    return {
        "composite_decision_payload": {
            "event_type": "open",
            "composite": composite,
            "gate_result": gate_result,
            "symbol": "XAUUSD+",
            "timeframe": str(getattr(cfg, "timeframe", "") or ""),
            "decision_ts": (bar or {}).get("time", decision_ts_fallback),
            "trade_id": pid_str,
            "position_id": pid_str,
            "portfolio_state": {
                "balance": (acct or {}).get("balance", 0),
                "equity": (acct or {}).get("equity", 0),
                "n_positions": len(positions_before or []),
                "session_pnl": float(session_pnl or 0.0),
            },
            "risk_state": risk_state or {},
            "action_reason": "executed",
            "action_json": {
                "position_id": int(pid),
                "volume": actual_api_volume,
                "requested_volume": requested_volume,
                "price": round(float(current_price or 0.0), 2),
                "fill_price": round(float(fill_price or 0.0), 2),
                "sl": round(float(sl_price or 0.0), 2),
                "tp": round(float(tp_price or 0.0), 2),
                "tick": int(tick or 0),
                **(learning_context or {}),
                **({"risk_verdict": risk_payload} if risk_verdict is not None else {}),
            },
        },
        "submitted_order_payload": {
            "event_type": "submitted",
            "trade_id": pid_str,
            "order_id": pid_str,
            "broker_order_id": pid_str,
            "price": float(current_price or 0.0),
            "volume": float(actual_api_volume or 0.0),
            "status": "submitted",
            "details": build_execution_quality_event_details(
                tick=tick,
                direction=direction,
                requested_price=current_price,
                fill_price=0.0,
                learning_context=learning_context,
            ),
        },
        "filled_order_payload": {
            "event_type": "filled",
            "trade_id": pid_str,
            "order_id": pid_str,
            "broker_order_id": pid_str,
            "price": float(fill_price or 0.0),
            "volume": float(actual_api_volume or 0.0),
            "status": "filled",
            "details": build_execution_quality_event_details(
                tick=tick,
                direction=direction,
                requested_price=current_price,
                fill_price=fill_price,
                learning_context=learning_context,
            ),
        },
        "position_event_payload": {
            "position_id": pid_str,
            "trade_id": pid_str,
            "symbol": "XAUUSD+",
            "event_type": "opened",
            "net_volume": float(actual_api_volume or 0.0),
            "avg_price": float(fill_price or 0.0),
            "details": {
                "tick": int(tick or 0),
                "direction": direction,
                "sl": round(float(sl_price or 0.0), 2),
                "tp": round(float(tp_price or 0.0), 2),
            },
            "event_ts": float(event_ts or 0.0),
        },
    }


def build_filled_open_recovery_payloads(
    *,
    position_id: int,
    broker: str,
    strategy_name: str,
    direction: int,
    fill_price: float,
    current_price: float,
    sl_price: float,
    tp_price: float,
    requested_volume: float,
    actual_api_volume: float,
    tick: int,
    entry_decision_id: str,
    entry_protection_plan: dict[str, Any],
    trade_attribution_payload: dict[str, Any] | None,
    learning_context: dict[str, Any] | None,
    context_integrity: str,
) -> dict[str, Any]:
    return {
        "state_payload": {
            "position_id": int(position_id),
            "symbol": "XAUUSD+",
            "direction": int(direction or 0),
            "open_price": float(fill_price or current_price or 0.0),
            "volume": float(actual_api_volume or 0.0),
            "entry_decision_id": str(entry_decision_id or ""),
        },
        "state_kwargs": {
            "broker": str(broker or ""),
            "strategy_name": str(strategy_name or "factor_v4"),
            "status": "open",
            "context_integrity": str(context_integrity or "full"),
        },
        "meta": {
            "tick": int(tick or 0),
            "sl": round(float(sl_price or 0.0), 2),
            "tp": round(float(tp_price or 0.0), 2),
            "entry_protection_plan": entry_protection_plan or {},
            "trade_attribution": trade_attribution_payload or {},
            **(learning_context or {}),
        },
    }


def build_trade_attribution_payload_from_composite(
    *,
    position_id: int,
    open_ts: float,
    open_price: float,
    direction: int,
    actual_api_volume: float,
    composite: Any,
) -> dict[str, Any]:
    factor_signals = dict(getattr(composite, "factor_signals", {}) or {})
    factor_roles = dict(getattr(composite, "factor_roles", {}) or {})
    active_weights = dict(getattr(composite, "active_weights", {}) or {})
    total_signal_abs = sum(
        abs(float(signal or 0.0))
        for factor, signal in factor_signals.items()
        if str(factor_roles.get(factor) or "alpha") == "alpha"
        and abs(float(active_weights.get(factor, 0.0) or 0.0)) > 0
    )
    return {
        "position_id": int(position_id),
        "open_ts": float(open_ts),
        "open_price": float(open_price),
        "direction": int(direction),
        "factor_signals": factor_signals,
        "factor_values": dict(getattr(composite, "factor_values", {}) or {}),
        "active_weights": active_weights,
        "factor_roles": factor_roles,
        "context_signals": dict(getattr(composite, "context_signals", {}) or {}),
        "composite_score": float(getattr(composite, "score", 0.0) or 0.0),
        "alpha_score": float(getattr(composite, "alpha_score", getattr(composite, "score", 0.0)) or 0.0),
        "tactical_score": float(getattr(composite, "tactical_score", 0.0) or 0.0),
        "macro_score": float(getattr(composite, "macro_score", 0.0) or 0.0),
        "calibrated_confidence": dict(getattr(composite, "calibrated_confidence", {}) or {}),
        "tags_breakdown": dict(getattr(composite, "tags_breakdown", {}) or {}),
        "total_signal_abs": total_signal_abs,
        "api_volume": float(actual_api_volume),
        "attribution_integrity": "full",
    }


def entry_quality_gate_from_learning_policy(
    *,
    policy: dict[str, Any],
    decision_quality: dict[str, Any],
    signal_score: float,
) -> dict[str, Any]:
    if not bool((policy or {}).get("active", False)):
        return {"active": False, "allowed": True, "reason": "inactive", "source": "entry_quality_gate"}

    abs_signal = abs(float(signal_score or 0.0))
    conflict_ratio = float((decision_quality or {}).get("factor_conflict_ratio", 0.0) or 0.0)
    top_contributors = (decision_quality or {}).get("top_contributors") or []
    controls = list((policy or {}).get("controls") or [])
    base = {
        "active": True,
        "allowed": True,
        "reason": "passed",
        "source": "entry_quality_gate",
        "control_count": len(controls),
        "metrics": {
            "signal_score_abs": round(abs_signal, 6),
            "factor_conflict_ratio": round(conflict_ratio, 6),
        },
    }
    for control in controls:
        action = str(control.get("action") or "")
        threshold = float(control.get("min_abs_signal_score") or 0.0)
        strong_override = float(control.get("strong_signal_override") or 1.0)
        if action == "raise_weak_signal_threshold" and threshold > 0 and abs_signal < threshold:
            return {
                **base,
                "allowed": False,
                "reason": "learning_weak_signal_threshold",
                "suggestion_id": str(control.get("suggestion_id") or ""),
                "scope_key": str(control.get("scope_key") or ""),
                "action": action,
                "thresholds": {
                    "min_abs_signal_score": threshold,
                    "strong_signal_override": strong_override,
                },
            }
        max_conflict = float(control.get("max_factor_conflict_ratio") or 0.0)
        if (
            action == "require_factor_agreement"
            and max_conflict > 0
            and conflict_ratio >= max_conflict
            and abs_signal < strong_override
        ):
            return {
                **base,
                "allowed": False,
                "reason": "learning_factor_conflict_control",
                "suggestion_id": str(control.get("suggestion_id") or ""),
                "scope_key": str(control.get("scope_key") or ""),
                "action": action,
                "thresholds": {
                    "max_factor_conflict_ratio": max_conflict,
                    "strong_signal_override": strong_override,
                },
            }
        suppressed_factor = str(control.get("suppressed_factor") or control.get("scope_key") or "")
        if action == "suppress_recent_worst_factor" and suppressed_factor and abs_signal < strong_override:
            matched = [
                item
                for item in top_contributors
                if str(item.get("factor") or "") == suppressed_factor
                and float(item.get("contribution_score") or 0.0) < 0
            ]
            if matched:
                return {
                    **base,
                    "allowed": False,
                    "reason": "learning_recent_worst_factor_control",
                    "suggestion_id": str(control.get("suggestion_id") or ""),
                    "scope_key": str(control.get("scope_key") or ""),
                    "action": action,
                    "thresholds": {"strong_signal_override": strong_override},
                    "evidence": {
                        "matched_factor_count": len(matched),
                        "suppressed_factor": suppressed_factor,
                    },
                }
    return base


def classify_trading_session(hour_utc: int) -> str:
    if 0 <= hour_utc < 7:
        return "asia"
    if 7 <= hour_utc < 13:
        return "europe"
    if 13 <= hour_utc < 21:
        return "us"
    return "rollover"


def timeframe_seconds(timeframe: str) -> int:
    mapping = {
        "M1": 60,
        "M5": 300,
        "M15": 900,
        "M30": 1800,
        "H1": 3600,
        "H4": 14400,
        "D1": 86400,
    }
    return mapping.get(str(timeframe or "").upper(), 0)


def temporal_context_for_trade(
    *,
    decision_ts: float,
    timeframe: str,
    evaluated_at_ts: float,
    session_last_trade_ts: float = 0.0,
    loop_started_at: float = 0.0,
) -> dict[str, Any]:
    ts = float(decision_ts or evaluated_at_ts)
    evaluated_at = float(evaluated_at_ts or ts)
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    tf_seconds = timeframe_seconds(timeframe)
    last_trade_gap = max(0.0, evaluated_at - session_last_trade_ts) if session_last_trade_ts > 0 else 0.0
    loop_uptime = max(0.0, evaluated_at - loop_started_at) if loop_started_at > 0 else 0.0
    return {
        "decision_ts": ts,
        "time_basis": "market_epoch_seconds_utc",
        "evaluated_at": evaluated_at,
        "runtime_basis": "system_epoch_seconds_utc",
        "timeframe": str(timeframe or ""),
        "timeframe_seconds": tf_seconds,
        "hour_utc": int(dt.hour),
        "minute_utc": int(dt.minute),
        "weekday_utc": int(dt.weekday()),
        "session_label": classify_trading_session(int(dt.hour)),
        "is_weekend_utc": bool(dt.weekday() >= 5),
        "seconds_since_last_trade": round(last_trade_gap, 3),
        "bars_since_last_trade": round(last_trade_gap / tf_seconds, 3) if tf_seconds > 0 and last_trade_gap > 0 else 0.0,
        "loop_uptime_seconds": round(loop_uptime, 3),
    }


def remember_close_reason(
    *,
    pending_reasons: MutableMapping[int, str],
    merge_recovery_meta: MergeRecoveryMeta,
    position_id: int,
    reason: str,
    now_fn: Callable[[], float] = time.time,
) -> None:
    if position_id <= 0 or not reason:
        return
    pid = int(position_id)
    pending_reasons[pid] = str(reason)
    merge_recovery_meta(
        pid,
        {
            "pending_close_reason": str(reason),
            "pending_close_reason_ts": now_fn(),
        },
    )


def consume_close_reason(
    *,
    pending_reasons: MutableMapping[int, str],
    load_recovery_row: LoadRecoveryRow,
    position_id: int,
    default: str = "broker_close",
) -> str:
    pending = pending_reasons.pop(int(position_id), None)
    if pending:
        return pending
    row = load_recovery_row(int(position_id))
    meta = dict((row or {}).get("recovery_meta") or {})
    recovered = str(meta.get("pending_close_reason") or "")
    return recovered or default


def serialize_close_verdict(verdict: Any) -> dict[str, Any]:
    try:
        payload = verdict.to_dict()
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass
    return {
        "allowed": False,
        "reason": "verdict_serialization_failed",
    }


def build_risk_state_with_policy_verdict(
    risk_state: Mapping[str, Any] | None,
    verdict: Any,
    *,
    serialized: bool = False,
) -> dict[str, Any]:
    state = dict(risk_state or {})
    if serialized:
        state["policy_verdict"] = verdict or {"allowed": False, "reason": "missing_verdict"}
    else:
        state["policy_verdict"] = serialize_close_verdict(verdict)
    return state


def remember_pending_open_attach(
    pending_until: MutableMapping[int, float],
    position_id: int,
    *,
    ttl_seconds: float,
    now_fn: Callable[[], float] = time.time,
) -> None:
    pid = int(position_id or 0)
    if pid <= 0:
        return
    pending_until[pid] = float(now_fn()) + float(ttl_seconds or 0.0)


def active_pending_open_attach_ids(
    pending_until: MutableMapping[int, float],
    current_position_ids: set[int] | None = None,
    *,
    now_fn: Callable[[], float] = time.time,
) -> list[int]:
    current_ids = set(current_position_ids or set())
    now = float(now_fn())
    active: list[int] = []
    for pid, until_ts in list(pending_until.items()):
        if int(pid) in current_ids or float(until_ts or 0.0) <= now:
            pending_until.pop(int(pid), None)
            continue
        active.append(int(pid))
    return sorted(active)


def restore_attribution_for_positions(
    attr_engine: Any,
    positions: list[Any] | None,
    *,
    load_recovery_row: LoadRecoveryRow,
    debug_log: Callable[[int, Exception], Any] | None = None,
) -> int:
    if attr_engine is None:
        return 0
    restored = 0
    for raw in positions or []:
        pid = position_id_value(raw)
        if pid <= 0:
            continue
        try:
            if hasattr(attr_engine, "has_open") and attr_engine.has_open(pid):
                continue
            row = load_recovery_row(pid)
            meta = dict((row or {}).get("recovery_meta") or {})
            payload = meta.get("trade_attribution") or {}
            if hasattr(attr_engine, "restore_open") and attr_engine.restore_open(pid, payload):
                restored += 1
        except Exception as exc:
            if debug_log is not None:
                debug_log(pid, exc)
    return restored


def current_regime_hint_from_composite(composite: Any) -> str:
    if not isinstance(composite, dict):
        return ""
    return str(resolve_market_regime(composite)["regime_id"] or "")


def enrich_positions_with_lifecycle_metrics(
    pos_list: list[Any],
    *,
    cfg: Any = None,
    now_ts: float,
    persist: bool,
    broker: str,
    strategy_name: str,
    coerce_positions: Callable[[list[Any]], list[dict[str, Any]]],
    apply_unrealized_pnl_fields_fn: Callable[..., list[dict[str, Any]]],
    holding_summary_for_position: Callable[..., dict[str, Any]],
    position_path_metrics_for_position: Callable[..., dict[str, Any]],
    evaluate_position_supervisor_for_position: Callable[..., dict[str, Any]],
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    raw_positions = apply_unrealized_pnl_fields_fn(coerce_positions(pos_list))
    for raw in raw_positions:
        item = dict(raw)
        item.update(holding_summary_for_position(item, cfg=cfg, now_ts=now_ts))
        if position_component_is_known(item, "pnl"):
            item.update(
                position_path_metrics_for_position(
                    item,
                    cfg=cfg,
                    now_ts=now_ts,
                    persist=persist,
                    broker=broker,
                    strategy_name=strategy_name,
                )
            )
        else:
            item["position_path_metrics_state"] = "unknown"
            item["position_path_metrics_reason_code"] = (
                str(item.get("pnl_reason_code") or "position_pnl_unknown")
            )
        supervisor = evaluate_position_supervisor_for_position(
            item,
            cfg=cfg,
            now_ts=now_ts,
            positions=pos_list,
            persist=persist,
            broker=broker,
            strategy_name=strategy_name,
        )
        item["supervisor"] = supervisor
        item["supervisor_action"] = supervisor.get("action")
        item["supervisor_label"] = supervisor.get("action_label")
        item["supervisor_reason"] = supervisor.get("summary_reason")
        item["supervisor_summary"] = supervisor.get("human_summary")
        enriched.append(item)
    return enriched


def remember_close_verdict(
    *,
    pending_verdicts: MutableMapping[int, dict[str, Any]],
    merge_recovery_meta: MergeRecoveryMeta,
    position_id: int,
    verdict: Any,
    now_fn: Callable[[], float] = time.time,
) -> None:
    if position_id <= 0 or verdict is None:
        return
    pid = int(position_id)
    payload = serialize_close_verdict(verdict)
    pending_verdicts[pid] = payload
    merge_recovery_meta(
        pid,
        {
            "pending_close_verdict": payload,
            "pending_close_verdict_ts": now_fn(),
        },
    )


def consume_close_verdict(
    *,
    pending_verdicts: MutableMapping[int, dict[str, Any]],
    load_recovery_row: LoadRecoveryRow,
    build_close_context: BuildCloseContext,
    risk_evaluate: RiskEvaluate,
    position_id: int,
    close_reason: str,
) -> dict[str, Any]:
    pending = pending_verdicts.pop(int(position_id), None)
    if pending:
        return pending
    row = load_recovery_row(int(position_id))
    meta = dict((row or {}).get("recovery_meta") or {})
    recovered = meta.get("pending_close_verdict")
    if isinstance(recovered, dict) and recovered:
        return recovered
    close_context = build_close_context(
        position_id=int(position_id),
        close_reason=close_reason,
        mode="live",
    )
    return risk_evaluate("close_position", close_context).to_dict()


def forget_pending_close_state(
    *,
    pending_reasons: MutableMapping[int, str],
    pending_verdicts: MutableMapping[int, dict[str, Any]],
    position_id: int,
) -> None:
    pid = int(position_id)
    pending_reasons.pop(pid, None)
    pending_verdicts.pop(pid, None)


def latest_close_evidence(ledger_latest: dict[str, Any] | None, trace_latest: dict[str, Any] | None) -> dict[str, Any]:
    ledger = ledger_latest or {}
    trace = trace_latest or {}
    if trace and (
        not ledger
        or float(trace.get("decision_ts") or 0.0) > float(ledger.get("decision_ts") or 0.0)
    ):
        return trace
    return ledger


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:
        return getattr(row, key, default)


def normalize_supervisor_event_row(row: Any, *, close_ts: float) -> dict[str, Any]:
    if not row:
        return {}
    action_json = _json_dict(_row_get(row, "action_json", "{}"))
    risk_state = _compact_supervisor_mapping(_json_dict(_row_get(row, "risk_state_json", "{}")))
    verdict = action_json.get("supervisor_verdict") or {}
    decision_ts = float(_row_get(row, "decision_ts", 0.0) or 0.0)
    return {
        "decision_id": str(_row_get(row, "decision_id", "") or ""),
        "event_type": str(_row_get(row, "event_type", "") or ""),
        "action_reason": str(_row_get(row, "action_reason", "") or ""),
        "decision_ts": decision_ts,
        "seconds_before_close": round(max(0.0, float(close_ts or 0.0) - decision_ts), 3),
        "action": str(verdict.get("action") or "").strip(),
        "summary_reason": str(verdict.get("summary_reason") or _row_get(row, "action_reason", "") or ""),
        "evidence": _compact_supervisor_mapping(verdict.get("evidence")),
        "recommended_controls": _compact_supervisor_mapping(verdict.get("recommended_controls")),
        "risk_state": risk_state,
    }


def normalize_protection_trace_row(row: Any, *, close_ts: float) -> dict[str, Any]:
    if not row:
        return {}
    verdict = _json_dict(_row_get(row, "verdict_json", "{}"))
    risk_state = _compact_supervisor_mapping(_json_dict(_row_get(row, "risk_verdict_json", "{}")))
    execution = _compact_supervisor_mapping(
        _json_dict(_row_get(row, "execution_json", "{}")),
        nested_keys=frozenset({"evidence", "controls"}),
    )
    evidence = _compact_supervisor_mapping(verdict.get("evidence"))
    source = str(evidence.get("protection_source") or "")
    action = str(_row_get(row, "action", "") or "")
    if source == "legacy_awe_trailing":
        event_type = "legacy_awe_trailing"
    elif source == "holding_timeout":
        event_type = "holding_timeout"
    else:
        event_type = f"supervisor_{action}" if action else "position_supervisor_trace"
    event_ts = float(_row_get(row, "event_ts", 0.0) or 0.0)
    return {
        "decision_id": str(_row_get(row, "decision_id", "") or ""),
        "trace_id": str(_row_get(row, "trace_id", "") or ""),
        "event_type": event_type,
        "action_reason": str(_row_get(row, "summary_reason", "") or ""),
        "decision_ts": event_ts,
        "seconds_before_close": round(max(0.0, float(close_ts or 0.0) - event_ts), 3),
        "action": action,
        "summary_reason": str(_row_get(row, "summary_reason", "") or ""),
        "evidence": evidence,
        "recommended_controls": _compact_supervisor_mapping(verdict.get("recommended_controls")),
        "risk_state": risk_state,
        "execution": execution,
        "stage": str(_row_get(row, "stage", "") or ""),
        "outcome": str(_row_get(row, "outcome", "") or ""),
    }


def replay_close_attribution(
    position_id: int,
    *,
    attr_engine: Any,
    close_price: float,
    close_ts: float,
    real_pnl: dict[str, Any] | None,
    recovery_meta: Mapping[str, Any] | None = None,
) -> tuple[dict[str, float], str]:
    """Attribute a replayed close using data that already exists — never a copy.

    Order of evidence:
      1. The in-memory attribution engine still holds this position's open
         context (tick loop opened it and nobody evicted it) → compute the
         marginal contributions at full integrity.
      2. The durable open snapshot stored in ``recovery_meta.trade_attribution``
         (persisted by ``record_open``) is restored into the engine via its own
         ``restore_open`` contract (which self-labels the context ``recovered``)
         → compute at recovered weight.
      3. Neither source has anything → stay conservative: no contributions,
         integrity ``missing``.

    Returns ``(factor_contributions, attribution_integrity)``.  On any engine
    error the result degrades to the conservative outcome rather than blocking
    the replayed close.
    """

    pid = int(position_id)
    if attr_engine is None or close_price <= 0.0:
        return {}, "missing"

    # 1) Live in-memory context.
    try:
        if bool(getattr(attr_engine, "has_open", lambda _p: False)(pid)):
            contributions = attr_engine.record_close(
                pid,
                close_price=close_price,
                close_ts=close_ts,
                real_pnl=real_pnl,
            ) or {}
            if contributions:
                return dict(contributions), "full"
    except Exception:
        pass

    # 2) Durable open snapshot from the position's own recovery metadata.
    meta = dict(recovery_meta or {})
    snapshot = meta.get("trade_attribution")
    restore_open = getattr(attr_engine, "restore_open", None)
    if isinstance(snapshot, dict) and snapshot and callable(restore_open):
        try:
            if restore_open(pid, snapshot):
                contributions = attr_engine.record_close(
                    pid,
                    close_price=close_price,
                    close_ts=close_ts,
                    real_pnl=real_pnl,
                ) or {}
                if contributions:
                    return dict(contributions), "recovered"
        except Exception:
            pass

    return {}, "missing"


def build_replayed_close_payloads(
    *,
    position_id: int,
    position_state: dict[str, Any],
    real_pnl: dict[str, Any] | None,
    strategy_name: str,
    now_ts: float,
    context_integrity_default: str,
    sl_hit_evidence: dict[str, Any] | None = None,
    attr_engine: Any = None,
) -> dict[str, Any]:
    state = position_state or {}
    pnl_payload = real_pnl or {}
    total_pnl = float(pnl_payload.get("net", state.get("close_pnl", 0.0)) or 0.0)
    close_price = trusted_broker_close_price(pnl_payload) or 0.0
    close_ts = float(pnl_payload.get("exec_timestamp", now_ts) or now_ts)
    context_integrity = str(state.get("context_integrity") or context_integrity_default or "")
    symbol = str(state.get("symbol") or "XAUUSD+")
    # A replayed close whose fill provably matched our broker-side stop-loss
    # is the strategy's natural lifecycle (broker stop-out).  The evidence
    # dict cites the durable intent/deal IDs; it never duplicates payloads.
    evidence = dict(sl_hit_evidence or {})
    close_reason = "broker_close" if evidence.get("matched") else "restart_replay"
    # Attribution: prefer the live engine context, then the durable open
    # snapshot already stored in this row's recovery metadata.  Both are
    # references to existing data — the snapshot is passed back through the
    # engine's own restore contract instead of being duplicated here.
    prior_meta = dict(state.get("recovery_meta") or {})
    contributions, attribution_integrity = replay_close_attribution(
        int(position_id),
        attr_engine=attr_engine,
        close_price=close_price,
        close_ts=close_ts,
        real_pnl=pnl_payload,
        recovery_meta=prior_meta,
    )
    replay_meta = {
        "replayed_at": float(now_ts or 0.0),
        "strategy_name": str(strategy_name or ""),
        **({"sl_hit_evidence": evidence} if evidence else {}),
    }
    action_json = {
        "position_id": int(position_id),
        "replayed": True,
        "close_reason": close_reason,
        "real_pnl": pnl_payload,
    }
    event_details = {
        "replayed": True,
        "close_reason": close_reason,
        "real_pnl": pnl_payload,
    }
    return {
        "position_id": int(position_id),
        "symbol": symbol,
        "total_pnl": total_pnl,
        "close_price": close_price,
        "close_ts": close_ts,
        "context_integrity": context_integrity,
        "recovery_meta": replay_meta,
        "decision": {
            "event_type": "close",
            "symbol": symbol,
            "timeframe": "",
            "trade_id": str(position_id),
            "position_id": str(position_id),
            "decision_ts": close_ts,
            "portfolio_state": {},
            "action_score": total_pnl,
            "action_reason": "restart_replay_close",
            "action_json": action_json,
        },
        "position_event": {
            "position_id": str(position_id),
            "trade_id": str(position_id),
            "symbol": symbol,
            "event_type": "closed",
            "avg_price": close_price,
            "realized_pnl": total_pnl,
            "details": event_details,
            "event_ts": close_ts,
        },
        "review": {
            "position_id": str(position_id),
            "pnl": total_pnl,
            "close_price": close_price,
            "close_ts": close_ts,
            "contributions": contributions,
            "attribution_integrity": attribution_integrity,
            "real_pnl": real_pnl,
            "close_reason": close_reason,
            "context_integrity": context_integrity,
        },
    }


def classify_close_source_from_evidence(
    *,
    close_reason: str,
    evidence: dict[str, Any] | None,
) -> dict[str, Any]:
    reason = str(close_reason or "")
    latest = _compact_supervisor_mapping(
        evidence,
        nested_keys=frozenset({"evidence", "recommended_controls", "execution", "risk_state"}),
    )
    source = "external_broker_close" if reason == "broker_close" else reason
    if reason == "restart_replay":
        source = "restart_replay"
    elif latest:
        event_type = str(latest.get("event_type") or "")
        if reason not in {"broker_close", "restart_replay"} and event_type == "supervisor_close":
            source = "supervisor_direct_close"
        elif reason == "broker_close" and event_type == "supervisor_tighten":
            source = "supervisor_tighten_stopout"
        elif reason == "broker_close" and event_type == "supervisor_reduce":
            source = "supervisor_reduce_partial_or_stopout"
        elif reason == "broker_close" and event_type == "supervisor_close":
            source = "supervisor_direct_close"
        elif reason == "broker_close" and event_type == "legacy_awe_trailing":
            source = "legacy_awe_trailing_stopout"
        elif reason == "broker_close" and event_type == "holding_timeout":
            source = "holding_timeout"
    return {
        "close_reason_source": source,
        "inferred_close_supervisor": latest,
    }


def build_close_position_risk_context_payload(
    *,
    position_id: int,
    close_reason: str,
    mode: str,
    broker: str,
    symbol: str,
    entry_ts: float,
    entry_ts_source: str,
    temporal_context: dict[str, Any],
    max_holding_bars: int,
    broker_schedule: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = float((temporal_context or {}).get("decision_ts") or 0.0)
    entry_timestamp = float(entry_ts or 0.0)
    timeframe_seconds = int((temporal_context or {}).get("timeframe_seconds", 0) or 0)
    max_bars = int(max_holding_bars or 0)
    max_holding_seconds = float(max_bars * timeframe_seconds) if max_bars > 0 and timeframe_seconds > 0 else 0.0
    entry_timestamp_known = entry_timestamp > 0
    # A missing broker/open-ledger timestamp is not a newborn position.  Use
    # the configured timeout boundary as a finite, JSON-safe representation
    # of infinite staleness so the close-only timeout path fails closed while
    # exposing that the elapsed value itself is unknown.
    holding_seconds = (
        max(0.0, now - entry_timestamp)
        if entry_timestamp_known
        else max_holding_seconds if max_holding_seconds > 0 else 0.0
    )
    market_budget: dict[str, Any] | None = None
    if entry_timestamp_known and max_holding_seconds > 0:
        market_budget = build_holding_timeout_market_budget(
            entry_ts=entry_timestamp,
            now_ts=now,
            max_holding_seconds=max_holding_seconds,
            symbol=symbol or "XAUUSD+",
            broker_schedule=broker_schedule,
        )
    return {
        "position_id": str(position_id),
        "close_reason": close_reason,
        "mode": mode,
        "broker": broker,
        "symbol": symbol,
        "entry_ts": entry_timestamp,
        "entry_ts_source": str(entry_ts_source or ""),
        "entry_ts_state": "known" if entry_timestamp_known else "unknown",
        "holding_seconds": holding_seconds,
        "holding_seconds_state": (
            "known" if entry_timestamp_known else "unknown_infinite_stale"
        ),
        "holding_timeout_fail_closed": bool(
            not entry_timestamp_known and max_holding_seconds > 0
        ),
        "timeframe_seconds": timeframe_seconds,
        "max_holding_bars": max_bars,
        "max_holding_seconds": max_holding_seconds,
        "market_time_budget": market_budget or {},
        "temporal_context": temporal_context,
    }


def build_holding_summary_from_close_context(close_context: dict[str, Any]) -> dict[str, Any]:
    holding_seconds = float(close_context.get("holding_seconds", 0.0) or 0.0)
    max_holding_seconds = float(close_context.get("max_holding_seconds", 0.0) or 0.0)
    holding_seconds_state = str(
        close_context.get("holding_seconds_state") or "known"
    )
    unknown_timestamp = holding_seconds_state != "known"
    timeout_enabled = bool(max_holding_seconds > 0)
    market_budget = dict(close_context.get("market_time_budget") or {})
    market_open_holding = float(
        market_budget.get("market_open_holding_seconds", holding_seconds) or 0.0
    )
    if timeout_enabled and max_holding_seconds > 0 and not unknown_timestamp:
        timeout_ratio = market_open_holding / max_holding_seconds
        timeout_exceeded = (
            bool(market_budget.get("timeout_on_market_time"))
            if "timeout_on_market_time" in market_budget
            else market_open_holding >= max_holding_seconds
        )
    else:
        timeout_ratio = (
            holding_seconds / max_holding_seconds
            if timeout_enabled and max_holding_seconds > 0
            else 0.0
        )
        timeout_exceeded = bool(
            max_holding_seconds > 0
            and (unknown_timestamp or holding_seconds >= max_holding_seconds)
        )
    if not timeout_enabled:
        timeout_status = "disabled"
    elif unknown_timestamp:
        timeout_status = "expired_unknown_timestamp"
    elif timeout_exceeded:
        timeout_status = "expired"
    elif timeout_ratio >= 0.8:
        timeout_status = "watch"
    else:
        timeout_status = "normal"
    remaining_seconds = (
        max(0.0, max_holding_seconds - market_open_holding)
        if timeout_enabled and not unknown_timestamp
        else 0.0
    )
    return {
        "holding_seconds": round(holding_seconds, 3),
        "holding_seconds_state": holding_seconds_state,
        "holding_minutes": round(holding_seconds / 60.0, 2) if holding_seconds > 0 else 0.0,
        "timeout_enabled": timeout_enabled,
        "max_holding_bars": int(close_context.get("max_holding_bars", 0) or 0),
        "max_holding_seconds": round(max_holding_seconds, 3) if max_holding_seconds > 0 else 0.0,
        "holding_timeout_exceeded": bool(timeout_exceeded),
        "holding_timeout_fail_closed": bool(
            timeout_enabled and unknown_timestamp
        ),
        "holding_timeout_ratio": round(timeout_ratio, 4) if timeout_enabled else 0.0,
        "holding_timeout_status": timeout_status,
        "holding_timeout_remaining_seconds": round(remaining_seconds, 3) if timeout_enabled else 0.0,
        "market_open_holding_seconds": round(market_open_holding, 3)
        if market_budget
        else None,
        "market_time_budget": market_budget,
    }


def holding_timeout_is_expired(close_context: dict[str, Any]) -> bool:
    max_holding_seconds = float((close_context or {}).get("max_holding_seconds", 0.0) or 0.0)
    holding_seconds = float((close_context or {}).get("holding_seconds", 0.0) or 0.0)
    holding_seconds_state = str(
        (close_context or {}).get("holding_seconds_state") or "known"
    )
    if max_holding_seconds <= 0:
        return False
    if holding_seconds_state != "known":
        return True
    market_budget = close_context.get("market_time_budget") or {}
    if isinstance(market_budget, dict) and market_budget:
        if "timeout_on_market_time" in market_budget:
            return bool(market_budget.get("timeout_on_market_time"))
        try:
            return float(market_budget.get("market_open_holding_seconds", 0.0) or 0.0) >= max_holding_seconds
        except (TypeError, ValueError):
            return False
    return holding_seconds >= max_holding_seconds


def build_holding_timeout_market_budget(
    *,
    entry_ts: float,
    now_ts: float,
    max_holding_seconds: float,
    symbol: str = "XAUUSD+",
    broker_schedule: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Market-open timeout accounting for one evaluation.

    ``market_holding_seconds`` counts only tradable time since entry;
    ``market_closed_pending`` flags that the wall-clock holding time has
    crossed the limit purely because the market was closed for part of it.
    """

    entry = float(entry_ts or 0.0)
    now = float(now_ts or 0.0)
    wall_holding = max(0.0, now - entry) if entry > 0 else 0.0
    open_holding, schedule_source = (
        market_open_seconds_between_with_source(
            entry,
            now,
            symbol=symbol or "XAUUSD+",
            broker_schedule=broker_schedule,
        )
        if entry > 0 and now > entry
        else (0.0, "config")
    )
    closed_in_window = bool(
        entry > 0
        and now > entry
        and open_holding < (now - entry)
    )
    timed_out_wall = bool(max_holding_seconds > 0 and wall_holding >= max_holding_seconds)
    timed_out_open = bool(max_holding_seconds > 0 and open_holding >= max_holding_seconds)
    return {
        "wall_clock_holding_seconds": round(wall_holding, 3),
        "market_open_holding_seconds": round(open_holding, 3),
        "market_closed_in_window": closed_in_window,
        "market_closed_pending": timed_out_wall and not timed_out_open,
        "timeout_on_market_time": timed_out_open,
        "schedule_source": schedule_source,
    }


def build_holding_timeout_verdict_payload(
    *,
    position_id: int,
    decision_ts: float,
    holding_seconds: float,
    max_holding_seconds: float,
) -> dict[str, Any]:
    return {
        "position_id": str(position_id),
        "decision_ts": float(decision_ts or 0.0),
        "action": "close",
        "confidence": 1.0,
        "severity": "warn",
        "summary_reason": "holding_timeout",
        "human_summary": "holding timeout exceeded",
        "evidence": {
            "holding_seconds": float(holding_seconds or 0.0),
            "max_holding_seconds": float(max_holding_seconds or 0.0),
            "protection_source": "holding_timeout",
        },
        "recommended_controls": {
            "close_reason": "holding_timeout",
            "protection_mode": "full_exit",
        },
        "supervisor_template": {},
    }


def build_holding_timeout_trace_fields(
    *,
    stage: str,
    outcome: str,
    decision_id: str,
    risk_verdict: dict[str, Any] | None,
    execution_status: str,
    execution_reason: str,
    execution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "stage": str(stage or ""),
        "outcome": str(outcome or ""),
        "decision_id": str(decision_id or ""),
        "risk_action": "close_position",
        "risk_verdict": risk_verdict or {},
        "execution_status": str(execution_status or ""),
        "execution_reason": str(execution_reason or ""),
        "execution": execution or {},
    }


def build_holding_timeout_result_trace_fields(
    *,
    result: str,
    decision_id: str,
    risk_verdict: dict[str, Any] | None,
    execution_reason: str = "",
) -> dict[str, Any]:
    if result == "risk_rejected":
        return build_holding_timeout_trace_fields(
            stage="risk_rejected",
            outcome="blocked",
            decision_id=decision_id,
            risk_verdict=risk_verdict,
            execution_status="blocked",
            execution_reason=execution_reason,
        )
    if result == "exception":
        return build_holding_timeout_trace_fields(
            stage="exception",
            outcome="failed",
            decision_id=decision_id,
            risk_verdict=risk_verdict,
            execution_status="exception",
            execution_reason=execution_reason,
        )
    if result == "applied":
        return build_holding_timeout_trace_fields(
            stage="protection_arbitrated",
            outcome="applied",
            decision_id=decision_id,
            risk_verdict=risk_verdict,
            execution_status="applied",
            execution_reason="close_position_success",
            execution={"close_reason_source": "holding_timeout"},
        )
    return build_holding_timeout_trace_fields(
        stage="execution_failed",
        outcome="failed",
        decision_id=decision_id,
        risk_verdict=risk_verdict,
        execution_status="failed",
        execution_reason=execution_reason or "close_failed",
    )


def build_position_supervisor_context_inputs(
    *,
    position: dict[str, Any],
    cfg: Any = None,
    positions: list[Any] | None = None,
    account: dict[str, Any] | None = None,
    entry_decision_id: str = "",
    risk_snapshot: dict[str, Any] | None = None,
    market_context: dict[str, Any] | None = None,
    supervisor_state: dict[str, Any] | None = None,
    total_api_volume: float = 0.0,
    loop_running: bool = True,
) -> dict[str, Any]:
    return {
        "position": position,
        "entry_decision_id": str(entry_decision_id or ""),
        "risk_snapshot": risk_snapshot or {},
        "market_context": market_context or {},
        "supervisor_state": supervisor_state or {},
        "max_holding_bars": int(getattr(cfg, "risk_max_holding_bars", 0) or 0) if cfg else 0,
        "open_position_count": len(positions or []),
        "total_api_volume": float(total_api_volume or 0.0),
        "account": account,
        "template_id": str(getattr(cfg, "position_supervisor_template_id", "") or ""),
        "loop_running": bool(loop_running),
    }


def build_position_supervisor_context_payload(
    *,
    position: dict[str, Any],
    temporal_context: dict[str, Any],
    position_metrics: dict[str, Any],
    entry_decision_id: str,
    risk_snapshot: dict[str, Any],
    market_context: dict[str, Any] | None = None,
    supervisor_state: dict[str, Any] | None = None,
    max_holding_bars: int,
    open_position_count: int,
    total_api_volume: float,
    account: dict[str, Any] | None,
    template_id: str,
    loop_running: bool,
) -> dict[str, Any]:
    current_price = float(position.get("current_price", position.get("price_current", 0.0)) or 0.0)
    stop_loss = float(position.get("sl", 0.0) or 0.0)
    take_profit = float(position.get("tp", 0.0) or 0.0)
    holding_timeout_ratio = float(position.get("holding_timeout_ratio", 0.0) or 0.0)
    timeframe_seconds = int(temporal_context.get("timeframe_seconds", 0) or 0)
    holding_seconds = float(temporal_context.get("holding_seconds", 0.0) or 0.0)
    market_budget = dict(temporal_context.get("market_time_budget") or {})
    market_open_holding = float(
        market_budget.get("market_open_holding_seconds", holding_seconds) or 0.0
    )
    max_holding_seconds = float(position.get("max_holding_seconds", 0.0) or 0.0)
    if max_holding_seconds > 0:
        if market_budget and str(temporal_context.get("holding_seconds_state") or "known") != "unknown_infinite_stale":
            holding_timeout_ratio = market_open_holding / max_holding_seconds
            holding_timeout_exceeded = (
                bool(market_budget.get("timeout_on_market_time"))
                if "timeout_on_market_time" in market_budget
                else market_open_holding >= max_holding_seconds
            )
        else:
            holding_timeout_exceeded = holding_seconds >= max_holding_seconds
    else:
        holding_timeout_exceeded = False
    raw_market_context = dict(market_context or {})
    context_state = raw_market_context.get("context_state")
    if not isinstance(context_state, dict):
        context_state = {}
    # Signal-reversal producer (2026-08-26): compare the latest committed
    # factor-composite direction with the position's own direction.  The
    # supervisor previously read this flag without any writer, so the
    # signal_reversal evidence family could never fire.
    entry_direction = int(position.get("direction", 0) or 0)
    composite_direction = int(raw_market_context.get("direction", 0) or 0)
    signal_reversal = bool(
        entry_direction != 0
        and composite_direction != 0
        and composite_direction != entry_direction
    )
    resolved_regime = resolve_market_regime(raw_market_context)
    regime_id = str(
        resolved_regime.get("regime_id")
        or position_metrics.get("current_regime")
        or ""
    )
    regime_dimensions = dict(resolved_regime.get("dimensions") or {})
    trend_strength_state = str(context_state.get("trend_strength_state") or "unknown")
    volatility_state = str(context_state.get("volatility_state") or "unknown")
    event_window_state = str(context_state.get("event_window_state") or "unknown")
    session_state = str(context_state.get("session_state") or "unknown")
    known_dimension_count = sum(
        state not in {"", "unknown", "none"}
        for state in (trend_strength_state, volatility_state)
    )
    market_dimensions_known = known_dimension_count == 2
    market_context_state = (
        "known" if known_dimension_count == 2
        else "partial" if known_dimension_count == 1
        else "unknown"
    )
    # ``atr_ratio`` in the compositor is a normalized signal, not an ATR
    # price.  Do not convert it into a price-space multiple here.  A future
    # canonical factor frame may provide a real ATR price explicitly.
    atr_multiple_from_entry = raw_market_context.get("atr_multiple_from_entry")
    if atr_multiple_from_entry in ("", 0.0):
        atr_multiple_from_entry = None
    market_space_context = {
        "distance_to_sl": round(abs(current_price - stop_loss), 6) if stop_loss > 0 else 0.0,
        "distance_to_tp": round(abs(take_profit - current_price), 6) if take_profit > 0 else 0.0,
        "atr_multiple_from_entry": atr_multiple_from_entry,
        "range_location": raw_market_context.get("range_location"),
        "structure_bias": raw_market_context.get("structure_bias"),
        "state": market_context_state,
    }
    entry_ctx = {
        "entry_decision_id": entry_decision_id,
        "entry_score": 0.0,
        "entry_reason": "",
        "factor_set_version": "",
        "policy_version": "",
        "expected_holding_profile": "",
        "entry_regime": position_metrics.get("entry_regime", ""),
        "entry_regime_confidence": 0.0,
    }
    risk_context = {
        "risk_snapshot": risk_snapshot or {},
        "policy_state": {},
        "max_holding_bars": int(max_holding_bars or 0),
        "max_holding_seconds": max_holding_seconds,
        "open_position_count": int(open_position_count or 0),
        "total_api_volume": total_api_volume,
        **position_metrics,
        "holding_timeout_ratio": holding_timeout_ratio,
        "holding_timeout_exceeded": bool(holding_timeout_exceeded),
        # Producer for the supervisor's signal_reversal evidence family.
        "signal_reversal": signal_reversal,
        "market_open_holding_seconds": round(market_open_holding, 3)
        if market_budget
        else None,
        "market_time_budget": market_budget,
        # Canonical market facts are written after the path-metric projection:
        # an empty/legacy metric field must not erase a real regime fact.
        "current_regime": str(position_metrics.get("current_regime") or regime_id),
        "entry_regime": str(position_metrics.get("entry_regime") or ""),
        "regime_confidence": float(resolved_regime.get("confidence") or 0.0),
        "regime_source": str(resolved_regime.get("source") or "unavailable"),
        # Recovery metadata is fed back into the next supervisor evaluation.
        # Keep only the bounded state projection here; carrying a previous
        # verdict's evidence would reintroduce the position/supervisor graph
        # into the next verdict.
        "supervisor_state": _compact_supervisor_mapping(
            supervisor_state,
            nested_keys=frozenset({"latest_supervisor", "latest_protection"}),
        ),
    }
    return {
        "position_supervisor_template": str(template_id or ""),
        "position": {
            "position_id": position.get("position_id") or position.get("ticket"),
            "trade_id": str(position.get("position_id") or position.get("ticket") or ""),
            "symbol": str(position.get("symbol") or "XAUUSD+"),
            "direction": int(position.get("direction", 0) or 0),
            "entry_price": float(
                position.get("entry_price", position.get("open_price", position.get("price_open", 0.0))) or 0.0
            ),
            "current_price": current_price,
            "volume": float(position.get("volume", position.get("api_volume", 0.0)) or 0.0),
            "opened_at": float(position.get("open_time", 0.0) or 0.0),
            "unrealized_pnl": float(position.get("profit", position.get("pnl", 0.0)) or 0.0),
            "realized_pnl": 0.0,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "type": str(position.get("type") or ""),
            "sl": stop_loss,
            "tp": take_profit,
        },
        "market": {
            "bid": current_price,
            "ask": current_price,
            "mid": current_price,
            "spread": 0.0,
            "timeframe": str(
                temporal_context.get("temporal_context", {}).get("timeframe", "")
                or temporal_context.get("timeframe", "")
            ),
            "timeframe_seconds": timeframe_seconds,
            "regime_state": regime_id,
            "regime_id": regime_id,
            "regime_confidence": float(resolved_regime.get("confidence") or 0.0),
            "regime_source": str(resolved_regime.get("source") or "unavailable"),
            "regime_dimensions": regime_dimensions,
            "trend_strength_state": trend_strength_state,
            "trend_strength_score": context_state.get("trend_strength_score"),
            "volatility_state": volatility_state,
            "volatility_score": context_state.get("volatility_score"),
            "event_window_state": event_window_state,
            "event_window_score": context_state.get("event_window_score"),
            "session_state": session_state,
            "market_context_state": market_context_state,
        },
        "risk": risk_context,
        "temporal_context": {
            **(temporal_context.get("temporal_context") or {}),
            "holding_seconds": temporal_context.get("holding_seconds", 0.0),
            "holding_minutes": round(holding_seconds / 60.0, 3),
            "holding_bars": round(holding_seconds / max(timeframe_seconds or 1, 1), 3)
            if timeframe_seconds > 0
            else 0.0,
        },
        "market_space_context": market_space_context,
        "entry_context": entry_ctx,
        "runtime": {
            "loop_running": bool(loop_running),
            "bridge_connected": True,
            "data_quality_state": "",
            "runtime_health": {},
            "account": account or {},
        },
    }


def build_position_path_metrics_result(
    *,
    metrics: dict[str, Any],
    entry_regime: str,
    current_regime: str,
    entry_ts_source: str,
    original_stop_loss: float = 0.0,
    original_take_profit: float = 0.0,
) -> dict[str, Any]:
    return {
        **metrics,
        "position_path_metrics_state": "known",
        "time_in_profit": round(float(metrics["time_in_profit_seconds"]), 6),
        "entry_regime": str(entry_regime or ""),
        "current_regime": str(current_regime or ""),
        "entry_ts_source": str(entry_ts_source or ""),
        "original_stop_loss": round(float(original_stop_loss or 0.0), 6),
        "original_take_profit": round(float(original_take_profit or 0.0), 6),
    }


def build_position_path_recovery_meta(
    *,
    recovery_meta: dict[str, Any] | None,
    next_state: dict[str, Any],
    entry_regime: str,
    current_regime: str,
) -> dict[str, Any]:
    next_meta = dict(recovery_meta or {})
    next_meta["position_path"] = next_state
    if entry_regime:
        next_meta["entry_regime"] = entry_regime
    if current_regime:
        next_meta["current_regime"] = current_regime
    return next_meta


def build_position_path_metrics_update(
    *,
    recovery_meta: dict[str, Any] | None,
    entry_context: dict[str, Any] | None,
    current_pnl: float,
    now_ts: float,
    holding_seconds: float,
    max_holding_seconds: float,
    current_regime: str,
    normalize_path_state_fn: Callable[[Any], dict[str, Any]],
    update_position_path_metrics_fn: Callable[..., tuple[dict[str, Any], dict[str, Any]]],
) -> dict[str, Any]:
    meta = dict(recovery_meta or {})
    entry_regime = str(meta.get("entry_regime") or "")
    entry_protection_plan = dict(meta.get("entry_protection_plan") or {})
    next_state, metrics = update_position_path_metrics_fn(
        previous_state=normalize_path_state_fn(meta.get("position_path")),
        current_pnl=float(current_pnl or 0.0),
        now_ts=float(now_ts or 0.0),
        holding_seconds=float(holding_seconds or 0.0),
        max_holding_seconds=float(max_holding_seconds or 0.0),
        entry_regime=entry_regime,
        current_regime=str(current_regime or ""),
    )
    result = build_position_path_metrics_result(
        metrics=metrics,
        entry_regime=entry_regime,
        current_regime=str(current_regime or ""),
        entry_ts_source=str((entry_context or {}).get("source") or ""),
        original_stop_loss=float(
            entry_protection_plan.get("target_stop_loss") or 0.0
        ),
        original_take_profit=float(
            entry_protection_plan.get("target_take_profit") or 0.0
        ),
    )
    next_meta = build_position_path_recovery_meta(
        recovery_meta=meta,
        next_state=next_state,
        entry_regime=entry_regime,
        current_regime=str(current_regime or ""),
    )
    return {
        "result": result,
        "next_meta": next_meta,
        "next_state": next_state,
        "metrics": metrics,
        "entry_regime": entry_regime,
        "current_regime": str(current_regime or ""),
    }


def build_position_path_metrics_inputs(
    *,
    position: Any,
    recovery_row: dict[str, Any] | None,
    entry_context: dict[str, Any] | None,
    holding_summary: dict[str, Any] | None,
    current_regime: str,
    current_pnl: float,
    now_ts: float,
    broker: str,
    strategy_name: str,
    loop_strategy_name: str,
    default_context_integrity: str,
) -> dict[str, Any]:
    row = recovery_row or {}
    holding = holding_summary or {}
    return {
        "position_id": position_id_value(position),
        "recovery_meta": row.get("recovery_meta") or {},
        "entry_context": entry_context or {},
        "current_pnl": float(current_pnl or 0.0),
        "now_ts": float(now_ts or 0.0),
        "holding_seconds": float(holding.get("holding_seconds", 0.0) or 0.0),
        "max_holding_seconds": float(holding.get("max_holding_seconds", 0.0) or 0.0),
        "current_regime": str(current_regime or ""),
        "upsert_defaults": build_recovery_upsert_defaults(
            recovery_row=row,
            broker=broker,
            strategy_name=strategy_name,
            loop_strategy_name=loop_strategy_name,
            default_context_integrity=default_context_integrity,
            meta={},
        ),
    }


def build_protection_candidate_verdict_payload(
    *,
    position_id: int,
    decision_ts: float,
    action: str,
    confidence: float,
    reason: str,
    source: str,
    evidence: dict[str, Any] | None,
    controls: dict[str, Any] | None,
    config_version: int,
    config_hash: str,
    position_side: str,
) -> dict[str, Any]:
    evidence_payload = dict(evidence or {})
    supervisor_template = evidence_payload.get("supervisor_template") or {}
    summary_reason = reason or source
    return {
        "position_id": str(position_id),
        "decision_ts": float(decision_ts or 0.0),
        "action": action,
        "confidence": float(confidence or 0.0),
        "severity": "warn",
        "thesis_status": "",
        "regime_shift": "",
        "summary_reason": summary_reason,
        "human_summary": summary_reason,
        "evidence": {
            **evidence_payload,
            "protection_source": source,
            "config_version": int(config_version or 0),
            "config_hash": str(config_hash or ""),
        },
        "recommended_controls": controls or {},
        "supervisor_template": {
            "schema_version": str(supervisor_template.get("schema_version") or ""),
            "template_id": str(supervisor_template.get("template_id") or ""),
            "template_version": str(supervisor_template.get("template_version") or ""),
            "template_role": str(supervisor_template.get("template_role") or ""),
            "thresholds": supervisor_template.get("thresholds") or {},
            "sl_policy": supervisor_template.get("sl_policy") or {},
            "tp_policy": supervisor_template.get("tp_policy") or {},
            "capture_policy": supervisor_template.get("capture_policy") or {},
        },
        "requires_risk_verdict": True,
        "action_label": "收紧保护" if action == "tighten" else action,
        "position_side": str(position_side or ""),
    }


def build_protection_superseded_trace_fields(
    *,
    candidate_payload: dict[str, Any],
    risk_action: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "stage": "protection_superseded",
        "outcome": "superseded",
        "risk_action": str(risk_action or ""),
        "execution_status": "superseded",
        "execution_reason": str(reason or ""),
        "execution": {
            "candidate": candidate_payload or {},
            "superseded_by": str(reason or ""),
        },
    }


def build_protection_execution_trace_fields(
    *,
    stage: str,
    outcome: str,
    decision_id: str,
    risk_action: str,
    risk_verdict: dict[str, Any] | None,
    execution_status: str,
    execution_reason: str,
    execution: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "stage": str(stage or ""),
        "outcome": str(outcome or ""),
        "decision_id": str(decision_id or ""),
        "risk_action": str(risk_action or ""),
        "risk_verdict": risk_verdict or {},
        "execution_status": str(execution_status or ""),
        "execution_reason": str(execution_reason or ""),
        "execution": execution or {},
    }


def build_protection_position_event_details(
    *,
    event_type: str,
    source: str,
    action: str,
    reason: str,
    risk_verdict_reason: str,
    sl_plan: dict[str, Any] | None,
    controls: dict[str, Any] | None,
    failure_reason: str = "",
    target_stop_loss_original: float = 0.0,
    target_stop_loss_sent: float = 0.0,
    target_take_profit_sent: float = 0.0,
) -> dict[str, Any]:
    base = {
        "protection_source": str(source or ""),
        "supervisor_action": str(action or ""),
        "supervisor_reason": str(reason or ""),
        "risk_verdict_reason": risk_verdict_reason,
    }
    if event_type == "amend_skipped":
        return {
            **base,
            "skip_stage": "protection_arbitrated",
            "skip_reason": (sl_plan or {}).get("reason"),
            "sl_plan": sl_plan or {},
            "applied_controls": controls or {},
        }
    if event_type == "tightened":
        return {
            **base,
            "close_reason_source": str(source or ""),
            "applied_controls": {
                **(controls or {}),
                "target_stop_loss_original": target_stop_loss_original,
                "target_stop_loss_sent": target_stop_loss_sent,
                "target_take_profit_sent": target_take_profit_sent,
                "sl_plan": sl_plan or {},
            },
        }
    if event_type == "amend_failed":
        return {
            **base,
            "failure_stage": "protection_arbitrated",
            "failure_reason": str(failure_reason or ""),
            "sl_plan": sl_plan or {},
            "applied_controls": controls or {},
        }
    return {
        **base,
        "sl_plan": sl_plan or {},
        "applied_controls": controls or {},
    }


def build_protection_execution_result_payloads(
    *,
    result: str,
    source: str,
    action: str,
    reason: str,
    risk_action: str,
    risk_verdict: dict[str, Any] | None,
    decision_id: str,
    candidate_payload: dict[str, Any],
    sl_plan: dict[str, Any] | None = None,
    controls: dict[str, Any] | None = None,
    failure_reason: str = "",
    target_stop_loss_original: float = 0.0,
    target_stop_loss_sent: float = 0.0,
    target_take_profit_sent: float = 0.0,
) -> dict[str, Any]:
    risk_payload = risk_verdict or {}
    risk_reason = str(risk_payload.get("reason") or "")
    if result == "risk_rejected":
        return {
            "position_event_type": "",
            "position_event_details": {},
            "trace_fields": build_protection_execution_trace_fields(
                stage="risk_rejected",
                outcome="blocked",
                decision_id=decision_id,
                risk_action=risk_action,
                risk_verdict=risk_payload,
                execution_status="blocked",
                execution_reason=risk_reason,
                execution={"candidate": candidate_payload},
            ),
        }
    if result == "skipped":
        plan = sl_plan or {}
        return {
            "position_event_type": "amend_skipped",
            "position_event_details": build_protection_position_event_details(
                event_type="amend_skipped",
                source=source,
                action=action,
                reason=reason,
                risk_verdict_reason=risk_reason,
                sl_plan=plan,
                controls=controls,
            ),
            "trace_fields": build_protection_execution_trace_fields(
                stage="execution_skipped",
                outcome="skipped",
                decision_id=decision_id,
                risk_action=risk_action,
                risk_verdict=risk_payload,
                execution_status="skipped",
                execution_reason=str(plan.get("reason") or ""),
                execution={"sl_plan": plan, "candidate": candidate_payload},
            ),
        }
    if result == "applied":
        plan = sl_plan or {}
        return {
            "position_event_type": "tightened",
            "position_event_details": build_protection_position_event_details(
                event_type="tightened",
                source=source,
                action=action,
                reason=reason,
                risk_verdict_reason=risk_reason,
                sl_plan=plan,
                controls=controls,
                target_stop_loss_original=target_stop_loss_original,
                target_stop_loss_sent=target_stop_loss_sent,
                target_take_profit_sent=target_take_profit_sent,
            ),
            "trace_fields": build_protection_execution_trace_fields(
                stage="protection_arbitrated",
                outcome="applied",
                decision_id=decision_id,
                risk_action=risk_action,
                risk_verdict=risk_payload,
                execution_status="applied",
                execution_reason="amend_position_sltp_success",
                execution={
                    "target_stop_loss_sent": target_stop_loss_sent,
                    "target_take_profit_sent": target_take_profit_sent,
                    "sl_plan": plan,
                    "candidate": candidate_payload,
                },
            ),
        }
    plan = sl_plan or {}
    fail_reason = str(failure_reason or "amend_failed")
    return {
        "position_event_type": "amend_failed",
        "position_event_details": build_protection_position_event_details(
            event_type="amend_failed",
            source=source,
            action=action,
            reason=reason,
            risk_verdict_reason=risk_reason,
            sl_plan=plan,
            controls=controls,
            failure_reason=fail_reason,
        ),
        "trace_fields": build_protection_execution_trace_fields(
            stage="execution_failed",
            outcome="failed",
            decision_id=decision_id,
            risk_action=risk_action,
            risk_verdict=risk_payload,
            execution_status="failed",
            execution_reason=fail_reason,
            execution={"sl_plan": plan, "candidate": candidate_payload},
        ),
    }


def build_supervisor_decision_ledger_payload(
    *,
    position: dict[str, Any],
    verdict: dict[str, Any],
    risk_state: dict[str, Any],
    risk_verdict: dict[str, Any] | None,
    account: dict[str, Any] | None,
    cfg: Any,
    event_type: str,
    tick: int,
    session_pnl: float,
    fallback_decision_ts: float,
) -> dict[str, Any]:
    pid = str(position.get("position_id") or position.get("ticket") or "")
    return {
        "event_type": str(event_type or ""),
        "symbol": str(position.get("symbol") or "XAUUSD+"),
        "timeframe": str(getattr(cfg, "timeframe", "") or ""),
        "trade_id": pid,
        "position_id": pid,
        "decision_ts": float(verdict.get("decision_ts") or fallback_decision_ts or 0.0),
        "portfolio_state": {
            "balance": (account or {}).get("balance", 0.0),
            "equity": (account or {}).get("equity", 0.0),
            "session_pnl": session_pnl,
        },
        "risk_state": risk_state or {},
        "action_score": float(verdict.get("confidence", 0.0) or 0.0),
        "action_reason": str(verdict.get("summary_reason") or event_type),
        "action_json": {
            "tick": tick,
            "supervisor_verdict": verdict,
            "risk_verdict": risk_verdict or {},
        },
    }


def build_supervisor_position_event_payload(
    *,
    position: dict[str, Any],
    event_type: str,
    details: dict[str, Any],
    realized_pnl: float = 0.0,
) -> dict[str, Any]:
    pid = str(position.get("position_id") or position.get("ticket") or "")
    return {
        "position_id": pid,
        "trade_id": pid,
        "symbol": str(position.get("symbol") or "XAUUSD+"),
        "event_type": str(event_type or ""),
        "net_volume": float(position.get("volume", 0.0) or 0.0),
        "avg_price": float(position.get("current_price", position.get("price_current", 0.0)) or 0.0),
        "realized_pnl": float(realized_pnl or 0.0),
        "details": details or {},
    }


def build_supervisor_trace_ledger_payload(
    *,
    position: dict[str, Any],
    verdict: dict[str, Any],
    cfg: Any,
    tick: int,
    stage: str,
    outcome: str,
    decision_id: str = "",
    risk_action: str = "",
    risk_verdict: dict[str, Any] | None = None,
    execution_status: str = "",
    execution_reason: str = "",
    execution: dict[str, Any] | None = None,
    account: dict[str, Any] | None = None,
    fallback_event_ts: float = 0.0,
) -> dict[str, Any]:
    pid = str(position.get("position_id") or position.get("ticket") or "")
    template = verdict.get("supervisor_template") or {}
    risk_payload = risk_verdict or {}
    execution_payload = dict(execution or {})
    is_real_execution = bool(
        str(stage or "") == "executed"
        and str(outcome or "") == "applied"
        and execution_payload.get("is_real_execution") is True
        and execution_payload.get("broker_action_confirmed") is True
        and execution_payload.get("reconcile_confirmed") is True
    )
    explicit_execution_class = str(
        execution_payload.get("execution_class")
        or verdict.get("execution_class")
        or ""
    ).strip().lower()
    if explicit_execution_class == "observed" or str(
        execution_status or ""
    ).strip().lower() == "observation_only":
        execution_class = "observed"
    elif is_real_execution:
        execution_class = "applied"
    elif str(stage or "") == "canary_shadow" or str(execution_status or "") == "shadow_only":
        execution_class = "shadow"
    elif str(outcome or "") == "blocked" or str(execution_status or "") == "blocked":
        execution_class = "blocked"
    elif str(outcome or "") == "superseded" or str(execution_status or "") == "superseded":
        execution_class = "superseded"
    elif str(outcome or "") in {"skipped", "hold"} or str(execution_status or "") in {
        "cooldown",
        "invalid_action",
        "not_required",
        "no_op",
        "skipped",
    }:
        execution_class = "skipped"
    elif str(outcome or "") == "failed" or str(execution_status or "") in {"exception", "failed"}:
        execution_class = "failed"
    else:
        execution_class = "observed"
    requested_action = str(
        verdict.get("requested_action") or verdict.get("action") or ""
    )
    effective_action = str(
        verdict.get("effective_action") or verdict.get("action") or ""
    )
    execution_payload.update(
        {
            "execution_class": execution_class,
            "is_real_execution": is_real_execution,
            "requested_action": requested_action,
            "effective_action": effective_action,
            "recommended_action": requested_action,
        }
    )
    return {
        "position_id": pid,
        "decision_id": str(decision_id or ""),
        "trade_id": pid,
        "symbol": str(position.get("symbol") or "XAUUSD+"),
        "timeframe": str(getattr(cfg, "timeframe", "") or ""),
        "tick": int(tick or 0),
        "event_ts": float(verdict.get("decision_ts") or fallback_event_ts or 0.0),
        "action": str(verdict.get("action") or ""),
        "summary_reason": str(verdict.get("summary_reason") or ""),
        "confidence": float(verdict.get("confidence", 0.0) or 0.0),
        "template_id": str(template.get("template_id") or ""),
        "template_version": str(template.get("template_version") or ""),
        "stage": str(stage or ""),
        "outcome": str(outcome or ""),
        "risk_action": str(risk_action or ""),
        "risk_allowed": bool(risk_payload.get("allowed", False)),
        "risk_reason": str(risk_payload.get("reason") or ""),
        "execution_status": str(execution_status or ""),
        "execution_reason": str(execution_reason or ""),
        "context": {
            "schema_version": "position_supervisor_trace_context.v1",
            "position": {
                "position_id": pid,
                "symbol": str(position.get("symbol") or "XAUUSD+"),
                "direction": int(position.get("direction", 0) or 0),
                "entry_price": float(position.get("entry_price", position.get("open_price", position.get("price_open", 0.0))) or 0.0),
                "current_price": float(position.get("current_price", position.get("price_current", 0.0)) or 0.0),
                "volume": float(position.get("volume", position.get("api_volume", 0.0)) or 0.0),
                "sl": float(position.get("sl", 0.0) or 0.0),
                "tp": float(position.get("tp", 0.0) or 0.0),
                "pnl": float(position.get("profit", position.get("pnl", 0.0)) or 0.0),
            },
            "account": {
                "equity": float((account or {}).get("equity", 0.0) or 0.0),
                "balance": float((account or {}).get("balance", 0.0) or 0.0),
            },
            "tick": int(tick or 0),
        },
        "verdict": _compact_supervisor_mapping(
            verdict,
            nested_keys=frozenset({"evidence", "recommended_controls", "supervisor_template"}),
        ),
        "risk_verdict": risk_payload,
        "execution": _compact_supervisor_mapping(
            execution_payload,
            nested_keys=frozenset({"evidence", "controls"}),
        ),
    }


def build_supervisor_action_fingerprint(
    *,
    position_id: int,
    action: str,
    direction: int,
    controls: dict[str, Any] | None,
) -> str:
    """Return a stable fingerprint for a broker-facing supervisor target."""
    controls = controls or {}
    payload = {
        "position_id": int(position_id or 0),
        "action": str(action or ""),
        "direction": int(direction or 0),
        "target_stop_loss": round(float(controls.get("target_stop_loss", 0.0) or 0.0), 8),
        "target_take_profit": round(float(controls.get("target_take_profit", 0.0) or 0.0), 8),
        "reduce_fraction": round(float(controls.get("reduce_fraction", 0.0) or 0.0), 8),
    }
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def supervisor_noop_fingerprint_seen(
    *,
    recovery_meta: dict[str, Any] | None,
    fingerprint: str,
) -> bool:
    return bool(fingerprint) and str((recovery_meta or {}).get("last_supervisor_noop_fingerprint") or "") == str(fingerprint)


def _recovery_row_value(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    if isinstance(row, Mapping):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:
        return getattr(row, key, default)


def build_recovered_open_ledger_payloads(
    *,
    position_id: int,
    recovery_row: Any,
    broker: str,
    close_ts: float,
    close_price: float,
    risk_state: dict[str, Any] | None,
    real_pnl: dict[str, Any] | None = None,
    close_reason: str = "broker_close",
    fallback_strategy_name: str = "factor_v4",
    context_integrity_default: str = "partial",
    fallback_now_ts: float = 0.0,
) -> dict[str, Any]:
    pnl = real_pnl or {}
    open_price = float(
        _recovery_row_value(recovery_row, "open_price")
        or pnl.get("entry_price")
        or close_price
        or 0.0
    )
    volume = float(_recovery_row_value(recovery_row, "volume") or 0.0)
    direction = int(_recovery_row_value(recovery_row, "direction") or 0)
    symbol = str(_recovery_row_value(recovery_row, "symbol") or "XAUUSD+")
    strategy_name = str(
        _recovery_row_value(recovery_row, "strategy_name")
        or fallback_strategy_name
        or "factor_v4"
    )
    first_seen_at = float(_recovery_row_value(recovery_row, "first_seen_at") or 0.0)
    close_or_now = float(close_ts or fallback_now_ts or 0.0)
    open_ts = first_seen_at if first_seen_at > 0 else max(0.0, close_or_now - 1.0)
    context_integrity = str(
        _recovery_row_value(recovery_row, "context_integrity") or context_integrity_default
    )
    status = str(_recovery_row_value(recovery_row, "status") or "open")
    pid = str(int(position_id))

    return {
        "decision_payload": {
            "event_type": "open",
            "symbol": symbol,
            "timeframe": "",
            "trade_id": pid,
            "position_id": pid,
            "decision_ts": open_ts,
            "portfolio_state": {},
            "risk_state": risk_state or {},
            "action_score": 0.0,
            "action_reason": "live_close_open_repair",
            "action_json": {
                "position_id": int(position_id),
                "broker": broker,
                "strategy_name": strategy_name,
                "price": open_price,
                "volume": volume,
                "direction": direction,
                "close_reason": close_reason,
                "repair_source": "recovery_position_state",
                "context_integrity": context_integrity,
                "real_pnl": pnl,
            },
        },
        "position_event_payload": {
            "position_id": pid,
            "trade_id": pid,
            "symbol": symbol,
            "event_type": "opened",
            "net_volume": volume,
            "avg_price": open_price,
            "details": {
                "repair_source": "recovery_position_state",
                "close_reason": close_reason,
                "direction": direction,
            },
            "event_ts": open_ts,
        },
        "recovery_state_payload": {
            "position_id": int(position_id),
            "symbol": symbol,
            "direction": direction,
            "open_price": open_price,
            "volume": volume,
        },
        "recovery_state_kwargs": {
            "broker": broker,
            "strategy_name": strategy_name,
            "status": status,
            "context_integrity": context_integrity,
        },
        "recovery_state_meta": {"open_repaired_before_close": True},
    }


def protection_candidate_supersede_reason(
    *,
    position_id: int,
    timeout_handled: set[int],
    protected_position_ids: set[int],
) -> str:
    pid = int(position_id or 0)
    if pid not in set(protected_position_ids or set()):
        return ""
    if pid in set(timeout_handled or set()):
        return "holding_timeout"
    return "position_supervisor"


def build_position_protection_cycle_result(
    *,
    timeout_handled: set[int],
    entry_repair_applied: set[int],
    supervisor_handled: set[int],
    trailing_applied: set[int],
    trailing_superseded: set[int],
) -> dict[str, list[int]]:
    return {
        "timeout": sorted(int(pid) for pid in timeout_handled or set()),
        "entry_repair": sorted(int(pid) for pid in entry_repair_applied or set()),
        "supervisor": sorted(int(pid) for pid in supervisor_handled or set()),
        "trailing_applied": sorted(int(pid) for pid in trailing_applied or set()),
        "trailing_superseded": sorted(int(pid) for pid in trailing_superseded or set()),
    }


def legacy_awe_trailing_atr_config(conviction: float) -> dict[str, float]:
    conviction = float(conviction or 0.0)
    if conviction >= 0.7:
        return {"trail_atr": 1.5, "activate_atr": 1.0}
    if conviction >= 0.4:
        return {"trail_atr": 2.0, "activate_atr": 1.5}
    return {"trail_atr": 3.0, "activate_atr": 2.0}


def build_legacy_awe_trailing_update(
    *,
    position: dict[str, Any],
    existing_state: dict[str, Any] | None,
    current_price: float,
    atr_price: float,
    conviction: float,
    config_version: int,
    config_hash: str,
) -> dict[str, Any]:
    pid_raw = (position or {}).get("position_id") or (position or {}).get("ticket")
    if pid_raw is None:
        return {"position_id": 0, "state": existing_state or {}, "activated_now": False, "candidate": None}
    pid = int(pid_raw)
    if not position_component_is_known(position, "price"):
        return {
            "position_id": pid,
            "state": existing_state or {},
            "activated_now": False,
            "candidate": None,
            "blocked": True,
            "reason": "position_price_component_unknown",
        }
    direction = int((position or {}).get("direction", 0) or 0)
    entry = float((position or {}).get("entry_price", 0) or (position or {}).get("open_price", 0) or 0.0)
    if pid <= 0 or entry <= 0 or direction == 0:
        return {"position_id": pid, "state": existing_state or {}, "activated_now": False, "candidate": None}

    cfg = legacy_awe_trailing_atr_config(conviction)
    trail_atr = float(cfg["trail_atr"])
    activate_atr = float(cfg["activate_atr"])
    state = dict(existing_state or {
        "best_price": entry,
        "activated": False,
        "entry_price": entry,
        "direction": direction,
    })
    state.setdefault("entry_price", entry)
    state.setdefault("direction", direction)
    state.setdefault("best_price", entry)
    state.setdefault("activated", False)

    current_price = float(current_price or 0.0)
    atr_price = float(atr_price or 0.0)
    if direction == 1:
        if current_price > float(state["best_price"]):
            state["best_price"] = current_price
        price_move = current_price - entry
    else:
        if current_price < float(state["best_price"]):
            state["best_price"] = current_price
        price_move = entry - current_price

    activated_now = False
    if not bool(state.get("activated")) and price_move >= atr_price * activate_atr:
        state["activated"] = True
        activated_now = True
    if not bool(state.get("activated")):
        return {"position_id": pid, "state": state, "activated_now": activated_now, "price_move": price_move, "candidate": None}

    current_sl = float((position or {}).get("sl", 0) or 0.0)
    current_tp = float((position or {}).get("tp", 0) or (position or {}).get("takeProfit", 0) or 0.0)
    best_price = float(state.get("best_price") or entry)
    target_sl = best_price - atr_price * trail_atr if direction == 1 else best_price + atr_price * trail_atr
    should_emit = target_sl > current_sl + 0.01 if direction == 1 else current_sl == 0 or target_sl < current_sl - 0.01
    if not should_emit:
        return {"position_id": pid, "state": state, "activated_now": activated_now, "price_move": price_move, "candidate": None}

    candidate = {
        "source": "legacy_awe_trailing",
        "action": "tighten",
        "priority": 50,
        "position_id": pid,
        "risk_action": "tighten_position",
        "controls": {
            "target_stop_loss": round(target_sl, 2),
            "target_take_profit": round(current_tp, 2) if current_tp > 0 else 0.0,
            "close_reason": "legacy_awe_trailing",
            "protection_mode": "legacy_awe_trailing_stop",
        },
        "evidence": {
            "conviction": round(float(conviction or 0.0), 6),
            "trail_atr": trail_atr,
            "activate_atr": activate_atr,
            "atr_price": atr_price,
            "best_price": best_price,
            "entry_price": entry,
            "current_price": current_price,
            "current_sl": current_sl,
            "target_sl": round(target_sl, 2),
            "confidence": min(0.85, max(0.45, float(conviction or 0.0))),
        },
        "reason": "legacy_awe_trailing",
        "position": dict(position or {}),
        "config_version": int(config_version or 0),
        "config_hash": str(config_hash or ""),
    }
    return {
        "position_id": pid,
        "state": state,
        "activated_now": activated_now,
        "price_move": price_move,
        "candidate": candidate,
    }


def build_entry_protection_plan_payload(
    *,
    schema_version: str,
    position_id: int,
    direction: int,
    entry_price: float,
    target_stop_loss: float,
    target_take_profit: float,
    requested_volume: float,
    actual_api_volume: float,
    tick: int,
    created_at: float,
    config_version: int,
    config_hash: str,
    status: str = "pending",
    source: str = "factor_v4_open",
    error: str = "",
) -> dict[str, Any]:
    return {
        "schema_version": str(schema_version or ""),
        "position_id": int(position_id),
        "source": str(source or "factor_v4_open"),
        "status": str(status or "pending"),
        "direction": int(direction or 0),
        "entry_price": round(float(entry_price or 0.0), 2),
        "target_stop_loss": round(float(target_stop_loss or 0.0), 2),
        "target_take_profit": round(float(target_take_profit or 0.0), 2),
        "requested_volume": float(requested_volume or 0.0),
        "actual_api_volume": float(actual_api_volume or 0.0),
        "tick": int(tick or 0),
        "attempts": 0,
        "last_attempt_ts": 0.0,
        "last_error": str(error or ""),
        "created_at": float(created_at or 0.0),
        "updated_at": float(created_at or 0.0),
        "config_version": int(config_version or 0),
        "config_hash": str(config_hash or ""),
    }


def update_entry_protection_plan_payload(
    *,
    plan: dict[str, Any],
    status: str,
    updated_at: float,
    error: str = "",
    attempted: bool = False,
    applied_sl: float = 0.0,
    applied_tp: float = 0.0,
) -> dict[str, Any]:
    next_plan = dict(plan or {})
    if not next_plan:
        return {}
    next_plan["status"] = str(status or next_plan.get("status") or "pending")
    next_plan["updated_at"] = float(updated_at or 0.0)
    if attempted:
        next_plan["attempts"] = int(next_plan.get("attempts") or 0) + 1
        next_plan["last_attempt_ts"] = float(updated_at or 0.0)
    if error:
        next_plan["last_error"] = str(error)
    elif status == "applied":
        next_plan["last_error"] = ""
    if applied_sl > 0:
        next_plan["applied_stop_loss"] = round(float(applied_sl), 2)
    if applied_tp > 0:
        next_plan["applied_take_profit"] = round(float(applied_tp), 2)
    return next_plan


def build_applied_entry_protection_plan_payload(
    *,
    plan: dict[str, Any],
    updated_at: float,
    applied_sl: float,
    applied_tp: float,
) -> dict[str, Any]:
    next_plan = dict(plan or {})
    if not next_plan:
        return {}
    next_plan.update(
        {
            "status": "applied",
            "updated_at": float(updated_at or 0.0),
            "applied_stop_loss": round(float(applied_sl or 0.0), 2),
            "applied_take_profit": round(float(applied_tp or 0.0), 2),
        }
    )
    return next_plan


def build_supervisor_tighten_sl_plan(
    *,
    current_sl: float,
    current_price: float,
    direction: int,
    target_sl: float,
    bid: float = 0.0,
    ask: float = 0.0,
    mid: float = 0.0,
    quote_age_seconds: float | None = None,
    quote_max_age_seconds: float = 10.0,
    min_stop_distance_points: float = 0.20,
    stop_safety_buffer_ratio: float = 0.00008,
    min_tighten_delta_points: float = 0.01,
    precision: int = 2,
    require_side_quote: bool = False,
) -> dict[str, Any]:
    direction = int(direction or 0)
    current_sl = float(current_sl or 0.0)
    current_price = float(current_price or 0.0)
    bid = float(bid or 0.0)
    ask = float(ask or 0.0)
    mid = float(mid or 0.0)
    reference_price = (
        bid if direction > 0 and bid > 0
        else ask if direction < 0 and ask > 0
        else mid if mid > 0
        else current_price
    )
    target_sl = float(target_sl or 0.0)
    min_delta = max(0.0, float(min_tighten_delta_points or 0.0))
    buffer = max(
        max(0.0, float(min_stop_distance_points or 0.0)),
        abs(reference_price) * max(0.0, float(stop_safety_buffer_ratio or 0.0)),
    ) if reference_price > 0 else 0.0
    normalized_quote_age = (
        max(0.0, float(quote_age_seconds))
        if quote_age_seconds is not None
        else None
    )
    plan = {
        "allowed": False,
        "reason": "",
        "target_sl": target_sl,
        "planned_sl": 0.0,
        "current_sl": current_sl,
        "current_price": current_price,
        "reference_price": reference_price,
        "bid": bid,
        "ask": ask,
        "direction": direction,
        "buffer": buffer,
        "quote_age_seconds": normalized_quote_age,
        "quote_max_age_seconds": float(quote_max_age_seconds or 0.0),
        "precision": int(precision or 0),
        "require_side_quote": bool(require_side_quote),
    }
    if normalized_quote_age is None:
        plan["reason"] = "quote_timestamp_unknown"
        return plan
    if normalized_quote_age > quote_max_age_seconds >= 0:
        plan["reason"] = "stale_quote"
        return plan
    if target_sl <= 0:
        plan["reason"] = "missing_target_stop_loss"
        return plan
    if direction == 0:
        plan["reason"] = "missing_position_direction"
        return plan
    if require_side_quote and direction > 0 and bid <= 0:
        plan["reason"] = "missing_bid_quote"
        return plan
    if require_side_quote and direction < 0 and ask <= 0:
        plan["reason"] = "missing_ask_quote"
        return plan
    if reference_price <= 0:
        plan["reason"] = "missing_current_price"
        return plan

    if direction > 0:
        legal_ceiling = reference_price - buffer
        planned_sl = min(target_sl, legal_ceiling)
        plan["legal_boundary"] = legal_ceiling
        if planned_sl <= 0:
            plan["reason"] = "invalid_long_stop_loss"
            return plan
        if current_sl > 0 and planned_sl <= current_sl + min_delta:
            plan["reason"] = "not_tightening_long_stop_loss"
            plan["planned_sl"] = round(planned_sl, precision)
            return plan
    else:
        legal_floor = reference_price + buffer
        planned_sl = max(target_sl, legal_floor)
        plan["legal_boundary"] = legal_floor
        if current_sl > 0 and planned_sl >= current_sl - min_delta:
            plan["reason"] = "not_tightening_short_stop_loss"
            plan["planned_sl"] = round(planned_sl, precision)
            return plan

    planned_sl = round(planned_sl, precision)
    if current_sl > 0 and abs(planned_sl - current_sl) < min_delta:
        plan["reason"] = "stop_loss_delta_too_small"
        plan["planned_sl"] = planned_sl
        return plan
    plan["allowed"] = True
    plan["reason"] = "ok"
    plan["planned_sl"] = planned_sl
    return plan


def build_supervisor_tighten_sl_plan_inputs(
    *,
    position: dict[str, Any],
    target_sl: float,
    quote: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
    evaluated_at_ts: float | None = None,
) -> dict[str, Any]:
    quote = quote or {}
    policy = policy or {}
    quote_ts = float(quote.get("ts") or 0.0)
    evaluated_at = float(
        time.time() if evaluated_at_ts is None else evaluated_at_ts
    )
    return {
        "current_sl": float_payload_value(position, "sl", "stop_loss", "stopLoss"),
        "current_price": float_payload_value(position, "current_price", "price_current", "price", "mark_price"),
        "direction": position_direction_from_payload(position),
        "target_sl": float(target_sl or 0.0),
        "bid": float_payload_value(quote, "bid"),
        "ask": float_payload_value(quote, "ask"),
        "mid": float_payload_value(quote, "mid", "price"),
        "quote_age_seconds": (
            max(0.0, evaluated_at - quote_ts) if quote_ts > 0 else None
        ),
        "quote_max_age_seconds": float(policy.get("quote_max_age_seconds", 10.0) or 10.0),
        "min_stop_distance_points": float(policy.get("min_stop_distance_points", 0.20) or 0.20),
        "stop_safety_buffer_ratio": float(policy.get("stop_safety_buffer_ratio", 0.00008) or 0.00008),
        "min_tighten_delta_points": float(policy.get("min_tighten_delta_points", 0.01) or 0.01),
        "precision": int(policy.get("precision", position.get("digits", 2)) or 2),
        "require_side_quote": bool(policy.get("require_side_quote", False)),
    }


def target_tp_is_extension(*, current_tp: float, target_tp: float, direction: int) -> bool:
    target_tp = float(target_tp or 0.0)
    if target_tp <= 0:
        return False
    current_tp = float(current_tp or 0.0)
    if current_tp <= 0:
        return True
    direction = int(direction or 0)
    min_delta = 0.01
    if direction > 0:
        return target_tp > current_tp + min_delta
    if direction < 0:
        return target_tp < current_tp - min_delta
    return abs(target_tp - current_tp) >= min_delta


def build_target_tp_extension_inputs(
    *,
    position: dict[str, Any],
    target_tp: float,
) -> dict[str, Any]:
    return {
        "current_tp": float_payload_value(position, "tp", "take_profit", "takeProfit"),
        "target_tp": float(target_tp or 0.0),
        "direction": position_direction_from_payload(position),
    }


def adjust_sl_plan_for_tp_only_protection(
    *,
    sl_plan: dict[str, Any],
    source: str,
    entry_protection_repair_source: str,
    position_sl: float,
    target_tp: float,
    tp_extension_only: bool,
) -> dict[str, Any]:
    plan = dict(sl_plan or {})
    if plan.get("allowed"):
        return {
            "planned_sl": float(plan.get("planned_sl") or 0.0),
            "sl_plan": plan,
        }
    existing_sl = float(position_sl or 0.0)
    if existing_sl <= 0:
        return {
            "planned_sl": float(plan.get("planned_sl") or 0.0),
            "sl_plan": plan,
        }
    if str(source or "") == str(entry_protection_repair_source or "") and float(target_tp or 0.0) > 0:
        return {
            "planned_sl": existing_sl,
            "sl_plan": {
                **plan,
                "allowed": True,
                "planned_sl": existing_sl,
                "reason": "preserve_existing_stop_loss_for_tp_repair",
            },
        }
    if bool(tp_extension_only):
        return {
            "planned_sl": existing_sl,
            "sl_plan": {
                **plan,
                "allowed": True,
                "planned_sl": existing_sl,
                "reason": "preserve_existing_stop_loss_for_tp_extension",
            },
        }
    return {
        "planned_sl": float(plan.get("planned_sl") or 0.0),
        "sl_plan": plan,
    }


def build_protection_execution_plan(
    *,
    position: dict[str, Any],
    controls: dict[str, Any],
    source: str,
    entry_protection_repair_source: str,
    quote: dict[str, Any] | None = None,
    evaluated_at_ts: float | None = None,
) -> dict[str, Any]:
    target_sl = float((controls or {}).get("target_stop_loss", 0.0) or 0.0)
    target_tp = float((controls or {}).get("target_take_profit", 0.0) or 0.0)
    position_sl = float_payload_value(position, "sl", "stop_loss", "stopLoss")
    position_tp = float_payload_value(position, "tp", "take_profit", "takeProfit")
    current_tp = target_tp if target_tp > 0 else position_tp
    sl_plan = build_supervisor_tighten_sl_plan(
        **build_supervisor_tighten_sl_plan_inputs(
            position=position,
            target_sl=target_sl,
            quote=quote,
            evaluated_at_ts=evaluated_at_ts,
        )
    )
    tp_extension_only = target_tp_is_extension(
        **build_target_tp_extension_inputs(
            position=position,
            target_tp=target_tp,
        )
    )
    sl_adjustment = adjust_sl_plan_for_tp_only_protection(
        sl_plan=sl_plan,
        source=source,
        entry_protection_repair_source=entry_protection_repair_source,
        position_sl=position_sl,
        target_tp=target_tp,
        tp_extension_only=tp_extension_only,
    )
    return {
        "target_sl": target_sl,
        "target_tp": target_tp,
        "position_sl": position_sl,
        "position_tp": position_tp,
        "current_tp": current_tp,
        "planned_sl": float(sl_adjustment.get("planned_sl") or 0.0),
        "sl_plan": sl_adjustment.get("sl_plan") or sl_plan,
        "tp_extension_only": bool(tp_extension_only),
    }


def build_supervisor_tighten_execution_plan(
    *,
    position: dict[str, Any],
    controls: dict[str, Any],
    quote: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
    evaluated_at_ts: float | None = None,
) -> dict[str, Any]:
    target_sl = float((controls or {}).get("target_stop_loss", 0.0) or 0.0)
    current_tp = float((position or {}).get("tp", 0.0) or 0.0)
    target_tp = float((controls or {}).get("target_take_profit", 0.0) or 0.0)
    planned_tp = (
        target_tp
        if target_tp_is_extension(
            **build_target_tp_extension_inputs(position=position, target_tp=target_tp)
        )
        else current_tp
    )
    sl_plan = build_supervisor_tighten_sl_plan(
        **build_supervisor_tighten_sl_plan_inputs(
            position=position,
            target_sl=target_sl,
            quote=quote,
            policy=policy,
            evaluated_at_ts=evaluated_at_ts,
        )
    )
    return {
        "target_sl": target_sl,
        "current_tp": current_tp,
        "target_tp": target_tp,
        "planned_tp": planned_tp,
        "sl_plan": sl_plan,
        "planned_sl": float(sl_plan.get("planned_sl") or 0.0),
    }


def build_supervisor_tighten_result_payloads(
    *,
    result: str,
    action: str,
    verdict: dict[str, Any],
    risk_action: str,
    risk_verdict: dict[str, Any] | None,
    decision_id: str,
    controls: dict[str, Any],
    sl_plan: dict[str, Any],
    target_sl: float = 0.0,
    planned_sl: float = 0.0,
    target_tp: float = 0.0,
    planned_tp: float = 0.0,
    current_tp: float = 0.0,
    failure_reason: str = "",
) -> dict[str, Any]:
    risk_payload = risk_verdict or {}
    summary_reason = (verdict or {}).get("summary_reason")
    base_details = {
        "supervisor_action": action,
        "supervisor_reason": summary_reason,
        "risk_verdict_reason": risk_payload.get("reason"),
    }
    if result == "skipped":
        reason = str((sl_plan or {}).get("reason") or "")
        return {
            "position_event_type": "amend_skipped",
            "position_event_details": {
                **base_details,
                "skip_stage": "supervisor_tighten_sltp",
                "skip_reason": (sl_plan or {}).get("reason"),
                "sl_plan": sl_plan or {},
                "applied_controls": controls or {},
            },
            "trace_fields": {
                "stage": "execution_skipped",
                "outcome": "skipped",
                "decision_id": decision_id,
                "risk_action": risk_action,
                "risk_verdict": risk_payload,
                "execution_status": "skipped",
                "execution_reason": reason,
                "execution": {"sl_plan": sl_plan or {}, "applied_controls": controls or {}},
            },
        }
    if result in {"applied", "applied_position_closed"}:
        position_closed_after_amend = result == "applied_position_closed"
        return {
            "position_event_type": (
                "tightened_then_closed"
                if position_closed_after_amend
                else "tightened"
            ),
            "position_event_details": {
                **base_details,
                "position_closed_after_amend": position_closed_after_amend,
                "applied_controls": {
                    **(controls or {}),
                    "target_stop_loss_original": target_sl,
                    "target_stop_loss_sent": planned_sl,
                    "target_take_profit_original": target_tp,
                    "target_take_profit_sent": planned_tp,
                    "sl_plan": sl_plan or {},
                },
            },
            "trace_fields": {
                "stage": "executed",
                "outcome": "applied",
                "decision_id": decision_id,
                "risk_action": risk_action,
                "risk_verdict": risk_payload,
                "execution_status": "applied",
                "execution_reason": (
                    "amend_accepted_position_closed"
                    if position_closed_after_amend
                    else "amend_position_sltp_success"
                ),
                "execution": {
                    "broker_action_confirmed": True,
                    "reconcile_confirmed": True,
                    "is_real_execution": True,
                    "target_stop_loss_sent": planned_sl,
                    "target_take_profit_sent": planned_tp,
                    "target_take_profit_changed": planned_tp != current_tp,
                    "position_closed_after_amend": position_closed_after_amend,
                    "sl_plan": sl_plan or {},
                    "applied_controls": controls or {},
                },
            },
        }
    reason = str(failure_reason or "amend_failed")
    return {
        "position_event_type": "amend_failed",
        "position_event_details": {
            **base_details,
            "failure_stage": "supervisor_tighten_sltp",
            "failure_reason": reason,
            "sl_plan": sl_plan or {},
            "applied_controls": controls or {},
        },
        "trace_fields": {
            "stage": "execution_failed",
            "outcome": "failed",
            "decision_id": decision_id,
            "risk_action": risk_action,
            "risk_verdict": risk_payload,
            "execution_status": "failed",
            "execution_reason": reason,
            "execution": {"sl_plan": sl_plan or {}, "applied_controls": controls or {}},
        },
    }


def build_supervisor_risk_context_payload(
    *,
    close_context: dict[str, Any],
    position: dict[str, Any],
    verdict: dict[str, Any],
) -> dict[str, Any]:
    payload = dict(close_context or {})
    controls = verdict.get("recommended_controls") or {}
    payload.update(
        {
            "supervisor_action": verdict.get("action"),
            "supervisor_confidence": verdict.get("confidence"),
            "supervisor_reason": verdict.get("summary_reason"),
            "supervisor_evidence": verdict.get("evidence") or {},
            "supervisor_decision_ts": verdict.get("decision_ts"),
            "recommended_controls": controls,
            "position_id": str(position.get("position_id") or position.get("ticket") or ""),
            "position": dict(position or {}),
        }
    )
    return payload


def supervisor_risk_action_for_action(action: str) -> str:
    return {
        "tighten": "tighten_position",
        "reduce": "reduce_position",
        "close": "close_position",
    }.get(str(action or ""), "")


def build_supervisor_runtime_risk_evaluation_inputs(
    *,
    action: str,
    risk_context: dict[str, Any],
    loop_running: bool,
    bridge_connected: bool,
) -> dict[str, Any]:
    context = dict(risk_context or {})
    context.update(
        {
            "loop_running": bool(loop_running),
            "bridge_connected": bool(bridge_connected),
        }
    )
    return {
        "risk_action": supervisor_risk_action_for_action(action),
        "risk_context": context,
    }


def build_supervisor_close_context_inputs(
    *,
    position: dict[str, Any],
    verdict: dict[str, Any],
    mode: str = "live",
    broker: str = "ctrader",
) -> dict[str, Any]:
    controls = (verdict or {}).get("recommended_controls") or {}
    return {
        "position_id": int(position.get("position_id") or position.get("ticket") or 0),
        "close_reason": str(controls.get("close_reason") or (verdict or {}).get("summary_reason") or ""),
        "mode": str(mode or "live"),
        "broker": str(broker or "ctrader"),
        "symbol": str(position.get("symbol") or "XAUUSD+"),
        "position": position,
    }


def build_protection_candidate_risk_context_payload(
    *,
    close_context: dict[str, Any],
    position: dict[str, Any],
    action: str,
    confidence: float,
    reason: str,
    evidence: dict[str, Any] | None,
    controls: dict[str, Any] | None,
    loop_running: bool,
    bridge_connected: bool,
    source: str,
) -> dict[str, Any]:
    payload = dict(close_context or {})
    payload.update(
        {
            "supervisor_action": action,
            "supervisor_confidence": float(confidence or 0.0),
            "supervisor_reason": reason,
            "supervisor_evidence": evidence or {},
            "recommended_controls": controls or {},
            "loop_running": bool(loop_running),
            "bridge_connected": bool(bridge_connected),
            "protection_source": source,
            "position": dict(position or {}),
        }
    )
    return payload


def build_protection_candidate_risk_context_from_candidate(
    *,
    close_context: dict[str, Any],
    position: dict[str, Any],
    candidate: Any,
    loop_running: bool,
    bridge_connected: bool,
) -> dict[str, Any]:
    evidence = payload_get(candidate, "evidence", {}) or {}
    controls = payload_get(candidate, "controls", {}) or {}
    return build_protection_candidate_risk_context_payload(
        close_context=close_context,
        position=position,
        action=str(payload_get(candidate, "action", "") or ""),
        confidence=float((evidence or {}).get("confidence", 0.0) or 0.0),
        reason=str(payload_get(candidate, "reason", "") or ""),
        evidence=evidence,
        controls=controls,
        loop_running=loop_running,
        bridge_connected=bridge_connected,
        source=str(payload_get(candidate, "source", "") or ""),
    )


def build_supervisor_recovery_meta(
    *,
    recovery_meta: dict[str, Any] | None,
    verdict: dict[str, Any],
    action_applied: str = "",
    applied_ts: float = 0.0,
) -> dict[str, Any]:
    meta = dict(recovery_meta or {})
    for key in ("latest_supervisor", "latest_protection"):
        previous = meta.get(key)
        if isinstance(previous, Mapping):
            meta[key] = _compact_supervisor_mapping(previous)
    meta["latest_supervisor"] = _compact_supervisor_mapping(
        verdict,
        nested_keys=frozenset({"evidence", "recommended_controls", "execution"}),
    )
    meta["latest_supervisor_source"] = "position_supervisor"
    evidence = dict(verdict.get("evidence") or {})
    has_state_contract = bool(
        "supervisor_posture" in evidence
        or "closed_bar_key" in evidence
        or "trigger_tags" in evidence
        or verdict.get("action_fingerprint")
    )
    if not has_state_contract:
        if action_applied:
            meta["last_supervisor_applied_action"] = action_applied
            meta["last_supervisor_applied_ts"] = float(applied_ts or 0.0)
            meta["last_supervisor_reason"] = verdict.get("summary_reason")
            meta["last_supervisor_applied_source"] = "position_supervisor"
        return meta
    posture = str(evidence.get("supervisor_posture") or "unknown_observe")
    previous_posture = str(meta.get("supervisor_posture") or "")
    meta["supervisor_posture"] = posture
    posture_changed = previous_posture not in {"", posture}
    if posture_changed:
        # A posture transition starts a new management episode.  Do not let
        # the previous posture's adaptive fingerprint suppress a fresh
        # recommendation if the position later returns to this posture.
        meta.pop("supervisor_last_adaptive_fingerprint", None)
        meta.pop("supervisor_last_adaptive_closed_bar_key", None)
        meta.pop("supervisor_last_adaptive_trigger_key", None)
    if posture_changed or not meta.get("supervisor_posture_since_ts"):
        meta["supervisor_posture_since_ts"] = float(
            verdict.get("decision_ts") or time.time()
        )
    closed_bar_key = str(
        evidence.get("closed_bar_key")
        or evidence.get("completed_bars_after_entry")
        or ""
    )
    if closed_bar_key:
        meta["supervisor_last_closed_bar_key"] = closed_bar_key
    trigger_tags = [str(item) for item in evidence.get("trigger_tags") or [] if str(item)]
    trigger_key = "|".join(sorted(set(trigger_tags)))
    previous_trigger_key = str(meta.get("supervisor_trigger_key") or "")
    if trigger_key and trigger_key != previous_trigger_key:
        # A changed trigger set is a new episode even when the bar has not
        # advanced.  Clearing the old fingerprint also handles the
        # clear-and-reenter case where the same trigger returns later.
        meta.pop("supervisor_last_adaptive_fingerprint", None)
        meta.pop("supervisor_last_adaptive_closed_bar_key", None)
        meta.pop("supervisor_last_adaptive_trigger_key", None)
        try:
            episode = int(meta.get("supervisor_trigger_episode") or 0) + 1
        except (TypeError, ValueError):
            episode = 1
        meta["supervisor_trigger_episode"] = episode
    elif not trigger_key:
        meta.pop("supervisor_last_adaptive_fingerprint", None)
        meta.pop("supervisor_last_adaptive_closed_bar_key", None)
        meta.pop("supervisor_last_adaptive_trigger_key", None)
    elif "supervisor_trigger_episode" not in meta:
        meta["supervisor_trigger_episode"] = 0
    meta["supervisor_trigger_key"] = trigger_key
    fingerprint = str(verdict.get("action_fingerprint") or "")
    if fingerprint:
        requested_action = str(
            verdict.get("requested_action")
            or verdict.get("recommended_action")
            or verdict.get("action")
            or ""
        ).strip().lower()
        summary_reason = str(verdict.get("summary_reason") or "")
        hard_action = bool(evidence.get("hard_risk_active")) or summary_reason in {
            "hard_risk_active",
            "holding_timeout_exceeded",
            "near_stop_loss_preemptive_exit",
            "thesis_broken",
            "regime_shift_detected",
        }
        if requested_action != "hold" and not hard_action:
            meta["supervisor_last_adaptive_fingerprint"] = fingerprint
            meta["supervisor_last_adaptive_closed_bar_key"] = closed_bar_key
            meta["supervisor_last_adaptive_trigger_key"] = trigger_key
    meta["supervisor_last_adaptive_execution_class"] = str(
        verdict.get("execution_class")
        or verdict.get("execution_status")
        or "observed"
    )
    if action_applied:
        meta["last_supervisor_applied_action"] = action_applied
        meta["last_supervisor_applied_ts"] = float(applied_ts or 0.0)
        meta["last_supervisor_reason"] = verdict.get("summary_reason")
        meta["last_supervisor_applied_source"] = "position_supervisor"
    return meta


def build_protection_recovery_meta(
    *,
    recovery_meta: dict[str, Any] | None,
    verdict: dict[str, Any],
    source: str,
    action_applied: str = "",
    applied_ts: float = 0.0,
) -> dict[str, Any]:
    meta = dict(recovery_meta or {})
    for key in ("latest_supervisor", "latest_protection"):
        previous = meta.get(key)
        if isinstance(previous, Mapping):
            meta[key] = _compact_supervisor_mapping(previous)
    source = str(source or "position_protection")
    meta["latest_protection"] = _compact_supervisor_mapping(
        verdict,
        nested_keys=frozenset({"evidence", "recommended_controls", "execution", "controls"}),
    )
    meta["latest_protection_source"] = source
    if action_applied:
        meta["last_protection_applied_action"] = action_applied
        meta["last_protection_applied_ts"] = float(applied_ts or 0.0)
        meta["last_protection_reason"] = verdict.get("summary_reason")
        meta["last_protection_applied_source"] = source
    return meta


def build_supervisor_state_upsert_payload(
    *,
    recovery_row: dict[str, Any] | None,
    verdict: dict[str, Any],
    broker: str,
    strategy_name: str,
    loop_strategy_name: str,
    default_context_integrity: str,
    action_applied: str = "",
    applied_ts: float = 0.0,
) -> dict[str, Any]:
    row = recovery_row or {}
    meta = build_supervisor_recovery_meta(
        recovery_meta=row.get("recovery_meta") or {},
        verdict=verdict,
        action_applied=action_applied,
        applied_ts=applied_ts,
    )
    return build_recovery_upsert_defaults(
        recovery_row=row,
        broker=broker,
        strategy_name=strategy_name,
        loop_strategy_name=loop_strategy_name,
        default_context_integrity=default_context_integrity,
        meta=meta,
    )


def build_protection_state_upsert_payload(
    *,
    recovery_row: dict[str, Any] | None,
    verdict: dict[str, Any],
    source: str,
    broker: str,
    strategy_name: str,
    loop_strategy_name: str,
    default_context_integrity: str,
    action_applied: str = "",
    applied_ts: float = 0.0,
) -> dict[str, Any]:
    row = recovery_row or {}
    meta = build_protection_recovery_meta(
        recovery_meta=row.get("recovery_meta") or {},
        verdict=verdict,
        source=source,
        action_applied=action_applied,
        applied_ts=applied_ts,
    )
    return build_recovery_upsert_defaults(
        recovery_row=row,
        broker=broker,
        strategy_name=strategy_name,
        loop_strategy_name=loop_strategy_name,
        default_context_integrity=default_context_integrity,
        meta=meta,
    )


def build_recovery_upsert_defaults(
    *,
    recovery_row: dict[str, Any] | None,
    broker: str,
    strategy_name: str,
    loop_strategy_name: str,
    default_context_integrity: str,
    meta: dict[str, Any],
) -> dict[str, Any]:
    row = recovery_row or {}
    return {
        "broker": str(broker or row.get("broker") or "ctrader"),
        "strategy_name": str(strategy_name or row.get("strategy_name") or loop_strategy_name or "factor_v4"),
        "status": str(row.get("status") or "open"),
        "context_integrity": str(row.get("context_integrity") or default_context_integrity),
        "meta": meta,
    }


def normalize_recovery_position_row(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    try:
        item = dict(row)
    except Exception:
        return {}
    item["recovery_meta"] = _json_dict(item.get("recovery_meta_json") or "{}")
    return item


def merge_recovery_meta_json(existing_meta_json: Any, meta: dict[str, Any] | None) -> dict[str, Any]:
    merged = _json_dict(existing_meta_json or "{}")
    if meta:
        merged.update(meta)
    return merged


def build_recovery_meta_update_payload(
    *,
    position_id: int,
    existing_meta_json: Any,
    meta: dict[str, Any] | None,
    now_ts: float,
) -> dict[str, Any]:
    return {
        "position_id": int(position_id),
        "recovery_meta": merge_recovery_meta_json(existing_meta_json, meta),
        "last_seen_at": float(now_ts or 0.0),
    }


def build_recovery_closed_update_payload(
    *,
    position_id: int,
    existing_meta_json: Any,
    close_reason: str,
    close_pnl: float,
    closed_at: float,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "position_id": int(position_id),
        "status": "closed_replayed",
        "closed_at": float(closed_at),
        "close_reason": str(close_reason or ""),
        "close_pnl": float(close_pnl),
        "recovery_meta": merge_recovery_meta_json(existing_meta_json, meta),
    }


def filter_removed_live_position(
    positions: list[Any] | None,
    *,
    position_id: int,
) -> dict[str, Any]:
    pid = int(position_id)
    source = list(positions or [])
    filtered = [
        pos for pos in source
        if position_id_value(pos) != pid
    ]
    return {
        "position_id": pid,
        "positions": filtered,
        "removed": len(filtered) != len(source),
    }


def recovery_active_position_ids(active_rows: list[dict[str, Any]] | None) -> set[int]:
    ids: set[int] = set()
    for row in active_rows or []:
        try:
            pid = int((row or {}).get("position_id") or 0)
        except Exception:
            pid = 0
        if pid > 0:
            ids.add(pid)
    return ids


def recovery_missing_position_ids(
    *,
    active_rows: list[dict[str, Any]] | None,
    current_ids: set[int],
) -> set[int]:
    return recovery_active_position_ids(active_rows) - {int(item) for item in current_ids or set()}


def recovery_replay_lookback_from(
    *,
    active_rows: list[dict[str, Any]] | None,
    replay_ids: set[int],
    now_ts: float,
    lookback_sec: float,
) -> int:
    ids = {int(item) for item in replay_ids or set()}
    timestamps: list[float] = []
    for row in active_rows or []:
        try:
            pid = int((row or {}).get("position_id") or 0)
        except Exception:
            pid = 0
        if pid not in ids:
            continue
        try:
            timestamps.append(float((row or {}).get("last_seen_at") or now_ts))
        except Exception:
            timestamps.append(float(now_ts or 0.0))
    if not timestamps:
        timestamps = [float(now_ts or 0.0)]
    return int(max(0.0, min(timestamps) - float(lookback_sec or 0.0)))


def supervisor_recently_applied_from_meta(
    *,
    recovery_meta: dict[str, Any] | None,
    action: str,
    now_ts: float,
    cooldown_seconds: float = 300.0,
) -> bool:
    meta = dict(recovery_meta or {})
    if str(meta.get("last_supervisor_applied_action") or "") != str(action or ""):
        return False
    source = str(meta.get("last_supervisor_applied_source") or "position_supervisor")
    if source not in {"position_supervisor", "supervisor"}:
        return False
    last_ts = float(meta.get("last_supervisor_applied_ts", 0.0) or 0.0)
    return last_ts > 0 and (float(now_ts or 0.0) - last_ts) < float(cooldown_seconds or 0.0)


def supervisor_reentry_key(symbol: str, direction: int) -> str:
    return f"{str(symbol or 'XAUUSD').replace('+', '').upper()}:{1 if int(direction or 0) > 0 else -1}"


def supervisor_reentry_cooldown_seconds(
    *,
    cooldown_bars: Any,
    timeframe: str,
    timeframe_seconds: Callable[[str], int | float],
) -> float:
    bars = int(cooldown_bars or 0)
    if bars <= 0:
        return 0.0
    tf_seconds = timeframe_seconds(str(timeframe or "M5")) or 300
    return float(max(1, bars) * tf_seconds)


def build_supervisor_reentry_block_payload(
    *,
    symbol: str,
    direction: int,
    position_id: int,
    action: str,
    reason: str,
    started_at: float,
    cooldown_seconds: float,
    current_price: float,
    tick: int,
) -> dict[str, Any]:
    return {
        "active": True,
        "source": "position_supervisor",
        "symbol": symbol,
        "direction": int(direction),
        "position_id": int(position_id or 0),
        "action": str(action or ""),
        "reason": str(reason or ""),
        "started_at": float(started_at or 0.0),
        "expires_at": float(started_at or 0.0) + float(cooldown_seconds or 0.0),
        "cooldown_seconds": float(cooldown_seconds or 0.0),
        "price": float(current_price or 0.0),
        "tick": int(tick or 0),
    }


def supervisor_reentry_block_view(
    block: dict[str, Any] | None,
    *,
    now_ts: float,
) -> dict[str, Any] | None:
    payload = dict(block or {})
    expires_at = float(payload.get("expires_at", 0.0) or 0.0)
    if not payload or (expires_at > 0 and float(now_ts or 0.0) >= expires_at):
        return None
    payload["remaining_seconds"] = max(0.0, expires_at - float(now_ts or 0.0))
    return payload


def build_pending_supervisor_reentry_block_payload(
    *,
    symbol: str,
    direction: int,
    position_id: int,
    action: str,
    reason: str,
    thesis_status: str,
    remaining_seconds: float,
) -> dict[str, Any]:
    return {
        "active": True,
        "source": "pending_position_supervisor",
        "symbol": symbol,
        "direction": int(direction),
        "position_id": int(position_id or 0),
        "action": str(action or "unknown"),
        "reason": str(reason or thesis_status or "position_supervisor"),
        "thesis_status": str(thesis_status or ""),
        "remaining_seconds": float(remaining_seconds or 0.0),
    }
