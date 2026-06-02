"""
Aroon Indicator
===============

Aroon 由 Tushar Chande 于 1995 年提出, 用于识别趋势的开始和强度.

定义:
    Aroon Up   = (period - bars_since_highest_high) / period * 100
    Aroon Down = (period - bars_since_lowest_low)  / period * 100

其中 bars_since_highest_high 是从当前 bar 往回看, 出现 period 区间内
最高 high 的 bar 距今多少根; bars_since_lowest_low 类似.

取值范围: [0, 100]
    - Aroon Up 接近 100 → 新高刚刚出现, 上升趋势
    - Aroon Down 接近 100 → 新低刚刚出现, 下降趋势

本模块只计算最近一根 bar 的 Aroon Up 值 (与项目因子接口一致, 输出 float).
若输入长度不足以覆盖 period, 返回 nan.
"""

from __future__ import annotations

import numpy as np


def compute_aroon(
    highs: np.ndarray,
    lows: np.ndarray,
    period: int = 14,
) -> float:
    """
    计算 Aroon 指标 (最近一根 bar 的值).

    本实现取 Aroon Up, 范围 [0, 100], 数值越大代表新高越近期.

    Args:
        highs: 高价序列, 长度至少 period.
        lows: 低价序列, 长度至少 period.
        period: 回看窗口. 默认 14.

    Returns:
        最近一根 bar 的 Aroon Up 值 (float). 长度不足时返回 nan.

    Raises:
        TypeError: 输入不是 numpy 数组.
    """
    highs = np.asarray(highs, dtype=np.float64)
    lows = np.asarray(lows, dtype=np.float64)

    if period <= 0:
        raise ValueError(f"period must be positive, got {period}")

    n = min(len(highs), len(lows))
    if n < period:
        return float("nan")

    # 取最近 period 根 bar (含当前 bar)
    h_window = highs[-period:]
    l_window = lows[-period:]

    # 在窗口内找最高 high / 最低 low 的位置 (0 = 最早, period-1 = 最新)
    high_idx = int(np.argmax(h_window))
    low_idx = int(np.argmin(l_window))

    # 距今多少根: 位置越靠后, 距今越近
    bars_since_high = (period - 1) - high_idx
    bars_since_low = (period - 1) - low_idx

    aroon_up = (period - bars_since_high) / period * 100.0
    # aroon_down = (period - bars_since_low) / period * 100.0
    return float(aroon_up)
