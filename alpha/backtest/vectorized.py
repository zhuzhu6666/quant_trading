"""alpha/backtest/vectorized.py — 向量化回测引擎 (Phase 1)。

用历史 bar 重放 Factor Takeover v4 管道，一次性预计算所有因子值，
逐 bar 走 SignalNormalizer → PortfolioCompositor → ExecutionGate → 模拟交易。

输出完整的 equity_curve + 交易明细 + 统计指标。

设计文档: docs/UPGRADE_BLUEPRINT.md §1.1
"""
from __future__ import annotations

import logging
import time as _time
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════
# 交易成本 (XAUUSD+, 0.01 volume unit)
# ═══════════════════════════════════════════════════════════
SPREAD_COST = 0.12       # 双边点差 $0.12
COMMISSION_PER_LOT = 6.0  # 双边佣金 $6/标准手
LOT_SIZE = 0.01           # 默认手数
CONTRACT_SIZE = 100       # XAUUSD 100 oz/标准手
SWAP_PER_LOT_DAY = -15.0  # 过夜利息 $15/手/天
INITIAL_CAPITAL = 10000.0


@dataclass
class BacktestResult:
    """一次回测的完整结果。"""

    # 权益曲线
    equity_curve: np.ndarray = field(default_factory=lambda: np.array([]))

    # 交易列表
    trades: list[dict] = field(default_factory=list)

    # 核心指标
    total_return: float = 0.0           # 总收益率 (期末/期初 - 1)
    total_pnl: float = 0.0              # 总盈亏 (USD)
    sharpe_ratio: float = 0.0           # 年化夏普
    calmar_ratio: float = 0.0           # 年化收益 / 最大回撤
    max_drawdown: float = 0.0           # 最大回撤 (%)
    sortino_ratio: float = 0.0          # 索提诺比率 (只用下行波动率)

    # 交易统计
    n_trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0          # 总盈利 / 总亏损
    avg_hold_bars: float = 0.0          # 平均持仓 bar 数
    sl_exits: int = 0
    tp_exits: int = 0

    # 高级指标
    monthly_returns: dict[str, float] = field(default_factory=dict)
    rolling_sharpe_24m: np.ndarray = field(default_factory=lambda: np.array([]))
    up_capture: float = 0.0
    down_capture: float = 0.0
    total_costs: float = 0.0
    total_swap: float = 0.0

    # 原始数据
    n_bars: int = 0
    timeframe: str = "M5"

    def to_dict(self) -> dict:
        """返回 JSON 兼容的字典。"""
        return {
            "total_return": round(self.total_return, 6),
            "total_pnl": round(self.total_pnl, 2),
            "sharpe_ratio": round(self.sharpe_ratio, 4),
            "calmar_ratio": round(self.calmar_ratio, 4),
            "max_drawdown": round(self.max_drawdown, 2),
            "sortino_ratio": round(self.sortino_ratio, 4),
            "n_trades": self.n_trades,
            "win_rate": round(self.win_rate, 4),
            "profit_factor": round(self.profit_factor, 4),
            "avg_hold_bars": round(self.avg_hold_bars, 1),
            "sl_exits": self.sl_exits,
            "tp_exits": self.tp_exits,
            "up_capture": round(self.up_capture, 4),
            "down_capture": round(self.down_capture, 4),
            "total_costs": round(self.total_costs, 2),
            "total_swap": round(self.total_swap, 2),
            "monthly_returns": {k: round(v, 4) for k, v in self.monthly_returns.items()},
            "n_bars": self.n_bars,
            "timeframe": self.timeframe,
        }


class FactorBacktester:
    """向量化回测引擎。

    用历史 DataFrame 重放 Factor Takeover v4 完整管道。

    Args:
        df: 历史 K 线 DataFrame (columns: time, open, high, low, close, volume)
        config: RuntimeConfig 实例或 None (使用默认)
        lot_size: 固定 volume unit (默认 0.01)
        initial_capital: 初始资金 (默认 $10,000)
        slippage_bps: 滑点 bps (默认 2 = 0.02%)
        commission_per_volume: 双边佣金 $/标准 volume unit (默认 $6)
        timeframe: K 线周期 (默认 "M5")
    """

    # 年化因子: bar 数 → sqrt(年化)
    _BPV: dict[str, int] = {
        "M1": 525600, "M5": 105120, "M15": 35040,
        "M30": 17520, "H1": 8760, "H4": 2190, "D1": 365,
    }

    def __init__(
        self,
        df: pd.DataFrame,
        config: Any = None,
        lot_size: float = 0.01,
        initial_capital: float = 10000.0,
        slippage_bps: float = 2.0,
        commission_per_volume: float = 6.0,
        timeframe: str = "M5",
    ):
        self.df = df
        self.config = config
        self.lot_size = lot_size
        self.initial_capital = initial_capital
        self.slippage_bps = slippage_bps
        self.commission_per_volume = commission_per_volume
        self.timeframe = timeframe

        # 延迟导入避免循环依赖
        self._normalizer = None
        self._compositor = None
        self._gate = None
        self._factor_values: dict[str, np.ndarray] = {}

    # ── 成本计算 ──────────────────────────────────────────

    @staticmethod
    def _calc_pnl(entry: float, exit_price: float, direction: int) -> float:
        """计算未扣除成本的毛盈亏。"""
        return (exit_price - entry) * direction * LOT_SIZE * CONTRACT_SIZE

    def _calc_trade_cost(self) -> float:
        """单笔双边成本 (点差 + 佣金)。"""
        return SPREAD_COST + self.commission_per_volume * self.lot_size

    @staticmethod
    def _calc_swap(entry_ts: float, exit_ts: float) -> float:
        """过夜利息。"""
        hours = (exit_ts - entry_ts) / 3600.0
        return SWAP_PER_LOT_DAY / 24.0 * hours * LOT_SIZE

    # ── 因子预计算 ────────────────────────────────────────

    def _precompute_factors(self) -> dict[str, np.ndarray]:
        """向量化一次性计算所有因子值。"""
        from alpha.registry import factor_registry

        factor_vals: dict[str, np.ndarray] = {}
        names = factor_registry.list()
        t0 = _time.time()
        for name in names:
            try:
                fn = factor_registry.get(name)
                if fn is None:
                    continue
                vals = fn(self.df)
                arr = np.asarray(vals, dtype=float)
                arr[np.isinf(arr)] = np.nan
                factor_vals[name] = arr
            except Exception as e:
                logger.debug("factor %s failed: %s", name, e)
        elapsed = _time.time() - t0
        logger.info("precomputed %d factors in %.1fs", len(factor_vals), elapsed)
        return factor_vals

    # ── 管道初始化 ────────────────────────────────────────

    def _init_pipeline(self):
        """初始化 SignalNormalizer / PortfolioCompositor / ExecutionGate。"""
        from alpha.signal_normalizer import SignalNormalizer
        from alpha.portfolio_compositor import PortfolioCompositor
        from alpha.execution_gate import ExecutionGate

        if self.config is not None:
            cfg = self.config
            signal_cfg = getattr(cfg, "factor_signal_config", {})
            weight_cfg = getattr(cfg, "factor_portfolio_weights", {})
            tactical_alpha = getattr(cfg, "factor_tactical_alpha", 0.7)
            signal_threshold = getattr(cfg, "factor_signal_threshold", 0.4)
        else:
            signal_cfg = {}
            weight_cfg = {}
            tactical_alpha = 0.7
            signal_threshold = 0.4

        # 合并 portfolio config
        all_names = set(signal_cfg) | set(weight_cfg)
        compositor_cfg: dict[str, dict] = {}
        for name in all_names:
            sc = signal_cfg.get(name, {})
            wc = weight_cfg.get(name, 1.0)
            weight = wc if isinstance(wc, (int, float)) else wc.get("weight", 1.0)
            compositor_cfg[name] = {
                "weight": weight,
                "tags": sc.get("tags", []),
                "mode": sc.get("mode", "rank_mapping"),
                "enabled": sc.get("enabled", True),
            }
        compositor_cfg["_tactical_alpha"] = tactical_alpha
        compositor_cfg["_signal_threshold"] = signal_threshold

        self._normalizer = SignalNormalizer(signal_cfg)
        self._compositor = PortfolioCompositor(compositor_cfg)
        self._gate = ExecutionGate({
            "signal_threshold": signal_threshold,
            "cooldown_bars": 3,  # 回测用较短冷却期
        })

    # ── 主回测循环 ────────────────────────────────────────

    def run(self, warmup_bars: int = 50) -> BacktestResult:
        """运行回测。"""
        # 预计算
        self._factor_values = self._precompute_factors()
        factor_names = list(self._factor_values.keys())

        # 初始化管道
        self._init_pipeline()

        n_bars = len(self.df)
        trades: list[dict] = []
        position: Optional[dict] = None
        balance = self.initial_capital
        equity_curve = [balance]

        for i in range(n_bars):
            row = self.df.iloc[i]
            bar = {
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
                "time": float(row["time"]),
                "timeframe": self.timeframe,
                "complete": True,
            }
            ts = bar["time"]

            # ── 检查持仓 SL/TP ──
            if position is not None:
                hit = False
                hi, lo = bar["high"], bar["low"]
                if position["direction"] == 1:
                    if lo <= position["sl"]:
                        position["exit_price"] = position["sl"]
                        hit = True
                        position["reason"] = "SL"
                    elif hi >= position["tp"]:
                        position["exit_price"] = position["tp"]
                        hit = True
                        position["reason"] = "TP"
                else:
                    if hi >= position["sl"]:
                        position["exit_price"] = position["sl"]
                        hit = True
                        position["reason"] = "SL"
                    elif lo <= position["tp"]:
                        position["exit_price"] = position["tp"]
                        hit = True
                        position["reason"] = "TP"

                if hit:
                    position["exit_time"] = ts
                    position["bars_held"] = position.get("_bars_held", 0)
                    pnl = (
                        self._calc_pnl(
                            position["entry_price"],
                            position["exit_price"],
                            position["direction"],
                        )
                        - self._calc_trade_cost()
                        + self._calc_swap(position["entry_time"], ts)
                    )
                    position["pnl"] = round(pnl, 2)
                    trades.append(position)
                    balance += pnl
                    equity_curve.append(balance)
                    position = None

            # ── 预热: buffer 不足时不交易 ──
            if i < warmup_bars:
                fv = {
                    name: (
                        float(vals[i])
                        if i < len(vals) and not (np.isnan(vals[i]) or np.isinf(vals[i]))
                        else None
                    )
                    for name, vals in self._factor_values.items()
                }
                self._normalizer.normalize(fv)
                self._gate.tick()
                equity_curve.append(balance)
                continue

            # ── 因子信号 ──
            fv = {
                name: (
                    float(vals[i])
                    if i < len(vals) and not (np.isnan(vals[i]) or np.isinf(vals[i]))
                    else None
                )
                for name, vals in self._factor_values.items()
            }
            signals = self._normalizer.normalize(fv)
            composite = self._compositor.compose(signals, fv)
            gate_result = self._gate.filter(composite, fv, bar)
            self._gate.tick()

            # ── 开仓 ──
            if position is None and gate_result.passed and composite.direction != 0:
                atr_val = fv.get("atr_ratio", 0)
                atr_price = (
                    atr_val * bar["close"]
                    if atr_val and atr_val > 0
                    else bar["close"] * 0.001
                )
                score = abs(composite.score)
                sl_mult = 1.5 + score * 2.5
                tp_mult = 2.0 + score * 4.0
                n_aligned = sum(
                    1
                    for s in signals.values()
                    if s and abs(s) > 0.2 and s * composite.direction > 0
                )
                sl_mult += n_aligned * 0.05
                tp_mult += n_aligned * 0.1
                sl_dist = atr_price * sl_mult
                tp_dist = atr_price * tp_mult
                entry = bar["close"]

                position = {
                    "entry_time": ts,
                    "entry_price": entry,
                    "direction": composite.direction,
                    "sl": entry - sl_dist if composite.direction == 1 else entry + sl_dist,
                    "tp": entry + tp_dist if composite.direction == 1 else entry - tp_dist,
                    "exit_time": 0.0,
                    "exit_price": 0.0,
                    "pnl": 0.0,
                    "reason": "",
                    "_score": composite.score,
                    "_bars_held": 0,
                }

            # ── 动态放宽止损 (前 3 bar) ──
            if position is not None:
                position["_bars_held"] = position.get("_bars_held", 0) + 1
                if position["_bars_held"] <= 3:
                    atr_val = fv.get("atr_ratio", 0)
                    atr_price = (
                        atr_val * bar["close"]
                        if atr_val and atr_val > 0
                        else bar["close"] * 0.001
                    )
                    wide_sl = atr_price * 4.0
                    if position["direction"] == 1:
                        candidate = bar["close"] - wide_sl
                        if candidate < position["sl"]:
                            position["sl"] = candidate
                    else:
                        candidate = bar["close"] + wide_sl
                        if candidate > position["sl"]:
                            position["sl"] = candidate

            equity_curve.append(balance)

        # ── 平最后持仓 ──
        if position is not None:
            last_close = float(self.df.iloc[-1]["close"])
            position["exit_price"] = last_close
            position["exit_time"] = float(self.df.iloc[-1]["time"])
            position["bars_held"] = position.get("_bars_held", 0)
            pnl = (
                self._calc_pnl(
                    position["entry_price"], last_close, position["direction"]
                )
                - self._calc_trade_cost()
                + self._calc_swap(position["entry_time"], position["exit_time"])
            )
            position["pnl"] = round(pnl, 2)
            position["reason"] = "END"
            trades.append(position)
            balance += pnl
            equity_curve.append(balance)

        # ── 构建结果 ──
        eq_arr = np.array(equity_curve)
        result = BacktestResult(
            equity_curve=eq_arr,
            trades=trades,
            n_bars=n_bars,
            timeframe=self.timeframe,
        )
        self._compute_metrics(result)
        return result

    # ── 指标计算 ──────────────────────────────────────────

    def _compute_metrics(self, result: BacktestResult):
        """计算所有统计指标。"""
        eq = result.equity_curve
        trades = result.trades
        n_trades = len(trades)
        result.n_trades = n_trades
        n_bars = result.n_bars
        bpv = self._BPV.get(self.timeframe, 35040)

        # 总收益
        result.total_pnl = eq[-1] - eq[0]
        result.total_return = eq[-1] / eq[0] - 1.0

        if n_trades == 0:
            return

        # Sharpe
        ret = np.diff(eq) / eq[:-1]
        if np.std(ret) > 0:
            result.sharpe_ratio = float(np.mean(ret) / np.std(ret) * np.sqrt(bpv))
        else:
            result.sharpe_ratio = 0.0

        # Max DD
        peak = np.maximum.accumulate(eq)
        dd = (eq - peak) / peak
        result.max_drawdown = float(np.min(dd) * 100)

        # Calmar
        if abs(result.max_drawdown) > 1e-6:
            result.calmar_ratio = (
                result.total_return * bpv / n_bars / abs(result.max_drawdown / 100)
            )
        else:
            result.calmar_ratio = 0.0

        # Sortino (只用下行波动率)
        ret_arr = np.diff(eq) / eq[:-1]
        down_ret = ret_arr[ret_arr < 0]
        if len(down_ret) > 0 and np.std(down_ret) > 0:
            result.sortino_ratio = float(
                np.mean(ret_arr) / np.std(down_ret) * np.sqrt(bpv)
            )
        else:
            result.sortino_ratio = 0.0

        # 胜率
        n_wins = sum(1 for t in trades if t["pnl"] > 0)
        result.win_rate = n_wins / n_trades if n_trades else 0.0

        # 盈亏比
        gross_profit = sum(t["pnl"] for t in trades if t["pnl"] > 0)
        gross_loss = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
        result.profit_factor = (
            gross_profit / gross_loss if gross_loss > 0 else float("inf")
        )

        # 平均持仓 bar 数
        bars_held = [t.get("bars_held", 0) for t in trades]
        result.avg_hold_bars = float(np.mean(bars_held)) if bars_held else 0.0

        # SL/TP 计数
        result.sl_exits = sum(1 for t in trades if t.get("reason") == "SL")
        result.tp_exits = sum(1 for t in trades if t.get("reason") == "TP")

        # 总成本
        result.total_costs = sum(self._calc_trade_cost() for _ in trades)
        result.total_swap = sum(
            self._calc_swap(t["entry_time"], t["exit_time"])
            for t in trades
            if t.get("exit_time", 0) > 0
        )

        # ── 月度收益 ──
        if len(eq) > 1:
            # 确保 equity_curve 与 bar 数对齐: 首个值是初始资金, 后续每 bar 一个点
            # eq[0] = initial, eq[1:] = per-bar equity
            eq_aligned = eq[: n_bars + 1] if len(eq) > n_bars else eq
            df_times = pd.to_datetime(self.df["time"].iloc[: len(eq_aligned) - 1], unit="s")
            monthly_eq = pd.Series(eq_aligned[1:], index=df_times).resample("ME").last()
            if len(monthly_eq) > 1:
                monthly_ret = monthly_eq.pct_change().dropna()
                result.monthly_returns = {
                    str(d.date()): round(float(v), 4)
                    for d, v in monthly_ret.items()
                }

        # ── 捕获率 ──
        if len(ret_arr) > 0:
            # 对齐 bar 收益和 forward returns
            # 用 close-to-close 收益率
            closes = self.df["close"].values.astype(float)
            bar_rets = np.diff(closes) / closes[:-1]
            # 确保长度对齐
            n = min(len(bar_rets), len(ret_arr))
            bar_rets = bar_rets[-n:]
            strat_rets = ret_arr[-n:]

            up_mask = bar_rets > 0
            down_mask = bar_rets < 0
            if np.sum(up_mask) > 0:
                result.up_capture = float(
                    np.mean(strat_rets[up_mask]) / np.mean(bar_rets[up_mask])
                )
            if np.sum(down_mask) > 0:
                result.down_capture = float(
                    np.mean(strat_rets[down_mask]) / np.mean(bar_rets[down_mask])
                )

    # ── 便利方法 ──────────────────────────────────────────

    @staticmethod
    def from_sqlite(
        db_path: str = "data/ctrader_data.duckdb",
        symbol: str = "XAUUSD+",
        timeframe: str = "M5",
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
        **kwargs,
    ) -> "FactorBacktester":
        """从 SQLite 加载数据并构造回测器。"""
        import sqlite3

        conn = sqlite3.connect(db_path)
        params = [symbol, timeframe]
        where = "symbol=? AND timeframe=?"
        if start_ts is not None:
            where += " AND time >= ?"
            params.append(start_ts)
        if end_ts is not None:
            where += " AND time < ?"
            params.append(end_ts)

        df = pd.read_sql_query(
            f"SELECT time, open, high, low, close, volume FROM bars "
            f"WHERE {where} ORDER BY time ASC",
            conn,
            params=params,
            parse_dates=False,
        )
        conn.close()
        if len(df) == 0:
            raise ValueError(f"No bars found for {symbol} {timeframe}")

        df.index = pd.to_datetime(df["time"], unit="s")
        df.index.name = "time"
        return FactorBacktester(df, timeframe=timeframe, **kwargs)
