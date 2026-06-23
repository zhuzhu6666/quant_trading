"""Tests for alpha/evaluation/result.py — 统一评价接口."""

import time
import pytest
from alpha.evaluation.result import EvaluationResult


class TestEvaluationResult:
    def test_from_backtest(self):
        """从 BacktestResult 构造."""
        class FakeBT:
            n_trades = 50
            total_pnl = 1250.0
            win_rate = 0.62
            sharpe_ratio = 1.8
            max_drawdown = 8.5
            avg_hold_bars = 12.0
            profit_factor = 1.5
            total_return = 0.125  # 12.5%

        result = EvaluationResult.from_backtest(FakeBT())
        assert result.source == "backtest"
        assert result.n_trades == 50
        assert result.total_pnl == 1250.0
        assert result.sharpe == 1.8
        assert result.total_return_pct == 12.5
        assert "BACKTEST" in result.summary_text()
        assert "50笔" in result.summary_text()

    def test_from_shadow(self):
        """从 ShadowPerf 构造."""
        class FakeShadow:
            factor = "test_factor"
            oos_bars = 88
            cumulative_pnl = 0.0123
            hit_rate = 0.61
            max_drawdown = 0.002
            timeframe = "M5"

        result = EvaluationResult.from_shadow(FakeShadow(), symbol="XAUUSD+")
        assert result.source == "shadow"
        assert result.n_trades == 88
        assert result.win_rate == 0.61
        assert result.total_return_pct == 1.23
        assert "test_factor" in result.factor_returns

    def test_from_attribution(self):
        """从 AttributionEngine 构造."""
        class FakeStats:
            n_trades = 30
            net_pnl = 450.0
            win_count = 18
            composite_sharpe_score = 1.2
            max_dd = 5.0
            avg_holding_runtime = 8.0
            avg_mc = 0.03

        class FakeAttr:
            def get_all_factor_stats(self):
                return {"rsi_14": FakeStats(), "macd": FakeStats()}

        result = EvaluationResult.from_attribution(FakeAttr(), symbol="XAUUSD+")
        assert result.source == "live"
        assert result.n_trades == 60  # 2 factors × 30
        assert result.win_rate == 0.6
        assert len(result.factor_returns) == 2

    def test_to_dict(self):
        """序列化."""
        result = EvaluationResult(
            source="backtest",
            n_trades=10,
            total_pnl=100.0,
            win_rate=0.5,
            sharpe=1.0,
        )
        d = result.to_dict()
        assert d["n_trades"] == 10
        assert d["total_pnl"] == 100.0
        assert d["source"] == "backtest"

    def test_summary_text(self):
        """可读摘要."""
        result = EvaluationResult(
            source="live",
            n_trades=30,
            total_pnl=750.0,
            win_rate=0.6,
            sharpe=1.5,
            max_drawdown=4.2,
        )
        text = result.summary_text()
        assert "LIVE" in text
        assert "30笔" in text
        assert "$750" in text or "750.00" in text
