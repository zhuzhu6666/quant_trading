from __future__ import annotations

from typing import Any

from backend.services.review_contract import build_system_issue_context


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value if value not in (None, "") else default)
    except Exception:
        return float(default)


def build_failure_taxonomy(review: dict[str, Any] | None) -> dict[str, Any]:
    payload = dict(review or {})
    labels: list[str] = []

    entry_quality = _safe_float(payload.get("entry_quality"))
    exit_quality = _safe_float(payload.get("exit_quality"))
    regime_fit = _safe_float(payload.get("regime_fit"), _safe_float(payload.get("regime_fit_score")))
    holding_efficiency = _safe_float(payload.get("holding_efficiency"))
    giveback_ratio = _safe_float(payload.get("giveback_ratio"))
    profit_capture_ratio = _safe_float(payload.get("profit_capture_ratio"))
    holding_seconds = _safe_float(payload.get("holding_seconds"))
    time_decay_score = _safe_float(payload.get("time_decay_score"))
    action_score = _safe_float(payload.get("action_score"))
    close_reason = str(payload.get("close_reason") or "")
    direction = str(payload.get("direction") or payload.get("side") or "").lower()
    thesis_status = str(payload.get("thesis_status_at_exit") or payload.get("thesis_status") or "")
    regime_shift = str(payload.get("regime_shift_at_exit") or payload.get("regime_shift") or "")
    pnl = _safe_float((payload.get("real_pnl") or {}).get("net"), _safe_float(payload.get("pnl")))
    mfe = _safe_float(payload.get("mfe"))
    mae = abs(_safe_float(payload.get("mae")))
    context_integrity = str(payload.get("context_integrity") or "full")
    same_direction_open_count = _safe_float(payload.get("same_direction_open_count"))
    event_context = payload.get("event_context") if isinstance(payload.get("event_context"), dict) else {}
    bar_context = payload.get("bar_context") if isinstance(payload.get("bar_context"), dict) else {}
    decision_quality_context = (
        payload.get("decision_quality_context")
        if isinstance(payload.get("decision_quality_context"), dict)
        else {}
    )
    market_micro_context = (
        payload.get("market_micro_context")
        if isinstance(payload.get("market_micro_context"), dict)
        else {}
    )
    data_quality_context = (
        payload.get("data_quality_context")
        if isinstance(payload.get("data_quality_context"), dict)
        else {}
    )
    system_issue_context = (
        payload.get("system_issue_context")
        if isinstance(payload.get("system_issue_context"), dict)
        else build_system_issue_context(payload)
    )
    adverse_slippage = _safe_float(market_micro_context.get("adverse_slippage_points"))
    factor_conflict_ratio = _safe_float(decision_quality_context.get("factor_conflict_ratio"))
    negative_contribution_abs = _safe_float(decision_quality_context.get("negative_contribution_abs"))
    positive_contribution_abs = _safe_float(decision_quality_context.get("positive_contribution_abs"))
    bar_close_location = _safe_float(bar_context.get("bar_close_location"), 0.5)
    chased_long = direction in {"buy", "long"} and bar_close_location >= 0.82
    chased_short = direction in {"sell", "short"} and bar_close_location <= 0.18
    event_multiplier = _safe_float(event_context.get("event_multiplier"), 1.0)

    if entry_quality >= 0.6 and exit_quality <= 0.45:
        labels.append("entry_good_exit_bad")
    if mfe > 0 and pnl >= 0 and giveback_ratio >= 0.5 and profit_capture_ratio < 0.7:
        labels.append("alpha_correct_but_capture_failed")
    if close_reason == "holding_timeout" or (holding_seconds >= 24 * 3600 and time_decay_score <= 0.35):
        labels.append("holding_too_long")
    if regime_shift == "confirmed":
        labels.append("regime_changed_during_hold")
    if regime_fit <= 0.4 and entry_quality >= 0.45:
        labels.append("factor_logic_ok_but_param_suspect")
    if thesis_status == "broken":
        labels.append("thesis_broken")
    if holding_efficiency < 0.35 and holding_seconds > 0:
        labels.append("holding_inefficient")
    if pnl <= 0 and same_direction_open_count >= 2:
        labels.append("entry_cluster_risk")
    if pnl <= 0 and bool(event_context.get("event_near")):
        labels.append("event_window_bad_entry")
    if pnl <= 0 and bool(event_context.get("event_near")) and event_multiplier < 1.0:
        labels.append("macro_event_overridden")
    if pnl <= 0 and adverse_slippage > 0:
        labels.append("execution_slippage")
    if pnl <= 0 and data_quality_context and not bool(data_quality_context.get("quote_fresh", True)):
        labels.append("data_quality_issue")
    if pnl <= 0 and system_issue_context:
        for label in system_issue_context.get("labels") or []:
            if label not in labels:
                labels.append(str(label))
    if pnl <= 0 and (chased_long or chased_short):
        labels.append("entry_chase")
    if pnl <= 0 and abs(action_score) < 0.45 and entry_quality <= 0.5:
        labels.append("weak_signal_overtraded")
    if pnl <= 0 and (factor_conflict_ratio >= 0.4 or negative_contribution_abs > positive_contribution_abs):
        labels.append("conflicting_factor_entry")
    if pnl <= 0 and mfe <= max(2.0, mae * 0.35) and mae >= 5.0:
        labels.append("low_reward_to_risk_entry")

    primary = "unclear"
    system_primary = str((system_issue_context or {}).get("primary_responsibility") or "")
    if system_primary:
        primary = system_primary
    elif "entry_cluster_risk" in labels:
        primary = "timing"
    elif "entry_chase" in labels:
        primary = "timing"
    elif "event_window_bad_entry" in labels:
        primary = "timing"
    elif "macro_event_overridden" in labels:
        primary = "event_risk"
    elif "data_quality_issue" in labels:
        primary = "data_quality"
    elif "execution_slippage" in labels:
        primary = "execution"
    elif "weak_signal_overtraded" in labels:
        primary = "signal_quality"
    elif "conflicting_factor_entry" in labels:
        primary = "factor_conflict"
    elif "low_reward_to_risk_entry" in labels:
        primary = "reward_risk"
    elif "entry_good_exit_bad" in labels:
        primary = "exit"
    elif "alpha_correct_but_capture_failed" in labels:
        primary = "exit"
    elif "holding_too_long" in labels:
        primary = "timing"
    elif "regime_changed_during_hold" in labels:
        primary = "regime"
    elif "factor_logic_ok_but_param_suspect" in labels:
        primary = "parameter"
    elif "thesis_broken" in labels:
        primary = "thesis"
    elif "holding_inefficient" in labels:
        primary = "holding"

    confidence = 0.35 + 0.12 * len(labels)
    if context_integrity != "full":
        confidence *= 0.7
    confidence = max(0.1, min(1.0, confidence))

    return {
        "primary_responsibility": primary,
        "responsibility_labels": labels,
        "confidence": round(confidence, 3),
        "context_integrity": context_integrity,
        "system_issue_context": system_issue_context or {},
    }
