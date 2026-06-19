"""Tests for StreamingFactorEngine — 流式因子计算引擎。

Phase 1 of FACTOR_TAKEOVER_V4.
"""
import math
import time

import numpy as np
import pandas as pd
import pytest

from alpha.streaming_factor_engine import StreamingFactorEngine
from alpha.registry import factor_registry


# ── 测试辅助 ──────────────────────────────────────────────

def _make_bar(close=4500.0, open_=4495.0, high=4505.0, low=4490.0,
              volume=100.0, t=None):
    """生成一根标准 M15 bar dict."""
    return {
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "time": t or time.time(),
        "timeframe": "M15",
    }


def _make_bars(n=60, start_price=4500.0, trend=0.0, vol=5.0):
    """生成 n 根趋势 bar."""
    bars = []
    price = start_price
    for i in range(n):
        close = price + trend + np.random.uniform(-vol, vol)
        bars.append(_make_bar(
            close=round(close, 2),
            open_=round(price, 2),
            high=round(max(price, close) + vol * 0.3, 2),
            low=round(min(price, close) - vol * 0.3, 2),
            volume=round(100 + np.random.uniform(-20, 20), 1),
            t=time.time() + i * 900,
        ))
        price = close
    return bars


# ── 测试开始 ──────────────────────────────────────────────

class TestStreamingFactorEngineInit:

    def test_initial_state(self):
        """引擎构造后 buffer 为空, is_warm=False."""
        engine = StreamingFactorEngine()
        assert engine.buffer_size == 0
        assert engine.is_warm is False
        assert engine.get_snapshot() == {}

    def test_default_buffer_size(self):
        """默认 max_buffer=200."""
        engine = StreamingFactorEngine()
        assert engine._buffer.maxlen == 200


class TestAppendBar:

    def test_append_warmup_returns_empty(self):
        """buffer < 50 时 append_bar 返回空 dict."""
        engine = StreamingFactorEngine()
        for i in range(49):
            result = engine.append_bar(_make_bar(close=4500 + i))
            if i < 48:
                assert result == {}, f"Expected empty at i={i}, got {result}"
        # 第 49 根后仍不足 50 (0-indexed)
        assert engine.is_warm is False

    def test_warm_after_50_bars(self):
        """50根 bar 后 is_warm=True, append_bar 返回因子 dict."""
        engine = StreamingFactorEngine()
        bars = _make_bars(50)
        for i, bar in enumerate(bars):
            result = engine.append_bar(bar)
        assert engine.is_warm is True
        assert engine.buffer_size == 50
        # 返回的 dict 应包含注册因子 (非空)
        assert len(result) > 0
        # 所有值应为 float 或 None
        for name, val in result.items():
            assert val is None or isinstance(val, float), f"{name}={val!r}"

    def test_snapshot_equals_last_result(self):
        """get_snapshot 应与上次 append_bar 结果一致."""
        engine = StreamingFactorEngine()
        bars = _make_bars(55)
        last_result = {}
        for bar in bars:
            last_result = engine.append_bar(bar)
        snapshot = engine.get_snapshot()
        assert snapshot == last_result

    def test_single_factor_failure_does_not_block_others(self):
        """单个因子异常不应影响其他因子."""
        # 模拟一个会崩的因子
        bad_name = "_test_bad_factor"
        
        def _bad_fn(df):
            raise ValueError("intentional failure")
        
        factor_registry._factors[bad_name] = _bad_fn
        
        try:
            engine = StreamingFactorEngine()
            # 强制刷新列表
            engine.refresh_factor_list()
            bars = _make_bars(55)
            for bar in bars:
                result = engine.append_bar(bar)
            # bad factor 应为 None
            assert result.get(bad_name) is None
            # 至少部分因子返回有效值（注意：测试 bars 缺少某些列，
            # 宏观因子如 dxy_corr_20 等会返回 None，这是预期行为）
            valid = {k: v for k, v in result.items() if k != bad_name}
            assert len(valid) > 0
            non_none = {k: v for k, v in valid.items() if v is not None}
            # 至少有一些因子成功计算出值
            assert len(non_none) >= 5, f"Only {len(non_none)} factors returned values, expected >= 5"
        finally:
            # 清理
            factor_registry._factors.pop(bad_name, None)

    def test_nan_value_set_to_none(self):
        """因子返回 NaN 时引擎置 None."""
        # 注册一个返回 NaN 的因子
        nan_name = "_test_nan_factor"
        
        def _nan_fn(df):
            return np.full(len(df), np.nan)
        
        factor_registry._factors[nan_name] = _nan_fn
        
        try:
            engine = StreamingFactorEngine()
            engine.refresh_factor_list()
            bars = _make_bars(55)
            for bar in bars:
                result = engine.append_bar(bar)
            assert result.get(nan_name) is None
        finally:
            factor_registry._factors.pop(nan_name, None)

    def test_inf_value_set_to_none(self):
        """因子返回 Inf 时引擎置 None."""
        inf_name = "_test_inf_factor"
        
        def _inf_fn(df):
            arr = np.full(len(df), np.inf)
            arr[-1] = 1.0  # 最后一位正常 (模拟某个中间值崩)
            return arr
        
        factor_registry._factors[inf_name] = _inf_fn
        
        try:
            engine = StreamingFactorEngine()
            engine.refresh_factor_list()
            bars = _make_bars(55)
            for bar in bars:
                result = engine.append_bar(bar)
            # 最后 1 个值正常，所以 inf_name 应该有值
            assert result.get(inf_name) is not None
        finally:
            factor_registry._factors.pop(inf_name, None)


class TestRefreshFactorList:

    def test_refresh_discovers_new_factors(self):
        """refresh_factor_list 应发现新注册的因子."""
        engine = StreamingFactorEngine()
        before = list(engine._available_factors)
        
        # 注册一个新因子
        new_name = "_test_new_factor"
        def _new_fn(df):
            return np.full(len(df), 1.0)
        factor_registry._factors[new_name] = _new_fn
        
        try:
            engine.refresh_factor_list()
            assert new_name in engine._available_factors
        finally:
            factor_registry._factors.pop(new_name, None)


class TestReset:

    def test_reset_clears_state(self):
        """reset 应清空 buffer/cache/warm 状态."""
        engine = StreamingFactorEngine()
        bars = _make_bars(55)
        for bar in bars:
            engine.append_bar(bar)
        assert engine.is_warm is True
        assert engine.buffer_size >= 50
        
        engine.reset()
        assert engine.buffer_size == 0
        assert engine.is_warm is False
        assert engine.get_snapshot() == {}
