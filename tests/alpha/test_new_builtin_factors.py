from __future__ import annotations

import time

import numpy as np
import pandas as pd

from alpha.registry import factor_registry
from alpha.streaming_factor_engine import StreamingFactorEngine


NEW_FACTORS = [
    "htf_trend_alignment",
    "donchian_breakout_20",
    "range_expansion_20",
    "price_location_50",
    "candle_body_pressure",
    "wick_rejection",
    "morning_evening_star",
    "harami",
    "fib_retracement_position",
    "fib_level_proximity",
    "fib_rejection_confirmation",
]


def _frame(n: int = 320) -> pd.DataFrame:
    timestamps = 1_700_000_000 + np.arange(n) * 300
    close = 4500.0 + np.cumsum(0.15 + 0.4 * np.sin(np.arange(n) / 9.0))
    open_ = np.r_[close[0], close[:-1]]
    return pd.DataFrame({
        "time": timestamps,
        "timeframe": "M5",
        "open": open_,
        "high": np.maximum(open_, close) + 2.0,
        "low": np.minimum(open_, close) - 2.0,
        "close": close,
        "volume": 100.0 + (np.arange(n) % 7),
    })


def test_new_builtin_factors_return_aligned_values_and_bounds():
    frame = _frame()
    for name in NEW_FACTORS:
        values = np.asarray(factor_registry.get(name)(frame), dtype=float)
        assert values.shape == (len(frame),)
        assert np.isfinite(values).sum() > 0
        if name != "range_expansion_20":
            finite = values[np.isfinite(values)]
            assert finite.min() >= -1.0
            assert finite.max() <= 1.0


def test_higher_timeframe_factor_excludes_incomplete_current_bucket():
    frame = _frame()
    before = factor_registry.get("htf_trend_alignment")(frame)
    changed = frame.copy()
    changed.loc[changed.index[-1], "close"] = 10000.0
    changed.loc[changed.index[-1], "high"] = 10001.0
    after = factor_registry.get("htf_trend_alignment")(changed)
    assert np.isfinite(before[-1])
    assert after[-1] == before[-1]


def test_range_location_and_breakout_exclude_current_bar_range():
    frame = _frame()
    changed = frame.copy()
    changed.loc[changed.index[-1], "high"] = 99999.0
    changed.loc[changed.index[-1], "low"] = 1.0
    for name in ("donchian_breakout_20", "price_location_50"):
        before = factor_registry.get(name)(frame)
        after = factor_registry.get(name)(changed)
        assert after[-1] == before[-1]


def test_candle_geometry_factors_are_directional_and_bounded():
    frame = _frame(8)
    frame.loc[7, ["open", "high", "low", "close"]] = [100.0, 120.0, 98.0, 108.0]
    assert factor_registry.get("candle_body_pressure")(frame)[-1] > 0
    assert factor_registry.get("wick_rejection")(frame)[-1] < 0

    frame.loc[7, ["open", "high", "low", "close"]] = [100.0, 110.0, 80.0, 101.0]
    assert factor_registry.get("wick_rejection")(frame)[-1] > 0


def test_fibonacci_is_confirmed_and_does_not_repaint_earlier_bars():
    frame = _frame(480)
    before = factor_registry.get("fib_retracement_position")(frame).copy()
    before_proximity = factor_registry.get("fib_level_proximity")(frame).copy()

    changed = frame.copy()
    # This is in a later M5 bucket.  It may change future swing confirmation,
    # but must not alter already emitted values before that bucket.
    changed.loc[changed.index[-1], "high"] = 99999.0
    changed.loc[changed.index[-1], "low"] = 1.0
    after = factor_registry.get("fib_retracement_position")(changed)
    after_proximity = factor_registry.get("fib_level_proximity")(changed)
    prefix = slice(0, -12)
    np.testing.assert_allclose(before[prefix], after[prefix], equal_nan=True)
    np.testing.assert_allclose(before_proximity[prefix], after_proximity[prefix], equal_nan=True)


def test_fibonacci_outputs_are_bounded_and_streamable():
    frame = _frame(480)
    for name in ("fib_retracement_position", "fib_level_proximity", "fib_rejection_confirmation"):
        values = np.asarray(factor_registry.get(name)(frame), dtype=float)
        finite = values[np.isfinite(values)]
        assert len(finite) > 0
        assert finite.min() >= -1.0
        assert finite.max() <= 1.0


def test_donchian_detects_breakout_using_prior_channel():
    n = 60
    close = np.full(n, 100.0)
    close[-1] = 110.0
    frame = pd.DataFrame({
        "open": close,
        "high": close + 1.0,
        "low": close - 1.0,
        "close": close,
        "volume": 100.0,
        "time": 1_700_000_000 + np.arange(n) * 300,
    })
    values = factor_registry.get("donchian_breakout_20")(frame)
    assert values[-1] > 0.0


def test_streaming_engine_calculates_new_factors_without_voting_weight():
    engine = StreamingFactorEngine(max_buffer=240, factor_ids=NEW_FACTORS)
    frame = _frame(240)
    result = {}
    for row in frame.to_dict("records"):
        result = engine.append_bar(row)
    assert set(result) == set(NEW_FACTORS)
    assert result["htf_trend_alignment"] is not None
    assert result["price_location_50"] is not None


def test_streaming_integer_volume_does_not_break_existing_volume_factor():
    engine = StreamingFactorEngine(max_buffer=80, factor_ids=["vol_ma_ratio"])
    frame = _frame(80)
    frame["volume"] = frame["volume"].astype(int)
    result = {}
    for row in frame.to_dict("records"):
        result = engine.append_bar(row)
    assert result["vol_ma_ratio"] is not None
