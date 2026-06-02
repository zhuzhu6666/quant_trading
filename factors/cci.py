"""
Commodity Channel Index (CCI)
=============================

Donald Lambert 1980 年提出, 用于衡量价格偏离其统计平均的程度.

公式:
    TP     = (high + low + close) / 3
    CCI    = (TP - SMA(TP, period)) / (0.015 * MAD(TP, period))
    MAD    = mean(|TP - SMA(TP, period)|)

常用阈值: +100 / -100. CCI > +100 表示超买 (强趋势), CCI < -100 表示超卖.

本模块输出最近一根 bar 的 CCI 值. 长度不足返回 nan.
"""

from __future__ import annotations

import numpy as np


def compute_cci(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    period: int = 20,
) -> float:
    """
    计算 CCI 指标 (最近一根 bar 的值).

    Args:
        highs: 高价序列.
        lows: 低价序列.
        closes: 收盘价序列.
        period: 回看窗口. 默认 20.

    Returns:
        最近一根 bar 的 CCI 值 (float). 长度不足或常数序列时返回 nan.
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
    c = closes[-period:]

    tp = (h + l + c) / 3.0
    sma_tp = tp.mean()
    mad = np.mean(np.abs(tp - sma_tp))

    # 防止除零 (价格常数序列)
    if mad < 1e-12:
        return float("nan")

    cci = (tp[-1] - sma_tp) / (0.015 * mad)
    return float(cci)
