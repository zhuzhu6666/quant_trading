"""scripts/equity_by_regime.py — Equity 曲线按 regime 着色

复用 paper_trader 流程,额外:
  1. 跑 multi_factor_m15 paper 模式 (50K bar)
  2. 每根 bar 末采样 equity + batch 算 4 个 trend/vol 标签
     (跳 macro/VIX/事件查 DB, 因为 50K * sqlite 开销是小时级, 不是这里的目的)
  3. matplotlib 画彩色 equity 曲线 (16:9, 黑色背景, 120 dpi)
  4. 各 regime 段平均 equity 输出

设计:
  TRENDING_UP/DOWN 用 EMA50/200 + ADX
  HIGH_VOL/LOW_VOL 用 ATR 百分位
  RANGING = ADX < 20
  4 标签足够区分主状态, 优先级 TRENDING > VOL > RANGING (单选)
"""
import logging
import math
import sys
import time as _time
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from data.store import DataStore
from strategy.registry import strategy_registry
from execution.paper_trader import PaperTrader

# 复用 trend 工具 — inline from strategies.trend_following
def _vector_ema(values, period):
    n = len(values)
    out = np.full(n, np.nan, dtype=np.float64)
    if n < period:
        return out
    k = 2.0 / (period + 1.0)
    out[period - 1] = float(np.mean(values[:period]))
    for i in range(period, n):
        out[i] = (values[i] - out[i - 1]) * k + out[i - 1]
    return out

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("equity_regime")
logger.setLevel(logging.WARNING)


# ── Regime 标签 (batch 版, 不查 DB) ──


def _wilder_smooth(values: np.ndarray, period: int) -> np.ndarray:
    """Wilder 平滑: values[0]=mean(period), 之后 (prev*(period-1) + cur) / period.
    前 period-1 个位置填 nan."""
    n = len(values)
    out = np.full(n, np.nan)
    if n < period:
        return out
    out[period - 1] = float(values[:period].mean())
    for i in range(period, n):
        out[i] = (out[i - 1] * (period - 1) + values[i]) / period
    return out


def _vector_atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> np.ndarray:
    """ATR(14) Wilder 平滑版. tr[0]=NaN (无前 close), 从 tr[1] 开始."""
    n = len(closes)
    if n < period + 1:
        return np.full(n, np.nan)
    tr = np.full(n, np.nan)
    # tr[1:] = max(high-low, |high-prev_close|, |low-prev_close|)
    tr[1:] = np.maximum.reduce([
        highs[1:] - lows[1:],
        np.abs(highs[1:] - closes[:-1]),
        np.abs(lows[1:] - closes[:-1]),
    ])
    # Wilder 从 tr[1:period+1] 取 mean 作为 out[period], 然后递推
    # 等价于 out[period] = mean(tr[1..period]), 之后 (out[i-1]*(period-1)+tr[i])/period
    out = np.full(n, np.nan)
    out[period] = float(np.nanmean(tr[1:period + 1]))
    for i in range(period + 1, n):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def _vector_adx(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> np.ndarray:
    """ADX(14) Wilder 平滑版. 返回 0-100."""
    n = len(closes)
    if n < 2 * period + 1:
        return np.full(n, np.nan)
    # DM
    up_move = np.zeros(n); up_move[1:] = highs[1:] - highs[:-1]
    down_move = np.zeros(n); down_move[1:] = lows[:-1] - lows[1:]
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    # TR
    tr = np.full(n, np.nan)
    tr[1:] = np.maximum.reduce([
        highs[1:] - lows[1:],
        np.abs(highs[1:] - closes[:-1]),
        np.abs(lows[1:] - closes[:-1]),
    ])
    # Wilder 平滑 TR / +DM / -DM
    def _ws(arr):
        out = np.full(n, np.nan)
        out[period] = float(np.nanmean(arr[1:period + 1]))
        for i in range(period + 1, n):
            out[i] = (out[i - 1] * (period - 1) + arr[i]) / period
        return out
    atr_s = _ws(tr)
    pdm_s = _ws(plus_dm)
    mdm_s = _ws(minus_dm)
    # DI
    plus_di = np.where(atr_s > 0, 100.0 * pdm_s / atr_s, np.nan)
    minus_di = np.where(atr_s > 0, 100.0 * mdm_s / atr_s, np.nan)
    sum_di = plus_di + minus_di
    dx = np.where(sum_di > 0, 100.0 * np.abs(plus_di - minus_di) / sum_di, np.nan)
    # ADX = Wilder 平滑 DX (起始于 DX[period] = mean(DX[1..period+1]))
    adx = np.full(n, np.nan)
    adx[2 * period] = float(np.nanmean(dx[period + 1:2 * period + 1]))
    for i in range(2 * period + 1, n):
        adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / period
    return adx


def compute_regimes(closes: np.ndarray, highs: np.ndarray, lows: np.ndarray,
                    period_ema_fast: int = 50, period_ema_slow: int = 200,
                    period_adx: int = 14, adx_trend: float = 25.0,
                    adx_range: float = 20.0,
                    atr_period: int = 14, atr_pct_window: int = 200) -> np.ndarray:
    """
    返回 50K 长度字符串数组, 每个元素是这根 bar 的主导 regime:
      "TRENDING_UP" / "TRENDING_DOWN" / "HIGH_VOL" / "LOW_VOL" / "RANGING"
    优先级: TRENDING > HIGH_VOL/LOW_VOL > RANGING (单选, 互斥)
    """
    n = len(closes)
    out = np.full(n, "RANGING", dtype=object)

    ema_fast = _vector_ema(closes, period_ema_fast)
    ema_slow = _vector_ema(closes, period_ema_slow)
    atr = _vector_atr(highs, lows, closes, atr_period)
    adx_arr = _vector_adx(highs, lows, closes, period_adx)

    # ATR 百分位: rolling 200 根, 取 rank
    atr_pct = np.full(n, np.nan)
    for i in range(atr_pct_window, n):
        w = atr[i - atr_pct_window + 1 : i + 1]
        valid = w[~np.isnan(w)]
        if valid.size < 50:
            continue
        atr_pct[i] = (np.searchsorted(np.sort(valid), atr[i]) + 1) / (valid.size + 1) * 100.0

    for i in range(n):
        if np.isnan(ema_fast[i]) or np.isnan(ema_slow[i]) or np.isnan(adx_arr[i]):
            continue
        adx = adx_arr[i]
        if adx > adx_trend:
            if ema_fast[i] > ema_slow[i]:
                out[i] = "TRENDING_UP"
                continue
            if ema_fast[i] < ema_slow[i]:
                out[i] = "TRENDING_DOWN"
                continue
        if not np.isnan(atr_pct[i]):
            if atr_pct[i] >= 80:
                out[i] = "HIGH_VOL"
                continue
            if atr_pct[i] <= 30:
                out[i] = "LOW_VOL"
                continue
        if adx < adx_range:
            out[i] = "RANGING"
    return out


# ── 主流程 ──


def main():
    print("=" * 72)
    print("  Equity by Regime — MultiFactorM15 M15, 50K bar")
    print("=" * 72)

    # 1. 跑 paper 流程 (复用 main.py run_paper 的配置)
    strategy = strategy_registry.create(
        "multi_factor_m15", symbol="XAUUSD+", timeframe="M15",
        sl_atr=3.0, tp_atr=4.0, cooldown_bars=3,
        enable_nfp_skip=True, nfp_skip_days=1,
        enable_dual_event_skip=True,
        enable_gvz_gate=True, gvz_drop_pct=-2.0,
    )
    store = DataStore("data/ctrader_data.duckdb")
    trader = PaperTrader(
        strategy=strategy, initial_balance=500.0, default_lots=0.01,
        max_lots=2.0, warmup_bars=500, enable_circuit=False,
    )
    trader.load_data(store, "XAUUSD+", "M15")

    t0 = _time.time()
    report = trader.run()
    print(f"Paper 跑完: {report.net_pnl:+.2f} ({report.total_return_pct:+.2f}%) "
          f"WR={report.win_rate:.1f}% DD={report.max_drawdown_pct:.2f}% "
          f"PF={report.profit_factor:.2f}  [{_time.time()-t0:.1f}s]")

    # 2. 取 OHLC + equity 序列
    bars = trader._bars
    closes = np.array([b["close"] for b in bars])
    highs = np.array([b["high"] for b in bars])
    lows = np.array([b["low"] for b in bars])
    times = np.array([b["time"] for b in bars])
    equity = np.array([e for _, e in trader._equity_curve])

    # 3. 算 regime
    print("\n算 regime 标签 (batch, 跳 DB)...")
    t0 = _time.time()
    regimes = compute_regimes(closes, highs, lows)
    print(f"  完成 [{_time.time()-t0:.1f}s]")

    # 4. 各 regime 段 equity 平均
    print("\n各 regime 段统计:")
    print(f"  {'Regime':<16s}  {'Count':>7s}  {'%':>6s}  {'AvgEq':>10s}  {'MaxEq':>10s}  {'MinEq':>10s}")
    print("  " + "-" * 70)
    for regime in ["TRENDING_UP", "TRENDING_DOWN", "HIGH_VOL", "LOW_VOL", "RANGING"]:
        mask = regimes == regime
        n = int(mask.sum())
        if n == 0:
            continue
        avg_e = equity[mask].mean()
        max_e = equity[mask].max()
        min_e = equity[mask].min()
        pct = n / len(regimes) * 100
        print(f"  {regime:<16s}  {n:>7d}  {pct:>5.1f}%  ${avg_e:>9.2f}  ${max_e:>9.2f}  ${min_e:>9.2f}")

    # 5. 画图
    print("\n画图...")
    regime_colors = {
        "TRENDING_UP":   "#22c55e",   # 绿
        "TRENDING_DOWN": "#ef4444",   # 红
        "HIGH_VOL":      "#f97316",   # 橙
        "LOW_VOL":       "#3b82f6",   # 蓝
        "RANGING":       "#6b7280",   # 灰
    }
    # 转成数值 cmap
    regime_to_idx = {r: i for i, r in enumerate(regime_colors.keys())}
    regime_idx = np.array([regime_to_idx.get(r, 4) for r in regimes])

    fig, ax = plt.subplots(figsize=(16, 9), facecolor="#0a0a0a")
    ax.set_facecolor("#0a0a0a")

    # 把连续 regime 段画成彩色粗线 (scatter 太密, 改用 axvspan)
    # 找连续段
    boundaries = np.where(np.diff(regime_idx) != 0)[0]
    starts = np.concatenate(([0], boundaries + 1))
    ends = np.concatenate((boundaries, [len(regimes) - 1]))

    t_dt = np.array([datetime.fromtimestamp(t, tz=timezone.utc) for t in times])

    # 主 equity 曲线 (暗色)
    ax.plot(t_dt, equity, color="#ffffff", linewidth=0.6, alpha=0.35, zorder=1)

    # 按段上色
    for s, e in zip(starts, ends):
        reg = regimes[s]
        if e == s:
            continue
        color = regime_colors.get(reg, "#6b7280")
        ax.plot(t_dt[s:e+1], equity[s:e+1], color=color, linewidth=1.4, alpha=0.95, zorder=2)

    # 图例
    from matplotlib.patches import Patch
    handles = [Patch(facecolor=c, label=r) for r, c in regime_colors.items()]
    ax.legend(handles=handles, loc="upper left", facecolor="#1a1a1a",
              edgecolor="#404040", labelcolor="white", fontsize=10)

    ax.set_title(f"Equity Curve by Regime — MultiFactorM15 M15 (2024-04 → 2026-05, "
                 f"{report.total_return_pct:+.1f}%, DD {report.max_drawdown_pct:.1f}%)",
                 color="white", fontsize=14, pad=15)
    ax.set_xlabel("Time (UTC)", color="white", fontsize=11)
    ax.set_ylabel("Equity (USD)", color="white", fontsize=11)
    ax.tick_params(colors="white", labelsize=9)
    for spine in ax.spines.values():
        spine.set_color("#404040")
    ax.grid(True, color="#1f1f1f", linewidth=0.5, alpha=0.6)
    ax.axhline(report.initial_balance, color="#fbbf24", linestyle="--",
               linewidth=0.8, alpha=0.5, label=f"Initial ${report.initial_balance:.0f}")

    fig.tight_layout()
    out_path = Path("data/charts/equity_by_regime.png")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=120, facecolor=fig.get_facecolor())
    plt.close(fig)
    size_kb = out_path.stat().st_size // 1024
    print(f"  → {out_path} ({size_kb} KB)")

    print("\n" + "=" * 72)
    print(f"  PNG 路径: {out_path.absolute()}")
    print("=" * 72)


if __name__ == "__main__":
    main()
