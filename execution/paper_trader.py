"""
Paper Trader — 模拟盘编排器

把 Bar feed → Strategy → PaperEngine 串起来，跑历史/实时 bar。

用法：
    from data.store import DataStore
    from strategy.registry import strategy_registry
    from execution.paper_trader import PaperTrader

    store = DataStore()
    strategy = strategy_registry.create("multi_factor_m15",
                                          symbol="XAUUSD+", timeframe="M15")
    strategy.on_init()

    trader = PaperTrader(strategy, initial_balance=500.0)
    trader.load_data(store, "XAUUSD+", "M15")
    report = trader.run()

两种模式：
  - run()  回放历史（replay） — 默认
  - run()  接实时 tick（live paper）— 用 --mode paper 启动

Sharpe / 最大回撤都基于 equity 曲线计算。
"""

import logging
import math
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, date

import numpy as np

from data.store import DataStore
from strategy.base import BaseStrategy
from execution.paper_engine import PaperExecutionEngine, PaperTrade
from execution._sharpe import sharpe_ratio_log_nw, TF_BARS_PER_YEAR  # OPT-2 (audit 2026-06-06)
from risk.pre_trade import PreTradeChecker
from risk.circuit import CircuitBreaker

# Factor imports (add-on, no side effects)
from alpha.ic_tracker import ICTracker
from factors import (
    compute_aroon,
    compute_cci,
    compute_mfi,
    compute_williams_r,
)

logger = logging.getLogger(__name__)


@dataclass
class PaperReport:
    """模拟盘报告"""
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
    final_position: str           # "flat" | "long 0.01 @ X.XX"
    daily_pnl: list[float] = field(default_factory=list)


class PaperTrader:
    """
    模拟盘引擎

    流程：
      1. 从 DataStore 加载历史 bar
      2. 逐根 bar 喂给 strategy.on_bar()
      3. strategy 返回的 Signal → PaperEngine.on_bar()
      4. PaperEngine 内部检查 SL/TP
      5. 收集 equity 序列，结束时输出报告
    """

    def __init__(self, strategy: BaseStrategy, initial_balance: float = 500.0,
                 default_lots: float = 0.01, max_lots: float = 0.5,
                 warmup_bars: int = 500,
                 # 风控参数（按 $500 账户调过, P3 调优 2026-06-02）
                 # 5% 在 50K bar 触发 13+ 次, PnL -33%
                 # 10% 触发 ~3 次, PnL -9% (从 -33% 提升 3.5x)
                 # 注: 0.01 手 XAUUSD 3ATR 单笔风险 ≈ $20-30
                 #     对 $500 账户等于 4-6%，已突破传统 1-2% 准则
                 #     阈值放宽到 10% 以让策略能开仓，依赖熔断器兜底
                 max_daily_loss_pct: float = 10.0,
                 max_consecutive_loss: int = 5,
                 max_trades_per_day: int = 20,
                 single_risk_usd: float = 35.0,
                 volatility_mult: float = 3.0,
                 # FOOTGUN-2 fix (audit 2026-06-06): 区分 None=禁用 vs 0.0=真 0% 风险
                 # None → 固定 default_lots, 0.0 → 拒单 (lots=0), >0 → Kelly
                 risk_per_trade_pct: float | None = None,
                 enable_circuit: bool = True,
                 # P2: 资金费/隔夜利息 (XAUUSD+ 典型 -1.0/lot/day long, 0 short)
                 enable_swap: bool = True,
                 swap_long_per_lot_per_day: float = -1.0,
                 swap_short_per_lot_per_day: float = 0.0,
                 event_sizing=None):
        # FOOTGUN-2 fix (audit 2026-06-06): 改 None=禁用 vs 0.0=真 0% 风险
        # 旧的"0.0 静默禁用"已修, paper_engine._open 走 3 分支:
        #   None  → 固定 default_lots × size_mult
        #   0.0   → lots=0 触发拒单
        #   > 0   → Kelly 动态仓位
        # 旧 warning 块已删除 (paper_engine.__init__ 现在发新 warning)
        self.strategy = strategy

        # 实例化风控层
        # enable_circuit=False 时把 pre_trade 也禁掉（保持"无风控 baseline"语义）
        if enable_circuit:
            self.pre_trade = PreTradeChecker(
                max_daily_loss_pct=max_daily_loss_pct,
                max_trades=max_trades_per_day,
                max_consecutive_loss=max_consecutive_loss,
                single_risk_usd=single_risk_usd,
            )
            self.circuit_breaker = CircuitBreaker(
                max_daily_loss_pct=max_daily_loss_pct,
                max_consecutive_loss=max_consecutive_loss,
                volatility_mult=volatility_mult,
            )
        else:
            self.pre_trade = None
            self.circuit_breaker = None

        # atr_source: 从 strategy 内部取最新 ATR
        def _atr_source(bar: dict) -> float | None:
            atr = self.strategy.last_atr
            if atr is not None and atr > 0:
                return float(atr)
            return None

        self.engine = PaperExecutionEngine(
            initial_balance=initial_balance,
            default_lots=default_lots,
            max_position_lots=max_lots,
            risk_per_trade_pct=risk_per_trade_pct,
            pre_trade=self.pre_trade,
            circuit_breaker=self.circuit_breaker,
            atr_source=_atr_source,
            enable_swap=enable_swap,
            swap_long_per_lot_per_day=swap_long_per_lot_per_day,
            swap_short_per_lot_per_day=swap_short_per_lot_per_day,
            event_sizing=event_sizing,
        )
        self.warmup_bars = warmup_bars
        self._bars: list[dict] = []

        # 因子 IC 追踪（add-on, 不影响现有逻辑）
        self.ic_tracker = ICTracker(window=5000)
        self._factor_buffer: dict[str, list[float]] = {
            "high": [], "low": [], "close": [], "volume": [],
        }
        self._prev_fvs: dict[str, float] | None = None
        self._prev_close: float | None = None

        # equity 曲线（每根 bar 末采样）
        self._equity_curve: list[tuple[float, float]] = []  # (time, equity)
        self._last_reset_date: date | None = None

    def load_data(self, store: DataStore, symbol: str, timeframe: str,
                  start: str | None = None, end: str | None = None):
        """从 DataStore 加载历史 bar 到内存"""
        df = store.load_bars(symbol, timeframe, start=start, end=end)
        if df.empty:
            raise ValueError(f"No {timeframe} data for {symbol}")

        bars = []
        for idx, row in df.iterrows():
            bars.append({
                "timeframe": timeframe,
                "time": idx.timestamp() if hasattr(idx, "timestamp") else float(idx),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row.get("volume", 0)),
                "spread": int(row.get("spread", 0) or 0),  # P2: bid/ask SL/TP 用
                "complete": True,
            })
        self._bars = bars
        logger.info(f"PaperTrader loaded {len(bars)} {timeframe} bars "
                     f"[{bars[0]['time']} → {bars[-1]['time']}]")

    # ── 主循环 ────────────────────────────────────────

    def run(self) -> PaperReport:
        """回放所有 bar，返回报告"""
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
            # 模拟盘每日重置 daily stats
            bar_date = datetime.utcfromtimestamp(bar["time"]).date()
            if self._last_reset_date is None:
                self._last_reset_date = bar_date
            elif bar_date != self._last_reset_date:
                self._reset_daily_stats(bar_date)

            # 1. 策略生成信号（warmup 内不交易）
            signal = None
            if i >= self.warmup_bars:
                signal = self.strategy.on_bar(bar)

            # 2. 模拟撮合
            self.engine.on_bar(bar, signal)

            # 3. 记录 equity
            self._equity_curve.append((bar["time"], self.engine.equity))

        return self._build_report()

    # ── 报告 ──────────────────────────────────────────

    def _reset_daily_stats(self, today: date):
        """重置 daily stats（保留累计 PnL）"""
        from core.state import state, DailyStats
        # 保留 balance/position，仅清当日统计
        peak = state.daily.peak_equity
        state.daily = DailyStats(date=today, peak_equity=peak)
        # 同步重置熔断器（清 ATR 序列 + 解除触发 + 清 daily stats）
        if self.circuit_breaker is not None:
            self.circuit_breaker.reset()
        # 关键：更新 _last_reset_date，否则下一根 bar 还会重置
        self._last_reset_date = today
        logger.debug(f"Daily stats reset for {today}")

    def _build_report(self) -> PaperReport:
        """生成报告"""
        eq = np.array([e for _, e in self._equity_curve])
        if len(eq) < 2:
            return PaperReport(
                symbol=self.strategy.symbol,
                timeframe=self.strategy.timeframe,
                strategy=self.strategy.name,
                start_date="", end_date="",
                n_bars=len(self._bars),
                initial_balance=self.engine.initial_balance,
                final_balance=self.engine.balance,
                net_pnl=0.0, total_return_pct=0.0,
                total_trades=0, wins=0, losses=0, win_rate=0.0,
                profit_factor=0.0, avg_win=0.0, avg_loss=0.0,
                max_drawdown_pct=0.0, sharpe=0.0,
                longest_win_streak=0, longest_loss_streak=0,
                final_position="flat",
            )

        # ── 收益 & 风险 ──
        net_pnl = self.engine.balance - self.engine.initial_balance
        total_return_pct = net_pnl / self.engine.initial_balance * 100

        # Sharpe (OPT-2 audit 2026-06-06: log returns + Newey-West HAC)
        # ──────────────────────────────────────────────────
        # 旧: simple returns + iid 假设
        #   rets = (eq[1] - eq[0]) / eq[0]  (arithmetic returns)
        #   sharpe = mean(rets) / std(rets) * sqrt(bars_per_year)
        #   缺陷: ① simple returns 不可加, 跨多期 Sharpe 偏高
        #         ② iid 假设违反: M15 跨夜 drift + 连续 win/loss + 仓位不调
        #            都会让 equity 序列强自相关, 真实 std 显著大于 iid 估计
        #            → Sharpe 虚高 20-50% (Lo, 2002 已知问题)
        # 新: log returns + Newey-West HAC 标准误 (委托给 _sharpe 模块)
        #   rets = log(eq[1] / eq[0])       (log returns, 可加)
        #   var_nw = γ_0 + 2 * Σ_{k=1}^L (1 - k/(L+1)) * γ_k
        #   L = floor(4 * (T/100)^(2/9))    (NW 1994 自动 lag 选择)
        #   sharpe = mean(rets) / sqrt(var_nw) * sqrt(bars_per_year)
        sharpe = sharpe_ratio_log_nw(eq, self.strategy.timeframe)

        # 最大回撤
        peak = np.maximum.accumulate(eq)
        dd = (peak - eq) / peak
        max_dd = float(dd.max() * 100) if len(dd) > 0 else 0.0

        # ── 交易统计 ──
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

        # 连胜/连败
        longest_win = longest_loss = cur_win = cur_loss = 0
        for t in closes:
            if t.pnl > 0:
                cur_win += 1
                cur_loss = 0
                longest_win = max(longest_win, cur_win)
            else:
                cur_loss += 1
                cur_win = 0
                longest_loss = max(longest_loss, cur_loss)

        # 日盈亏（按 close 价分日）
        daily_pnl = self._daily_pnl_series(closes)

        # 当前持仓
        pos = self.engine.position
        if pos and pos.direction != 0:
            pos_str = (f"{'LONG' if pos.direction==1 else 'SHORT'} "
                       f"{pos.volume} @ {pos.entry_price:.2f} "
                       f"sl={pos.sl_price:.2f} tp={pos.tp_price:.2f}")
        else:
            pos_str = "flat"

        return PaperReport(
            symbol=self.strategy.symbol,
            timeframe=self.strategy.timeframe,
            strategy=self.strategy.name,
            start_date=datetime.utcfromtimestamp(self._bars[0]["time"]).strftime("%Y-%m-%d"),
            end_date=datetime.utcfromtimestamp(self._bars[-1]["time"]).strftime("%Y-%m-%d"),
            n_bars=len(self._bars),
            initial_balance=self.engine.initial_balance,
            final_balance=self.engine.balance,
            net_pnl=net_pnl,
            total_return_pct=total_return_pct,
            total_trades=total,
            wins=len(wins),
            losses=len(losses),
            win_rate=win_rate,
            profit_factor=pf,
            avg_win=avg_win,
            avg_loss=avg_loss,
            max_drawdown_pct=max_dd,
            sharpe=sharpe,
            longest_win_streak=longest_win,
            longest_loss_streak=longest_loss,
            final_position=pos_str,
            daily_pnl=daily_pnl,
        )

    def _daily_pnl_series(self, closes: list[PaperTrade]) -> list[float]:
        """按 close 时间聚合成日 PnL"""
        by_day: dict[date, float] = {}
        for t in closes:
            d = datetime.utcfromtimestamp(t.time).date()
            by_day[d] = by_day.get(d, 0.0) + t.pnl
        return [v for _, v in sorted(by_day.items())]

    def print_report(self, r: PaperReport):
        """打印可读报告"""
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
        print(f"  Trades        : {r.total_trades}  "
              f"(W:{r.wins} / L:{r.losses}  WR={r.win_rate:.1f}%)")
        print(f"  Avg Win/Loss  : ${r.avg_win:+.2f} / ${r.avg_loss:+.2f}  "
              f"(PF={r.profit_factor:.2f})")
        print(f"  Max Drawdown  : {r.max_drawdown_pct:.2f}%")
        print(f"  Sharpe (ann.) : {r.sharpe:.3f}")
        print(f"  Streaks       : {r.longest_win_streak}W / {r.longest_loss_streak}L")
        print(f"  Final Pos.    : {r.final_position}")
        print("=" * 72)
