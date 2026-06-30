"""Tests for AttributionEngine — 实盘归因引擎。

Phase 4 of FACTOR_TAKEOVER_V4.
"""
import json
import time
from pathlib import Path

import numpy as np
import pytest

from alpha.attribution_engine import (
    TradeAttribution,
    FactorAttributionStats,
    AttributionEngine,
)


def _make_attribution(
    position_id=101,
    direction=1,
    open_price=4500.0,
    signals=None,
    values=None,
    weights=None,
):
    signals = signals or {"rsi_14": 0.6, "di_spread": 0.8, "dxy_corr_20": 0.3}
    return TradeAttribution(
        position_id=position_id,
        open_ts=time.time() - 3600,
        open_price=open_price,
        direction=direction,
        factor_signals=signals,
        factor_values=values or signals,
        active_weights=weights or {n: 1.0 for n in signals},
        composite_score=0.5,
        tactical_score=0.6,
        macro_score=0.3,
        tags_breakdown={"技术": 0.5, "宏观": 0.3},
        total_signal_abs=sum(abs(s) for s in signals.values() if s is not None),
    )


class TestTradeAttribution:

    def test_default_construction(self):
        t = TradeAttribution(
            position_id=1, open_ts=0.0, open_price=4500.0, direction=1,
            factor_signals={}, factor_values={}, active_weights={},
            composite_score=0.0, tactical_score=0.0, macro_score=0.0,
            tags_breakdown={}, total_signal_abs=0.0,
        )
        assert t.position_id == 1
        assert t.direction == 1


class TestFactorAttributionStats:

    def test_initial_state(self):
        s = FactorAttributionStats(name="rsi_14")
        assert s.n_trades == 0
        assert s.win_rate == 0.0
        assert s.avg_mc == 0.0

    def test_record_updates_stats(self):
        s = FactorAttributionStats(name="rsi_14")
        s.record(mc=0.05, is_win=True, tags={"技术": 0.5})
        assert s.n_trades == 1
        assert s.n_voted == 1
        assert s.wins == 1
        assert s.win_rate == 1.0
        assert s.avg_mc == 0.05

    def test_record_multiple_trades(self):
        s = FactorAttributionStats(name="rsi_14")
        for mc, win in [(0.05, True), (-0.03, False), (0.08, True)]:
            s.record(mc=mc, is_win=win, tags={})
        assert s.n_trades == 3
        assert s.n_voted == 3
        assert s.wins == 2
        assert s.win_rate == pytest.approx(2 / 3)
        assert s.avg_mc == pytest.approx(0.1 / 3)

    def test_sharpe_short_returns_nan_when_insufficient_data(self):
        s = FactorAttributionStats(name="test")
        assert np.isnan(s.sharpe_short)

    def test_sharpe_after_sufficient_trades(self):
        """有 50+ 条记录时 sharpe_short 应为有限值。"""
        s = FactorAttributionStats(name="test")
        np.random.seed(42)
        for mc in np.random.randn(60) * 0.02:
            s.record(mc=mc, is_win=mc > 0, tags={})
        short = s.sharpe_short
        assert not np.isnan(short), f"sharpe_short={short}"
        assert isinstance(short, float)


class TestAttributionEngine:

    def test_open_close_linear_mc(self):
        """开仓→平仓→计算线性 MC 归因。"""
        engine = AttributionEngine()
        attr = _make_attribution(position_id=1, signals={
            "rsi_14": 0.6, "di_spread": 0.8,
        })
        engine.record_open(1, attr)
        # 平仓: 价格上涨 (long → 盈利)
        result = engine.record_close(1, close_price=4550.0, close_ts=time.time())
        # 应该有归因
        assert len(result) > 0
        assert "rsi_14" in result
        assert "di_spread" in result
        # PnL = (4550-4500) * 1 = 50
        # MC_rsi = 0.6 / (0.6+0.8) * 50 = 0.6/1.4 * 50 ≈ 21.43
        # MC_di = 0.8 / (0.6+0.8) * 50 = 0.8/1.4 * 50 ≈ 28.57
        assert result["rsi_14"] == pytest.approx(0.6 / 1.4 * 50, rel=1e-3)
        assert result["di_spread"] == pytest.approx(0.8 / 1.4 * 50, rel=1e-3)

    def test_close_uses_actual_net_pnl_for_allocation(self):
        engine = AttributionEngine()
        attr = _make_attribution(
            position_id=11,
            signals={"rsi_14": 0.5, "di_spread": 0.5},
        )
        engine.record_open(11, attr)

        result = engine.record_close(
            11,
            close_price=4550.0,
            close_ts=time.time(),
            real_pnl={"gross": 12.0, "swap": 0.0, "commission": -2.0, "net": 10.0},
        )

        assert sum(result.values()) == pytest.approx(10.0)
        assert result["rsi_14"] == pytest.approx(5.0)
        assert result["di_spread"] == pytest.approx(5.0)

    def test_close_unknown_position(self):
        """不存在的 position_id 返回空 dict。"""
        engine = AttributionEngine()
        result = engine.record_close(999, close_price=4550.0, close_ts=time.time())
        assert result == {}

    def test_restore_open_rebuilds_in_memory_context_without_record_open(self):
        attr = _make_attribution(position_id=7, signals={"rsi_14": 0.5, "di_spread": 0.5})
        payload = attr.to_jsonable()

        engine = AttributionEngine()
        restored = engine.restore_open(7, payload)
        result = engine.record_close(7, close_price=4510.0, close_ts=time.time())

        assert restored is True
        assert engine.open_integrity(7) == "missing"
        assert result["rsi_14"] == pytest.approx(5.0)
        assert result["di_spread"] == pytest.approx(5.0)

    def test_short_position_negative_pnl(self):
        """空头方向计算正确。"""
        engine = AttributionEngine()
        attr = _make_attribution(
            position_id=2, direction=-1, open_price=4550.0,
            signals={"rsi_14": -0.7, "di_spread": -0.5},
        )
        engine.record_open(2, attr)
        # 价格下跌 → 空头盈利
        result = engine.record_close(2, close_price=4500.0, close_ts=time.time())
        pnl = (4500 - 4550) * (-1)  # = 50
        assert len(result) == 2
        assert result["rsi_14"] == pytest.approx(
            -0.7 / (0.7 + 0.5) * pnl, rel=1e-3
        )

    def test_per_factor_stats_accumulate(self):
        """多次交易后因子统计正确累积。"""
        tmp_path = str(Path(__file__).resolve().parent / "_test_attribution.json")
        engine = AttributionEngine(stats_snapshot_path=tmp_path)
        for pid in range(10):
            attr = _make_attribution(
                position_id=pid,
                signals={"rsi_14": 0.5, "di_spread": 0.5},
            )
            engine.record_open(pid, attr)
            engine.record_close(pid, close_price=4510.0, close_ts=time.time())
        stats = engine.get_all_factor_stats()
        assert "rsi_14" in stats
        assert stats["rsi_14"].n_trades == 10
        Path(tmp_path).unlink(missing_ok=True)
        assert stats["rsi_14"].n_voted == 10

    def test_get_factor_stats_returns_none_for_unknown(self):
        engine = AttributionEngine(stats_snapshot_path=":memory:")
        assert engine.get_factor_stats("nonexistent") is None

    def test_trade_log_is_written(self):
        """平仓时应写入 trade log JSONL。"""
        tmp_dir = Path(__file__).resolve().parent
        tmp_log = str(tmp_dir / "_test_trades.jsonl")
        tmp_snapshot = str(tmp_dir / "_test_attribution.json")
        engine = AttributionEngine(
            trade_log_path=tmp_log, stats_snapshot_path=tmp_snapshot,
        )
        attr = _make_attribution(position_id=1)
        engine.record_open(1, attr)
        engine.record_close(1, close_price=4550.0, close_ts=time.time())

        lines = Path(tmp_log).read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) >= 1
        entry = json.loads(lines[0])
        assert entry["position_id"] == 1
        assert "marginal_contributions" in entry
        Path(tmp_log).unlink(missing_ok=True)
        Path(tmp_snapshot).unlink(missing_ok=True)
