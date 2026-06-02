"""
Money Flow Index (MFI)
======================

Gene Quong 和 Avrum Soudack 1994 年提出, 又称成交量加权 RSI.
与 RSI 的区别: MFI 在计算涨跌时同时考虑成交量, 范围 [0, 100].

公式:
    Typical Price (TP) = (high + low + close) / 3
    Raw Money Flow     = TP * volume
    Money Flow Positive = 当 TP > TP_prev, 取 RMF
    Money Flow Negative = 当 TP < TP_prev, 取 RMF
    Money Flow Ratio    = sum(MF+) / sum(MF-)   (period 窗口)
    MFI                 = 100 - 100 / (1 + MFR)

解读:
    MFI > 80  → 超买 (资金大量流入)
    MFI < 20  → 超卖 (资金大量流出)

本模块输出最近一根 bar 的 MFI 值. 若 volume 全为 0 或缺失, 返回 50.0 (中性).
"""

from __future__ import annotations

import numpy as np


def compute_mfi(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    volumes: np.ndarray,
    period: int = 14,
) -> float:
    """
    计算 MFI 指标 (最近一根 bar 的值).

    Args:
        highs: 高价序列.
        lows: 低价序列.
        closes: 收盘价序列.
        volumes: 成交量序列. 若全为 0 或长度不足, 返回 50.0 中性值.
        period: 回看窗口. 默认 14.

    Returns:
        最近一根 bar 的 MFI 值 (float, 范围 [0, 100]).
        长度不足时返回 nan; 无成交量时返回 50.0 中性值.
    """
    highs = np.asarray(highs, dtype=np.float64)
    lows = np.asarray(lows, dtype=np.float64)
    closes = np.asarray(closes, dtype=np.float64)

    # volumes 允许为 None / 空, 此时直接返回中性值
    if volumes is None:
        return 50.0
    volumes = np.asarray(volumes, dtype=np.float64)
    if volumes.size == 0:
        return 50.0

    if period <= 0:
        raise ValueError(f"period must be positive, got {period}")

    n = min(len(highs), len(lows), len(closes), len(volumes))
    if n < period + 1:
        # MFI 需要 period 根变化 → period+1 个 TP
        return float("nan")

    h = highs[-(period + 1):]
    l = lows[-(period + 1):]
    c = closes[-(period + 1):]
    v = volumes[-(period + 1):]

    # 成交量全 0 视作"无成交量", 返回中性
    if np.all(v <= 0):
        return 50.0

    tp = (h + l + c) / 3.0
    rmf = tp * v

    tp_prev = tp[:-1]
    tp_now = tp[1:]

    positive_mask = tp_now > tp_prev
    negative_mask = tp_now < tp_prev

    mf_pos = rmf[1:][positive_mask].sum()
    mf_neg = rmf[1:][negative_mask].sum()

    if mf_neg < 1e-12:
        # 全是正向资金流 → MFI → 100
        return 100.0
    mfr = mf_pos / mf_neg
    mfi = 100.0 - 100.0 / (1.0 + mfr)
    return float(mfi)
