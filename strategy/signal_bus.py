"""
Signal Bus — 多策略信号融合

多个策略同时产生信号时，需要：
1. 去重：同一方向重复信号合并
2. 冲突解决：多空信号同时出现时的优先级
3. 降噪：过滤弱信号
"""

import logging
from collections import defaultdict

from strategy.base import Signal

logger = logging.getLogger(__name__)


class SignalBus:
    """
    信号融合总线

    策略:
    - 同方向多个信号 → 取信号最强的
    - 多空同时出现 → 取强信号方向
    - 强度低于阈值 → 丢弃
    """

    def __init__(self, min_strength: float = 0.3):
        self.min_strength = min_strength
        self._pending: dict[str, list[Signal]] = defaultdict(list)
        self._last_signal: Signal | None = None

    def receive(self, signal: Signal):
        """接收一个信号"""
        if signal.strength < self.min_strength:
            return
        key = f"{signal.symbol}_{signal.timeframe}" if hasattr(signal, 'timeframe') else signal.symbol
        self._pending[key].append(signal)

    def resolve(self, symbol_key: str = "default") -> Signal | None:
        """
        解析当前pending信号，返回融合后的最终信号

        返回None表示无有效信号。
        """
        signals = self._pending.get(symbol_key, [])
        self._pending[symbol_key] = []

        if not signals:
            return None

        # 按方向分组
        longs = [s for s in signals if s.direction == 1]
        shorts = [s for s in signals if s.direction == -1]
        flats = [s for s in signals if s.direction == 0]

        # 有平仓信号 → 优先
        if flats:
            return max(flats, key=lambda s: s.strength)

        # 多空对比
        long_strength = sum(s.strength for s in longs)
        short_strength = sum(s.strength for s in shorts)

        if long_strength > short_strength and longs:
            winner = max(longs, key=lambda s: s.strength)
        elif short_strength > long_strength and shorts:
            winner = max(shorts, key=lambda s: s.strength)
        else:
            return None  # 平局，不交易

        self._last_signal = winner
        return winner

    def clear(self):
        self._pending.clear()
