from __future__ import annotations

from typing import Any


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
    close_reason = str(payload.get("close_reason") or "")
    thesis_status = str(payload.get("thesis_status_at_exit") or payload.get("thesis_status") or "")
    regime_shift = str(payload.get("regime_shift_at_exit") or payload.get("regime_shift") or "")
    pnl = _safe_float((payload.get("real_pnl") or {}).get("net"), _safe_float(payload.get("pnl")))
    mfe = _safe_float(payload.get("mfe"))
    context_integrity = str(payload.get("context_integrity") or "full")

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

    primary = "unclear"
    if "entry_good_exit_bad" in labels:
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
    }
