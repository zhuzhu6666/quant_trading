"""
State — 全局状态管理

集中管理系统的可查询状态：持仓、余额、订单、当日统计。
所有模块通过 State 读取共享状态，避免模块间直接耦合。

OPT-4 (audit 2026-06-06): 多账户支持
─────────────────────────────
旧: 单实例 global `state = State()`, 一个程序只能跟踪一个账户
    多 MT5/cTrader 账户 (Bybit-Live-2, Pepperstone-Demo, ...) 共享
    同一 balance/position, 仓位互相污染
新: AccountState 单账户 (原 State 类) + State 多账户容器
    - state.accounts: dict[str, AccountState]  # 多账户
    - state.default_name: str = "default"         # 向后兼容代理目标
    - state.balance / position / daily 仍可访问 (代理到 default 账户)
    - 新建账户: state.create_account("bybit_live_2")
    - 获取: state.get_account("bybit_live_2")
"""

import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, Optional


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


class AccountState:
    """
    线程安全的单账户状态管理器

    只存放运行时状态，不持久化。
    """

    def __init__(self, name: str = "default", initial_balance: float = 100.0):
        self.name = name
        self._lock = threading.Lock()

        # 账户
        self.balance: float = initial_balance
        self.equity: float = initial_balance
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
                    data={"reason": reason, "account": self.name},
                    source="state.mark_breaker",
                ))
            except Exception:
                # event bus 不可用不应阻断熔断本身
                pass

    def set_sl_price(self, price: float):
        """持锁设 position.sl_price (其他字段的 helper 留后续 PR)"""
        with self._lock:
            self.position.sl_price = price


# 向后兼容别名: 旧代码用 State() 实例化, 新代码用 AccountState()
# (重构期间允许过渡, 后续 PR 可全替换)
State = AccountState


class StateContainer:
    """
    OPT-4 (audit 2026-06-06): 多账户状态容器

    用法:
        state = StateContainer()                  # 自动建 "default" 账户
        bybit = state.create_account("bybit_live_2", initial_balance=500.0)
        demo = state.create_account("pepperstone_demo", initial_balance=1000.0)

        # 访问默认账户 (向后兼容, 旧代码 state.balance / state.position 不变)
        state.balance                              # 走 default 账户
        state.position
        state.daily

        # 访问指定账户
        state.accounts["bybit_live_2"].balance
        state.get_account("bybit_live_2").update_equity(2000.0)

        # 切换默认账户
        state.set_default("bybit_live_2")
        state.balance                              # 现在走 bybit_live_2
    """

    def __init__(self, default_name: str = "default", initial_balance: float = 100.0):
        self._lock = threading.RLock()
        self.accounts: Dict[str, AccountState] = {}
        self.default_name: str = default_name
        # 自动建默认账户
        self.create_account(default_name, initial_balance=initial_balance)

    def create_account(self, name: str, initial_balance: float = 100.0) -> AccountState:
        """新建账户, 名字重复抛 ValueError"""
        with self._lock:
            if name in self.accounts:
                raise ValueError(f"Account {name!r} already exists")
            acct = AccountState(name=name, initial_balance=initial_balance)
            self.accounts[name] = acct
            return acct

    def get_account(self, name: str) -> AccountState:
        """获取账户, 不存在抛 KeyError"""
        with self._lock:
            if name not in self.accounts:
                raise KeyError(f"Account {name!r} not found. "
                               f"Existing: {list(self.accounts.keys())}")
            return self.accounts[name]

    def set_default(self, name: str):
        """切换默认账户 (之后 state.balance 等代理访问走新默认)"""
        with self._lock:
            if name not in self.accounts:
                raise KeyError(f"Account {name!r} not found")
            self.default_name = name

    def remove_account(self, name: str):
        """删除账户 (非默认)"""
        with self._lock:
            if name == self.default_name:
                raise ValueError(f"Cannot remove default account {name!r}")
            if name not in self.accounts:
                raise KeyError(f"Account {name!r} not found")
            del self.accounts[name]

    # ── 向后兼容代理: 旧代码访问 state.balance / state.position 走默认账户 ──
    def _default(self) -> AccountState:
        return self.accounts[self.default_name]

    @property
    def balance(self) -> float:
        return self._default().balance

    @balance.setter
    def balance(self, value: float):
        self._default().balance = value

    @property
    def equity(self) -> float:
        return self._default().equity

    @equity.setter
    def equity(self, value: float):
        self._default().equity = value

    @property
    def margin_used(self) -> float:
        return self._default().margin_used

    @margin_used.setter
    def margin_used(self, value: float):
        self._default().margin_used = value

    @property
    def position(self) -> Position:
        return self._default().position

    @position.setter
    def position(self, value: Position):
        self._default().position = value

    @property
    def daily(self) -> DailyStats:
        return self._default().daily

    @daily.setter
    def daily(self, value: DailyStats):
        self._default().daily = value

    @property
    def is_trading(self) -> bool:
        return self._default().is_trading

    @is_trading.setter
    def is_trading(self, value: bool):
        self._default().is_trading = value

    @property
    def is_circuit_breaker(self) -> bool:
        return self._default().is_circuit_breaker

    @is_circuit_breaker.setter
    def is_circuit_breaker(self, value: bool):
        self._default().is_circuit_breaker = value

    @property
    def circuit_reason(self) -> str:
        return self._default().circuit_reason

    @circuit_reason.setter
    def circuit_reason(self, value: str):
        self._default().circuit_reason = value

    @property
    def active_orders(self) -> dict:
        return self._default().active_orders

    @active_orders.setter
    def active_orders(self, value: dict):
        self._default().active_orders = value

    @property
    def has_position(self) -> bool:
        return self._default().has_position

    @property
    def win_rate(self) -> float:
        return self._default().win_rate

    @property
    def daily_loss_pct(self) -> float:
        return self._default().daily_loss_pct

    def update_equity(self, current_price: float):
        self._default().update_equity(current_price)

    def record_trade(self, pnl: float, commission: float = 0.0, slippage: float = 0.0):
        self._default().record_trade(pnl, commission, slippage)

    def reset_daily(self, preserve_peak: bool = True):
        self._default().reset_daily(preserve_peak)

    def mark_breaker(self, tripped: bool, reason: str = ""):
        self._default().mark_breaker(tripped, reason)

    def set_sl_price(self, price: float):
        self._default().set_sl_price(price)


# 全局单例 (OPT-4: 现在是容器, 旧代码 state.balance 仍 work)
state = StateContainer()
