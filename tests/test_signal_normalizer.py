"""Smoke tests for SignalNormalizer —归一化边界条件."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import math
import time
from collections import deque

import pytest

from alpha.signal_normalizer import (
    SignalNormalizer,
    _normalize_zscore_tanh,
    _normalize_rank,
    _normalize_discrete,
    HOUR_WEIGHTS,
    DAY_WEIGHTS,
)


# ═══════════════════════ zscore_tanh ═══════════════════════

def test_zscore_tanh_insufficient_samples():
    """Not enough samples → returns None (cold start)."""
    history = deque([50.0, 52.0, 48.0], maxlen=100)  # 3 < min_samples=30
    result = _normalize_zscore_tanh(55.0, history, window=100, min_samples=30)
    assert result is None


def test_zscore_tanh_all_same_values():
    """All values identical → std ≈ 0 → returns 0.0 (neutral)."""
    history = deque([50.0] * 50, maxlen=100)
    result = _normalize_zscore_tanh(50.0, history, window=50, min_samples=30)
    assert result == pytest.approx(0.0, abs=0.01)


def test_zscore_tanh_above_mean():
    """Value above mean → positive signal (but scaled by std)."""
    import numpy as np
    # Mean ≈ 50, std ≈ 10
    history = deque([float(x) for x in np.random.normal(50, 10, 100)], maxlen=100)
    result = _normalize_zscore_tanh(70.0, history, window=100, min_samples=30)
    # 2 sigma above mean → tanh(2) ≈ 0.96
    assert result is not None
    assert result > 0.5  # definitely positive


def test_zscore_tanh_below_mean():
    """Value below mean → negative signal."""
    import numpy as np
    history = deque([float(x) for x in np.random.normal(50, 10, 100)], maxlen=100)
    result = _normalize_zscore_tanh(30.0, history, window=100, min_samples=30)
    assert result is not None
    assert result < -0.5  # definitely negative


def test_zscore_tanh_clips_to_range():
    """tanh output should always be in [-1, +1]."""
    import numpy as np
    history = deque([float(x) for x in np.random.randn(100) * 10 + 50], maxlen=100)
    for val in [-100.0, 0.0, 100.0, 200.0]:
        result = _normalize_zscore_tanh(val, history, window=100, min_samples=30)
        assert result is not None
        assert -1.0 <= result <= 1.0


# ═══════════════════════ rank_mapping ═══════════════════════

def test_rank_mapping_insufficient_samples():
    history = deque([1.0] * 5, maxlen=100)
    result = _normalize_rank(2.0, history, window=100, min_samples=30)
    assert result is None


def test_rank_mapping_top_value():
    """Highest rank → signal ≈ 1.0 (after direction)."""
    history = deque(range(100), maxlen=100)
    result = _normalize_rank(99.0, history, window=100, min_samples=30, direction=1)
    assert result > 0.9
    assert result <= 1.0


def test_rank_mapping_low_value():
    """Lowest rank → signal ≈ -1.0."""
    history = deque(range(100), maxlen=100)
    result = _normalize_rank(0.0, history, window=100, min_samples=30, direction=1)
    assert result < -0.9
    assert result >= -1.0


def test_rank_mapping_direction_reversal():
    """direction=-1 should flip the sign."""
    history = deque(range(100), maxlen=100)
    result_pos = _normalize_rank(99.0, history, window=100, min_samples=30, direction=1)
    result_neg = _normalize_rank(99.0, history, window=100, min_samples=30, direction=-1)
    assert result_pos > 0
    assert result_neg < 0
    assert abs(result_pos - (-result_neg)) < 0.02


# ═══════════════════════ discrete ═══════════════════════

def test_discrete_known_value():
    value_map = {"1": 1.0, "-1": -1.0, "0": 0.0}
    assert _normalize_discrete("1", value_map) == 1.0
    assert _normalize_discrete("-1", value_map) == -1.0
    assert _normalize_discrete(0, value_map) == 0.0


def test_discrete_unknown_value():
    """Unknown value → neutral (0.0)."""
    value_map = {"1": 1.0}
    assert _normalize_discrete("unknown", value_map) == 0.0
    assert _normalize_discrete(99, value_map) == 0.0


# ═══════════════════════ hour/day weights ═══════════════════════

def test_hour_weights_summary():
    """Verify HOUR_WEIGHTS covers expected ranges and values are in [-1, 1]."""
    covered = set()
    max_w = 0.0
    min_w = 0.0
    for r, w in HOUR_WEIGHTS.items():
        for h in r:
            covered.add(h)
            max_w = max(max_w, w)
            min_w = min(min_w, w)
    # The weights map covers specific ranges; verify they're all valid
    assert len(covered) > 0
    assert min_w >= -1.0
    assert max_w <= 1.0


def test_day_weights_5_days():
    """All 5 weekdays should have defined weights."""
    assert set(DAY_WEIGHTS.keys()) == {0, 1, 2, 3, 4}


# ═══════════════════════ NaN handling ═══════════════════════

def test_normalizer_handles_nan():
    """NaN factor values should return None."""
    norm = SignalNormalizer({})
    result = norm.normalize({
        "rsi_14": float("nan"),
        "adx": None,
        "valid": 50.0,
    })
    assert result["rsi_14"] is None
    assert result["adx"] is None


def test_warmup_fills_history():
    """Warmup should pre-fill rolling window history."""
    config = {
        "rsi_14": {"mode": "zscore_tanh", "window": 100, "min_samples": 30},
    }
    norm = SignalNormalizer(config)

    snapshots = [{"rsi_14": 50.0 + i} for i in range(50)]
    norm.warmup(snapshots)

    # After warmup, zscore should work (50+ samples >= min_samples=30)
    result = norm.normalize({"rsi_14": 55.0})
    assert result["rsi_14"] is not None
    assert -1.0 <= result["rsi_14"] <= 1.0


def test_low_frequency_factor_history_samples_only_on_value_change():
    config = {
        "cot_mm_net": {
            "mode": "rank_mapping",
            "window": 100,
            "min_samples": 2,
            "direction": 1,
        },
    }
    norm = SignalNormalizer(config)

    for _ in range(5):
        norm.normalize({"cot_mm_net": 123.0})
    assert len(norm._histories["cot_mm_net"]) == 1

    norm.normalize({"cot_mm_net": 124.0})
    assert len(norm._histories["cot_mm_net"]) == 2


def test_bar_factor_history_samples_every_bar():
    norm = SignalNormalizer({
        "rsi_14": {"mode": "zscore_tanh", "window": 100, "min_samples": 2},
    })
    for _ in range(5):
        norm.normalize({"rsi_14": 50.0})
    assert len(norm._histories["rsi_14"]) == 5


def test_low_frequency_fallback_fills_only_missing_live_raw_values():
    norm = SignalNormalizer({
        "dxy_corr_20": {
            "mode": "rank_mapping",
            "window": 100,
            "min_samples": 2,
        },
    })
    norm.seed_low_frequency_fallback(
        {"latest_values": {"dxy_corr_20": -0.42}},
        refreshed_at=time.time(),
    )

    assert norm.resolve_factor_values({"dxy_corr_20": None}) == {
        "dxy_corr_20": -0.42,
    }
    assert norm.resolve_factor_values({"dxy_corr_20": 0.25}) == {
        "dxy_corr_20": 0.25,
    }


def test_low_frequency_fallback_refreshes_from_configured_pit_loader():
    calls = []
    config = {"dxy_corr_20": {"mode": "rank_mapping"}}
    norm = SignalNormalizer(config)
    norm.configure_low_frequency_fallback(
        lambda **kwargs: calls.append(kwargs)
        or {"latest_values": {"dxy_corr_20": -0.6}},
        config,
    )

    resolved = norm.resolve_factor_values({"dxy_corr_20": None})

    assert resolved == {"dxy_corr_20": -0.6}
    assert calls[0]["signal_config"] == config
    assert calls[0]["as_of"] > 0
