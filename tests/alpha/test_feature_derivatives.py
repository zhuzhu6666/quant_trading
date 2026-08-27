from __future__ import annotations

import warnings

import numpy as np
import pandas as pd

from alpha.features.derivatives import FeatureDeriver


def test_degenerate_ohlcv_does_not_emit_math_or_fragmentation_warnings():
    bars = pd.DataFrame(
        {
            "open": np.zeros(64),
            "high": np.zeros(64),
            "low": np.zeros(64),
            "close": np.zeros(64),
            "volume": np.zeros(64),
        }
    )

    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        warnings.simplefilter("error", pd.errors.PerformanceWarning)
        features = FeatureDeriver().derive(bars, {"flat": np.zeros(len(bars))})

    assert features.shape[0] == len(bars)
    assert features.shape[1] > 0
