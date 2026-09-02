"""Tests for session drawdown attribution aggregation."""
from __future__ import annotations

from backend.services.drawdown_attribution import (
    SCHEMA_VERSION,
    aggregate_reviews,
)


def _review(
    *,
    pnl: float,
    responsibility: str = "entry",
    labels: list[str] | None = None,
    factor_mc: dict | None = None,
    entry_quality: float = 0.5,
    hold_quality: float = 0.5,
    observed_at: float = 1_000_000.0,
) -> dict:
    review = {
        "primary_responsibility": responsibility,
        "responsibility_labels": labels or [],
        "factor_contributions": factor_mc or {},
    }
    return {
        "_observed_at": observed_at,
        "pnl": pnl,
        "entry_quality": entry_quality,
        "hold_quality": hold_quality,
        "outcome_label": "loss" if pnl < 0 else "win",
        "review": review,
    }


def test_aggregate_attributes_primary_responsibility_and_loss_factors() -> None:
    now = 2_000_000.0
    reviews = [
        _review(
            pnl=-10.0,
            responsibility="holding",
            labels=["thesis_broken", "holding_inefficient"],
            factor_mc={"ema_slope": -6.0, "stoch_k": -4.0},
            observed_at=now - 60,
        ),
        _review(
            pnl=-8.0,
            responsibility="execution",
            labels=["execution_slippage"],
            factor_mc={"ema_slope": -5.0, "adx": -3.0},
            observed_at=now - 120,
        ),
        _review(
            pnl=5.0,
            responsibility="entry",
            labels=[],
            factor_mc={"ema_slope": 3.0},
            observed_at=now - 180,
        ),
    ]
    report = aggregate_reviews(reviews, now=now, window_hours=24.0)

    assert report["schema_version"] == SCHEMA_VERSION
    assert report["trade_count"] == 3
    assert report["losing_count"] == 2
    assert report["winning_count"] == 1
    assert report["total_pnl"] == -13.0
    # worst pnl responsibility leads the sorted bucket list
    assert report["primary_responsibility"] == "holding"
    assert report["responsibility_buckets"][0]["responsibility"] == "holding"
    assert report["responsibility_buckets"][0]["trade_count"] == 1
    assert report["responsibility_buckets"][0]["labels"] == {
        "thesis_broken": 1,
        "holding_inefficient": 1,
    }
    # factor damage sums losing trades only; winning ema_slope +3 excluded
    factor_rows = {row["factor"]: row for row in report["top_loss_factors"]}
    assert factor_rows["ema_slope"]["loss_weighted_mc"] == -11.0
    assert "adx" in factor_rows
    assert "holding" in report["narrative"]
    assert "ema_slope" in report["narrative"]


def test_aggregate_empty_window_reports_no_reviews() -> None:
    report = aggregate_reviews([], now=2_000_000.0, window_hours=24.0)
    assert report["trade_count"] == 0
    assert report["primary_responsibility"] == "no_reviews_in_window"
    assert report["total_pnl"] == 0.0
    assert "nothing to attribute" in report["narrative"]


def test_aggregate_respects_window_cutoff() -> None:
    now = 2_000_000.0
    reviews = [
        _review(pnl=-4.0, observed_at=now - 3600),  # inside 24h window
        _review(pnl=-9.0, observed_at=now - 25 * 3600),  # outside
    ]
    report = aggregate_reviews(reviews, now=now, window_hours=24.0)
    assert report["trade_count"] == 1
    assert report["total_pnl"] == -4.0
