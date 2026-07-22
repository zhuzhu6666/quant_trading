from __future__ import annotations

import numpy as np
import pandas as pd

from backend.services.low_frequency_factor_warmup import (
    build_low_frequency_factor_snapshots,
)


class _FrameBuilder:
    def __init__(self, frame):
        self.frame = frame
        self.calls = []

    def build(self, **kwargs):
        self.calls.append(kwargs)
        return self.frame


def test_daily_pit_history_seeds_low_frequency_normalizer_inputs():
    index = pd.date_range("2026-01-01", periods=80, freq="1D")
    frame = pd.DataFrame(
        {
            "open": np.linspace(2400.0, 2480.0, 80),
            "high": np.linspace(2401.0, 2481.0, 80),
            "low": np.linspace(2399.0, 2479.0, 80),
            "close": np.linspace(2400.0, 2480.0, 80),
            "volume": np.ones(80),
            "dxy_corr_20": np.linspace(-0.8, 0.3, 80),
            "real_yield_chg": np.linspace(-10.0, 10.0, 80),
            "real_yield_pct_rank": np.linspace(0.2, 0.9, 80),
            "slv_gld_ratio": np.linspace(-0.1, 0.1, 80),
        },
        index=index,
    )
    builder = _FrameBuilder(frame)

    result = build_low_frequency_factor_snapshots(
        signal_config={
            name: {"window": 100, "min_samples": 30}
            for name in (
                "dxy_corr_20",
                "real_yield_chg",
                "real_yield_pct_rank",
                "slv_gld_ratio",
            )
        },
        as_of=index[-1] + pd.Timedelta(days=1),
        frame_builder=builder,
    )

    assert result["daily_bar_count"] == 80
    assert len(result["snapshots"]) == 80
    assert result["factor_counts"] == {
        "dxy_corr_20": 80,
        "real_yield_chg": 80,
        "real_yield_pct_rank": 80,
        "slv_gld_ratio": 80,
    }
    assert result["latest_values"]["dxy_corr_20"] == 0.3
    assert result["latest_values"]["slv_gld_ratio"] == 0.1
    assert builder.calls == [
        {
            "symbol": "XAUUSD+",
            "timeframe": "D1",
            "limit": 160,
            "as_of": index[-1] + pd.Timedelta(days=1),
        }
    ]
