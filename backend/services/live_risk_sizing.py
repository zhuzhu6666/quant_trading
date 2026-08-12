"""Pure sizing helpers for the live trading service."""

from __future__ import annotations

import math
from typing import Any


def ceil_api_volume_to_step(volume: float, bridge_meta: dict[str, Any] | None) -> float:
    meta = bridge_meta or {}
    min_vol = float(meta.get("api_min_volume") or 1.0)
    step_vol = float(meta.get("api_step_volume") or 1.0)
    raw = max(0.0, float(volume or 0.0))
    if step_vol <= 0:
        return max(min_vol, raw)
    steps = int((raw + step_vol - 1e-9) // step_vol)
    if steps * step_vol < raw:
        steps += 1
    return max(min_vol, steps * step_vol)


def round_api_volume_to_step(volume: float, bridge_meta: dict[str, Any] | None) -> float:
    meta = bridge_meta or {}
    min_vol = float(meta.get("api_min_volume") or 1.0)
    step_vol = float(meta.get("api_step_volume") or 1.0)
    if step_vol <= 0:
        return max(min_vol, float(volume or 0.0))
    return max(min_vol, round(float(volume or 0.0) / step_vol) * step_vol)


def floor_api_volume_to_step(volume: float, bridge_meta: dict[str, Any] | None) -> float:
    meta = bridge_meta or {}
    min_vol = float(meta.get("api_min_volume") or 1.0)
    step_vol = float(meta.get("api_step_volume") or 1.0)
    raw = max(0.0, float(volume or 0.0))
    if step_vol <= 0:
        return raw if raw >= min_vol else 0.0
    floored = (raw // step_vol) * step_vol
    return floored if floored >= min_vol else 0.0


def risk_kelly_sizing(
    *,
    cfg: Any,
    direction: int,
    current_price: float,
    sl_price: float,
    bridge_meta: dict[str, Any] | None,
    account: dict[str, Any] | None,
    kelly_data: dict[str, Any] | None,
) -> dict[str, Any]:
    meta = bridge_meta or {}
    min_vol = float(meta.get("api_min_volume") or 1.0)
    step_vol = float(meta.get("api_step_volume") or 1.0)
    default_vol = ceil_api_volume_to_step(min_vol, meta)
    max_position_api = float(getattr(cfg, "max_position_api_volume", 0.0) or 0.0)
    dynamic_cap = float(getattr(cfg, "dynamic_sizing_max_api_volume", 0.0) or 0.0)
    max_order_api = dynamic_cap if dynamic_cap > 0 else max_position_api
    if max_position_api > 0:
        max_order_api = min(max_order_api, max_position_api) if max_order_api > 0 else max_position_api
    api_units_per_display_unit = float(
        getattr(cfg, "dynamic_sizing_api_units_per_display_unit", 100.0) or 100.0
    )
    trace: dict[str, Any] = {
        "schema_version": "position_sizing_trace.v1",
        "method": "kelly_dynamic" if getattr(cfg, "kelly_enabled", False) else "min_volume",
        "enabled": bool(getattr(cfg, "dynamic_sizing_enabled", True)),
        "direction": int(direction or 0),
        "api_min_volume": min_vol,
        "api_step_volume": step_vol,
        "api_units_per_display_unit": api_units_per_display_unit,
        "max_position_api_volume": max_position_api,
        "max_order_api_volume": max_order_api,
    }

    if not getattr(cfg, "kelly_enabled", False):
        trace.update({"reason": "kelly_disabled", "raw_api_volume": default_vol})
        return {"volume": default_vol, "trace": trace}
    if not bool(getattr(cfg, "dynamic_sizing_enabled", True)):
        trace.update({"reason": "dynamic_sizing_disabled", "raw_api_volume": default_vol})
        return {"volume": default_vol, "trace": trace}

    kelly_payload = dict(kelly_data or {})
    kelly_raw = kelly_payload.get("kelly_fraction", 0)
    try:
        kelly_f = float(kelly_raw or 0.0)
    except (TypeError, ValueError):
        kelly_f = 0.0
    kelly_value_valid = math.isfinite(kelly_f)
    if not kelly_value_valid:
        kelly_f = 0.0
    min_closed_trades = int(getattr(cfg, "kelly_min_closed_trades", 0) or 0)
    canary_max_api = float(getattr(cfg, "kelly_canary_max_api_volume", 0.0) or 0.0)
    closed_trades_raw = kelly_payload.get("closed_trades", kelly_payload.get("trades"))
    try:
        closed_trades = max(0, int(closed_trades_raw)) if closed_trades_raw is not None else 0
    except (TypeError, ValueError):
        closed_trades = 0
    kelly_canary_active = min_closed_trades > 0 and (
        closed_trades_raw is None or closed_trades < min_closed_trades
    )
    trace.update(
        {
            "kelly_closed_trades": closed_trades,
            "kelly_min_closed_trades": min_closed_trades,
            "kelly_canary_max_api_volume": canary_max_api,
            "kelly_canary_cap_active": kelly_canary_active,
        }
    )
    autonomy_mode = str(getattr(cfg, "autonomy_mode", "") or "")
    demo_exploration_mode = autonomy_mode in {"demo_nursery", "demo_autonomous"}
    demo_nursery_exploration = autonomy_mode == "demo_nursery"
    equity = float((account or {}).get("equity", 0) or 0)
    if equity <= 0:
        trace.update(
            {
                "reason": "missing_equity",
                "equity": equity,
                "kelly_fraction": float(kelly_f or 0.0),
                "raw_api_volume": 0.0,
                "base_api_volume": 0.0,
                "final_api_volume": 0.0,
                "blocked_reason": "missing_equity_for_dynamic_sizing",
            }
        )
        return {"volume": 0.0, "trace": trace}

    try:
        entry_price = float(current_price)
        stop_price = float(sl_price)
    except (TypeError, ValueError):
        entry_price = 0.0
        stop_price = 0.0
    protective_stop_valid = (
        math.isfinite(entry_price)
        and math.isfinite(stop_price)
        and entry_price > 0.0
        and stop_price > 0.0
        and int(direction or 0) in {-1, 1}
        and (
            (int(direction) > 0 and stop_price < entry_price)
            or (int(direction) < 0 and stop_price > entry_price)
        )
    )
    if not protective_stop_valid:
        trace.update(
            {
                "reason": "invalid_protective_stop",
                "equity": equity,
                "entry_price": entry_price,
                "stop_price": stop_price,
                "protective_stop_valid": False,
                "kelly_fraction": float(kelly_f or 0.0),
                "raw_api_volume": 0.0,
                "base_api_volume": 0.0,
                "final_api_volume": 0.0,
                "blocked_reason": "invalid_protective_stop",
                "exploration_eligible": False,
            }
        )
        return {"volume": 0.0, "trace": trace}

    kelly_mult = getattr(cfg, "kelly_fraction", 0.5)
    risk_pct_raw = float(getattr(cfg, "kelly_risk_per_trade_pct", 0.01) or 0.0)
    risk_pct = risk_pct_raw / 100.0 if risk_pct_raw > 1.0 else risk_pct_raw
    risk_pct = max(0.0, risk_pct)
    risk_capital = equity * risk_pct
    sl_dist = abs(entry_price - stop_price)
    min_volume_display_units = default_vol / api_units_per_display_unit
    min_volume_stop_risk = sl_dist * min_volume_display_units
    min_volume_stop_risk_fraction = min_volume_stop_risk / equity
    trace.update(
        {
            "entry_price": entry_price,
            "stop_price": stop_price,
            "protective_stop_valid": True,
            "sl_distance": sl_dist,
            "risk_per_trade_pct": risk_pct,
            "risk_capital": risk_capital,
            "min_volume_stop_risk": min_volume_stop_risk,
            "min_volume_stop_risk_fraction": min_volume_stop_risk_fraction,
        }
    )

    def apply_demo_min_volume_exploration(exploration_reason: str) -> float:
        """Reuse the existing demo Kelly exploration path for broker granularity.

        Kelly remains the single sizing calculation.  This bounded fallback only
        expresses a target below the broker's discrete minimum as one minimum
        order in demo mode, provided the existing hard stop-risk budget allows it.
        Non-demo modes stay fail-closed at zero; no parallel live sizing route is
        introduced here.
        """

        exploration_prefix = "demo_nursery" if demo_nursery_exploration else "demo_autonomous"
        blocked_reason = ""
        if max_order_api > 0 and default_vol > max_order_api:
            blocked_reason = f"{exploration_prefix}_min_volume_exceeds_cap"
        elif min_volume_stop_risk > risk_capital + 1e-12:
            blocked_reason = f"{exploration_prefix}_min_volume_risk_budget_exceeded"
        exploration_eligible = not blocked_reason
        trace.update(
            {
                "reason": blocked_reason or f"{exploration_prefix}_min_volume_exploration",
                "raw_api_volume": default_vol if exploration_eligible else 0.0,
                "base_api_volume": default_vol if exploration_eligible else 0.0,
                "final_api_volume": default_vol if exploration_eligible else 0.0,
                "blocked_reason": blocked_reason,
                "demo_exploration": True,
                "demo_nursery_exploration": demo_nursery_exploration,
                "exploration_reason": exploration_reason,
                "exploration_api_volume": default_vol,
                "exploration_eligible": exploration_eligible,
                "exploration_risk_budget": risk_capital,
            }
        )
        return default_vol if exploration_eligible else 0.0

    if not kelly_value_valid:
        trace.update(
            {
                "reason": "invalid_kelly_fraction",
                "kelly_fraction": 0.0,
                "raw_api_volume": 0.0,
                "base_api_volume": 0.0,
                "final_api_volume": 0.0,
                "blocked_reason": "invalid_kelly_fraction",
                "exploration_eligible": False,
            }
        )
        return {"volume": 0.0, "trace": trace}

    if kelly_f <= 0:
        if demo_exploration_mode:
            exploration_volume = apply_demo_min_volume_exploration("non_positive_kelly")
            trace["kelly_fraction"] = kelly_f
            return {"volume": exploration_volume, "trace": trace}
        trace.update(
            {
                "reason": "kelly_fraction_non_positive",
                "kelly_fraction": kelly_f,
                "raw_api_volume": 0.0,
                "base_api_volume": 0.0,
                "final_api_volume": 0.0,
                "blocked_reason": "kelly_fraction_non_positive",
                "exploration_eligible": False,
            }
        )
        return {"volume": 0.0, "trace": trace}

    f_star = kelly_f * kelly_mult
    effective_risk_fraction = min(max(0.0, float(f_star or 0.0)), max(0.0, risk_pct))
    risk_budget = equity * effective_risk_fraction
    raw_display_units = risk_budget / sl_dist if sl_dist > 0 else 0.0
    raw_api_volume = raw_display_units * api_units_per_display_unit
    max_pct = getattr(cfg, "kelly_max_pct", 0.25)
    max_api_volume_calc = (
        (equity * max_pct / sl_dist) * api_units_per_display_unit
        if sl_dist > 0
        else default_vol
    )
    capped_raw = min(raw_api_volume, max_api_volume_calc)
    if max_order_api > 0:
        capped_raw = min(capped_raw, max_order_api)
    pre_canary_capped = capped_raw
    if kelly_canary_active and canary_max_api > 0:
        canary_cap = canary_max_api
        if max_order_api > 0:
            canary_cap = min(canary_cap, max_order_api)
        capped_raw = min(capped_raw, canary_cap)
    tiered_volume = floor_api_volume_to_step(capped_raw, meta)
    volume = tiered_volume
    if max_order_api > 0:
        max_order_tier = floor_api_volume_to_step(max_order_api, meta)
        if max_order_tier > 0 and volume > 0:
            volume = min(volume, max_order_tier)
    blocked_reason = ""
    if volume <= 0:
        blocked_reason = (
            f"kelly_sizing_below_min: raw={capped_raw:.2f}<{default_vol:.0f}"
        )
    sizing_reason = "kelly_canary_cap" if kelly_canary_active and volume > 0 else (
        "ok" if volume > 0 else "kelly_sizing_below_min"
    )
    trace.update(
        {
            "reason": sizing_reason,
            "equity": equity,
            "kelly_fraction": float(kelly_f or 0.0),
            "kelly_multiplier": float(kelly_mult or 0.0),
            "effective_kelly_fraction": float(f_star or 0.0),
            "effective_risk_fraction": effective_risk_fraction,
            "risk_per_trade_pct": risk_pct,
            "risk_capital": risk_capital,
            "risk_budget": risk_budget,
            "raw_display_units": raw_display_units,
            "raw_api_volume": raw_api_volume,
            "max_api_volume_by_capital": max_api_volume_calc,
            "pre_canary_capped_api_volume": pre_canary_capped,
            "capped_raw_api_volume": capped_raw,
            "tiered_base_api_volume": tiered_volume,
            "base_api_volume": volume,
            "final_api_volume": volume,
            "blocked_reason": blocked_reason,
        }
    )
    if volume <= 0 and demo_exploration_mode:
        # A positive Kelly target can still be smaller than one broker lot.  In
        # demo mode, use the same existing minimum-lot exploration budget as the
        # non-positive Kelly path so the system can learn from real executions.
        target_raw_api_volume = raw_api_volume
        target_capped_api_volume = capped_raw
        exploration_volume = apply_demo_min_volume_exploration(
            "positive_kelly_below_broker_minimum"
        )
        trace.update(
            {
                "kelly_target_api_volume": target_raw_api_volume,
                "kelly_target_capped_api_volume": target_capped_api_volume,
                "kelly_target_below_broker_minimum": True,
            }
        )
        return {"volume": exploration_volume, "trace": trace}
    return {"volume": volume, "trace": trace}


def apply_entry_event_sizing(
    *,
    base_volume: float,
    event_multiplier: float,
    bridge_meta: dict[str, Any] | None,
    sizing_trace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    trace = dict(sizing_trace or {})
    upstream_blocked_reason = str(trace.get("blocked_reason") or "")
    min_vol = float((bridge_meta or {}).get("api_min_volume") or 1.0)
    try:
        multiplier = float(event_multiplier)
    except (TypeError, ValueError):
        multiplier = 1.0
    base = float(base_volume or 0.0)
    raw_after_event = base * multiplier
    if base <= 0:
        final_volume = 0.0
        blocked_reason = upstream_blocked_reason or "non_positive_base_volume"
    elif multiplier < 1.0:
        final_volume = floor_api_volume_to_step(raw_after_event, bridge_meta)
        blocked_reason = upstream_blocked_reason
        if final_volume <= 0:
            blocked_reason = (
                f"event_sizing_below_min: {base:.0f}*{multiplier:.2f}="
                f"{raw_after_event:.0f}<{min_vol:.0f}"
            )
    else:
        final_volume = base
        blocked_reason = upstream_blocked_reason
    trace.update(
        {
            "event_multiplier": multiplier,
            "event_raw_api_volume": raw_after_event,
            "event_adjusted_api_volume": final_volume,
            "final_api_volume": final_volume,
            "blocked_reason": blocked_reason,
        }
    )
    return {
        "volume": final_volume,
        "blocked_reason": blocked_reason,
        "trace": trace,
    }


def should_full_close_untradeable_reduce(
    *,
    current_volume: float,
    raw_reduce_volume: float,
    reduce_volume: float,
    min_volume: float,
    verdict: dict[str, Any] | None,
) -> tuple[bool, str]:
    current_volume = float(current_volume or 0.0)
    raw_reduce_volume = float(raw_reduce_volume or 0.0)
    reduce_volume = float(reduce_volume or 0.0)
    min_volume = float(min_volume or 0.0)
    if min_volume <= 0 or current_volume <= 0 or raw_reduce_volume <= 0:
        return False, "missing_reduce_volume"
    if current_volume > min_volume + 1e-9:
        return False, "not_minimum_position"
    if reduce_volume > 0:
        return False, "reduce_volume_tradeable"

    evidence = dict((verdict or {}).get("evidence") or {})
    controls = dict((verdict or {}).get("recommended_controls") or {})
    if controls.get("allow_full_close_fallback") is False:
        return False, "full_close_fallback_disabled"
    summary_reason = str((verdict or {}).get("summary_reason") or "")
    thesis_status = str(evidence.get("thesis_status") or "").lower()
    trigger_tags = evidence.get("trigger_tags") or []
    if isinstance(trigger_tags, str):
        trigger_tags = [trigger_tags]
    trigger_tags = {str(tag) for tag in trigger_tags}

    try:
        giveback_ratio = float(evidence.get("giveback_ratio", 0.0) or 0.0)
    except (TypeError, ValueError):
        giveback_ratio = 0.0
    try:
        current_pnl = float(evidence.get("current_pnl", 0.0) or 0.0)
    except (TypeError, ValueError):
        current_pnl = 0.0
    try:
        stop_loss_progress = float(evidence.get("stop_loss_progress", 0.0) or 0.0)
    except (TypeError, ValueError):
        stop_loss_progress = 0.0
    try:
        reduce_fraction = float(controls.get("reduce_fraction", 0.0) or 0.0)
    except (TypeError, ValueError):
        reduce_fraction = 0.0
    try:
        near_stop_loss_progress = float(
            (
                ((verdict or {}).get("supervisor_template") or {}).get("thresholds")
                or {}
            ).get("near_stop_loss_progress", 0.85)
            or 0.85
        )
    except (TypeError, ValueError):
        near_stop_loss_progress = 0.85

    thesis_break_confirmed = bool(evidence.get("thesis_break_confirmed"))
    try:
        thesis_broken_confirmations = int(evidence.get("thesis_broken_confirmations") or 0)
    except (TypeError, ValueError):
        thesis_broken_confirmations = 0
    signal_reversal = bool(evidence.get("signal_reversal"))
    if (
        thesis_status == "broken"
        and (
            thesis_break_confirmed
            or thesis_broken_confirmations >= 2
            or signal_reversal
            or stop_loss_progress >= near_stop_loss_progress
        )
    ):
        return True, "minimum_position_thesis_broken"
    if (
        giveback_ratio >= 1.0
        and current_pnl <= 0
        and stop_loss_progress >= near_stop_loss_progress
    ):
        return True, "minimum_position_full_giveback_near_stop"
    if (
        summary_reason == "profit_giveback_after_mfe"
        and "profit_giveback_after_mfe" in trigger_tags
        and current_pnl <= 0
        and reduce_fraction > 0
        and stop_loss_progress >= near_stop_loss_progress
    ):
        return True, "minimum_position_profit_giveback_near_stop"
    return False, "risk_evidence_not_strong_enough"


def normalize_event_sizing_context(
    *,
    context: dict[str, Any] | None,
    enabled: bool,
    stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = dict(context or {})
    multiplier = float(payload.get("multiplier", 1.0) or 1.0)
    payload["enabled"] = bool(enabled)
    payload["multiplier"] = max(0.0, min(1.0, multiplier))
    if stats is not None:
        payload["stats"] = stats
    else:
        payload.setdefault("stats", {})
    return payload


def build_event_sizing_fallback_context(
    *,
    enabled: bool,
    multiplier: float,
    event_near: bool,
    event: Any,
    stats: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "enabled": bool(enabled),
        "multiplier": max(0.0, min(1.0, float(multiplier or 1.0))),
        "event_near": bool(event_near),
        "event": event,
        "stats": stats or {},
    }


def protection_prices_from_reference(
    direction: int,
    reference_price: float,
    sl_dist: float,
    tp_dist: float,
    digits: int = 2,
) -> tuple[float, float]:
    ref = float(reference_price or 0.0)
    sl_delta = abs(float(sl_dist or 0.0))
    tp_delta = abs(float(tp_dist or 0.0))
    if direction == 1:
        sl_price = ref - sl_delta
        tp_price = ref + tp_delta
    else:
        sl_price = ref + sl_delta
        tp_price = ref - tp_delta
    return round(float(sl_price), int(digits)), round(float(tp_price), int(digits))
