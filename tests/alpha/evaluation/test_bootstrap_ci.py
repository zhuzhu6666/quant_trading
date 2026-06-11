"""tests/alpha/evaluation/test_bootstrap_ci.py — Tests for BootstrapCI."""
import numpy as np
import pytest

from alpha.evaluation.bootstrap_ci import BootstrapCI, CIResult


class TestBootstrapCI:
    """Tests for BootstrapCI."""

    def test_default_init(self):
        """Default parameters should be set correctly."""
        b = BootstrapCI()
        assert b.alpha == 0.05
        assert b.n_iterations == 1000
        assert b.annualization_factor == 252.0

    def test_custom_init(self):
        """Custom parameters should be accepted."""
        b = BootstrapCI(alpha=0.01, n_iterations=5000, random_seed=42, annualization_factor=52.0)
        assert b.alpha == 0.01
        assert b.n_iterations == 5000
        assert b.annualization_factor == 52.0

    def test_invalid_alpha_raises(self):
        """alpha outside (0, 1) should raise ValueError."""
        with pytest.raises(ValueError, match="alpha"):
            BootstrapCI(alpha=0.0)
        with pytest.raises(ValueError, match="alpha"):
            BootstrapCI(alpha=1.0)
        with pytest.raises(ValueError, match="alpha"):
            BootstrapCI(alpha=-0.1)

    def test_invalid_n_iterations_raises(self):
        """n_iterations < 10 should raise ValueError."""
        with pytest.raises(ValueError, match="n_iterations"):
            BootstrapCI(n_iterations=0)
        with pytest.raises(ValueError, match="n_iterations"):
            BootstrapCI(n_iterations=5)

    def test_repr(self):
        """__repr__ should include key parameters."""
        b = BootstrapCI(alpha=0.05, n_iterations=1000)
        r = repr(b)
        assert "alpha=0.05" in r
        assert "n_iterations=1000" in r

    # ── ci_mean ─────────────────────────────────────────────────────

    def test_ci_mean_returns_ciresult(self):
        """ci_mean should return a CIResult."""
        b = BootstrapCI(n_iterations=100, random_seed=42)
        samples = np.random.default_rng(42).normal(0, 1, 500)
        result = b.ci_mean(samples)
        assert isinstance(result, CIResult)
        assert result.alpha == 0.05
        assert result.n_iterations == 100

    def test_ci_mean_point_estimate(self):
        """Point estimate should be the sample mean."""
        b = BootstrapCI(n_iterations=100, random_seed=42)
        samples = np.array([1.0, 2.0, 3.0, 4.0, 5.0], dtype=np.float64)
        result = b.ci_mean(samples)
        assert abs(result.point_estimate - 3.0) < 1e-10

    def test_ci_mean_ci_contains_true_mean(self):
        """The true mean should be inside the CI for a large sample."""
        rng = np.random.default_rng(42)
        samples = rng.normal(0, 1, 2000)
        b = BootstrapCI(alpha=0.05, n_iterations=500, random_seed=42)
        result = b.ci_mean(samples)
        # True mean is 0, should be within CI
        assert result.ci_lower <= result.point_estimate <= result.ci_upper

    def test_ci_mean_ci_lower_less_than_upper(self):
        """CI lower bound should be less than upper bound."""
        rng = np.random.default_rng(42)
        samples = rng.normal(0, 1, 500)
        b = BootstrapCI(n_iterations=200, random_seed=42)
        result = b.ci_mean(samples)
        assert result.ci_lower < result.ci_upper

    def test_ci_mean_nan_handling(self):
        """NaN values should be dropped."""
        b = BootstrapCI(n_iterations=100, random_seed=42)
        samples = np.array([1.0, 2.0, np.nan, 4.0, np.nan, 6.0])
        result = b.ci_mean(samples)
        assert abs(result.point_estimate - 3.25) < 1e-10  # mean of [1,2,4,6]

    def test_ci_mean_bootstrap_samples_shape(self):
        """bootstrap_samples should have shape (n_iterations,)."""
        b = BootstrapCI(n_iterations=100, random_seed=42)
        samples = np.random.default_rng(42).normal(0, 1, 200)
        result = b.ci_mean(samples)
        assert result.bootstrap_samples.shape == (100,)

    # ── ci_sharpe ──────────────────────────────────────────────────

    def test_ci_sharpe_returns_ciresult(self):
        """ci_sharpe should return a CIResult."""
        b = BootstrapCI(n_iterations=100, random_seed=42)
        returns = np.random.default_rng(42).normal(0.001, 0.02, 500)
        result = b.ci_sharpe(returns)
        assert isinstance(result, CIResult)

    def test_ci_sharpe_zero_std(self):
        """ci_sharpe should handle zero-variance returns gracefully."""
        b = BootstrapCI(n_iterations=100, random_seed=42)
        returns = np.ones(100) * 0.001
        result = b.ci_sharpe(returns)
        assert result.point_estimate == 0.0

    def test_ci_sharpe_positive_for_positive_returns(self):
        """Positive returns should yield a positive Sharpe ratio."""
        rng = np.random.default_rng(42)
        returns = rng.normal(0.001, 0.02, 1000)
        b = BootstrapCI(n_iterations=200, random_seed=42)
        result = b.ci_sharpe(returns)
        assert result.point_estimate > 0.0

    def test_ci_sharpe_annualization(self):
        """Sharpe should scale with sqrt(annualization_factor)."""
        b_daily = BootstrapCI(n_iterations=100, random_seed=42, annualization_factor=252)
        b_weekly = BootstrapCI(n_iterations=100, random_seed=42, annualization_factor=52)
        returns = np.random.default_rng(42).normal(0.001, 0.02, 500)
        daily_sharpe = b_daily.ci_sharpe(returns).point_estimate
        weekly_sharpe = b_weekly.ci_sharpe(returns).point_estimate
        # sqrt(252/52) ≈ 2.2
        expected_ratio = np.sqrt(252.0 / 52.0)
        assert abs(daily_sharpe / weekly_sharpe - expected_ratio) < 0.1

    # ── ci_ic ──────────────────────────────────────────────────────

    def test_ci_ic_returns_ciresult(self):
        """ci_ic should return a CIResult."""
        b = BootstrapCI(n_iterations=100, random_seed=42)
        signal = np.random.default_rng(42).normal(0, 1, 200)
        returns = signal * 0.5 + np.random.default_rng(43).normal(0, 1, 200)
        result = b.ci_ic(signal, returns)
        assert isinstance(result, CIResult)

    def test_ci_ic_known_correlation(self):
        """IC for noisily related data should be non-zero."""
        rng = np.random.default_rng(42)
        n = 500
        signal = rng.normal(0, 1, n)
        # Returns = signal + noise => moderate positive IC
        returns = signal * 0.3 + rng.normal(0, 0.5, n)
        b = BootstrapCI(n_iterations=200, random_seed=42)
        result = b.ci_ic(signal, returns)
        assert result.point_estimate > 0.1

    def test_ci_ic_zero_correlation(self):
        """IC for independent data should be near zero."""
        rng = np.random.default_rng(42)
        n = 500
        signal = rng.normal(0, 1, n)
        returns = rng.normal(0, 1, n)
        b = BootstrapCI(n_iterations=200, random_seed=42)
        result = b.ci_ic(signal, returns)
        assert abs(result.point_estimate) < 0.1

    def test_ci_ic_length_mismatch_raises(self):
        """Length mismatch between signal and returns should raise ValueError."""
        b = BootstrapCI(n_iterations=100, random_seed=42)
        with pytest.raises(ValueError, match="shape"):
            b.ci_ic(np.array([1, 2, 3]), np.array([1, 2]))

    def test_ci_ic_nan_handling(self):
        """NaN values should be dropped pairwise."""
        b = BootstrapCI(n_iterations=100, random_seed=42)
        signal = np.array([1.0, np.nan, 3.0, 4.0, np.nan])
        returns = np.array([0.1, 0.2, np.nan, 0.4, 0.5])
        # After dropna: (1.0, 0.1) and (4.0, 0.4) remain
        result = b.ci_ic(signal, returns)
        # Only 2 points left, so IC should be 1.0 (perfectly aligned)
        assert not np.isnan(result.point_estimate)

    def test_ci_ic_preserves_paired_structure(self):
        """Paired bootstrap should preserve the joint distribution."""
        rng = np.random.default_rng(42)
        n = 500
        signal = rng.normal(0, 1, n)
        # Strong relationship
        returns = signal + rng.normal(0, 0.2, n)
        b = BootstrapCI(n_iterations=200, random_seed=42)
        result = b.ci_ic(signal, returns)
        # Should be close to theoretical IC (Spearman ≈ 1 for strong linear)
        assert result.point_estimate > 0.8

    def test_ci_ic_returns_bootstrap_samples(self):
        """bootstrap_samples should have the right shape."""
        b = BootstrapCI(n_iterations=100, random_seed=42)
        rng = np.random.default_rng(42)
        signal = rng.normal(0, 1, 200)
        returns = signal + rng.normal(0, 0.5, 200)
        result = b.ci_ic(signal, returns)
        assert result.bootstrap_samples.shape == (100,)

    # ── CIResult dataclass ─────────────────────────────────────────

    def test_ciresult_frozen(self):
        """CIResult should be frozen."""
        b = BootstrapCI(n_iterations=100, random_seed=42)
        samples = np.random.default_rng(42).normal(0, 1, 200)
        result = b.ci_mean(samples)
        with pytest.raises(AttributeError):
            result.point_estimate = 0.0
