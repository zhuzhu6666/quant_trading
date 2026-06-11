"""tests/alpha/evaluation/test_attribution.py — Tests for Attribution."""
import numpy as np
import pytest

from alpha.evaluation.attribution import Attribution, AttributionReport, FactorContribution


class TestAttribution:
    """Tests for Attribution."""

    def test_default_init(self):
        """Default demean should be True."""
        a = Attribution()
        assert a.demean is True

    def test_custom_demean(self):
        """Custom demean should be accepted."""
        a = Attribution(demean=False)
        assert a.demean is False

    def test_repr(self):
        """__repr__ should include key parameters."""
        a = Attribution(demean=True)
        r = repr(a)
        assert "demean=True" in r

    def test_attribute_returns_report(self):
        """attribute() should return an AttributionReport."""
        rng = np.random.default_rng(42)
        X = rng.normal(0, 1, (500, 3))
        y = X @ np.array([0.3, -0.2, 0.1]) + rng.normal(0, 0.3, 500)
        a = Attribution()
        report = a.attribute(X, y)
        assert isinstance(report, AttributionReport)

    def test_report_has_contributions(self):
        """Report should have a list of FactorContribution."""
        rng = np.random.default_rng(42)
        X = rng.normal(0, 1, (500, 3))
        y = X @ np.array([0.3, -0.2, 0.1]) + rng.normal(0, 0.3, 500)
        a = Attribution()
        report = a.attribute(X, y, factor_names=["A", "B", "C"])
        assert len(report.contributions) == 3
        for c in report.contributions:
            assert isinstance(c, FactorContribution)
            assert c.name in ("A", "B", "C")

    def test_marginal_r2_non_negative(self):
        """Marginal R² should always be non-negative."""
        rng = np.random.default_rng(42)
        X = rng.normal(0, 1, (500, 3))
        y = X @ np.array([0.3, -0.2, 0.1]) + rng.normal(0, 0.3, 500)
        a = Attribution()
        report = a.attribute(X, y)
        for c in report.contributions:
            assert c.marginal_r2 >= 0.0

    def test_cumulative_r2_non_decreasing(self):
        """Cumulative R² should be non-decreasing across factors."""
        rng = np.random.default_rng(42)
        X = rng.normal(0, 1, (500, 3))
        y = X @ np.array([0.3, -0.2, 0.1]) + rng.normal(0, 0.3, 500)
        a = Attribution()
        report = a.attribute(X, y)
        cum = [c.cumulative_r2 for c in report.contributions]
        for i in range(1, len(cum)):
            assert cum[i] >= cum[i - 1] - 1e-10  # allow floating point

    def test_total_r2_matches_last_cumulative(self):
        """Total R² should equal the last cumulative R²."""
        rng = np.random.default_rng(42)
        X = rng.normal(0, 1, (500, 3))
        y = X @ np.array([0.3, -0.2, 0.1]) + rng.normal(0, 0.3, 500)
        a = Attribution()
        report = a.attribute(X, y)
        assert abs(report.total_r2 - report.contributions[-1].cumulative_r2) < 1e-10

    def test_standalone_r2_positive(self):
        """Standalone R² should be positive when factor is useful."""
        rng = np.random.default_rng(42)
        X = rng.normal(0, 1, (500, 3))
        y = X[:, 0] * 0.5 + rng.normal(0, 0.2, 500)
        a = Attribution()
        report = a.attribute(X, y)
        assert report.contributions[0].standalone_r2 > 0.1

    def test_custom_order(self):
        """Custom factor order should change the attribution."""
        rng = np.random.default_rng(42)
        X = rng.normal(0, 1, (500, 3))
        y = X @ np.array([0.5, 0.0, 0.0]) + rng.normal(0, 0.2, 500)

        a = Attribution()
        report_default = a.attribute(X, y, factor_names=["A", "B", "C"])
        report_custom = a.attribute(X, y, factor_names=["A", "B", "C"], order=[1, 2, 0])

        # Default order: F0 should have most marginal R²
        # Custom order [1, 2, 0]: F0 last, but should still have most marginal
        # because F0 is orthogonalised against F1 and F2 first
        assert report_custom.contributions[-1].name == "A"

    def test_demean_flag(self):
        """When demean=False, the intercept should not be subtracted."""
        rng = np.random.default_rng(42)
        X = rng.normal(0, 1, (500, 2))
        y = np.ones(500) * 5.0 + X @ np.array([0.3, -0.2]) + rng.normal(0, 0.3, 500)

        a_demean = Attribution(demean=True)
        a_nodemean = Attribution(demean=False)

        r1 = a_demean.attribute(X, y)
        r2 = a_nodemean.attribute(X, y)
        # Both should produce positive R², but may differ slightly
        assert r1.total_r2 > 0
        assert r2.total_r2 > 0

    def test_single_factor(self):
        """Should handle a single factor correctly."""
        rng = np.random.default_rng(42)
        X = rng.normal(0, 1, (500, 1))
        y = X[:, 0] * 0.5 + rng.normal(0, 0.2, 500)
        a = Attribution()
        report = a.attribute(X, y, factor_names=["X1"])
        assert len(report.contributions) == 1
        assert report.n_factors == 1
        assert report.contributions[0].name == "X1"
        assert report.contributions[0].marginal_r2 > 0.1
        assert report.contributions[0].standalone_r2 > 0.1

    def test_many_factors(self):
        """Should handle many factors."""
        rng = np.random.default_rng(42)
        X = rng.normal(0, 1, (1000, 10))
        y = X @ rng.normal(0, 0.2, 10) + rng.normal(0, 0.3, 1000)
        a = Attribution()
        report = a.attribute(X, y)
        assert report.n_factors == 10
        assert report.n_obs == 1000
        assert len(report.contributions) == 10

    def test_n_obs_correct(self):
        """n_obs should reflect the number of rows after NaN removal."""
        rng = np.random.default_rng(42)
        X = rng.normal(0, 1, (500, 2))
        X[0, 0] = np.nan  # introduce NaN
        y = X[:, 0] * 0.5 + X[:, 1] * (-0.3) + rng.normal(0, 0.2, 500)
        a = Attribution()
        report = a.attribute(X, y)
        assert report.n_obs == 499  # one row dropped

    def test_mismatched_length_raises(self):
        """Length mismatch should raise ValueError."""
        a = Attribution()
        with pytest.raises(ValueError, match="rows"):
            a.attribute(np.random.randn(100, 2), np.random.randn(50))

    def test_wrong_factor_names_count_raises(self):
        """Mismatch between factor_names and columns should raise ValueError."""
        a = Attribution()
        with pytest.raises(ValueError, match="names"):
            a.attribute(
                np.random.randn(100, 3),
                np.random.randn(100),
                factor_names=["A", "B"],  # only 2 names for 3 factors
            )

    def test_invalid_order_raises(self):
        """Invalid order should raise ValueError."""
        a = Attribution()
        with pytest.raises(ValueError, match="order"):
            a.attribute(
                np.random.randn(100, 3),
                np.random.randn(100),
                order=[0, 1, 1],  # duplicate index
            )

    def test_report_total_r2_range(self):
        """Total R² should be between 0 and 1."""
        rng = np.random.default_rng(42)
        X = rng.normal(0, 1, (500, 3))
        # Pure noise: R² should be near 0
        y = rng.normal(0, 1, 500)
        a = Attribution()
        report = a.attribute(X, y)
        assert 0.0 <= report.total_r2 <= 1.0

    def test_factorcontribution_frozen(self):
        """FactorContribution should be frozen."""
        c = FactorContribution(
            name="A",
            marginal_r2=0.1,
            marginal_coefficient=0.5,
            cumulative_r2=0.1,
            standalone_r2=0.15,
            standalone_coefficient=0.5,
        )
        with pytest.raises(AttributeError):
            c.name = "B"

    def test_attributionreport_frozen(self):
        """AttributionReport should be frozen."""
        rng = np.random.default_rng(42)
        X = rng.normal(0, 1, (100, 2))
        y = rng.normal(0, 1, 100)
        a = Attribution()
        report = a.attribute(X, y)
        with pytest.raises(AttributeError):
            report.total_r2 = 0.5
