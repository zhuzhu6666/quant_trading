"""
Portfolio Manager — 仓位分配

根据风险预算在多策略间分配仓位：
- 等权重分配
- Kelly公式
- 固定风险比例
"""

import logging

from core.state import state

logger = logging.getLogger(__name__)


class PortfolioManager:
    """
    仓位管理器

    计算每笔交易的仓位大小。
    支持杠杆：Bybit默认500倍。
    """

    def __init__(self, method: str = "risk_fixed", risk_pct: float = 2.0,
                 max_lots: float = 0.5, min_lots: float = 0.01,
                 leverage: int = 500, contract_size: int = 100):
        self.method = method
        self.risk_pct = risk_pct / 100.0   # 转小数
        self.max_lots = max_lots
        self.min_lots = min_lots
        self.leverage = leverage
        self.contract_size = contract_size
        self.margin_rate = 1.0 / leverage   # 500x → 0.002

    def compute_size(self, entry_price: float, sl_price: float,
                     atr: float | None = None) -> float:
        """
        计算开仓手数

        Args:
            entry_price: 入场价
            sl_price: 止损价
            atr: 当前ATR（备用）

        Returns: 手数（精确到0.01）

        两个约束取最小值：
        1. 风险约束: lots = (balance × risk%) / (|entry-sl| × contract_size)
        2. 杠杆约束: lots = (balance / margin_rate) / (price × contract_size)
        """
        balance = state.equity

        # 约束1: 风险约束
        pip_risk = abs(entry_price - sl_price)
        if pip_risk <= 0:
            return self.min_lots
        risk_lots = (balance * self.risk_pct) / (pip_risk * self.contract_size)

        # 约束2: 杠杆/保证金约束
        buying_power = balance / self.margin_rate
        leverage_lots = buying_power / (entry_price * self.contract_size)

        # 取两者中较小的
        lots = min(risk_lots, leverage_lots)

        # 钳制
        lots = max(self.min_lots, min(lots, self.max_lots))
        return round(lots, 2)

    def compute_kelly(self, win_rate: float, avg_win: float, avg_loss: float,
                      balance: float | None = None) -> float:
        """
        Kelly公式仓位

        f* = (p*b - q) / b
        其中: p=胜率, b=盈亏比, q=1-p
        """
        b = avg_win / max(avg_loss, 1e-6)
        p = win_rate
        q = 1 - p
        kelly_fraction = (p * b - q) / max(b, 1e-6)

        # 半凯利（更保守）
        kelly_fraction = max(0.0, min(kelly_fraction * 0.5, 0.25))

        balance = balance or state.equity
        return balance * kelly_fraction
