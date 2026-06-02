"""
Williams %R
===========

Larry Williams 提出的动量指标, 衡量收盘价在最近 N 根 bar 最高/最低区间中的位置.

公式:
    %R = (highest_high - close) / (highest_high - lowest_low) * -100

等价形式: %R = (close - lowest_low) / (highest_high - lowest_low) * -100 + 0
     即:  (close - lowest_low) / (highest_high - lowest_low) * -100

取值范围: [-100, 0]
    %R > -20 → 超买
    %R < -80 → 超卖

本模块输出最近一根 bar 的 Williams %R 值. 长度不足返回 nan.
"""

from __future__ import annotations

import numpy as np


def compute_williams_r(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    period: int = 14,
) -> float:
    """
    计算 Williams %R 指标 (最近一根 bar 的值).

    Args:
        highs: 高价序列.
        lows: 低价序列.
        closes: 收盘价序列.
        period: 回看窗口. 默认 14.

    Returns:
        最近一根 bar 的 Williams %R 值 (float, 范围 [-100, 0]).
        长度不足或 highest == lowest 时返回 nan.
    """
    highs = np.asarray(highs, dtype=np.float64)
    lows = np.asarray(lows, dtype=np.float64)
    closes = np.asarray(closes, dtype=np.float64)

    if period <= 0:
        raise ValueError(f"period must be positive, got {period}")

    n = min(len(highs), len(lows), len(closes))
    if n < period:
        return float("nan")

    h = highs[-period:]
    l = lows[-period:]
    close = closes[-1]

    highest = h.max()
    lowest = l.min()

    rng = highest - lowest
    if rng < 1e-12:
        return float("nan")

    wr = (highest - close) / rng * -100.0
    return float(wr)
