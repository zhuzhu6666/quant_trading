from __future__ import annotations

from typing import Any


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value if value not in (None, "") else default)
    except Exception:
        return float(default)


def normalize_trade_review_contract(
    review_payload: dict[str, Any] | None,
    *,
    entry_quality: Any = 0.0,
    hold_quality: Any = 0.0,
    exit_quality: Any = 0.0,
    regime_fit_score: Any = 0.0,
    execution_quality: Any = 0.0,
) -> dict[str, Any]:
    review = dict(review_payload or {})
    normalized = dict(review)

    normalized["contract_version"] = str(
        review.get("contract_version")
        or review.get("review_contract_version")
        or "phase_d.v1"
    )
    normalized["entry_quality"] = round(
        _safe_float(review.get("entry_quality"), _safe_float(entry_quality)), 6
    )
    normalized["hold_quality"] = round(
        _safe_float(review.get("hold_quality"), _safe_float(hold_quality)), 6
    )
    normalized["exit_quality"] = round(
        _safe_float(review.get("exit_quality"), _safe_float(exit_quality)), 6
    )
    normalized["regime_fit_score"] = round(
        _safe_float(review.get("regime_fit_score"), _safe_float(regime_fit_score)), 6
    )
    normalized["regime_fit"] = round(
        _safe_float(review.get("regime_fit"), normalized["regime_fit_score"]), 6
    )
    normalized["execution_quality"] = round(
        _safe_float(review.get("execution_quality"), _safe_float(execution_quality)), 6
    )
    normalized["holding_efficiency"] = round(
        _safe_float(review.get("holding_efficiency")), 6
    )
    normalized["profit_capture_ratio"] = round(
        _safe_float(review.get("profit_capture_ratio")), 6
    )
    normalized["giveback_ratio"] = round(
        _safe_float(review.get("giveback_ratio")), 6
    )
    normalized["time_in_profit"] = round(
        _safe_float(
            review.get("time_in_profit"),
            _safe_float(review.get("time_in_profit_seconds")),
        ),
        6,
    )
    normalized["time_in_profit_seconds"] = round(
        _safe_float(
            review.get("time_in_profit_seconds"),
            normalized["time_in_profit"],
        ),
        6,
    )
    normalized["time_in_profit_ratio"] = round(
        _safe_float(review.get("time_in_profit_ratio")), 6
    )
    normalized["thesis_status_at_exit"] = str(
        review.get("thesis_status_at_exit")
        or review.get("thesis_status")
        or ""
    )
    normalized["regime_shift_at_exit"] = str(
        review.get("regime_shift_at_exit")
        or review.get("regime_shift")
        or ""
    )
    return normalized
