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
    break_even_trades: int = 0     # BUG-5: 跟赢/亏区分
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
        """日内亏损百分比（只算亏损, 盈盈利日返回 0）。

        旧版用 abs(net_pnl) 导致大盈盈利日被误判为亏损熔断。
        """
        if self.balance <= 0:
            return 0.0
        # 只算亏损: net_pnl < 0 时返回正值百分比, net_pnl >= 0 时返回 0
        return max(0.0, -self.daily.net_pnl) / self.balance * 100

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
        """记录一笔完整交易

        BUG-11 (audit 2026-06-04) 契约: pnl 必须是 NET (已含 commission)
        - daily.net_pnl += pnl (pnl 已经是 net)
        - daily.commission 累加 (用于 audit, 不影响 balance)
        - balance += pnl (不 -commission, 避免双扣)
        - daily.gross_pnl 累加 (gross = net + commission, 用于 audit)

        若 caller 传的是 gross, 请先在 caller 端做 net = gross - commission。
        """
        with self._lock:
            self.daily.total_trades += 1
            self.daily.net_pnl += pnl
            self.daily.commission += commission
            self.daily.slippage += slippage
            self.daily.gross_pnl += pnl + commission
            self.balance += pnl  # pnl is net, 不再 -commission

            if pnl > 0:
                self.daily.winning_trades += 1
                self.daily.consecutive_losses = 0
            elif pnl < 0:
                self.daily.losing_trades += 1
                self.daily.consecutive_losses += 1
            else:
                # 零净利单独计 (commission 跟 pnl 抵消的边界)
                self.daily.break_even_trades += 1

    def reset_daily(self, preserve_peak: bool = True):
        """每日重置

        ARCH-4 (audit 2026-06-04) 合约: 默认 preserve_peak=True,
        跟 CircuitBreaker.reset() 的 "caller 构造 DailyStats(date, peak=peak)"
        约定一致 — peak 不被清零, 否则 circuit.check_all 的分母会跌到 balance。

        preserve_peak=False 显式清 peak (用于 reset_for_test 等场景)。
        """
        with self._lock:
            if preserve_peak:
                # 保留 peak, 重置其他
                old_peak = self.daily.peak_equity
                old_date = self.daily.date
                self.daily = DailyStats()
                self.daily.peak_equity = old_peak
                self.daily.date = old_date
            else:
                self.daily = DailyStats()
            self.active_orders.clear()
            self.is_circuit_breaker = False
            self.circuit_reason = ""

    # ---------------------------------------------------------------------
    # P5a (audit 2026-06-04 ARCH-3 + BUG-10): 走 helper 的 mutation
    # 持有 _lock 保证多线程下不会 torn state,
    # mark_breaker 还会发 EventType.CIRCUIT_BREAK 让 bus subscriber 收到。
    # ---------------------------------------------------------------------

    def mark_breaker(self, tripped: bool, reason: str = ""):
        """持锁设熔断 + 同步发 EventType.CIRCUIT_BREAK event (tripped=True)

        reset (tripped=False) 不发 event, 避免误报。daily reset 也走这里
        一次, 由 reset_daily 内部已 acquire lock, 不会再发 event。
        """
        with self._lock:
            self.is_circuit_breaker = tripped
            self.circuit_reason = reason
        # event 在锁外发, 避免持锁时回调递归
        if tripped:
            try:
                from core.event_bus import bus, Event, EventType
                bus.publish_sync(Event(
                    type=EventType.CIRCUIT_BREAK,
                    data={"reason": reason},
                    source="state.mark_breaker",
                ))
            except Exception:
                # event bus 不可用不应阻断熔断本身
                pass

    def set_sl_price(self, price: float):
        """持锁设 position.sl_price (其他字段的 helper 留后续 PR)"""
        with self._lock:
            self.position.sl_price = price


# 全局单例
state = State()
