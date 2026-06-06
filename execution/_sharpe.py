"""
execution/_sharpe.py - Sharpe ratio 计算 (OPT-2 audit 2026-06-06)

集中放 log returns + Newey-West HAC 标准误, 避免 paper_trader 和 mab_paper_runner
两处复制。

为什么不用 iid 假设的 std:
- Lo (2002) "The Statistics of Sharpe Ratios": iid 假设下 Sharpe 虚高 20-50%
- M15 黄金跨夜 drift, 连续 win/loss, 仓位不调 → equity 序列强自相关
- Newey-West (1994) HAC 用 Bartlett kernel + 自动 lag, 调自相关

Why log returns:
- simple returns 不可加: 1 笔 +50% 然后 -33% 复合 = 0, 算 simple sum = +17%
- log returns 可加: log(1.5) + log(0.67) = 0, 算 sum = 0 ✓
- 长期 Sharpe 比较 (跨 N 期) 必须用 log
"""

from __future__ import annotations

import math

import numpy as np


# ── Timeframe → bars per year ──────────────────────────
# 假设 252 交易日/年, 24h/天 (黄金实际 23h 但 24h 误差可忽略)
TF_BARS_PER_YEAR: dict[str, int] = {
    "M5": 252 * 24 * 12,    # 72576
    "M15": 252 * 24 * 4,    # 24192
    "M30": 252 * 24 * 2,    # 12096
    "H1": 252 * 24,         # 6048
    "H4": 252 * 6,          # 1512
    "D1": 252,              # 252
}


def _newey_west_lag(n: int) -> int:
    """Newey-West (1994) automatic lag selection: floor(4 * (T/100)^(2/9))"""
    if n < 2:
        return 1
    lag = int(math.floor(4.0 * (n / 100.0) ** (2.0 / 9.0)))
    return max(1, min(lag, n - 1))


def sharpe_ratio_log_nw(equity: np.ndarray, timeframe: str) -> float:
    """
    Annualized Sharpe ratio using log returns + Newey-West HAC standard error.

    Parameters
    ----------
    equity : np.ndarray
        Equity curve (length T, all values > 0).
    timeframe : str
        Bar timeframe: M5/M15/M30/H1/H4/D1. 决定 bars_per_year.

    Returns
    -------
    float
        Annualized Sharpe. 0.0 if data insufficient or zero variance.

    Notes
    -----
    - log returns: r_t = log(eq_t / eq_{t-1})
    - Newey-West HAC variance: γ_0 + 2 * Σ_{k=1}^L (1 - k/(L+1)) * γ_k
    - 跟 iid 估计相比, NW 调整会把 std 调高 (强正自相关时), Sharpe 调低
    - 这是真实风险调整后收益, 不是"我连续赢了 5 笔 = Sharpe 5"的错觉
    """
    eq = np.asarray(equity, dtype=float)
    if len(eq) < 2:
        return 0.0
    # ① log returns
    rets = np.log(eq[1:] / eq[:-1])
    rets = rets[np.isfinite(rets)]
    if len(rets) < 2 or rets.std() < 1e-12:
        return 0.0
    bars_per_year = TF_BARS_PER_YEAR.get(timeframe, 252 * 24 * 4)
    # ② Newey-West HAC variance
    n = len(rets)
    lag = _newey_west_lag(n)
    r_mean = rets.mean()
    r_centered = rets - r_mean
    var_nw = float(np.sum(r_centered ** 2)) / n
    for k in range(1, lag + 1):
        gamma_k = float(np.sum(r_centered[:-k] * r_centered[k:])) / n
        weight = 1.0 - k / (lag + 1.0)  # Bartlett kernel
        var_nw += 2.0 * weight * gamma_k
    var_nw = max(var_nw, 1e-24)
    return float(r_mean / math.sqrt(var_nw) * math.sqrt(bars_per_year))
