"""
Pre-Trade Checks — 开仓前风控

在订单执行前检查：
- 单笔风险是否超限
- 日内交易次数是否达上限
- 熔断是否激活
- 持仓是否已满
"""

import logging

from core.state import state

logger = logging.getLogger(__name__)


class PreTradeChecker:
    """
    前置风控检查器

    所有检查通过后才允许下单。
    """

    def __init__(self, max_daily_loss_pct: float = 10.0,
                 max_trades: int = 20,
                 max_consecutive_loss: int = 5,
                 # FOOTGUN-3 fix (audit 2026-06-06): 默认 2.0 → 35.0
                 # 0.01 lot × 3ATR SL × 100 oz = $25-30 实际单笔风险, 2.0 默认会拒几乎所有开仓
                 # PaperTrader 默认已用 35.0, 这里跟 PaperTrader 对齐
                 single_risk_usd: float = 35.0):
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_trades = max_trades
        self.max_consecutive_loss = max_consecutive_loss
        self.single_risk_usd = single_risk_usd

    def check(self, entry_price: float, sl_price: float, size: float) -> tuple[bool, str]:
        """
        执行所有前置检查

        Returns: (通过?, 原因)
        """
        # 1. 熔断检查
        if state.is_circuit_breaker:
            return False, f"熔断激活: {state.circuit_reason}"

        # 2. 交易时间检查（TODO: session filter）
        
        # 3. 日内亏损检查
        if state.daily_loss_pct >= self.max_daily_loss_pct:
            reason = f"日内亏损{state.daily_loss_pct:.1f}%达到上限"
            # P5a (BUG-10): 走 mark_breaker 发 event, 不再直写
            state.mark_breaker(True, reason)
            return False, reason

        # 4. 交易次数检查
        if state.daily.total_trades >= self.max_trades:
            return False, f"日交易次数{state.daily.total_trades}已达上限"

        # 5. 连续亏损检查
        if state.daily.consecutive_losses >= self.max_consecutive_loss:
            reason = f"连续亏损{state.daily.consecutive_losses}笔"
            # P5a (BUG-10): 同上
            state.mark_breaker(True, reason)
            return False, reason

        # 6. 单笔风险检查
        pip_risk = abs(entry_price - sl_price)
        dollar_risk = pip_risk * size * 100  # 100 oz/lot
        if dollar_risk > self.single_risk_usd:
            return False, f"单笔风险${dollar_risk:.2f}超过上限${self.single_risk_usd}"

        # 7. 持仓检查
        if state.has_position:
            return False, "已有持仓"

        return True, "OK"
