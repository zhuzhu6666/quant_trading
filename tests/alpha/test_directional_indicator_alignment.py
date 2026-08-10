import numpy as np
import pandas as pd

from alpha.registry import factor_adx, factor_atr_ratio, factor_di_spread, factor_rsi_14
from alpha.streaming_factor_engine import StreamingFactorEngine
from alpha.technical_indicators import rsi_wilder
from risk import regime


def _ohlc_frame(n: int = 80) -> pd.DataFrame:
    idx = np.arange(n, dtype=float)
    close = 100.0 + np.sin(idx / 4.0) * 2.0 + idx * 0.2
    high = close + 0.8 + np.cos(idx / 7.0) * 0.1
    low = close - 0.9 - np.sin(idx / 8.0) * 0.1
    open_ = close - 0.1
    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": np.full(n, 100.0),
        }
    )


def test_alpha_adx_and_di_spread_match_risk_regime_wilder_adx():
    df = _ohlc_frame()
    adx, plus_di, minus_di = regime._adx(
        df["high"].values,
        df["low"].values,
        df["close"].values,
        period=14,
    )

    np.testing.assert_allclose(factor_adx(df), adx, equal_nan=True)
    np.testing.assert_allclose(factor_di_spread(df), plus_di - minus_di, equal_nan=True)


def test_alpha_atr_ratio_matches_risk_regime_wilder_atr():
    df = _ohlc_frame()
    atr = regime._atr(
        df["high"].values,
        df["low"].values,
        df["close"].values,
        period=14,
    )
    expected = atr / df["close"].values

    np.testing.assert_allclose(factor_atr_ratio(df), expected, equal_nan=True)


def test_streaming_adx_override_matches_risk_regime_wilder_adx():
    df = _ohlc_frame()
    expected, _, _ = regime._adx(
        df["high"].values,
        df["low"].values,
        df["close"].values,
        period=7,
    )

    actual = StreamingFactorEngine._factor_adx(df, length=7)
    np.testing.assert_allclose(actual, expected, equal_nan=True)


def test_rsi_uses_shared_wilder_smoothing_and_preserves_warmup():
    close = np.array([100.0, 101.0, 100.0, 102.0, 101.0, 103.0, 102.0, 104.0])
    expected = np.array([
        np.nan,
        np.nan,
        50.0,
        80.0,
        55.17241379310345,
        76.78571428571429,
        56.39344262295083,
        75.72992700729927,
    ])

    actual = rsi_wilder(close, period=3)

    np.testing.assert_allclose(actual, expected, equal_nan=True)


def test_registry_and_streaming_rsi_use_the_same_wilder_implementation():
    df = _ohlc_frame()
    expected = rsi_wilder(df["close"].values, period=14)

    np.testing.assert_allclose(factor_rsi_14(df), expected, equal_nan=True)
    np.testing.assert_allclose(
        StreamingFactorEngine._factor_rsi(df, length=14),
        expected,
        equal_nan=True,
    )
