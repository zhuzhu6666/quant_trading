"""Test FactorHealth independence real corr matrix (C2)."""

import warnings

import numpy as np
import pytest

from alpha.factor_health import FactorHealth
from alpha.ic_tracker import ICTracker


def _make_tracker(factor_data: dict[str, np.ndarray]) -> ICTracker:
    """Helper: seed an ICTracker with synthetic data."""
    tracker = ICTracker(window=500)
    for name, vals in factor_data.items():
        rets = np.random.default_rng(42).normal(0, 0.01, len(vals))
        tracker.update(name, vals, rets)
    return tracker


def test_export_vals_returns_factor_values():
    """ICTracker.export_vals returns the factor value array (not returns)."""
    tracker = ICTracker(window=100)
    vals = np.array([1.0, 2.0, 3.0, 4.0, 5.0], dtype=np.float64)
    rets = np.array([0.01, -0.02, 0.03, -0.01, 0.02], dtype=np.float64)
    tracker.update("test_f", vals, rets)
    exported = tracker.export_vals("test_f")
    assert len(exported) == 5
    np.testing.assert_array_almost_equal(exported, vals)


def test_export_vals_unknown_returns_empty():
    tracker = ICTracker(window=100)
    assert len(tracker.export_vals("nonexistent")) == 0


def test_export_vals_filters_nan():
    """export_vals skips NaN entries (matching rolling_ic convention)."""
    tracker = ICTracker(window=100)
    vals = np.array([1.0, np.nan, 3.0, np.nan, 5.0], dtype=np.float64)
    rets = np.array([0.01, 0.02, 0.03, 0.04, 0.05], dtype=np.float64)
    tracker.update("test_f", vals, rets)
    exported = tracker.export_vals("test_f")
    assert len(exported) == 3  # NaN pairs are skipped


def test_rolling_ic_constant_series_returns_zero_without_runtime_warning():
    tracker = ICTracker(window=100)
    vals = np.ones(60, dtype=np.float64)
    rets = np.linspace(-0.01, 0.01, 60, dtype=np.float64)
    tracker.update("constant_factor", vals, rets)

    with warnings.catch_warnings(record=True) as records:
        warnings.simplefilter("always")
        ic = tracker.rolling_ic("constant_factor")

    assert ic == 0.0
    assert not any(isinstance(record.message, RuntimeWarning) for record in records)


def test_independence_perfectly_correlated():
    """Two factors with identical values → corr=1 → independence=0."""
    rng = np.random.default_rng(42)
    vals_a = rng.normal(0, 1, 200)
    vals_b = vals_a.copy()  # perfectly correlated
    rets = rng.normal(0, 0.01, 200)

    tracker = ICTracker(window=500)
    tracker.update("factor_a", vals_a, rets)
    tracker.update("factor_b", vals_b, rets)

    health = FactorHealth(tracker, active_factor_names=["factor_a", "factor_b"])
    status = health.evaluate("factor_a")
    # independence should be near 0 (perfectly collinear)
    assert status.components["independence"] < 10.0, (
        f"expected near 0 for identical factors, got {status.components['independence']}"
    )


def test_independence_uncorrelated():
    """Two factors with orthogonal values → corr≈0 → independence≈100."""
    rng = np.random.default_rng(42)
    vals_a = rng.normal(0, 1, 200)
    vals_b = rng.normal(0, 1, 200)  # independent noise
    rets = rng.normal(0, 0.01, 200)

    tracker = ICTracker(window=500)
    tracker.update("factor_a", vals_a, rets)
    tracker.update("factor_b", vals_b, rets)

    health = FactorHealth(tracker, active_factor_names=["factor_a", "factor_b"])
    status = health.evaluate("factor_a")
    # independent noise → corr should be near 0
    assert status.components["independence"] > 80.0, (
        f"expected high independence for uncorrelated factors, got {status.components['independence']}"
    )


def test_independence_inversely_correlated():
    """Two factors with vals_a ≈ -vals_b → |corr|≈1 → independence≈0."""
    rng = np.random.default_rng(42)
    vals_a = rng.normal(0, 1, 200)
    vals_b = -vals_a  # perfectly anti-correlated
    rets = rng.normal(0, 0.01, 200)

    tracker = ICTracker(window=500)
    tracker.update("factor_a", vals_a, rets)
    tracker.update("factor_b", vals_b, rets)

    health = FactorHealth(tracker, active_factor_names=["factor_a", "factor_b"])
    status = health.evaluate("factor_a")
    assert status.components["independence"] < 10.0, (
        f"expected near 0 for anti-correlated factors, got {status.components['independence']}"
    )


def test_independence_defaults_to_50_when_no_active():
    """When active_factor_names is empty, independence defaults to 50."""
    rng = np.random.default_rng(42)
    vals = rng.normal(0, 1, 200)
    rets = rng.normal(0, 0.01, 200)

    tracker = ICTracker(window=500)
    tracker.update("factor_a", vals, rets)

    health = FactorHealth(tracker, active_factor_names=[])
    status = health.evaluate("factor_a")
    assert status.components["independence"] == 50.0


def test_independence_constant_peer_does_not_emit_runtime_warning():
    rng = np.random.default_rng(42)
    rets = rng.normal(0, 0.01, 200)
    tracker = ICTracker(window=500)
    tracker.update("factor_a", rng.normal(0, 1, 200), rets)
    tracker.update("constant_peer", np.ones(200, dtype=np.float64), rets)

    health = FactorHealth(tracker, active_factor_names=["factor_a", "constant_peer"])
    with warnings.catch_warnings(record=True) as records:
        warnings.simplefilter("always")
        status = health.evaluate("factor_a")

    assert 0 <= status.components["independence"] <= 100
    assert not any(isinstance(record.message, RuntimeWarning) for record in records)


def test_independence_few_observations_returns_50():
    """With < MIN_N_OBS observations, evaluate returns UNKNOWN (no components).

    The independence=50 neutral fallback lives inside _compute_components
    for the case where we have enough observations but corrcoef fails.
    With < 100 obs, we never reach _compute_components — early return UNKNOWN.
    """
    tracker = ICTracker(window=500)
    vals = np.array([1.0, 2.0], dtype=np.float64)
    rets = np.array([0.01, -0.02], dtype=np.float64)
    tracker.update("factor_a", vals, rets)
    tracker.update("factor_b", vals + 0.1, rets)

    health = FactorHealth(tracker, active_factor_names=["factor_a", "factor_b"])
    status = health.evaluate("factor_a")
    # With only 2 observations (< MIN_N_OBS=100), status is UNKNOWN
    assert status.status == "UNKNOWN"
    assert status.components == {}


def test_independence_many_factors():
    """Check independence with 5+ factors — all uncorrelated → high scores."""
    rng = np.random.default_rng(42)
    names = [f"factor_{i}" for i in range(5)]
    tracker = ICTracker(window=500)
    rets = rng.normal(0, 0.01, 300)
    for name in names:
        vals = rng.normal(0, 1, 300)
        tracker.update(name, vals, rets)

    health = FactorHealth(tracker, active_factor_names=names)
    for name in names:
        status = health.evaluate(name)
        indep = status.components["independence"]
        assert 0 <= indep <= 100, (
            f"{name}: independence={indep} out of [0, 100]"
        )
        assert indep > 50.0, (
            f"{name}: uncorrelated factors should have high independence, got {indep}"
        )
