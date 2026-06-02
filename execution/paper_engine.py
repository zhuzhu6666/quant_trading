"""
Paper Execution Engine — 模拟撮合引擎

用途：
- paper 模式（实时数据，模拟成交）
- 回放历史 bar 验证策略

撮合规则（保守）：
  对多头：
    1. 若 SL 在区间内且 TP 不在 → SL 触发
    2. 若 TP 在区间内且 SL 不在 → TP 触发
    3. 都在：按 OHLC 顺序 → open→high→low→close，SL/TP 谁先被 hit
    4. 都不在 → 以 close 价成交（视为 hold）
  对空头对称。

手续费：$6/lot 单边（与实盘一致）
滑点：2bps，固定

状态：复用 core.state 单例，与回测/实盘一致。
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from core.state import state, Position
from strategy.base import Signal
from risk.pre_trade import PreTradeChecker
from risk.circuit import CircuitBreaker
from execution.slippage import DynamicSlippageModel

logger = logging.getLogger(__name__)


@dataclass
class PaperTrade:
    """模拟成交记录"""
    ticket: int
    symbol: str
    direction: int            # 1=open long, -1=open short, 2=close long, -2=close short
    volume: float
    price: float
    time: float
    pnl: float = 0.0
    commission: float = 0.0
    reason: str = ""          # "open" | "sl" | "tp" | "signal_flip" | "eod"
    strategy: str = ""


class PaperExecutionEngine:
    """
    模拟撮合引擎

    接收 Signal → 在每根 bar 上检查是否开仓
    持有期间每根 bar 检查 SL/TP → 触发即平仓
    """

    # 合约/费用参数（XAUUSD+ Bybit 实盘一致）
    CONTRACT_SIZE = 100        # 100 oz/lot
    COMMISSION_PER_LOT = 6.0   # $6/lot 单边
    SLIPPAGE_BPS = 2.0         # 2bps
    DIGITS = 2

    def __init__(self, initial_balance: float = 500.0,
                 default_lots: float = 0.01,
                 max_position_lots: float = 0.5,
                 risk_per_trade_pct: float = 0.0,  # 单笔风险占账户% (0=固定 default_lots)
                 min_lots: float = 0.01,
                 pre_trade: Optional[PreTradeChecker] = None,
                 circuit_breaker: Optional[CircuitBreaker] = None,
                 atr_source: Optional[callable] = None,
                 slippage_model: Optional[DynamicSlippageModel] = None):
        self.initial_balance = initial_balance
        self.default_lots = default_lots
        self.max_position_lots = max_position_lots
        self.min_lots = min_lots
        self.risk_per_trade_pct = risk_per_trade_pct  # 0 = 禁用动态仓位
        self.pre_trade = pre_trade
        self.circuit_breaker = circuit_breaker
        # atr_source: 接收 bar 字典，返回当前 ATR 值（用于熔断喂入 + 动态滑点）
        self.atr_source = atr_source
        # 动态滑点模型（None = 保留固定 2bps）
        self.slippage_model = slippage_model
        # 当前 bar 上下文（供 _apply_slippage 使用）
        self._current_bar: dict | None = None
        self._current_atr: float | None = None
        self._is_event_day: bool = False

        # 运行状态
        self.balance = initial_balance
        self.equity = initial_balance
        self.position: Optional[Position] = None
        self._ticket_counter = 100000
        self._trades: list[PaperTrade] = []
        # pending signal: 由 bar[t] 末尾的策略产生 → bar[t+1] 开盘成交
        self._pending_signal: Optional[Signal] = None
        # 熔断统计：累计被风控拦下的笔数
        self._blocked_count = 0

        # 同步到全局 state
        state.balance = initial_balance
        state.equity = initial_balance
        state.position = Position()

    # ── 内部 ──────────────────────────────────────────

    def _new_ticket(self) -> int:
        self._ticket_counter += 1
        return self._ticket_counter

    def _apply_slippage(self, price: float, direction: int) -> float:
        """动态滑点（优先 DynamicSlippageModel）或固定 2bps 回退"""
        if self.slippage_model is not None:
            # 用当前 bar 上下文 + ATR + 事件日标志估算 USD/oz 滑点
            bar = self._current_bar or {}
            slip = self.slippage_model.estimate(
                bar=bar,
                atr=self._current_atr,
                is_event_day=self._is_event_day,
            )
            return price + slip if direction == 1 else price - slip
        # 原有固定 2bps 滑点
        slip = price * self.SLIPPAGE_BPS / 10000.0
        return price + slip if direction == 1 else price - slip

    def _commission(self, lots: float) -> float:
        return lots * self.COMMISSION_PER_LOT

    def _open(self, signal: Signal, fill_price: float):
        """开仓"""
        # 计算 SL/TP 价（用信号给的 atr 倍数）
        atr = signal.atr if signal.atr > 0 else 1.0
        if signal.direction == 1:
            sl_price = fill_price - atr * signal.sl_atr
            tp_price = fill_price + atr * signal.tp_atr
        else:
            sl_price = fill_price + atr * signal.sl_atr
            tp_price = fill_price - atr * signal.tp_atr

        # ── 动态仓位（Kelly-style） ──────────────────────
        # risk_per_trade_pct > 0 时按账户余额 × 风险% / 单笔价差 反推手数
        # signal.strength (默认 1.0) 作为额外乘数（FOMC boost 用）
        size_mult = max(0.01, float(getattr(signal, 'strength', 1.0) or 1.0))
        if self.risk_per_trade_pct > 0:
            pip_risk = abs(fill_price - sl_price)
            risk_dollars = self.equity * (self.risk_per_trade_pct / 100.0) * size_mult
            if pip_risk > 0:
                # lots = risk_usd / (sl_distance × contract_size)
                lots = risk_dollars / (pip_risk * self.CONTRACT_SIZE)
            else:
                lots = self.default_lots
            # 钳制 [min_lots, max_position_lots]，并按 0.01 取整（MT5 步进）
            lots = max(self.min_lots, min(round(lots, 2), self.max_position_lots))
        else:
            # 无 Kelly 时，strength 也作为倍数（但要 max_lots 钳制）
            lots = round(self.default_lots * size_mult, 2)
            lots = max(self.min_lots, min(lots, self.max_position_lots))

        # ── 前置风控检查 ──────────────────────────────
        if self.pre_trade is not None:
            passed, reason = self.pre_trade.check(fill_price, sl_price, lots)
            if not passed:
                self._blocked_count += 1
                logger.info(
                    f"BLOCKED OPEN by pre_trade: {signal.strategy} "
                    f"{'BUY' if signal.direction==1 else 'SELL'} — {reason}"
                )
                return None
        # ── 熔断检查 ──────────────────────────────────
        if self.circuit_breaker is not None and self.circuit_breaker.is_tripped:
            self._blocked_count += 1
            logger.info(
                f"BLOCKED OPEN by circuit_breaker: {state.circuit_reason}"
            )
            return None

        # 模拟滑点
        actual_price = self._apply_slippage(fill_price, signal.direction)
        comm = self._commission(lots)
        # 计算实际滑点（百分点）
        slip_pct = abs(actual_price - fill_price) / fill_price * 100
        if self.circuit_breaker is not None:
            self.circuit_breaker.feed_slippage(slip_pct)

        self.balance -= comm
        self.equity = self.balance
        self.position = Position(
            symbol=signal.symbol,
            direction=signal.direction,
            volume=lots,
            entry_price=actual_price,
            current_price=actual_price,
            sl_price=sl_price,
            tp_price=tp_price,
            entry_time=datetime.utcnow(),
        )
        state.position = self.position
        state.balance = self.balance
        state.equity = self.equity

        trade = PaperTrade(
            ticket=self._new_ticket(),
            symbol=signal.symbol,
            direction=signal.direction,
            volume=lots,
            price=actual_price,
            time=signal.timestamp,
            pnl=-comm,
            commission=comm,
            reason="open",
            strategy=signal.strategy,
        )
        self._trades.append(trade)
        logger.info(
            f"OPEN {'LONG' if signal.direction==1 else 'SHORT'} "
            f"ticket={trade.ticket} price={actual_price:.2f} "
            f"sl={sl_price:.2f} tp={tp_price:.2f} lots={lots} comm=${comm:.2f}"
        )

    def _close(self, fill_price: float, reason: str, bar_time: float | None = None) -> Optional[PaperTrade]:
        """平仓"""
        if not self.position or self.position.direction == 0:
            return None

        pos = self.position
        # 平仓方向与持仓相反
        close_dir = -pos.direction
        actual_price = self._apply_slippage(fill_price, close_dir)
        comm = self._commission(pos.volume)

        # 计算 PnL：1 手 = 100 oz，价差×手数×100
        if pos.direction == 1:
            pnl = (actual_price - pos.entry_price) * pos.volume * self.CONTRACT_SIZE
        else:
            pnl = (pos.entry_price - actual_price) * pos.volume * self.CONTRACT_SIZE

        net_pnl = pnl - comm
        self.balance += net_pnl
        self.equity = self.balance

        # 当日统计
        state.daily.total_trades += 1
        state.daily.gross_pnl += pnl
        state.daily.commission += comm
        if net_pnl > 0:
            state.daily.winning_trades += 1
            state.daily.consecutive_losses = 0
        else:
            state.daily.losing_trades += 1
            state.daily.consecutive_losses += 1
        state.daily.net_pnl = self.balance - self.initial_balance
        state.daily.peak_equity = max(state.daily.peak_equity, self.balance)

        # 持仓清空
        trade = PaperTrade(
            ticket=self._new_ticket(),
            symbol=pos.symbol,
            direction=2 if pos.direction == 1 else -2,
            volume=pos.volume,
            price=actual_price,
            time=bar_time if bar_time is not None else (pos.entry_time.timestamp() if pos.entry_time else 0.0),
            pnl=net_pnl,
            commission=comm,
            reason=reason,
        )
        self._trades.append(trade)

        logger.info(
            f"CLOSE reason={reason} ticket={trade.ticket} "
            f"price={actual_price:.2f} pnl=${net_pnl:+.2f} "
            f"bal=${self.balance:.2f}"
        )

        self.position = None
        state.position = Position()
        state.balance = self.balance
        state.equity = self.equity
        return trade

    # ── 主入口 ────────────────────────────────────────

    def on_bar(self, bar: dict, signal: Optional[Signal]) -> Optional[PaperTrade]:
        """
        每根 bar 调用一次。

        关键时序（防未来函数）：
          - signal 由 bar[t].on_bar() 在 bar[t] close 时生成
          - signal 的执行必须在 bar[t+1].open 价（与 backtrader exectype=Close 一致）
          - bar[t] 自身的 SL/TP 用 bar[t].high/low 检查（已发生的事）

        Args:
            bar: 完成的 K 线 {open, high, low, close, time, ...}
            signal: 策略信号（None = 无信号）— 由上一根 bar 累积触发

        Returns:
            本根 bar 触发的成交记录（开或平），无则 None
        """
        result = None

        # 0. 缓存 bar 上下文供动态滑点模型使用
        self._current_bar = bar
        if self.atr_source is not None:
            self._current_atr = self.atr_source(bar)
        else:
            self._current_atr = None

        # 0.5 喂入 ATR 给熔断器（每根 bar 末调用）
        if self.circuit_breaker is not None and self.atr_source is not None:
            atr_val = self.atr_source(bar)
            if atr_val is not None and atr_val > 0:
                self.circuit_breaker.feed_atr(atr_val)

        # 0.5 熔断检查（用最新权益 + 最新 daily stats）
        if self.circuit_breaker is not None and not self.circuit_breaker.is_tripped:
            tripped, reason = self.circuit_breaker.check_all()
            # trip 由 check_all 内部完成，这里只打日志

        # 1. 若有持仓，先用本 bar 的 high/low 检查 SL/TP
        #    （SL/TP 是历史已经发生的事，可以基于本 bar 数据判断）
        if self.position and self.position.direction != 0:
            result = self._check_exit(bar)

        # 2. 处理 pending 信号 → 在本 bar OPEN 价成交
        #    关键规则（与 backtrader 一致）：
        #    - 有持仓时，pending signal 被忽略（不立即反向）
        #    - 等本 bar 的 SL/TP 触发平仓后，下一根 bar 的 signal 才开新仓
        if self._pending_signal is not None and (not self.position or self.position.direction == 0):
            sig = self._pending_signal
            fill_price = bar["open"]
            self._open(sig, fill_price)
        # 无论是否有持仓，pending signal 都被消费掉
        # （有持仓时丢弃，无持仓时已用）
        self._pending_signal = None

        # 3. 缓存当前 bar 产生的信号（等下一根 bar 开盘成交）
        if signal is not None:
            self._pending_signal = signal

        # 4. 更新持仓当前价（用 close）
        if self.position and self.position.direction != 0:
            self.position.current_price = bar["close"]
            if self.position.direction == 1:
                self.position.unrealized_pnl = (
                    (bar["close"] - self.position.entry_price)
                    * self.position.volume * self.CONTRACT_SIZE
                )
            else:
                self.position.unrealized_pnl = (
                    (self.position.entry_price - bar["close"])
                    * self.position.volume * self.CONTRACT_SIZE
                )
            self.equity = self.balance + self.position.unrealized_pnl
            state.equity = self.equity

        # 5. 用 close 后权益更新 state.daily.peak_equity（熔断用）
        if self.equity > state.daily.peak_equity:
            state.daily.peak_equity = self.equity
        # 同步日损所需字段
        state.daily.net_pnl = self.balance - self.initial_balance

        return result

    def _check_exit(self, bar: dict) -> Optional[PaperTrade]:
        """
        在单根 bar 的 OHLC 4 步里检查 SL/TP：

        1. 假设 open 后行情朝某方向走
        2. 走到 high（多头）或 low（空头）前先看 SL
        3. 简化：若 high >= TP（多）或 low <= TP（空）→ TP hit
                若 low <= SL（多）或 high >= SL（空）→ SL hit
        4. SL/TP 同根：默认 SL 优先（保守）
        """
        pos = self.position
        o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
        sl, tp = pos.sl_price, pos.tp_price

        if pos.direction == 1:  # 多仓
            sl_hit = l <= sl
            tp_hit = h >= tp
        else:  # 空仓
            sl_hit = h >= sl
            tp_hit = l <= tp

        if sl_hit and tp_hit:
            # 同根都触发：保守取 SL
            return self._close(sl, reason="sl", bar_time=bar.get("time"))
        if sl_hit:
            return self._close(sl, reason="sl", bar_time=bar.get("time"))
        if tp_hit:
            return self._close(tp, reason="tp", bar_time=bar.get("time"))
        return None

    # ── 统计 ──────────────────────────────────────────

    @property
    def trades(self) -> list[PaperTrade]:
        return list(self._trades)

    def summary(self) -> dict:
        """返回当前统计"""
        closes = [t for t in self._trades if t.direction in (2, -2)]
        wins = [t for t in closes if t.pnl > 0]
        losses = [t for t in closes if t.pnl <= 0]
        total = len(closes)
        gross_pnl = sum(t.pnl for t in closes)
        win_rate = (len(wins) / total * 100) if total > 0 else 0.0
        avg_win = (sum(t.pnl for t in wins) / len(wins)) if wins else 0.0
        avg_loss = (sum(t.pnl for t in losses) / len(losses)) if losses else 0.0
        return {
            "total_trades": total,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": win_rate,
            "gross_pnl": gross_pnl,
            "net_pnl": self.balance - self.initial_balance,
            "balance": self.balance,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "profit_factor": (sum(t.pnl for t in wins) / abs(sum(t.pnl for t in losses)))
                             if losses and sum(t.pnl for t in losses) != 0 else float("inf"),
        }
