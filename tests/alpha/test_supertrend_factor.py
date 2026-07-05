import numpy as np
import pandas as pd

from alpha.registry import factor_supertrend_str
from alpha.streaming_factor_engine import StreamingFactorEngine


def _trend_frame(*, start: float, step: float, n: int = 80) -> pd.DataFrame:
    close = start + np.arange(n, dtype=float) * step
    return pd.DataFrame(
        {
            "open": close - step * 0.25,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.full(n, 100.0),
        }
    )


def test_supertrend_str_is_positive_in_clean_uptrend():
    values = factor_supertrend_str(_trend_frame(start=100.0, step=1.0), period=10, multiplier=3.0)
    finite = values[np.isfinite(values)]

    assert len(finite) > 0
    assert finite[-1] > 0


def test_supertrend_str_is_negative_in_clean_downtrend():
    values = factor_supertrend_str(_trend_frame(start=180.0, step=-1.0), period=10, multiplier=3.0)
    finite = values[np.isfinite(values)]

    assert len(finite) > 0
    assert finite[-1] < 0


def test_streaming_supertrend_matches_registry_implementation():
    frame = _trend_frame(start=100.0, step=0.75)

    registry_values = factor_supertrend_str(frame, period=7, multiplier=2.0)
    streaming_values = StreamingFactorEngine._factor_supertrend_str(
        frame,
        atr_length=7,
        multiplier=2.0,
    )

    np.testing.assert_allclose(streaming_values, registry_values, equal_nan=True)
