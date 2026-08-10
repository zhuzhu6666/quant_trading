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


def rsi_wilder(close: np.ndarray, period: int = 14) -> np.ndarray:
    """Return the classic RSI using Wilder's recursive smoothing.

    The first gain/loss average is seeded from the first ``period`` bars,
    matching :func:`wilder_smooth` and the other shared Wilder indicators.
    Flat windows are explicitly mapped to 50, while one-sided windows map to
    the corresponding 0/100 boundary instead of an artificial near-boundary
    value.
    """
    close = np.asarray(close, dtype=float)
    if len(close) == 0:
        return np.asarray([], dtype=float)

    delta = np.diff(close, prepend=close[0])
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_gain = wilder_smooth(gain, period)
    avg_loss = wilder_smooth(loss, period)

    result = np.full(len(close), np.nan, dtype=float)
    valid = np.isfinite(avg_gain) & np.isfinite(avg_loss)
    flat = valid & (avg_gain == 0.0) & (avg_loss == 0.0)
    rising_only = valid & (avg_gain > 0.0) & (avg_loss == 0.0)
    falling_only = valid & (avg_gain == 0.0) & (avg_loss > 0.0)
    mixed = valid & ~(flat | rising_only | falling_only)

    result[flat] = 50.0
    result[rising_only] = 100.0
    result[falling_only] = 0.0
    result[mixed] = 100.0 - 100.0 / (1.0 + avg_gain[mixed] / avg_loss[mixed])
    return result


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
