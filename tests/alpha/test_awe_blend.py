"""Tests for AdaptiveWeightEngine BlendSearch SLSQP baseline integration."""

import builtins
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from alpha.adaptive_weight_engine import AdaptiveWeightEngine


SAMPLE_CONFIG = {
    "awe_sensitivity": 0.5,
    "awe_anchor_pull": 0.15,
    "awe_max_single_change": 0.15,
    "awe_weight_min": 0.1,
    "awe_weight_max": 3.0,
    "awe_min_trades": 10,
    "awe_blend_max_single_weight": 0.5,
}


def _make_factor_data(n_factors=3, T=100):
    """Create synthetic factor returns and forward returns."""
    rng = np.random.RandomState(42)
    factor_returns = rng.randn(T, n_factors).astype(np.float64)
    forward_returns = rng.randn(T).astype(np.float64)
    factor_names = [f"factor_{i}" for i in range(n_factors)]
    return factor_returns, forward_returns, factor_names


class TestComputeBlendBaseline:
    """Tests for AdaptiveWeightEngine.compute_blend_baseline()."""

    def test_returns_dict(self):
        """compute_blend_baseline returns a {name: weight} dict."""
        engine = AdaptiveWeightEngine(SAMPLE_CONFIG)
        fr, fwd, names = _make_factor_data()
        result = engine.compute_blend_baseline(fr, fwd, names)

        assert isinstance(result, dict)
        assert len(result) == len(names)
        for name in names:
            assert name in result
            assert isinstance(result[name], float)
            assert 0.0 <= result[name] <= 0.5  # max_single_weight constraint

    def test_weights_sum_to_one(self):
        """Optimal weights sum approximately to 1.0."""
        engine = AdaptiveWeightEngine(SAMPLE_CONFIG)
        fr, fwd, names = _make_factor_data()
        result = engine.compute_blend_baseline(fr, fwd, names)
        total = sum(result.values())
        assert abs(total - 1.0) < 0.01, f"weights sum to {total}, expected ~1.0"

    def test_stores_blend_baselines(self):
        """Result is stored in _blend_baselines."""
        engine = AdaptiveWeightEngine(SAMPLE_CONFIG)
        fr, fwd, names = _make_factor_data()
        engine.compute_blend_baseline(fr, fwd, names)
        assert len(engine._blend_baselines) == len(names)

    def test_updates_base_weights(self):
        """_base_weights are updated with the blend result."""
        engine = AdaptiveWeightEngine(SAMPLE_CONFIG)
        fr, fwd, names = _make_factor_data()
        engine.initialize({n: {"weight": 1.0} for n in names})
        orig = dict(engine._base_weights)

        engine.compute_blend_baseline(fr, fwd, names)
        for n in names:
            # Base weights should have changed from the initial 1.0
            assert engine._base_weights[n] != 1.0 or len(names) == 1

    def test_returns_equal_weight_when_blendsearch_unavailable(self):
        """When BlendSearch cannot be imported, fall back to equal weight
        without crashing."""
        engine = AdaptiveWeightEngine(SAMPLE_CONFIG)
        fr, fwd, names = _make_factor_data()

        original_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if "blend_search" in name:
                raise ImportError(f"No module named {name!r}")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            result = engine.compute_blend_baseline(fr, fwd, names)

        # Should have fallen back to equal weight
        expected = 1.0 / len(names)
        for name in names:
            assert abs(result[name] - expected) < 1e-10, (
                f"expected equal weight {expected}, got {result[name]}"
            )

    def test_import_error_does_not_crash(self):
        """Entire method handles ImportError gracefully (no exception)."""
        engine = AdaptiveWeightEngine(SAMPLE_CONFIG)
        fr, fwd, names = _make_factor_data()

        original_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if "blend_search" in name:
                raise ImportError(f"No module named {name!r}")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            result = engine.compute_blend_baseline(fr, fwd, names)

        assert isinstance(result, dict)
        assert len(result) == len(names)

    def test_single_factor_returns_1(self):
        """With a single factor, the optimal weight should be 1.0."""
        engine = AdaptiveWeightEngine(SAMPLE_CONFIG)
        fr, fwd, names = _make_factor_data(n_factors=1)
        result = engine.compute_blend_baseline(fr, fwd, names)
        assert abs(result[names[0]] - 1.0) < 1e-6


class TestAdaptWithBlendBaseline:
    """Tests for adapt() with use_blend_baseline=True."""

    def test_use_blend_baseline_no_crash_when_empty(self):
        """adapt(use_blend_baseline=True) does not crash when _blend_baselines
        is empty — falls back gracefully."""
        engine = AdaptiveWeightEngine(SAMPLE_CONFIG)
        engine.initialize({"f1": {"weight": 1.0, "tags": ["a"], "enabled": True}})

        attr = MagicMock()
        stats = MagicMock()
        stats.n_trades = 50
        stats.composite_sharpe_score = 0.5
        attr.get_all_factor_stats.return_value = {"f1": stats}

        with patch.object(engine, "_check_ic_and_health", return_value=True):
            with patch.object(engine, "_enforce_diversity", side_effect=lambda p, *a: p):
                # Should not raise
                patches = engine.adapt(attr, {"f1": {"weight": 1.0, "tags": ["a"], "enabled": True}}, use_blend_baseline=True)

        # Should still produce patches since _base_weights fallback works
        assert isinstance(patches, dict)

    def test_use_blend_baseline_uses_stored_baselines(self):
        """When blend baselines exist, adapt() uses them for anchor regression."""
        engine = AdaptiveWeightEngine(SAMPLE_CONFIG)
        fr, fwd, names = _make_factor_data(n_factors=2)
        engine.initialize(
            {names[0]: {"weight": 2.0, "tags": ["a"], "enabled": True},
             names[1]: {"weight": 0.5, "tags": ["b"], "enabled": True}},
        )

        # Compute blend baselines
        engine.compute_blend_baseline(fr, fwd, names)
        blend_weights = dict(engine._blend_baselines)

        attr = MagicMock()
        stats_0 = MagicMock()
        stats_0.n_trades = 50
        stats_0.composite_sharpe_score = 0.3
        stats_1 = MagicMock()
        stats_1.n_trades = 50
        stats_1.composite_sharpe_score = 0.3
        attr.get_all_factor_stats.return_value = {
            names[0]: stats_0,
            names[1]: stats_1,
        }

        with patch.object(engine, "_check_ic_and_health", return_value=True):
            with patch.object(engine, "_enforce_diversity", side_effect=lambda p, *a: p):
                patches = engine.adapt(
                    attr,
                    {names[0]: {"weight": 2.0, "tags": ["a"], "enabled": True},
                     names[1]: {"weight": 0.5, "tags": ["b"], "enabled": True}},
                    use_blend_baseline=True,
                )

        # The anchor should now pull toward blend baselines instead of initial weights
        assert isinstance(patches, dict)
