"""execution/paper_execution.py — 撮合 + 持仓 + 滑点 三合一
合并原三模块(543+403+105) → 单一交易链路
保留：bar[t+1].open 撮合、持仓与 SL/TP 管理、滑点估算（一次交易三步）
关键注释：与backtrader一致 in-bar SL/TP — signal[t] 在 bar[t+1].open 成交，SL/TP 用当根 high/low 判定
_sharpe.py 112 保留不动
"""
from __future__ import annotations
import logging
import math
import os
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timezone, date
from typing import Optional
import numpy as np
from core.state import state, Position
from strategy.base import BaseStrategy, Signal
from risk.pre_trade import PreTradeChecker
from risk.circuit import CircuitBreaker
from execution.event_sizing import EventSizing
from execution._sharpe import sharpe_ratio_log_nw, TF_BARS_PER_YEAR
logger = logging.getLogger(__name__)

# ── 滑点：原 slippage.py 合并进来 ──────────────────────────
GOLD_TICK_USD = 0.01

class DynamicSlippageModel:
    """动态滑点估算器 (黄金) — 与原 slippage.py 一致"""
    DEFAULT_CONFIG = {
        "base_ticks": 0.5,
        "atr_mult": 0.05,
        "event_boost": 2.0,
        "low_liquidity_hours": [22, 23, 0, 1],
        "low_liquidity_boost": 1.5,
        "max_ticks": 3.0,
    }
    def __init__(self, config: Optional[dict] = None):
        self.config = {**self.DEFAULT_CONFIG, **(config or {})}
    def _is_low_liquidity_hour(self, bar: dict) -> bool:
        ts = bar.get("time")
        if ts is None:
            return False
        try:
            hour = datetime.fromtimestamp(ts, tz=timezone.utc).hour
        except (OSError, ValueError, OverflowError):
            return False
        return hour in self.config["low_liquidity_hours"]
    def estimate(self, bar: Optional[dict] = None, atr: Optional[float] = None, is_event_day: bool = False) -> float:
        cfg = self.config
        ticks = cfg["base_ticks"]
        if atr is not None and atr > 0:
            ticks += atr * cfg["atr_mult"] / GOLD_TICK_USD
        if is_event_day:
            ticks *= cfg["event_boost"]
        if bar is not None and self._is_low_liquidity_hour(bar):
            ticks *= cfg["low_liquidity_boost"]
        ticks = min(ticks, cfg["max_ticks"])
        return ticks * GOLD_TICK_USD
    def get_spread_estimate(self, atr: Optional[float] = None, is_event: bool = False, bar: Optional[dict] = None) -> dict:
        cfg = self.config
        base_ticks = cfg["base_ticks"]
        atr_ticks = (atr * cfg["atr_mult"] / GOLD_TICK_USD) if (atr and atr > 0) else 0.0
        total_ticks = base_ticks + atr_ticks
        event_factor = cfg["event_boost"] if is_event else 1.0
        total_ticks *= event_factor
        low_liq_factor = 1.0
        if bar is not None and self._is_low_liquidity_hour(bar):
            low_liq_factor = cfg.get("low_liquidity_boost", 1.5)
            total_ticks *= low_liq_factor
        total_ticks = min(total_ticks, cfg["max_ticks"])
        return {"base_ticks": base_ticks, "atr_component_ticks": atr_ticks, "event_boost": event_factor, "low_liquidity_boost": low_liq_factor, "max_ticks_cap": cfg["max_ticks"], "total_ticks": round(total_ticks,4), "total_usd_per_oz": round(total_ticks*GOLD_TICK_USD,4)}

FORCE_CLOSE_BASED_SLTP = os.environ.get("FORCE_CLOSE_BASED_SLTP", "0") == "1"

@dataclass
class PaperTrade:
    ticket: int
    symbol: str
    direction: int
    volume: float
    price: float
    time: float
    pnl: float = 0.0
    commission: float = 0.0
    swap: float = 0.0
    reason: str = ""
    strategy: str = ""

class PaperExecutionEngine:
    """模拟撮合引擎 — 与backtrader一致 in-bar SL/TP；signal[t]→bar[t+1].open 撮合"""
    CONTRACT_SIZE = 100
    COMMISSION_PER_VOLUME = 6.0
    SLIPPAGE_BPS = 2.0
    DIGITS = 2
    def __init__(self, initial_balance: float = 500.0, default_volume: float = 0.01, max_position_volume: float = 0.5, risk_per_trade_pct: float | None = None, min_volume: float = 0.01, pre_trade: Optional[PreTradeChecker] = None, circuit_breaker: Optional[CircuitBreaker] = None, atr_source: Optional[callable] = None, slippage_model: Optional[DynamicSlippageModel] = None, event_sizing: Optional[EventSizing] = None, swap_long_per_volume_per_day: float = -1.0, swap_short_per_volume_per_day: float = 0.0, enable_swap: bool = True):
        self.initial_balance = initial_balance
        self.default_volume = default_volume
        self.max_position_volume = max_position_volume
        self.min_volume = min_volume
        self.risk_per_trade_pct = risk_per_trade_pct
        if risk_per_trade_pct is None:
            logger.debug("[FOOTGUN-2] risk_per_trade_pct=None, 禁用 Kelly")
        elif risk_per_trade_pct == 0.0:
            logger.warning("[FOOTGUN-2] risk_per_trade_pct=0.0 真0% → 拒单，欲禁用请传 None")
        self.pre_trade = pre_trade
        self.circuit_breaker = circuit_breaker
        self.atr_source = atr_source
        self.slippage_model = slippage_model
        self.event_sizing = event_sizing
        self.enable_swap = enable_swap
        self.swap_long_per_volume_per_day = swap_long_per_volume_per_day
        self.swap_short_per_volume_per_day = swap_short_per_volume_per_day
        self._current_bar: dict | None = None
        self._current_atr: float | None = None
        self._is_event_day: bool = False
        self.balance = initial_balance
        self.equity = initial_balance
        self.position: Optional[Position] = None
        self._ticket_counter = 100000
        self._trades: list[PaperTrade] = []
        self._pending_signal: Optional[Signal] = None
        self._blocked_count = 0
        state.balance = initial_balance
        state.equity = initial_balance
        state.position = Position()
    def _new_ticket(self) -> int:
        self._ticket_counter += 1
        return self._ticket_counter
    def _apply_slippage(self, price: float, direction: int) -> float:
        if self.slippage_model is not None:
            bar = self._current_bar or {}
            slip = self.slippage_model.estimate(bar=bar, atr=self._current_atr, is_event_day=self._is_event_day)
            return price + slip if direction == 1 else price - slip
        slip = price * self.SLIPPAGE_BPS / 10000.0
        return price + slip if direction == 1 else price - slip
    def _commission(self, volume: float) -> float:
        return volume * self.COMMISSION_PER_VOLUME
    def _open(self, signal: Signal, fill_price: float, bar_time: float | None = None):
        atr = signal.atr if signal.atr > 0 else 1.0
        if signal.direction == 1:
            sl_price = fill_price - atr * signal.sl_atr
            tp_price = fill_price + atr * signal.tp_atr
        else:
            sl_price = fill_price + atr * signal.sl_atr
            tp_price = fill_price - atr * signal.tp_atr
        size_mult = max(0.01, float(getattr(signal, 'strength', 1.0) or 1.0))
        if self.risk_per_trade_pct is not None and self.risk_per_trade_pct > 0:
            pip_risk = abs(fill_price - sl_price)
            risk_dollars = self.equity * (self.risk_per_trade_pct / 100.0) * size_mult
            volume = (risk_dollars / (pip_risk * self.CONTRACT_SIZE)) if pip_risk > 0 else self.default_volume
        elif self.risk_per_trade_pct == 0.0:
            volume = 0.0
        else:
            volume = self.default_volume * size_mult
        if self.event_sizing is not None and signal.timestamp > 0:
            event_mult = self.event_sizing.get_multiplier(signal.timestamp)
            volume *= event_mult
            if event_mult < 1.0:
                logger.debug(f"EVENT SIZING: mult={event_mult:.2f} vol={volume:.4f}")
        volume = max(self.min_volume, min(round(volume, 2), self.max_position_volume))
        if self.pre_trade is not None:
            passed, reason = self.pre_trade.check(fill_price, sl_price, volume)
            if not passed:
                self._blocked_count += 1
                logger.info(f"BLOCKED OPEN by pre_trade: {signal.strategy} {'BUY' if signal.direction==1 else 'SELL'} — {reason}")
                return None
        if self.circuit_breaker is not None and self.circuit_breaker.is_tripped:
            self._blocked_count += 1
            logger.info(f"BLOCKED OPEN by circuit_breaker: {state.circuit_reason}")
            return None
        actual_price = self._apply_slippage(fill_price, signal.direction)
        comm = self._commission(volume)
        slip_pct = abs(actual_price - fill_price) / fill_price * 100
        if self.circuit_breaker is not None:
            self.circuit_breaker.feed_slippage(slip_pct)
        self.balance -= comm
        self.equity = self.balance
        entry_dt = datetime.fromtimestamp(float(bar_time), tz=timezone.utc) if bar_time is not None else datetime.now(timezone.utc)
        self.position = Position(symbol=signal.symbol, direction=signal.direction, volume=volume, entry_price=actual_price, current_price=actual_price, sl_price=sl_price, tp_price=tp_price, entry_time=entry_dt)
        state.position = self.position
        state.balance = self.balance
        state.equity = self.equity
        trade = PaperTrade(ticket=self._new_ticket(), symbol=signal.symbol, direction=signal.direction, volume=volume, price=actual_price, time=signal.timestamp, pnl=-comm, commission=comm, reason="open", strategy=signal.strategy)
        self._trades.append(trade)
        logger.info(f"OPEN {'LONG' if signal.direction==1 else 'SHORT'} ticket={trade.ticket} price={actual_price:.2f} sl={sl_price:.2f} tp={tp_price:.2f} vol={volume} comm=${comm:.2f}")
    def _close(self, fill_price: float, reason: str, bar_time: float | None = None) -> Optional[PaperTrade]:
        if not self.position or self.position.direction == 0:
            return None
        pos = self.position
        close_dir = -pos.direction
        actual_price = self._apply_slippage(fill_price, close_dir)
        comm = self._commission(pos.volume)
        pnl = (actual_price - pos.entry_price) * pos.volume * self.CONTRACT_SIZE if pos.direction == 1 else (pos.entry_price - actual_price) * pos.volume * self.CONTRACT_SIZE
        swap_cost = 0.0
        if self.enable_swap and pos.entry_time is not None and bar_time is not None:
            try:
                entry_ts = pos.entry_time.timestamp() if isinstance(pos.entry_time, datetime) else float(pos.entry_time)
                hold_sec = max(0.0, float(bar_time) - entry_ts)
                hold_days = hold_sec / 86400.0
                swap_rate = self.swap_long_per_volume_per_day if pos.direction == 1 else self.swap_short_per_volume_per_day
                swap_cost = swap_rate * pos.volume * hold_days
            except Exception as e:
                logger.debug(f"swap calc skipped: {e}")
                swap_cost = 0.0
        net_pnl = pnl - comm + swap_cost
        self.balance += net_pnl
        self.equity = self.balance
        state.daily.total_trades += 1
        state.daily.gross_pnl += pnl
        state.daily.commission += comm
        if net_pnl > 0:
            state.daily.winning_trades += 1
            state.daily.consecutive_losses = 0
        elif net_pnl < 0:
            state.daily.losing_trades += 1
            state.daily.consecutive_losses += 1
        else:
            state.daily.break_even_trades += 1
        state.daily.net_pnl = self.balance - self.initial_balance
        state.daily.peak_equity = max(state.daily.peak_equity, self.balance)
        trade = PaperTrade(ticket=self._new_ticket(), symbol=pos.symbol, direction=2 if pos.direction == 1 else -2, volume=pos.volume, price=actual_price, time=bar_time if bar_time is not None else (pos.entry_time.timestamp() if isinstance(pos.entry_time, datetime) else 0.0), pnl=net_pnl, commission=comm, swap=swap_cost, reason=reason)
        self._trades.append(trade)
        logger.info(f"CLOSE reason={reason} ticket={trade.ticket} price={actual_price:.2f} pnl=${net_pnl:+.2f} (swap=${swap_cost:+.2f}) bal=${self.balance:.2f}")
        self.position = None
        state.position = Position()
        state.balance = self.balance
        state.equity = self.equity
        return trade
    def on_bar(self, bar: dict, signal: Optional[Signal]) -> Optional[PaperTrade]:
        """每根 bar 调用 — 与backtrader一致 in-bar SL/TP：signal[t] 在 bar[t+1].open 撮合，本 bar 先用 high/low 判定 SL/TP"""
        result = None
        self._current_bar = bar
        self._current_atr = self.atr_source(bar) if self.atr_source else None
        if self.circuit_breaker is not None and self.atr_source is not None:
            atr_val = self.atr_source(bar)
            if atr_val is not None and atr_val > 0:
                self.circuit_breaker.feed_atr(atr_val)
        if self.circuit_breaker is not None and not self.circuit_breaker.is_tripped:
            tripped, reason = self.circuit_breaker.check_all()
            if tripped:
                logger.warning('Circuit breaker tripped: %s', reason)
        if self.position and self.position.direction != 0:
            result = self._check_exit(bar)
        if self._pending_signal is not None and (not self.position or self.position.direction == 0):
            sig = self._pending_signal
            fill_price = bar["open"]
            if not FORCE_CLOSE_BASED_SLTP:
                spread_pts = int(bar.get("spread", 0) or 0)
                half_spread = spread_pts * 0.01 / 2.0
                if sig.direction == 1:
                    fill_price = fill_price + half_spread
            self._open(sig, fill_price, bar_time=bar.get("time"))
        self._pending_signal = None
        if signal is not None:
            self._pending_signal = signal
        if self.position and self.position.direction != 0:
            self.position.current_price = bar["close"]
            if self.position.direction == 1:
                self.position.unrealized_pnl = (bar["close"] - self.position.entry_price) * self.position.volume * self.CONTRACT_SIZE
            else:
                self.position.unrealized_pnl = (self.position.entry_price - bar["close"]) * self.position.volume * self.CONTRACT_SIZE
            self.equity = self.balance + self.position.unrealized_pnl
            state.equity = self.equity
        if self.equity > state.daily.peak_equity:
            state.daily.peak_equity = self.equity
        state.daily.net_pnl = self.balance - self.initial_balance
        return result
    def _check_exit(self, bar: dict) -> Optional[PaperTrade]:
        """在单根 bar 的 OHLC 4 步里检查 SL/TP（与backtrader一致 in-bar SL/TP，保守 SL 优先）"""
        pos = self.position
        o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
        sl, tp = pos.sl_price, pos.tp_price
        if FORCE_CLOSE_BASED_SLTP:
            if pos.direction == 1:
                sl_hit = l <= sl
                tp_hit = h >= tp
            else:
                sl_hit = h >= sl
                tp_hit = l <= tp
        else:
            spread_pts = int(bar.get("spread", 0) or 0)
            spread_usd = spread_pts * 0.01
            ask_low = l + spread_usd
            if pos.direction == 1:
                sl_hit = l <= sl
                tp_hit = h >= tp
            else:
                sl_hit = h >= sl
                tp_hit = ask_low <= tp
        if sl_hit and tp_hit:
            return self._close(sl, reason="sl", bar_time=bar.get("time"))
        if sl_hit:
            return self._close(sl, reason="sl", bar_time=bar.get("time"))
        if tp_hit:
            return self._close(tp, reason="tp", bar_time=bar.get("time"))
        return None
    @property
    def trades(self) -> list[PaperTrade]:
        return list(self._trades)
    def summary(self) -> dict:
        closes = [t for t in self._trades if t.direction in (2, -2)]
        wins = [t for t in closes if t.pnl > 0]
        losses = [t for t in closes if t.pnl <= 0]
        total = len(closes)
        gross_pnl = sum(t.pnl for t in closes)
        win_rate = (len(wins) / total * 100) if total > 0 else 0.0
        avg_win = (sum(t.pnl for t in wins) / len(wins)) if wins else 0.0
        avg_loss = (sum(t.pnl for t in losses) / len(losses)) if losses else 0.0
        return {"total_trades": total, "wins": len(wins), "losses": len(losses), "win_rate": win_rate, "gross_pnl": gross_pnl, "net_pnl": self.balance - self.initial_balance, "balance": self.balance, "avg_win": avg_win, "avg_loss": avg_loss, "profit_factor": (sum(t.pnl for t in wins) / abs(sum(t.pnl for t in losses))) if losses and sum(t.pnl for t in losses) != 0 else float("inf")}

# 兼容别名（旧 tune 脚本）
PaperEngine = PaperExecutionEngine

@dataclass
class PaperReport:
    symbol: str
    timeframe: str
    strategy: str
    start_date: str
    end_date: str
    n_bars: int
    initial_balance: float
    final_balance: float
    net_pnl: float
    total_return_pct: float
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    profit_factor: float
    avg_win: float
    avg_loss: float
    max_drawdown_pct: float
    sharpe: float
    longest_win_streak: int
    longest_loss_streak: int
    final_position: str
    daily_pnl: list[float] = field(default_factory=list)

class PaperTrader:
    """模拟盘编排器 — Bar feed → Strategy → PaperExecutionEngine"""
    def __init__(self, strategy: BaseStrategy, initial_balance: float = 500.0, default_volume: float = 0.01, max_position_volume: float = 0.5, warmup_bars: int = 500, max_daily_loss_pct: float = 10.0, max_consecutive_loss: int = 5, max_trades_per_day: int = 20, single_risk_usd: float = 35.0, volatility_mult: float = 3.0, risk_per_trade_pct: float | None = None, enable_circuit: bool = True, enable_swap: bool = True, swap_long_per_volume_per_day: float = -1.0, swap_short_per_volume_per_day: float = 0.0, event_sizing=None):
        self.strategy = strategy
        if enable_circuit:
            self.pre_trade = PreTradeChecker(max_daily_loss_pct=max_daily_loss_pct, max_trades=max_trades_per_day, max_consecutive_loss=max_consecutive_loss, single_risk_usd=single_risk_usd)
            self.circuit_breaker = CircuitBreaker(max_daily_loss_pct=max_daily_loss_pct, max_consecutive_loss=max_consecutive_loss, volatility_mult=volatility_mult)
        else:
            self.pre_trade = None
            self.circuit_breaker = None
        def _atr_source(bar: dict) -> float | None:
            atr = self.strategy.last_atr
            return float(atr) if atr is not None and atr > 0 else None
        self.engine = PaperExecutionEngine(initial_balance=initial_balance, default_volume=default_volume, max_position_volume=max_position_volume, risk_per_trade_pct=risk_per_trade_pct, pre_trade=self.pre_trade, circuit_breaker=self.circuit_breaker, atr_source=_atr_source, enable_swap=enable_swap, swap_long_per_volume_per_day=swap_long_per_volume_per_day, swap_short_per_volume_per_day=swap_short_per_volume_per_day, event_sizing=event_sizing)
        self.warmup_bars = warmup_bars
        self._bars: list[dict] = []
        self._equity_curve: list[tuple[float, float]] = []
        self._last_reset_date: date | None = None
    def load_data(self, store, symbol: str, timeframe: str, start: str | None = None, end: str | None = None):
        df = store.load_bars(symbol, timeframe, start=start, end=end)
        if df.empty:
            raise ValueError(f"No {timeframe} data for {symbol}")
        bars = []
        idxs = df.index.to_numpy()
        opens = df["open"].to_numpy()
        highs = df["high"].to_numpy()
        lows = df["low"].to_numpy()
        closes = df["close"].to_numpy()
        vols = df["volume"].to_numpy() if "volume" in df.columns else np.zeros(len(df))
        spreads = df["spread"].to_numpy() if "spread" in df.columns else np.zeros(len(df), dtype=int)
        for i in range(len(df)):
            bars.append({"timeframe": timeframe, "time": idxs[i].timestamp() if hasattr(idxs[i], "timestamp") else float(idxs[i]), "open": float(opens[i]), "high": float(highs[i]), "low": float(lows[i]), "close": float(closes[i]), "volume": float(vols[i]), "spread": int(spreads[i] or 0), "complete": True})
        self._bars = bars
        logger.info(f"PaperTrader loaded {len(bars)} {timeframe} bars")
    def run(self) -> PaperReport:
        n = len(self._bars)
        if n == 0:
            raise ValueError("No bars loaded")
        self.strategy.on_init()
        self.engine.balance = self.engine.initial_balance
        self.engine.equity = self.engine.initial_balance
        self.engine.position = None
        self._equity_curve.clear()
        logger.info(f"Paper run start: {n} bars, warmup={self.warmup_bars}")
        for i, bar in enumerate(self._bars):
            bar_date = datetime.fromtimestamp(bar["time"], tz=timezone.utc).date()
            if self._last_reset_date is None:
                self._last_reset_date = bar_date
            elif bar_date != self._last_reset_date:
                self._reset_daily_stats(bar_date)
            signal = None
            if i >= self.warmup_bars:
                signal = self.strategy.on_bar(bar)
            self.engine.on_bar(bar, signal)
            self._equity_curve.append((bar["time"], self.engine.equity))
        return self._build_report()
    def _reset_daily_stats(self, today: date):
        from core.state import state, DailyStats
        peak = state.daily.peak_equity
        state.daily = DailyStats(date=today, peak_equity=peak)
        if self.circuit_breaker is not None:
            self.circuit_breaker.reset()
        self._last_reset_date = today
        logger.debug(f"Daily stats reset for {today}")
    def _build_report(self) -> PaperReport:
        eq = np.array([e for _, e in self._equity_curve])
        if len(eq) < 2:
            return PaperReport(symbol=self.strategy.symbol, timeframe=self.strategy.timeframe, strategy=self.strategy.name, start_date="", end_date="", n_bars=len(self._bars), initial_balance=self.engine.initial_balance, final_balance=self.engine.balance, net_pnl=0.0, total_return_pct=0.0, total_trades=0, wins=0, losses=0, win_rate=0.0, profit_factor=0.0, avg_win=0.0, avg_loss=0.0, max_drawdown_pct=0.0, sharpe=0.0, longest_win_streak=0, longest_loss_streak=0, final_position="flat")
        net_pnl = self.engine.balance - self.engine.initial_balance
        total_return_pct = net_pnl / self.engine.initial_balance * 100
        sharpe = sharpe_ratio_log_nw(eq, self.strategy.timeframe)
        peak = np.maximum.accumulate(eq)
        dd = (peak - eq) / peak
        max_dd = float(dd.max() * 100) if len(dd) > 0 else 0.0
        closes = [t for t in self.engine.trades if t.direction in (2, -2)]
        wins = [t for t in closes if t.pnl > 0]
        losses = [t for t in closes if t.pnl <= 0]
        total = len(closes)
        win_rate = (len(wins) / total * 100) if total > 0 else 0.0
        avg_win = float(np.mean([t.pnl for t in wins])) if wins else 0.0
        avg_loss = float(np.mean([t.pnl for t in losses])) if losses else 0.0
        gross_profit = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        pf = (gross_profit / gross_loss) if gross_loss > 1e-9 else float("inf")
        longest_win = longest_loss = cur_win = cur_loss = 0
        for t in closes:
            if t.pnl > 0:
                cur_win += 1; cur_loss = 0; longest_win = max(longest_win, cur_win)
            else:
                cur_loss += 1; cur_win = 0; longest_loss = max(longest_loss, cur_loss)
        daily_pnl = self._daily_pnl_series(closes)
        pos = self.engine.position
        pos_str = f"{'LONG' if pos.direction==1 else 'SHORT'} {pos.volume} @ {pos.entry_price:.2f} sl={pos.sl_price:.2f} tp={pos.tp_price:.2f}" if pos and pos.direction != 0 else "flat"
        return PaperReport(symbol=self.strategy.symbol, timeframe=self.strategy.timeframe, strategy=self.strategy.name, start_date=datetime.fromtimestamp(self._bars[0]["time"], tz=timezone.utc).strftime("%Y-%m-%d"), end_date=datetime.fromtimestamp(self._bars[-1]["time"], tz=timezone.utc).strftime("%Y-%m-%d"), n_bars=len(self._bars), initial_balance=self.engine.initial_balance, final_balance=self.engine.balance, net_pnl=net_pnl, total_return_pct=total_return_pct, total_trades=total, wins=len(wins), losses=len(losses), win_rate=win_rate, profit_factor=pf, avg_win=avg_win, avg_loss=avg_loss, max_drawdown_pct=max_dd, sharpe=sharpe, longest_win_streak=longest_win, longest_loss_streak=longest_loss, final_position=pos_str, daily_pnl=daily_pnl)
    def _daily_pnl_series(self, closes: list[PaperTrade]) -> list[float]:
        by_day: dict[date, float] = {}
        for t in closes:
            d = datetime.fromtimestamp(t.time, tz=timezone.utc).date()
            by_day[d] = by_day.get(d, 0.0) + t.pnl
        return [v for _, v in sorted(by_day.items())]
    def print_report(self, r: PaperReport):
        print()
        print("=" * 72)
        print(f"  PAPER TRADING REPORT — {r.strategy}")
        print("=" * 72)
        print(f"  Symbol        : {r.symbol}  ({r.timeframe})")
        print(f"  Period        : {r.start_date} → {r.end_date}  ({r.n_bars} bars)")
        print(f"  Initial       : ${r.initial_balance:.2f}")
        print(f"  Final         : ${r.final_balance:.2f}")
        print(f"  Net PnL       : ${r.net_pnl:+.2f}  ({r.total_return_pct:+.2f}%)")
        print("-" * 72)
        print(f"  Trades        : {r.total_trades}  (W:{r.wins} / L:{r.losses}  WR={r.win_rate:.1f}%)")
        print(f"  Avg Win/Loss  : ${r.avg_win:+.2f} / ${r.avg_loss:+.2f}  (PF={r.profit_factor:.2f})")
        print(f"  Max Drawdown  : {r.max_drawdown_pct:.2f}%")
        print(f"  Sharpe (ann.) : {r.sharpe:.3f}")
        print(f"  Streaks       : {r.longest_win_streak}W / {r.longest_loss_streak}L")
        print(f"  Final Pos.    : {r.final_position}")
        print("=" * 72)

__all__ = ["DynamicSlippageModel", "PaperTrade", "PaperExecutionEngine", "PaperEngine", "PaperReport", "PaperTrader", "GOLD_TICK_USD", "FORCE_CLOSE_BASED_SLTP"]
