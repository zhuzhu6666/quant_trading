from __future__ import annotations

from typing import Any

from backend.services.position_supervisor_templates import normalize_position_supervisor_template


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except Exception:
        return int(default)


def _side_name(direction: int) -> str:
    return "long" if int(direction or 0) >= 0 else "short"


def _tightened_sl(
    *,
    direction: int,
    entry_price: float,
    current_price: float,
    current_sl: float,
    profit_capture_ratio: float,
) -> float:
    if entry_price <= 0 or current_price <= 0:
        return 0.0
    if direction >= 0:
        breakeven = entry_price
        lock_price = entry_price + max(0.0, (current_price - entry_price) * max(0.25, profit_capture_ratio * 0.6))
        target = max(current_sl, breakeven, lock_price)
    else:
        breakeven = entry_price
        lock_price = entry_price - max(0.0, (entry_price - current_price) * max(0.25, profit_capture_ratio * 0.6))
        target = min(current_sl if current_sl > 0 else breakeven, breakeven, lock_price)
    return round(target, 2)


def _target_progress(
    *,
    direction: int,
    entry_price: float,
    current_price: float,
    target_price: float,
    target_kind: str,
) -> float:
    if entry_price <= 0 or current_price <= 0 or target_price <= 0:
        return 0.0
    if target_kind == "tp":
        denom = (target_price - entry_price) if direction >= 0 else (entry_price - target_price)
        move = (current_price - entry_price) if direction >= 0 else (entry_price - current_price)
    else:
        denom = (entry_price - target_price) if direction >= 0 else (target_price - entry_price)
        move = (entry_price - current_price) if direction >= 0 else (current_price - entry_price)
    if denom <= 0:
        return 0.0
    return _clamp(move / denom, 0.0, 10.0)


def humanize_supervisor_reason(action: str, reason: str, evidence: dict[str, Any] | None = None) -> str:
    evidence = evidence or {}
    if reason == "holding_timeout_exceeded":
        return "这笔仓位已经超过持仓时长上限，系统建议主动收口，不再继续等待原始止盈止损。"
    if reason == "thesis_broken":
        return "仓位 thesis 已经被判定为失效，继续占用风险预算的价值很低，系统建议直接退出。"
    if reason == "profit_giveback_after_mfe":
        return "仓位曾经浮盈明显，但已经出现较大回吐，系统建议先收紧保护，避免继续把已证明的利润吐回去。"
    if reason == "time_decay_and_low_efficiency":
        return "这笔仓位已经拿得偏久，但收益效率没有跟上，系统判断继续硬拿的性价比在下降。"
    if reason == "regime_shift_detected":
        return "当前市场状态与入场时不再一致，系统怀疑这笔仓位的适用环境已经发生切换。"
    if reason == "near_take_profit_capture":
        return "仓位已经非常接近原始止盈目标，系统建议直接兑现利润，避免临门回吐。"
    if reason == "near_stop_loss_preemptive_exit":
        return "仓位已经非常接近止损，且持仓证据偏弱，系统建议提前止损离场，不再等到被动打掉。"
    if action == "reduce":
        return "系统判断这笔仓位仍有逻辑，但不值得继续满仓承受同样风险，建议先降一部分。"
    if action == "tighten":
        return "系统判断仓位还没到必须退出，但保护应该更紧，不适合继续裸拿。"
    return "系统判断这笔仓位暂时仍可继续持有，没有看到足够强的主动收口信号。"


def evaluate_position_supervisor(position_context: dict[str, Any]) -> dict[str, Any]:
    template = normalize_position_supervisor_template(
        position_context.get("position_supervisor_template")
        or position_context.get("supervisor_template")
        or position_context.get("template")
    )
    thresholds = template.get("thresholds") or {}
    position = position_context.get("position") or {}
    risk = position_context.get("risk") or {}
    temporal = position_context.get("temporal_context") or {}
    market_space = position_context.get("market_space_context") or {}
    entry_context = position_context.get("entry_context") or {}

    position_id = str(position.get("position_id") or position.get("ticket") or "")
    direction = _safe_int(position.get("direction") or (1 if position.get("type") == "buy" else -1 if position.get("type") == "sell" else 0))
    entry_price = _safe_float(position.get("entry_price") or position.get("open_price") or position.get("price_open"))
    current_price = _safe_float(position.get("current_price") or position.get("price_current"))
    current_sl = _safe_float(position.get("sl") or position.get("stop_loss"))
    current_tp = _safe_float(position.get("tp") or position.get("take_profit"))
    current_pnl = _safe_float(position.get("unrealized_pnl", position.get("profit", position.get("pnl"))))
    volume = _safe_float(position.get("volume") or position.get("api_volume"))

    holding_seconds = _safe_float(temporal.get("holding_seconds"))
    max_holding_seconds = _safe_float(risk.get("max_holding_seconds"))
    timeout_ratio = _safe_float(risk.get("holding_timeout_ratio"))
    if timeout_ratio <= 0 and max_holding_seconds > 0:
        timeout_ratio = holding_seconds / max_holding_seconds

    mfe = _safe_float(risk.get("mfe"))
    mae = _safe_float(risk.get("mae"))
    giveback_ratio = _safe_float(risk.get("giveback_ratio"))
    profit_capture_ratio = _safe_float(risk.get("profit_capture_ratio"))
    time_in_profit = _safe_float(risk.get("time_in_profit", risk.get("time_in_profit_seconds")))
    holding_efficiency = _safe_float(risk.get("holding_efficiency"))
    time_decay_score = _safe_float(risk.get("time_decay_score"))
    thesis_status = str(risk.get("thesis_status") or "intact")
    regime_shift = str(risk.get("regime_shift") or "none")

    trigger_tags: list[str] = []
    action = "hold"
    summary_reason = "position_healthy"
    severity = "info"
    min_thesis_break_seconds = _safe_float(thresholds.get("min_thesis_break_seconds"))
    broken_holding_efficiency_threshold = _safe_float(thresholds.get("broken_holding_efficiency_threshold"), 0.20)
    giveback_reduce_threshold = _safe_float(thresholds.get("giveback_reduce_threshold"), 0.70)
    giveback_tighten_threshold = _safe_float(thresholds.get("giveback_tighten_threshold"), 0.35)
    profit_capture_min_threshold = _safe_float(thresholds.get("profit_capture_min_threshold"), 0.35)
    time_decay_reduce_threshold = _safe_float(thresholds.get("time_decay_reduce_threshold"), 0.35)
    timeout_tighten_ratio = _safe_float(thresholds.get("timeout_tighten_ratio"), 0.80)
    timeout_reduce_ratio = _safe_float(thresholds.get("timeout_reduce_ratio"), 0.80)
    weakening_efficiency_threshold = _safe_float(thresholds.get("weakening_holding_efficiency_threshold"), 0.45)
    near_tp_progress_threshold = _safe_float(thresholds.get("near_take_profit_progress"), 0.92)
    near_sl_progress_threshold = _safe_float(thresholds.get("near_stop_loss_progress"), 0.85)
    near_sl_efficiency_threshold = _safe_float(thresholds.get("near_stop_loss_efficiency_threshold"), 0.25)
    take_profit_progress = _target_progress(
        direction=direction,
        entry_price=entry_price,
        current_price=current_price,
        target_price=current_tp,
        target_kind="tp",
    )
    stop_loss_progress = _target_progress(
        direction=direction,
        entry_price=entry_price,
        current_price=current_price,
        target_price=current_sl,
        target_kind="sl",
    )

    if max_holding_seconds > 0 and holding_seconds >= max_holding_seconds:
        trigger_tags.append("holding_timeout_exceeded")
        action = "close"
        summary_reason = "holding_timeout_exceeded"
        severity = "warn"
    elif current_pnl > 0 and take_profit_progress >= near_tp_progress_threshold:
        trigger_tags.append("near_take_profit")
        action = "close"
        summary_reason = "near_take_profit_capture"
        severity = "info"
    elif (
        stop_loss_progress >= near_sl_progress_threshold
        and current_pnl <= 0
        and (
            thesis_status == "broken"
            or holding_efficiency <= near_sl_efficiency_threshold
            or time_decay_score <= time_decay_reduce_threshold
            or regime_shift == "confirmed"
        )
    ):
        trigger_tags.append("near_stop_loss")
        action = "close"
        summary_reason = "near_stop_loss_preemptive_exit"
        severity = "warn"
    elif (
        thesis_status == "broken"
        and holding_seconds >= min_thesis_break_seconds
        and holding_efficiency <= broken_holding_efficiency_threshold
    ):
        trigger_tags.append("thesis_broken")
        action = "close"
        summary_reason = "thesis_broken"
        severity = "error"
    elif regime_shift == "confirmed" and current_pnl <= 0:
        trigger_tags.append("regime_shift_detected")
        action = "close"
        summary_reason = "regime_shift_detected"
        severity = "warn"
    elif giveback_ratio >= giveback_reduce_threshold and mfe > 0 and profit_capture_ratio <= profit_capture_min_threshold:
        trigger_tags.append("profit_giveback_after_mfe")
        action = "reduce"
        summary_reason = "profit_giveback_after_mfe"
        severity = "warn"
    elif time_decay_score <= time_decay_reduce_threshold or (timeout_ratio >= timeout_reduce_ratio and holding_efficiency <= weakening_efficiency_threshold):
        trigger_tags.append("time_decay_and_low_efficiency")
        action = "reduce" if current_pnl > 0 else "close"
        summary_reason = "time_decay_and_low_efficiency"
        severity = "warn"
    elif giveback_ratio >= giveback_tighten_threshold or thesis_status in {"weakening", "broken"} or timeout_ratio >= timeout_tighten_ratio:
        if giveback_ratio >= giveback_tighten_threshold:
            trigger_tags.append("profit_giveback_after_mfe")
            summary_reason = "profit_giveback_after_mfe"
        elif timeout_ratio >= timeout_tighten_ratio:
            trigger_tags.append("time_decay_and_low_efficiency")
            summary_reason = "time_decay_and_low_efficiency"
        elif thesis_status == "broken":
            trigger_tags.append("thesis_broken_delayed")
            summary_reason = "thesis_weakening"
        else:
            trigger_tags.append("thesis_weakening")
            summary_reason = "thesis_weakening"
        action = "tighten"
        severity = "warn"

    confidence = 0.35
    if action == "hold":
        confidence = 0.55 + _clamp(holding_efficiency * 0.35 + profit_capture_ratio * 0.1, 0.0, 0.35)
    elif action == "tighten":
        confidence = 0.60 + _clamp(giveback_ratio * 0.2 + max(0.0, timeout_ratio - 0.5) * 0.2, 0.0, 0.25)
    elif action == "reduce":
        confidence = 0.70 + _clamp(giveback_ratio * 0.2 + (0.5 - time_decay_score) * 0.25, 0.0, 0.2)
    elif action == "close":
        confidence = 0.82 + _clamp((1.0 - time_decay_score) * 0.15 + (0.15 if thesis_status == "broken" else 0.0), 0.0, 0.15)
    confidence = round(_clamp(confidence), 4)

    recommended_controls = {
        "target_stop_loss": 0.0,
        "target_take_profit": current_tp,
        "reduce_fraction": 0.0,
        "close_reason": "",
        "protection_mode": "",
    }
    if action == "tighten":
        recommended_controls["target_stop_loss"] = _tightened_sl(
            direction=direction,
            entry_price=entry_price,
            current_price=current_price,
            current_sl=current_sl,
            profit_capture_ratio=profit_capture_ratio,
        )
        recommended_controls["close_reason"] = "supervisor_tighten"
        recommended_controls["protection_mode"] = "tightened_stop"
    elif action == "reduce":
        recommended_controls["reduce_fraction"] = 0.5
        recommended_controls["close_reason"] = "supervisor_reduce"
        recommended_controls["protection_mode"] = "partial_de_risk"
        if current_pnl > 0:
            recommended_controls["target_stop_loss"] = _tightened_sl(
                direction=direction,
                entry_price=entry_price,
                current_price=current_price,
                current_sl=current_sl,
                profit_capture_ratio=max(profit_capture_ratio, 0.5),
            )
    elif action == "close":
        recommended_controls["close_reason"] = summary_reason
        recommended_controls["protection_mode"] = "full_exit"

    evidence = {
        "holding_seconds": round(holding_seconds, 6),
        "holding_timeout_ratio": round(max(0.0, timeout_ratio), 6),
        "mfe": round(mfe, 6),
        "mae": round(mae, 6),
        "giveback_ratio": round(giveback_ratio, 6),
        "profit_capture_ratio": round(profit_capture_ratio, 6),
        "time_in_profit": round(time_in_profit, 6),
        "holding_efficiency": round(holding_efficiency, 6),
        "time_decay_score": round(time_decay_score, 6),
        "thesis_status": thesis_status,
        "regime_shift": regime_shift,
        "current_pnl": round(current_pnl, 6),
        "volume": round(volume, 6),
        "distance_to_sl": _safe_float(market_space.get("distance_to_sl")),
        "distance_to_tp": _safe_float(market_space.get("distance_to_tp")),
        "take_profit_progress": round(take_profit_progress, 6),
        "stop_loss_progress": round(stop_loss_progress, 6),
        "entry_regime": str(entry_context.get("entry_regime") or risk.get("entry_regime") or ""),
        "current_regime": str(risk.get("current_regime") or ""),
        "trigger_tags": trigger_tags,
        "supervisor_template_id": str(template.get("template_id") or ""),
        "supervisor_template_version": str(template.get("template_version") or ""),
    }
    human_summary = humanize_supervisor_reason(action, summary_reason, evidence)
    return {
        "position_id": position_id,
        "decision_ts": _safe_float(temporal.get("decision_ts")),
        "action": action,
        "confidence": confidence,
        "severity": severity,
        "thesis_status": thesis_status,
        "regime_shift": regime_shift,
        "summary_reason": summary_reason,
        "human_summary": human_summary,
        "evidence": evidence,
        "recommended_controls": recommended_controls,
        "supervisor_template": {
            "schema_version": str(template.get("schema_version") or ""),
            "template_id": str(template.get("template_id") or ""),
            "template_version": str(template.get("template_version") or ""),
            "template_role": str(template.get("template_role") or ""),
            "thresholds": thresholds,
        },
        "requires_risk_verdict": action in {"tighten", "reduce", "close"},
        "action_label": {
            "hold": "继续持有",
            "tighten": "收紧保护",
            "reduce": "先降仓位",
            "close": "主动平仓",
        }.get(action, action),
        "position_side": _side_name(direction),
    }
