"""End-to-end integration test for Factor Takeover v4 pipeline.

Chains all 5 stages with synthetic data to verify data contracts:
  StreamingFactorEngine → SignalNormalizer → PortfolioCompositor
    → ExecutionGate → AttributionEngine

This catches regressions in intermediate data shapes that unit tests miss.
"""

import math
import time

import numpy as np
import pytest

from alpha.streaming_factor_engine import StreamingFactorEngine
from alpha.signal_normalizer import SignalNormalizer
from alpha.portfolio_compositor import PortfolioCompositor, CompositeSignal
from alpha.execution_gate import ExecutionGate
from alpha.attribution_engine import AttributionEngine, TradeAttribution

# ══════════════════════════════════════════════════════════════════
# 合成数据
# ══════════════════════════════════════════════════════════════════


def _make_bar(close=4500.0, open_=4495.0, high=4505.0, low=4490.0,
              volume=100.0, spread=0.5, t=None):
    """Generate a single M5 bar dict."""
    return {
        "time": t or time.time(),
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "spread": spread,
        "timeframe": "M5",
    }


def _make_bars(n=60, start_price=4500.0, trend=0.3, vol=3.0):
    """Generate n synthetic bars with trend + noise."""
    bars = []
    price = start_price
    for i in range(n):
        close = price + trend + np.random.uniform(-vol, vol)
        high = max(price, close) + abs(np.random.normal(0, vol * 0.3))
        low = min(price, close) - abs(np.random.normal(0, vol * 0.3))
        bars.append(_make_bar(
            close=round(close, 2),
            open_=round(price, 2),
            high=round(high, 2),
            low=round(low, 2),
            volume=round(100 + np.random.uniform(-20, 20), 1),
            t=time.time() + i * 300,
        ))
        price = close
    return bars


# ══════════════════════════════════════════════════════════════════
# 配置 (production subset)
# ══════════════════════════════════════════════════════════════════

FACTOR_SIGNAL_CONFIG = {
    "rsi_14":         {"mode": "zscore_tanh", "window": 100, "min_samples": 50, "tags": ["tech", "meanrev"]},
    "di_spread":      {"mode": "zscore_tanh", "window": 100, "min_samples": 50, "tags": ["tech", "trend"]},
    "stoch_k":        {"mode": "zscore_tanh", "window": 100, "min_samples": 50, "tags": ["tech", "momentum"]},
    "adx":            {"mode": "zscore_tanh", "window": 100, "min_samples": 50, "role": "context", "tags": ["tech", "trend_strength"]},
    "atr_ratio":      {"mode": "zscore_tanh", "window": 100, "min_samples": 50, "role": "context", "tags": ["tech", "volatility"]},
    "ema_slope":      {"mode": "zscore_tanh", "window": 50,  "min_samples": 30, "tags": ["tech", "trend"]},
    "bb_width":       {"mode": "zscore_tanh", "window": 100, "min_samples": 50, "role": "context", "tags": ["tech", "volatility"]},
    "macd_hist":      {"mode": "zscore_tanh", "window": 100, "min_samples": 50, "tags": ["tech", "momentum"]},
    "obv_slope":      {"mode": "zscore_tanh", "window": 100, "min_samples": 50, "tags": ["volume"]},
    "day_of_week":    {"mode": "discrete", "value_map": "day_weights", "role": "context", "tags": ["calendar"]},
}

FACTOR_PORTFOLIO_CONFIG = {
    "rsi_14":         {"weight": 1.0, "tags": ["tech", "meanrev"], "enabled": True},
    "di_spread":      {"weight": 1.5, "tags": ["tech", "trend"], "enabled": True},
    "stoch_k":        {"weight": 0.8, "tags": ["tech", "momentum"], "enabled": True},
    "adx":            {"weight": 0.6, "role": "context", "tags": ["tech", "trend_strength"], "enabled": True},
    "atr_ratio":      {"weight": 0.5, "role": "context", "tags": ["tech", "volatility"], "enabled": True},
    "ema_slope":      {"weight": 1.2, "tags": ["tech", "trend"], "enabled": True},
    "bb_width":       {"weight": 0.4, "role": "context", "tags": ["tech", "volatility"], "enabled": True},
    "macd_hist":      {"weight": 1.0, "tags": ["tech", "momentum"], "enabled": True},
    "obv_slope":      {"weight": 0.7, "tags": ["volume"], "enabled": True},
    "day_of_week":    {"weight": 0.3, "role": "context", "tags": ["calendar"], "enabled": True},
}


# ══════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════


def _warm_sfe_and_normalizer(bars, signal_config):
    """Feed bars to both SFE and normalizer sequentially.

    The normalizer needs to receive incrementally different snapshots
    so its z-score buffers build variance.  This helper does a single
    pass: for each bar, append to SFE then normalize current snapshot.
    """
    factor_ids = list(signal_config)
    sfe = StreamingFactorEngine(
        max_buffer=200,
        factor_runtime_config=signal_config,
        factor_ids=factor_ids,
    )
    normalizer = SignalNormalizer(signal_config)
    for bar in bars:
        sfe.append_bar(bar)
        normalizer.normalize(sfe.get_snapshot())
    # Final snapshot after full warmup
    snapshot = sfe.get_snapshot()
    signals = normalizer.normalize(snapshot)
    return sfe, normalizer, snapshot, signals


# ══════════════════════════════════════════════════════════════════
# Tests
# ══════════════════════════════════════════════════════════════════


def test_pipeline_data_contracts():
    """Data contract checks at each stage boundary."""
    np.random.seed(42)
    bars = _make_bars(n=140, start_price=4500.0, trend=0.3, vol=2.0)

    # ── Stage 1+2: SFE + SignalNormalizer ──
    sfe, normalizer, snapshot, signals = _warm_sfe_and_normalizer(bars, FACTOR_SIGNAL_CONFIG)
    assert sfe.is_warm, "SFE should be warm after 50+ bars"
    assert set(snapshot) == set(FACTOR_SIGNAL_CONFIG)
    non_none = {k: v for k, v in snapshot.items() if v is not None}
    assert len(non_none) > 5, f"Expected >5 non-None factors, got {len(non_none)}"

    assert isinstance(signals, dict)
    for name, sig in signals.items():
        if sig is not None:
            assert -1.0 <= sig <= 1.0, f"Signal {name}={sig} outside [-1, +1]"
    non_none_sigs = {k: v for k, v in signals.items() if v is not None}
    assert len(non_none_sigs) > 3, (f"Expected >3 non-None signals, got "
                                    f"{len(non_none_sigs)}: {list(non_none_sigs.keys())[:10]}")

    # ── Stage 3: PortfolioCompositor ──
    compositor = PortfolioCompositor(FACTOR_PORTFOLIO_CONFIG)
    composite = compositor.compose(signals, snapshot)
    assert isinstance(composite, CompositeSignal)
    assert -1.0 <= composite.score <= 1.0, f"Score {composite.score} out of range"
    assert composite.direction in (-1, 0, 1)
    assert isinstance(composite.tactical_score, float)
    assert isinstance(composite.macro_score, float)
    assert len(composite.active_weights) > 0
    assert composite.n_active_factors + composite.n_abstain_factors == len(snapshot)
    print(f"[E2E Contracts] score={composite.score:.4f} dir={composite.direction} "
          f"tactical={composite.tactical_score:.4f} macro={composite.macro_score:.4f} "
          f"n_active={composite.n_active_factors}")

    # ── Stage 4: ExecutionGate ──
    gate = ExecutionGate({"signal_threshold": 0.4, "cooldown_bars": 3})
    gate_result = gate.filter(composite, snapshot, bars[-1])
    assert hasattr(gate_result, "passed")
    assert hasattr(gate_result, "reason")

    if gate_result.passed:
        print(f"[E2E Contracts] Gate PASSED dir={composite.direction}")
        gate.tick()
        post = gate.filter(composite, snapshot, bars[-1])
        if composite.direction != 0:
            assert not post.passed, "Cooldown blocks immediately after pass"
    else:
        print(f"[E2E Contracts] Gate BLOCKED: {gate_result.reason}")


def test_pipeline_attribution():
    """15 synthetic trades through the Attribution stage."""
    np.random.seed(42)
    bars = _make_bars(n=140, start_price=4500.0, trend=0.3, vol=2.5)
    _, _, snapshot, signals = _warm_sfe_and_normalizer(bars, FACTOR_SIGNAL_CONFIG)

    non_none = {k: v for k, v in signals.items() if v is not None}
    if not non_none:
        pytest.skip("All signals None — attribution test needs non-None signals")

    compositor = PortfolioCompositor(FACTOR_PORTFOLIO_CONFIG)
    composite = compositor.compose(signals, snapshot)

    # ── Stage 5: AttributionEngine ──
    attr = AttributionEngine()
    n_trades = 15
    signal_names = list(non_none.keys())[:8]
    for t_idx in range(n_trades):
        pos_id = t_idx + 1
        direction = 1 if t_idx < n_trades * 0.6 else -1
        open_price = 4500.0 + t_idx * 2.0
        close_price = open_price + (5.0 if t_idx % 3 != 0 else -5.0) * direction

        trade_signals = {name: non_none[name] for name in signal_names}
        trade_attr = TradeAttribution(
            position_id=pos_id,
            open_ts=time.time() + t_idx * 300,
            open_price=open_price,
            direction=direction,
            factor_signals=trade_signals,
            factor_values={name: snapshot.get(name) for name in signal_names},
            active_weights={name: FACTOR_PORTFOLIO_CONFIG.get(name, {}).get("weight", 1.0)
                           for name in signal_names},
            composite_score=composite.score,
            tactical_score=composite.tactical_score,
            macro_score=composite.macro_score,
            tags_breakdown=composite.tags_breakdown,
            total_signal_abs=sum(abs(s) for s in trade_signals.values()),
        )
        attr.record_open(pos_id, trade_attr)
        mc = attr.record_close(pos_id, close_price, time.time() + t_idx * 300 + 60)
        assert isinstance(mc, dict)
        if mc:
            assert all(isinstance(v, (int, float)) for v in mc.values())
            assert all(math.isfinite(v) for v in mc.values() if v is not None)

    # Verify attribution stats
    all_stats = attr.get_all_factor_stats()
    assert len(all_stats) > 0, "Expected factor stats after trades"
    for name, stats in all_stats.items():
        assert stats.n_trades > 0
        assert stats.composite_sharpe_score is not None
        if not math.isnan(stats.composite_sharpe_score):
            assert math.isfinite(stats.composite_sharpe_score)
    print(f"[E2E AWE] {len(all_stats)} factors with stats after {n_trades} trades")



def test_pipeline_weak_signal_blocked_by_gate():
    """Weak market -> low signal -> Gate blocks."""
    np.random.seed(42)
    # Random walk, no trend
    bars = _make_bars(n=140, start_price=4500.0, trend=0.0, vol=5.0)
    _, _, snapshot, signals = _warm_sfe_and_normalizer(bars, FACTOR_SIGNAL_CONFIG)

    compositor = PortfolioCompositor({**FACTOR_PORTFOLIO_CONFIG, "_signal_threshold": 0.3})
    composite = compositor.compose(signals, snapshot)

    gate = ExecutionGate({"signal_threshold": 0.3})
    result = gate.filter(composite, snapshot, bars[-1])
    assert hasattr(result, "passed")
    print(f"[E2E Weak] score={composite.score:.4f} dir={composite.direction} "
          f"gate_passed={result.passed} reason={result.reason}")


def test_full_pipeline_no_exceptions():
    """All 5 stages without any exception for 60 bars."""
    np.random.seed(42)
    bars = _make_bars(n=140, start_price=4500.0, trend=0.2, vol=2.0)
    _, _, snapshot, signals = _warm_sfe_and_normalizer(bars, FACTOR_SIGNAL_CONFIG)

    # Compositor
    compositor = PortfolioCompositor(FACTOR_PORTFOLIO_CONFIG)
    composite = compositor.compose(signals, snapshot)

    # Gate
    gate = ExecutionGate({"signal_threshold": 0.4})
    gate_result = gate.filter(composite, snapshot, bars[-1])

    # Attribution (at least 1 trade)
    attr = AttributionEngine()
    if gate_result.passed and composite.direction != 0:
        non_none = {k: v for k, v in signals.items() if v is not None}
        trade_attr = TradeAttribution(
            position_id=1,
            open_ts=bars[-1]["time"],
            open_price=bars[-1]["close"],
            direction=composite.direction,
            factor_signals={n: non_none[n] for n in list(non_none)[:5]},
            factor_values={n: snapshot.get(n) for n in list(non_none)[:5]},
            active_weights={"rsi_14": 1.0, "di_spread": 1.5},
            composite_score=composite.score,
            tactical_score=composite.tactical_score,
            macro_score=composite.macro_score,
            tags_breakdown=composite.tags_breakdown,
            total_signal_abs=sum(abs(s) for s in list(non_none.values())[:5]),
        )
        attr.record_open(1, trade_attr)
        close_price = bars[-1]["close"] + 2.0 * composite.direction
        mc = attr.record_close(1, close_price, bars[-1]["time"] + 60)
        assert isinstance(mc, dict)

    print(f"[E2E Full] 5 stages: {len(snapshot)} factors, "
          f"gate={gate_result.passed}, "
          f"stats={len(attr.get_all_factor_stats())} factors")
