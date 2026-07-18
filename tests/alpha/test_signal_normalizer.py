"""Tests for SignalNormalizer — 三域归一化引擎。

Phase 2 of FACTOR_TAKEOVER_V4.
"""
import math
import time
from collections import deque

import numpy as np
import pytest

from alpha.signal_normalizer import (
    SignalNormalizer,
    _normalize_zscore_tanh,
    _normalize_rank,
    _normalize_discrete,
    HOUR_WEIGHTS,
    DAY_WEIGHTS,
)


# ── 模块级函数测试 ──────────────────────────────────────

class TestNormalizeZscoreTanh:

    def test_returns_none_on_insufficient_history(self):
        """历史不足 min_samples 时返回 None."""
        history = deque([1.0, 2.0], maxlen=100)
        result = _normalize_zscore_tanh(3.0, history, window=100, min_samples=5)
        assert result is None

    def test_returns_zero_on_zero_std(self):
        """无波动时返回 0.0 (中性)."""
        history = deque([5.0] * 30, maxlen=100)
        result = _normalize_zscore_tanh(5.0, history, window=100, min_samples=5)
        assert result == 0.0

    def test_returns_between_minus_one_and_one(self):
        """输出始终在 [-1, +1]."""
        np.random.seed(42)
        values = np.random.randn(100) * 10 + 50
        history = deque(values, maxlen=200)
        for val in [0, 25, 50, 75, 100, 999, -999]:
            result = _normalize_zscore_tanh(float(val), history, window=100, min_samples=30)
            assert result is not None
            assert -1.0 <= result <= 1.0, f"val={val} -> {result}"

    def test_positive_zscore_positive_signal(self):
        """大于均值的值应产生正信号。"""
        # 100 个值，均值 ~50，有一定波动
        np.random.seed(42)
        history = deque(list(np.random.randn(100) * 5 + 50), maxlen=200)
        # value 远高于均值
        result = _normalize_zscore_tanh(80.0, history, window=100, min_samples=30)
        assert result is not None and result > 0

    def test_negative_zscore_negative_signal(self):
        """小于均值的值应产生负信号。"""
        np.random.seed(42)
        history = deque(list(np.random.randn(100) * 5 + 50), maxlen=200)
        result = _normalize_zscore_tanh(20.0, history, window=100, min_samples=30)
        assert result is not None and result < 0


class TestNormalizeRank:

    def test_returns_none_on_insufficient_history(self):
        history = deque([1.0, 2.0], maxlen=100)
        result = _normalize_rank(3.0, history, window=100, min_samples=5, direction=1)
        assert result is None

    def test_returns_between_minus_one_and_one(self):
        np.random.seed(42)
        values = np.random.randn(200)
        history = deque(values, maxlen=500)
        for val in [-5, -2, 0, 1, 5]:
            result = _normalize_rank(float(val), history, window=100, min_samples=30, direction=1)
            assert result is not None
            assert -1.0 <= result <= 1.0, f"val={val} -> {result}"

    def test_direction_reverses_signal(self):
        """direction=-1 反转信号。"""
        np.random.seed(42)
        values = list(np.random.randn(100))
        history = deque(values, maxlen=500)
        # 一个较大的值
        big_val = max(values) + 1.0
        pos = _normalize_rank(big_val, history.copy(), window=100, min_samples=30, direction=1)
        neg = _normalize_rank(big_val, history.copy(), window=100, min_samples=30, direction=-1)
        assert pos is not None and neg is not None
        assert pos > 0
        assert neg < 0

    def test_high_rank_gives_positive_signal(self):
        """排名高的值产生正信号。"""
        history = deque(list(range(100)), maxlen=200)
        result = _normalize_rank(99.0, history, window=100, min_samples=30, direction=1)
        assert result is not None and result > 0

    def test_constant_history_is_neutral(self):
        """forward-filled low-frequency values should not become extreme votes."""
        history = deque([5.0] * 50, maxlen=100)
        assert _normalize_rank(5.0, history, window=100, min_samples=30, direction=1) == 0.0
        assert _normalize_rank(5.0, history, window=100, min_samples=30, direction=-1) == 0.0

    def test_ties_use_average_rank(self):
        """Equal values map near the middle of their tied bucket."""
        history = deque([1.0, 1.0, 2.0, 2.0], maxlen=100)
        result = _normalize_rank(1.0, history, window=100, min_samples=4, direction=1)
        assert result == pytest.approx(-0.5)


class TestNormalizeDiscrete:

    def test_known_value_maps_correctly(self):
        value_map = {"-1": -1.0, "0": 0.0, "1": 1.0}
        assert _normalize_discrete(-1, value_map) == -1.0
        assert _normalize_discrete(0, value_map) == 0.0
        assert _normalize_discrete(1, value_map) == 1.0

    def test_unknown_value_returns_neutral(self):
        value_map = {"-1": -1.0, "0": 0.0, "1": 1.0}
        assert _normalize_discrete(999, value_map) == 0.0
        assert _normalize_discrete("unknown", value_map) == 0.0

    def test_string_float_key(self):
        value_map = {"-0.8": -0.8, "0.0": 0.0, "0.8": 0.8}
        assert _normalize_discrete(-0.8, value_map) == -0.8


# ── SignalNormalizer 类测试 ──────────────────────────────

SAMPLE_CONFIG = {
    "rsi_14": {
        "mode": "zscore_tanh", "window": 100, "min_samples": 50,
        "tags": ["技术", "均值回归"], "weight": 1.0,
    },
    "di_spread": {
        "mode": "zscore_tanh", "window": 100, "min_samples": 50,
        "tags": ["技术", "趋势"], "weight": 1.75,
    },
    "cot_extreme_signal": {
        "mode": "discrete",
        "value_map": {"-1": -1.0, "0": 0.0, "1": 1.0},
        "tags": ["COT", "反转", "综合"], "weight": 1.5,
    },
    "engulfing": {
        "mode": "discrete",
        "value_map": {"-1": -1.0, "0": 0.0, "1": 1.0},
        "tags": ["形态", "反转"], "weight": 1.0,
    },
    "dxy_corr_20": {
        "mode": "rank_mapping", "window": 100, "min_samples": 30,
        "direction": -1, "tags": ["宏观", "美元"], "weight": 0.8,
    },
}


class TestSignalNormalizerInit:

    def test_initializes_with_config(self):
        normalizer = SignalNormalizer(SAMPLE_CONFIG)
        assert len(normalizer._configs) == len(SAMPLE_CONFIG)
        assert normalizer._histories == {}

    def test_empty_config_creates_empty_normalizer(self):
        normalizer = SignalNormalizer({})
        assert normalizer._histories == {}


class TestSignalNormalize:

    def test_none_input_returns_none(self):
        normalizer = SignalNormalizer(SAMPLE_CONFIG)
        result = normalizer.normalize({"rsi_14": None})
        assert result["rsi_14"] is None

    def test_nan_input_returns_none(self):
        normalizer = SignalNormalizer(SAMPLE_CONFIG)
        result = normalizer.normalize({"rsi_14": float("nan")})
        assert result["rsi_14"] is None

    def test_zscore_tanh_factor(self):
        """zscore_tanh 模式因子返回 [-1, +1] 信号。"""
        normalizer = SignalNormalizer(SAMPLE_CONFIG)
        # 先在窗口填满数据
        for i in range(60):
            normalizer.normalize({"rsi_14": 50.0 + i * 0.5})
        result = normalizer.normalize({"rsi_14": 80.0})
        assert result["rsi_14"] is not None
        assert -1.0 <= result["rsi_14"] <= 1.0

    def test_discrete_factor(self):
        """discrete 模式因子直接映射。"""
        normalizer = SignalNormalizer(SAMPLE_CONFIG)
        result = normalizer.normalize({"cot_extreme_signal": 1})  # int 1 → str "1" → value_map["1"] = 1.0
        assert result["cot_extreme_signal"] == 1.0
        result = normalizer.normalize({"cot_extreme_signal": -1})
        assert result["cot_extreme_signal"] == -1.0

    def test_rank_mapping_factor(self):
        """rank_mapping 模式因子返回 [-1, +1]。"""
        normalizer = SignalNormalizer(SAMPLE_CONFIG)
        for i in range(60):
            normalizer.normalize({"dxy_corr_20": float(i)})
        # 较高的 dxy_corr 因 direction=-1 应产生负信号
        result = normalizer.normalize({"dxy_corr_20": 80.0})
        assert result["dxy_corr_20"] is not None
        assert -1.0 <= result["dxy_corr_20"] <= 1.0

    def test_unknown_factor_uses_default_gp_config(self):
        """未配置因子可积累历史，但默认配置不能形成可执行投票。"""
        normalizer = SignalNormalizer(SAMPLE_CONFIG)
        for i in range(60):
            normalizer.normalize({"_unknown_gp_factor": float(i)})
        result = normalizer.normalize({"_unknown_gp_factor": 50.0})
        assert result["_unknown_gp_factor"] is not None
        assert -1.0 <= result["_unknown_gp_factor"] <= 1.0
        assert normalizer._configs["_unknown_gp_factor"]["enabled"] is False
        assert normalizer._configs["_unknown_gp_factor"]["weight"] == 0.0

    def test_history_is_maintained_across_calls(self):
        """多次 normalize 调用累积历史窗口。"""
        normalizer = SignalNormalizer(SAMPLE_CONFIG)
        for i in range(50):
            normalizer.normalize({"rsi_14": 50.0})
        assert len(normalizer._histories["rsi_14"]) == 50


class TestWarmup:

    def test_warmup_prefills_histories(self):
        """warmup 从历史快照填充滚动窗口。"""
        normalizer = SignalNormalizer(SAMPLE_CONFIG)
        snapshots = [
            {"rsi_14": 45.0 + i * 0.2, "dxy_corr_20": 0.5 + i * 0.01}
            for i in range(60)
        ]
        normalizer.warmup(snapshots)
        assert len(normalizer._histories.get("rsi_14", [])) == 60
        assert len(normalizer._histories.get("dxy_corr_20", [])) == 60

    def test_warmup_skips_none_values(self):
        """warmup 跳过 None 和 NaN 值。"""
        normalizer = SignalNormalizer(SAMPLE_CONFIG)
        snapshots = [
            {"rsi_14": 45.0 if i % 2 == 0 else None}
            for i in range(60)
        ]
        normalizer.warmup(snapshots)
        assert len(normalizer._histories.get("rsi_14", [])) == 30  # 跳过了 None

    def test_warmup_after_normalize_combined(self):
        """warmup 和 normalize 的历史叠加。"""
        normalizer = SignalNormalizer(SAMPLE_CONFIG)
        normalizer.warmup([{"rsi_14": 40.0}] * 30)
        normalizer.normalize({"rsi_14": 60.0})
        assert len(normalizer._histories["rsi_14"]) == 31


class TestHourDayWeights:

    def test_hour_weights_are_valid(self):
        """时段权重在 [0, 1] 内。"""
        for key, weight in HOUR_WEIGHTS.items():
            if isinstance(key, range):
                for h in key:
                    assert 0.0 <= weight <= 1.0, f"hour={h} weight={weight}"

    def test_day_weights_are_valid(self):
        """周内权重在 [-1, 1] 内。"""
        for day, weight in DAY_WEIGHTS.items():
            assert -1.0 <= weight <= 1.0, f"day={day} weight={weight}"
