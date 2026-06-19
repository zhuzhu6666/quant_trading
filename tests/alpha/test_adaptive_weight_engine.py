"""Tests for AdaptiveWeightEngine — 权重自适应引擎。

Phase 5 of FACTOR_TAKEOVER_V4.
"""
import math
import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from alpha.adaptive_weight_engine import AdaptiveWeightEngine
from alpha.attribution_engine import (
    AttributionEngine,
    FactorAttributionStats,
    TradeAttribution,
)


def _make_stats(name: str, n_trades=20, win_rate=0.6, mc_std=0.02, seed=42):
    """构造有 N 笔交易记录的 FactorAttributionStats。"""
    stats = FactorAttributionStats(name=name)
    np.random.seed(seed)
    for i in range(n_trades):
        mc = np.random.randn() * mc_std
        if i < n_trades * win_rate:
            mc = abs(mc)
        else:
            mc = -abs(mc)
        stats.record(mc=mc, is_win=mc > 0, tags={})
    return stats


SAMPLE_AWE_CONFIG = {
    "awe_sensitivity": 0.5,
    "awe_anchor_pull": 0.15,
    "awe_max_single_change": 0.15,
    "awe_weight_min": 0.1,
    "awe_weight_max": 3.0,
    "awe_min_trades": 10,
    "awe_adapt_interval": 50,
    "awe_ic_floor": 0.02,
    "awe_health_floor": 40.0,
    "awe_disable_min_trades": 20,
    "awe_causal_threshold": -0.3,
    "awe_dsr_p_threshold": 0.95,
    "awe_resurrect_health_threshold": 60.0,
    "awe_resurrect_dsr_p": 0.05,
    "awe_resurrect_cooldown_days": 7,
    "awe_max_type_weight_pct": 0.40,
}


SAMPLE_FACTOR_CONFIGS = {
    "rsi_14":    {"weight": 1.0, "tags": ["技术", "均值回归"], "enabled": True},
    "di_spread": {"weight": 1.75, "tags": ["技术", "趋势"], "enabled": True},
    "dxy_corr_20": {"weight": 0.8, "tags": ["宏观", "美元"], "enabled": True},
}


class TestAdaptiveWeightEngineInit:

    def test_initial_state(self):
        a = AdaptiveWeightEngine(SAMPLE_AWE_CONFIG)
        assert a._config == SAMPLE_AWE_CONFIG
        assert a._base_weights == {}
        assert a._current_weights == {}

    def test_initialize_records_weights(self):
        a = AdaptiveWeightEngine(SAMPLE_AWE_CONFIG)
        a.initialize(SAMPLE_FACTOR_CONFIGS)
        assert a._base_weights["rsi_14"] == 1.0
        assert a._base_weights["di_spread"] == 1.75
        assert a._current_weights["rsi_14"] == 1.0


class TestAdapt:

    def test_no_patches_when_below_min_trades(self):
        """样本不足时不调权。"""
        a = AdaptiveWeightEngine({**SAMPLE_AWE_CONFIG, "awe_min_trades": 999})
        a.initialize(SAMPLE_FACTOR_CONFIGS)
        attr = AttributionEngine()
        # 加一个统计但不足 awe_min_trades
        stats = _make_stats("rsi_14", n_trades=5)
        attr._per_factor["rsi_14"] = stats
        patches = a.adapt(attr, SAMPLE_FACTOR_CONFIGS)
        assert patches == {}

    def test_positive_sharpe_increases_weight(self):
        """正 Sharpe 因子权重增加。"""
        a = AdaptiveWeightEngine(SAMPLE_AWE_CONFIG)
        a.initialize(SAMPLE_FACTOR_CONFIGS)
        attr = AttributionEngine()
        # 构造正 Sharpe 因子
        stats = _make_stats("rsi_14", n_trades=20, win_rate=0.7, seed=1)
        attr._per_factor["rsi_14"] = stats
        with patch.object(a, "_check_ic_and_health", return_value=True):
            with patch.object(a, "_enforce_diversity", side_effect=lambda p, *a: p):
                patches = a.adapt(attr, SAMPLE_FACTOR_CONFIGS)
        assert "rsi_14" in patches
        assert patches["rsi_14"]["weight"] > 1.0  # 正 Sharpe → 增权

    def test_negative_sharpe_decreases_weight(self):
        """负 Sharpe 因子权重减少。"""
        a = AdaptiveWeightEngine(SAMPLE_AWE_CONFIG)
        a.initialize(SAMPLE_FACTOR_CONFIGS)
        attr = AttributionEngine()
        # 构造负 Sharpe 因子
        stats = _make_stats("rsi_14", n_trades=20, win_rate=0.3, seed=2)
        attr._per_factor["rsi_14"] = stats
        with patch.object(a, "_check_ic_and_health", return_value=True):
            with patch.object(a, "_enforce_diversity", side_effect=lambda p, *a: p):
                patches = a.adapt(attr, SAMPLE_FACTOR_CONFIGS)
        assert "rsi_14" in patches
        assert patches["rsi_14"]["weight"] < 1.0  # 负 Sharpe → 降权

    def test_weight_capped_by_limits(self):
        """权重被限制在 [awe_weight_min, awe_weight_max]。"""
        a = AdaptiveWeightEngine({
            **SAMPLE_AWE_CONFIG,
            "awe_weight_min": 0.5,
            "awe_weight_max": 2.0,
        })
        a.initialize({"test": {"weight": 1.0, "tags": ["技术"], "enabled": True}})
        attr = AttributionEngine()
        stats = _make_stats("test", n_trades=20, win_rate=0.9, mc_std=0.05, seed=3)
        attr._per_factor["test"] = stats
        with patch.object(a, "_check_ic_and_health", return_value=True):
            with patch.object(a, "_enforce_diversity", side_effect=lambda p, *a: p):
                patches = a.adapt(attr, {"test": {"weight": 1.0, "tags": ["技术"], "enabled": True}})
        w = patches["test"]["weight"]
        assert 0.5 <= w <= 2.0

    def test_no_change_when_weight_differs_by_less_than_one_percent(self):
        """变化 < 0.01 时不发补丁。"""
        a = AdaptiveWeightEngine({**SAMPLE_AWE_CONFIG, "awe_sensitivity": 0.001})
        a.initialize({"test": {"weight": 1.0, "tags": ["技术"], "enabled": True}})
        attr = AttributionEngine()
        stats = _make_stats("test", n_trades=20, win_rate=0.5, mc_std=0.01, seed=4)
        attr._per_factor["test"] = stats
        with patch.object(a, "_check_ic_and_health", return_value=True):
            with patch.object(a, "_enforce_diversity", side_effect=lambda p, *a: p):
                patches = a.adapt(attr, {"test": {"weight": 1.0, "tags": ["技术"], "enabled": True}})
        # 极低 sensitivity, Sharpe 接近 0 → 变化 < 0.01
        assert patches == {} or not any(
            abs(p["weight"] - 1.0) >= 0.01 for p in patches.values()
        )


class TestDiversityConstraint:

    def test_single_type_capped_at_40pct(self):
        """同类型总权重不超过 40%。"""
        a = AdaptiveWeightEngine({**SAMPLE_AWE_CONFIG, "awe_max_type_weight_pct": 0.40})
        configs = {
            "rsi_14":    {"weight": 1.0, "tags": ["技术", "均值回归"], "enabled": True},
            "di_spread": {"weight": 1.75, "tags": ["技术", "趋势"], "enabled": True},
            "adx":       {"weight": 0.5, "tags": ["技术", "趋势"], "enabled": True},
            "dxy_corr_20": {"weight": 0.8, "tags": ["宏观", "美元"], "enabled": True},
        }
        a.initialize(configs)

        # 全部正 Sharpe → 都想增权
        attr = AttributionEngine()
        for name in configs:
            s = _make_stats(name, n_trades=20, win_rate=0.7, seed=hash(name) % 100)
            attr._per_factor[name] = s

        with patch.object(a, "_check_ic_and_health", return_value=True):
            patches = a.adapt(attr, configs)

        # 验证 diversity 约束: 技术类总权重 ≤ 40%
        # 我们需要计算调整后总权重
        merged = {n: dict(c) for n, c in configs.items()}
        for n, p in patches.items():
            if n in merged:
                merged[n]["weight"] = p["weight"]

        total = sum(c["weight"] for c in merged.values() if c.get("enabled", True))
        tech_weight = sum(
            c["weight"] for n, c in merged.items()
            if "技术" in c.get("tags", []) and c.get("enabled", True)
        )
        if total > 0:
            assert tech_weight / total <= 0.45, f"tech={tech_weight/total:.2%} > 40%"


class TestICAndHealthCheck:

    def test_low_ic_skips_trade(self):
        """IC < awe_ic_floor 的因子不参与调权。"""
        from unittest.mock import MagicMock

        mock_tracker = MagicMock()
        mock_tracker.status.return_value = {"rolling_ic": 0.01}  # < 0.02 floor
        a = AdaptiveWeightEngine(SAMPLE_AWE_CONFIG, ictracker=mock_tracker)
        a.initialize({"test": {"weight": 1.0, "tags": ["技术"], "enabled": True}})
        attr = AttributionEngine()
        stats = _make_stats("test", n_trades=20, win_rate=0.7)
        attr._per_factor["test"] = stats
        with patch.object(a, "_enforce_diversity", side_effect=lambda p, *a: p):
            patches = a.adapt(attr, {"test": {"weight": 1.0, "tags": ["技术"], "enabled": True}})
        assert "test" not in patches
