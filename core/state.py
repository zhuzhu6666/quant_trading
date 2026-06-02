"""
State — 全局状态管理

集中管理系统的可查询状态：持仓、余额、订单、当日统计。
所有模块通过 State 读取共享状态，避免模块间直接耦合。
"""

import threading
from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass
class Position:
    symbol: str = ""
    direction: int = 0         # 1=long, -1=short, 0=flat
    volume: float = 0.0
    entry_price: float = 0.0
    current_price: float = 0.0
    sl_price: float = 0.0
    tp_price: float = 0.0
    entry_time: datetime | None = None
    unrealized_pnl: float = 0.0


@dataclass
class DailyStats:
    """当日交易统计"""
    date: date = field(default_factory=date.today)
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    consecutive_losses: int = 0
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    commission: float = 0.0
    slippage: float = 0.0
    max_drawdown_pct: float = 0.0
    peak_equity: float = 0.0


class State:
    """
    线程安全的状态管理器
    
    只存放运行时状态，不持久化。
    """

    def __init__(self):
        self._lock = threading.Lock()

        # 账户
        self.balance: float = 100.0
        self.equity: float = 100.0
        self.margin_used: float = 0.0

        # 持仓
        self.position: Position = Position()
        
        # 当日统计
        self.daily: DailyStats = DailyStats()

        # 系统状态
        self.is_trading: bool = False
        self.is_circuit_breaker: bool = False
        self.circuit_reason: str = ""

        # 活跃订单 {ticket: order_info}
        self.active_orders: dict[int, dict] = {}

    @property
    def has_position(self) -> bool:
        return self.position.direction != 0 and self.position.volume > 0

    @property
    def win_rate(self) -> float:
        if self.daily.total_trades == 0:
            return 0.0
        return self.daily.winning_trades / self.daily.total_trades * 100

    @property
    def daily_loss_pct(self) -> float:
        if self.balance <= 0:
            return 0.0
        return abs(self.daily.net_pnl) / self.balance * 100

    def update_equity(self, current_price: float):
        """根据当前价格更新权益"""
        with self._lock:
            if self.position.direction != 0:
                if self.position.direction == 1:
                    pnl = (current_price - self.position.entry_price) * self.position.volume * 100
                else:
                    pnl = (self.position.entry_price - current_price) * self.position.volume * 100
                self.position.unrealized_pnl = pnl
                self.position.current_price = current_price
                self.equity = self.balance + pnl
            else:
                self.equity = self.balance

            # 更新日内峰值
            if self.equity > self.daily.peak_equity:
                self.daily.peak_equity = self.equity
            # 更新日内回撤
            if self.daily.peak_equity > 0:
                dd = (self.daily.peak_equity - self.equity) / self.daily.peak_equity * 100
                if dd > self.daily.max_drawdown_pct:
                    self.daily.max_drawdown_pct = dd

    def record_trade(self, pnl: float, commission: float = 0.0, slippage: float = 0.0):
        """记录一笔完整交易"""
        with self._lock:
            self.daily.total_trades += 1
            self.daily.net_pnl += pnl
            self.daily.commission += commission
            self.daily.slippage += slippage
            self.balance += pnl - commission

            if pnl > 0:
                self.daily.winning_trades += 1
                self.daily.consecutive_losses = 0
            else:
                self.daily.losing_trades += 1
                self.daily.consecutive_losses += 1

    def reset_daily(self):
        """每日重置"""
        with self._lock:
            self.daily = DailyStats()
            self.active_orders.clear()
            self.is_circuit_breaker = False
            self.circuit_reason = ""


# 全局单例
state = State()
