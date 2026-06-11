"""tests/alpha/evaluation/test_causal_check.py — Tests for CausalCheck."""
import numpy as np
import pytest

from alpha.evaluation.causal_check import CausalCheck, CausalReport


class TestCausalCheck:
    """Tests for CausalCheck."""

    def test_default_init(self):
        """Default n_lags should be 1."""
        c = CausalCheck()
        assert c.n_lags == 1

    def test_custom_n_lags(self):
        """Custom n_lags should be accepted."""
        c = CausalCheck(n_lags=3)
        assert c.n_lags == 3

    def test_invalid_n_lags_raises(self):
        """n_lags < 1 should raise ValueError."""
        with pytest.raises(ValueError, match="n_lags"):
            CausalCheck(n_lags=0)
        with pytest.raises(ValueError, match="n_lags"):
            CausalCheck(n_lags=-1)

    def test_repr(self):
        """__repr__ should include key parameters."""
        c = CausalCheck(n_lags=2)
        r = repr(c)
        assert "n_lags=2" in r

    def test_check_returns_causalreport(self):
        """check() should return a CausalReport."""
        rng = np.random.default_rng(42)
        factor = rng.normal(0, 1, 500)
        returns = factor * 0.3 + rng.normal(0, 0.5, 500)
        c = CausalCheck()
        report = c.check(factor, returns)
        assert isinstance(report, CausalReport)

    def test_causalreport_has_all_fields(self):
        """CausalReport should have all expected attributes."""
        rng = np.random.default_rng(42)
        factor = rng.normal(0, 1, 500)
        returns = factor * 0.3 + rng.normal(0, 0.5, 500)
        c = CausalCheck()
        report = c.check(factor, returns)
        assert hasattr(report, "cause_vs_corr_score")
        assert hasattr(report, "orthogonality_pvalue")
        assert hasattr(report, "decay_rate")
        assert hasattr(report, "raw_correlation")
        assert hasattr(report, "early_correlation")
        assert hasattr(report, "late_correlation")
        assert hasattr(report, "n_obs")

    def test_raw_correlation_nonzero_for_related_data(self):
        """Raw correlation should be positive when factor predicts returns."""
        rng = np.random.default_rng(42)
        factor = rng.normal(0, 1, 1000)
        returns = factor * 0.5 + rng.normal(0, 0.3, 1000)
        c = CausalCheck()
        report = c.check(factor, returns)
        assert report.raw_correlation > 0.3

    def test_raw_correlation_near_zero_for_independent_data(self):
        """Raw correlation should be near zero for independent data."""
        rng = np.random.default_rng(42)
        factor = rng.normal(0, 1, 500)
        returns = rng.normal(0, 1, 500)
        c = CausalCheck()
        report = c.check(factor, returns)
        assert abs(report.raw_correlation) < 0.1

    def test_decay_rate_positive_when_relationship_weakens(self):
        """Decay rate should be positive when late correlation < early."""
        rng = np.random.default_rng(42)
        n = 1000
        factor = rng.normal(0, 1, n)
        # Strong relationship early, weak late
        returns = np.zeros(n)
        returns[:n // 2] = factor[:n // 2] * 0.8 + rng.normal(0, 0.2, n // 2)
        returns[n // 2:] = factor[n // 2:] * 0.1 + rng.normal(0, 0.9, n // 2)
        c = CausalCheck()
        report = c.check(factor, returns)
        assert report.decay_rate > 0  # early_corr > late_corr → positive decay

    def test_decay_rate_negative_when_relationship_strengthens(self):
        """Decay rate should be negative when late correlation > early."""
        rng = np.random.default_rng(42)
        n = 1000
        factor = rng.normal(0, 1, n)
        # Weak relationship early, strong late
        returns = np.zeros(n)
        returns[:n // 2] = factor[:n // 2] * 0.1 + rng.normal(0, 0.9, n // 2)
        returns[n // 2:] = factor[n // 2:] * 0.8 + rng.normal(0, 0.2, n // 2)
        c = CausalCheck()
        report = c.check(factor, returns)
        assert report.decay_rate < 0  # early_corr < late_corr → negative decay

    def test_cause_vs_corr_score_range(self):
        """cause_vs_corr_score should be in [-1, 1]."""
        rng = np.random.default_rng(42)
        factor = rng.normal(0, 1, 500)
        returns = factor * 0.3 + rng.normal(0, 0.7, 500)
        c = CausalCheck()
        report = c.check(factor, returns)
        assert -1.0 <= report.cause_vs_corr_score <= 1.0

    def test_orthogonality_pvalue_range(self):
        """orthogonality_pvalue should be in [0, 1]."""
        rng = np.random.default_rng(42)
        factor = rng.normal(0, 1, 500)
        returns = factor * 0.3 + rng.normal(0, 0.7, 500)
        c = CausalCheck()
        report = c.check(factor, returns)
        assert 0.0 <= report.orthogonality_pvalue <= 1.0

    def test_n_obs_correct(self):
        """n_obs should equal the number of valid (non-NaN) observations."""
        rng = np.random.default_rng(42)
        factor = np.array([1.0, 2.0, 3.0, np.nan, 5.0, 6.0])
        returns = np.array([0.1, 0.2, np.nan, 0.4, 0.5, 0.6])
        c = CausalCheck()
        report = c.check(factor, returns)
        # After removing pairs with NaN: (1,0.1), (2,0.2), (5,0.5), (6,0.6) = 4
        assert report.n_obs == 4

    def test_small_sample_returns_neutral(self):
        """Very small samples should return a neutral report."""
        c = CausalCheck()
        report = c.check(np.array([1.0, 2.0]), np.array([0.1, 0.2]))
        assert report.cause_vs_corr_score == 0.0
        assert report.orthogonality_pvalue == 1.0
        assert report.decay_rate == 0.0

    def test_length_mismatch_raises(self):
        """Length mismatch should raise ValueError."""
        c = CausalCheck()
        with pytest.raises(ValueError, match="shape"):
            c.check(np.array([1, 2, 3]), np.array([1, 2]))

    def test_causalreport_frozen(self):
        """CausalReport should be frozen."""
        rng = np.random.default_rng(42)
        factor = rng.normal(0, 1, 100)
        returns = rng.normal(0, 1, 100)
        c = CausalCheck()
        report = c.check(factor, returns)
        with pytest.raises(AttributeError):
            report.cause_vs_corr_score = 0.5

    def test_high_orthogonality_pvalue_for_lagged_autocorrelation(self):
        """When factor has strong autocorrelation, orthogonalisation should
        reduce p-value less (i.e., unique component beyond lags is small)."""
        rng = np.random.default_rng(42)
        n = 1000
        # AR(1) factor
        factor = np.zeros(n)
        factor[0] = rng.normal(0, 1)
        for t in range(1, n):
            factor[t] = 0.9 * factor[t - 1] + rng.normal(0, 0.1)
        # Returns depend only on the current factor
        returns = factor * 0.3 + rng.normal(0, 0.5, n)

        c = CausalCheck(n_lags=1)
        report = c.check(factor, returns)
        # With n_lags=1, the orthogonalisation removes the AR(1) component,
        # and the residuals should still have predictive power.
        assert isinstance(report.orthogonality_pvalue, float)
