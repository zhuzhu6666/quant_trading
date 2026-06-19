"""Tests for ExecutionGate — 开仓闸门。

Phase 3 of FACTOR_TAKEOVER_V4.
"""
import time
from unittest.mock import patch

import pytest

from alpha.execution_gate import ExecutionGate, GateResult
from alpha.portfolio_compositor import CompositeSignal


def _make_composite(direction=1, score=0.5):
    return CompositeSignal(
        direction=direction, score=score,
        tactical_score=score, macro_score=0.0,
        tactical_weight=0.7, macro_weight=0.3,
        factor_signals={}, factor_values={}, active_weights={},
        tags_breakdown={}, n_active_factors=5, n_abstain_factors=0,
        timestamp=time.time(),
    )


def _make_bar(close=4500.0):
    return {
        "open": 4495.0, "high": 4505.0, "low": 4490.0,
        "close": close, "volume": 100, "time": time.time(),
        "timeframe": "M15",
    }


GATE_CONFIG = {
    "signal_threshold": 0.4,
    "filter_macd_enabled": True,
    "cooldown_bars": 3,
    "macd_hist_allow_long_when_positive": False,
    "macd_hist_allow_short_when_negative": False,
}


class TestGateResult:

    def test_default_construction(self):
        r = GateResult(passed=True, reason="ok")
        assert r.passed is True

    def test_failed_result(self):
        r = GateResult(passed=False, reason="signal_below_threshold")
        assert r.passed is False


class TestExecutionGateInit:

    def test_initial_cooldown_zero(self):
        g = ExecutionGate(GATE_CONFIG)
        assert g._cooldown_bars == 0


class TestFilter:

    def test_signal_below_threshold(self):
        """direction=0 时过滤。"""
        g = ExecutionGate(GATE_CONFIG)
        composite = _make_composite(direction=0, score=0.2)
        result = g.filter(composite, {}, _make_bar())
        assert result.passed is False
        assert "signal" in result.reason.lower()

    def test_cooldown_active(self):
        """冷却期内过滤。"""
        g = ExecutionGate(GATE_CONFIG)
        g._cooldown_bars = 2
        composite = _make_composite(direction=1, score=0.6)
        result = g.filter(composite, {}, _make_bar())
        assert result.passed is False
        assert "cooldown" in result.reason

    def test_cooldown_decrements_on_tick(self):
        """tick() 递减冷却计数。"""
        g = ExecutionGate(GATE_CONFIG)
        g._cooldown_bars = 3
        g.tick()
        assert g._cooldown_bars == 2
        g.tick()
        assert g._cooldown_bars == 1

    def test_pass_sets_cooldown(self):
        """通过时设置冷却期。"""
        g = ExecutionGate(GATE_CONFIG)
        composite = _make_composite(direction=1, score=0.6)
        result = g.filter(composite, {}, _make_bar())
        assert result.passed is True
        assert g._cooldown_bars == GATE_CONFIG["cooldown_bars"]

    def test_nfp_skip(self):
        """NFP 启用时在 NFP 日过滤。"""
        config = {**GATE_CONFIG, "strategy_enable_nfp_skip": True}
        g = ExecutionGate(config)
        composite = _make_composite(direction=1, score=0.6)
        # 2026-06-05 是 NFP 日 (周五)
        from datetime import datetime
        bar = _make_bar()
        bar["time"] = datetime(2026, 6, 5, 12, 0).timestamp()
        result = g.filter(composite, {}, bar)
        assert result.passed is False
        assert "nfp" in result.reason

    def test_nfp_disabled_does_not_skip(self):
        """NFP 未启用时即使 NFP 日也不过滤。"""
        config = {**GATE_CONFIG, "strategy_enable_nfp_skip": False}
        g = ExecutionGate(config)
        composite = _make_composite(direction=1, score=0.6)
        from datetime import datetime
        bar = _make_bar()
        bar["time"] = datetime(2026, 6, 5, 12, 0).timestamp()
        result = g.filter(composite, {}, bar)
        assert result.passed is True
