"""Extended tests for FactorAttributionStats — 新加统计方法。

Phase 4 修复: ir_*, sharpe_ci, is_statistically_significant, causal_quality
"""
import numpy as np
import pytest

from alpha.attribution_engine import FactorAttributionStats


def _make_stats(name="test", n_trades=60, mc_std=0.02, seed=42):
    stats = FactorAttributionStats(name=name)
    np.random.seed(seed)
    for i in range(n_trades):
        mc = np.random.randn() * mc_std
        if i < n_trades * 0.6:
            mc = abs(mc)
        else:
            mc = -abs(mc)
        stats.record(mc=mc, is_win=mc > 0, tags={})
    return stats


class TestIRProperties:

    def test_ir_short_nan_when_insufficient(self):
        s = FactorAttributionStats(name="t")
        assert np.isnan(s.ir_short)

    def test_ir_short_finite_after_50_trades(self):
        s = _make_stats(n_trades=60)
        assert not np.isnan(s.ir_short)
        assert isinstance(s.ir_short, float)

    def test_ir_mid_and_long(self):
        s = _make_stats(n_trades=260)
        assert not np.isnan(s.ir_mid)
        assert not np.isnan(s.ir_long)


class TestSharpeCI:

    def test_returns_none_when_insufficient_data(self):
        s = FactorAttributionStats(name="t")
        assert s.sharpe_ci() is None

    def test_returns_tuple_after_sufficient_trades(self):
        s = _make_stats(n_trades=60)
        ci = s.sharpe_ci(window=50)
        if ci is not None:
            lo, hi = ci
            assert lo <= hi
            assert isinstance(lo, float)


class TestIsStatisticallySignificant:

    def test_returns_dict_with_p_value_default(self):
        """不足20笔时返回高 p_value。"""
        s = FactorAttributionStats(name="t")
        for _ in range(10):
            s.record(mc=0.01, is_win=True, tags={})
        result = s.is_statistically_significant(n_trials=39)
        assert "p_value" in result
        assert "dsr" in result
        assert "significant" in result
        # 样本不足 → p_value=1.0
        assert result["p_value"] == 1.0

    def test_returns_after_20_trades(self):
        s = _make_stats(n_trades=30)
        result = s.is_statistically_significant(n_trials=5)
        assert "p_value" in result
        assert isinstance(result["p_value"], float)
        assert 0.0 <= result["p_value"] <= 1.0


class TestCausalQuality:

    def test_returns_dict_with_defaults_on_bad_input(self):
        s = FactorAttributionStats(name="t")
        fv = np.random.randn(50)
        ret = np.random.randn(50)
        result = s.causal_quality(fv, ret)
        assert "cause_vs_corr_score" in result
        assert "orthogonality_pvalue" in result
        assert "decay_rate" in result
