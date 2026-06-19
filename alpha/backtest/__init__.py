"""alpha/backtest — 回测基础设施 (Phase 1).

- vectorized.py: FactorBacktester — 向量化回测引擎
- (future) event_driven.py: 事件驱动回测
"""

from alpha.backtest.vectorized import FactorBacktester, BacktestResult

__all__ = ["FactorBacktester", "BacktestResult"]
