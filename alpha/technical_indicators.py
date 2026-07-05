"""Shared technical indicator helpers used by alpha and risk modules."""

from __future__ import annotations

import numpy as np


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    prev_close = np.empty_like(close, dtype=float)
    if len(close) == 0:
        return np.asarray([], dtype=float)
    prev_close[0] = np.nan
    prev_close[1:] = close[:-1]
    tr1 = high - low
    tr2 = np.abs(high - prev_close)
    tr3 = np.abs(low - prev_close)
    return np.nanmax(np.stack([tr1, tr2, tr3], axis=0), axis=0)


def wilder_smooth(series: np.ndarray, period: int) -> np.ndarray:
    values = np.asarray(series, dtype=float)
    out = np.full(len(values), np.nan, dtype=float)
    if period <= 0 or len(values) < period:
        return out
    out[period - 1] = np.nanmean(values[:period])
    for i in range(period, len(values)):
        out[i] = out[i - 1] + (values[i] - out[i - 1]) / period
    return out


def atr_wilder(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    period: int = 14,
) -> np.ndarray:
    return wilder_smooth(true_range(high, low, close), period)


def adx_wilder(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    period: int = 14,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    n = len(close)
    if n < 2 * period:
        nan = np.full(n, np.nan, dtype=float)
        return nan, nan, nan

    up_move = np.diff(high, prepend=high[0])
    down_move = -np.diff(low, prepend=low[0])
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    atr = atr_wilder(high, low, close, period)
    smooth_plus = wilder_smooth(plus_dm, period)
    smooth_minus = wilder_smooth(minus_dm, period)

    safe_atr = np.where(atr == 0, np.nan, atr)
    plus_di = 100.0 * smooth_plus / safe_atr
    minus_di = 100.0 * smooth_minus / safe_atr

    dx_num = np.abs(plus_di - minus_di)
    dx_den = plus_di + minus_di
    dx = np.where(dx_den == 0, 0.0, 100.0 * dx_num / np.where(dx_den == 0, np.nan, dx_den))
    adx = wilder_smooth(dx, period)
    return adx, plus_di, minus_di
