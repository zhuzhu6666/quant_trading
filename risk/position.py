"""
Position Monitor — 持仓实时监控

监控持仓状态：
- 止损/止盈触发检查
- 移动止损(Trailing Stop)
- 最大回撤保护
"""

import logging

from core.state import state
from core.event_bus import bus, Event, EventType

logger = logging.getLogger(__name__)


class PositionMonitor:
    """
    持仓监控器

    每个tick检查是否需要平仓。
    """

    def __init__(self, enable_trailing_stop: bool = False,
                 trail_atr_mult: float = 1.5):
        self.enable_trailing_stop = enable_trailing_stop
        self.trail_atr_mult = trail_atr_mult
        self._trail_high: float = 0.0  # 多仓最高价
        self._trail_low: float = float("inf")  # 空仓最低价

    def on_tick(self, event: Event):
        """
        处理tick，检查SL/TP/移动止损

        返回: 是否需要平仓
        """
        if not state.has_position:
            return False

        tick = event.data
        bid = tick["bid"]
        ask = tick["ask"]
        pos = state.position

        # 更新移动止损基准
        if self.enable_trailing_stop:
            if pos.direction == 1:
                self._trail_high = max(self._trail_high, bid)
            else:
                self._trail_low = min(self._trail_low, ask)

        # 止损/止盈检查
        if pos.direction == 1:  # 多仓
            if bid <= pos.sl_price:
                logger.info(f"SL hit: bid={bid:.2f} <= sl={pos.sl_price:.2f}")
                return True
            if bid >= pos.tp_price:
                logger.info(f"TP hit: bid={bid:.2f} >= tp={pos.tp_price:.2f}")
                return True
            # 移动止损
            if self.enable_trailing_stop and self._trail_high > pos.entry_price:
                trail_sl = self._trail_high - self.trail_atr_mult * (pos.sl_price - pos.entry_price) / abs(pos.sl_price - pos.entry_price) if pos.sl_price != pos.entry_price else self._trail_high - 5
                if trail_sl > pos.sl_price:
                    pos.sl_price = trail_sl

        elif pos.direction == -1:  # 空仓
            if ask >= pos.sl_price:
                logger.info(f"SL hit: ask={ask:.2f} >= sl={pos.sl_price:.2f}")
                return True
            if ask <= pos.tp_price:
                logger.info(f"TP hit: ask={ask:.2f} <= tp={pos.tp_price:.2f}")
                return True
            # 移动止损
            if self.enable_trailing_stop and self._trail_low < pos.entry_price:
                trail_sl = self._trail_low + (pos.sl_price - pos.entry_price) if pos.sl_price != pos.entry_price else self._trail_low + 5
                if trail_sl < pos.sl_price:
                    pos.sl_price = trail_sl

        return False

    def reset(self):
        """新开仓时重置移动止损基准"""
        pos = state.position
        if pos.direction == 1:
            self._trail_high = pos.entry_price
        else:
            self._trail_low = pos.entry_price
