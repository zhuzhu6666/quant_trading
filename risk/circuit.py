"""
Circuit Breaker — 熔断机制

触发条件：
1. 日内亏损超限 (max_daily_loss_pct，相对日内峰值权益)
2. 连续亏损N笔 (consecutive_losses)
3. 滑点过大（已实现平均滑点 > max_slippage_pct）
4. 波动率异常（当前ATR > 历史中位数 × volatility_mult）

一旦触发：当日禁止所有新开仓；已持仓 SL/TP 照常走（不平）。

说明：
- 日损分母用 peak_equity 而非 balance：避免"亏到小数后分母变小"反复触发的死循环
- ATR 序列由调用方维护（中位数基线 + 当前 ATR）
- 熔断触发后必须等到 reset() 才解除（防止同一根 bar 反复触发）
"""

import logging
from collections import deque
from statistics import median

from core.state import state
from core.event_bus import bus, Event, EventType

logger = logging.getLogger(__name__)


class CircuitBreaker:
    """
    熔断控制器

    一旦触发，当日禁止所有新交易。
    """

    def __init__(self, max_daily_loss_pct: float = 5.0,
                 max_consecutive_loss: int = 5,
                 max_slippage_pct: float = 0.5,
                 volatility_mult: float = 3.0,
                 atr_window: int = 100):
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_consecutive_loss = max_consecutive_loss
        self.max_slippage_pct = max_slippage_pct
        self.volatility_mult = volatility_mult
        self.atr_window = atr_window

        # ATR 历史（bar 末尾喂入）
        self._atr_history: deque[float] = deque(maxlen=atr_window)
        self._slippage_sum: float = 0.0
        self._slippage_count: int = 0

    def feed_atr(self, atr: float):
        """每根 bar 喂入当前 ATR（用于波动率熔断）"""
        if atr is not None and atr > 0:
            self._atr_history.append(atr)

    def feed_slippage(self, slippage_pct: float):
        """每笔成交喂入实际滑点（百分比形式，如 0.02 表示 0.02%）"""
        if slippage_pct is not None and slippage_pct >= 0:
            self._slippage_sum += slippage_pct
            self._slippage_count += 1

    @property
    def avg_slippage_pct(self) -> float:
        return (self._slippage_sum / self._slippage_count) if self._slippage_count else 0.0

    @property
    def is_tripped(self) -> bool:
        return state.is_circuit_breaker

    def check_all(self) -> tuple[bool, str]:
        """
        检查所有熔断条件

        Returns: (是否触发, 原因)
        """
        # 已触发就别再检查（避免重复日志）
        if state.is_circuit_breaker:
            return True, state.circuit_reason

        # 1. 日内亏损（相对 peak_equity，更稳定）
        peak = state.daily.peak_equity or state.balance
        if peak > 0:
            dd_pct = (peak - state.equity) / peak * 100
            if dd_pct >= self.max_daily_loss_pct:
                self.trip(f"日内回撤{dd_pct:.1f}%达到上限{self.max_daily_loss_pct}%")
                return True, state.circuit_reason

        # 2. 连续亏损
        if state.daily.consecutive_losses >= self.max_consecutive_loss:
            self.trip(f"连续亏损{state.daily.consecutive_losses}笔达到上限{self.max_consecutive_loss}")
            return True, state.circuit_reason

        # 3. 滑点
        if self._slippage_count >= 5 and self.avg_slippage_pct > self.max_slippage_pct:
            self.trip(f"平均滑点{self.avg_slippage_pct:.2f}%超过上限{self.max_slippage_pct}%")
            return True, state.circuit_reason

        # 4. 波动率异常
        if len(self._atr_history) >= max(20, self.atr_window // 5):
            med = median(self._atr_history)
            cur = self._atr_history[-1]
            if med > 0 and cur > self.volatility_mult * med:
                self.trip(f"波动率异常：ATR={cur:.2f} > {self.volatility_mult}×中位数{med:.2f}")
                return True, state.circuit_reason

        return False, "OK"

    def trip(self, reason: str):
        """手动触发熔断"""
        if state.is_circuit_breaker:
            return  # 已触发
        state.is_circuit_breaker = True
        state.circuit_reason = reason
        logger.warning(f"CIRCUIT BREAKER TRIPPED: {reason}")

        bus.publish_sync(Event(
            type=EventType.CIRCUIT_BREAK,
            data={"reason": reason},
            source="circuit_breaker",
        ))

    def reset(self):
        """重置熔断（跨日后）"""
        state.is_circuit_breaker = False
        state.circuit_reason = ""
        state.reset_daily()
        self._atr_history.clear()
        self._slippage_sum = 0.0
        self._slippage_count = 0
        logger.info("Circuit breaker reset")
