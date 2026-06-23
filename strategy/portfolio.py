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
                 max_volume: float = 0.5, min_volume: float = 0.01,
                 leverage: int = 500, contract_size: int = 100):
        self.method = method
        self.risk_pct = risk_pct / 100.0   # 转小数
        self.max_volume = max_volume
        self.min_volume = min_volume
        self.leverage = leverage
        self.contract_size = contract_size
        self.margin_rate = 1.0 / leverage   # 500x → 0.002

    def compute_size(self, entry_price: float, sl_price: float,
                     atr: float | None = None) -> float:
        """
        计算开仓 volume

        Args:
            entry_price: 入场价
            sl_price: 止损价
            atr: 当前ATR（备用）

        Returns: volume（精确到0.01）

        两个约束取最小值：
        1. 风险约束: volume = (balance × risk%) / (|entry-sl| × contract_size)
        2. 杠杆约束: volume = (balance / margin_rate) / (price × contract_size)
        """
        balance = state.equity

        pip_risk = abs(entry_price - sl_price)
        if pip_risk <= 0:
            return self.min_volume
        risk_volume = (balance * self.risk_pct) / (pip_risk * self.contract_size)

        buying_power = balance / self.margin_rate
        leverage_volume = buying_power / (entry_price * self.contract_size)

        volume = min(risk_volume, leverage_volume)
        volume = max(self.min_volume, min(volume, self.max_volume))
        return round(volume, 2)

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

        kelly_fraction = max(0.0, min(kelly_fraction * 0.5, 0.25))

        balance = balance or state.equity
        return balance * kelly_fraction
