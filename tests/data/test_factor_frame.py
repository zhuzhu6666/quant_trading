from __future__ import annotations

import pandas as pd

from data.factor_frame import FactorFrameBuilder


class _Store:
    def __init__(self, bars: pd.DataFrame) -> None:
        self.bars = bars
        self.calls = []

    def load_bars(self, symbol, timeframe, start=None, end=None, limit=None):
        self.calls.append(
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "start": start,
                "end": end,
                "limit": limit,
            }
        )
        return self.bars


class _ExternalLoader:
    def __init__(self) -> None:
        self.calls = []

    def align_to_bars(self, bar_df: pd.DataFrame, as_of=None) -> pd.DataFrame:
        self.calls.append({"index": bar_df.index.copy(), "as_of": as_of})
        assert isinstance(bar_df.index, pd.DatetimeIndex)
        return pd.DataFrame(
            {
                "GLD_tonnes_chg_5d": [1.0, 2.0, 3.0],
                "hours_to_fomc": [-24.0, 0.0, 24.0],
            },
            index=bar_df.index,
        )


def test_factor_frame_build_preserves_bars_and_adds_pit_features():
    idx = pd.date_range("2026-01-01 00:00:00", periods=3, freq="5min")
    bars = pd.DataFrame(
        {
            "time": idx.astype("int64") / 1_000_000_000,
            "open": [100.0, 101.0, 102.0],
            "high": [101.0, 102.0, 103.0],
            "low": [99.0, 100.0, 101.0],
            "close": [100.5, 101.5, 102.5],
            "volume": [10, 11, 12],
        }
    )
    loader = _ExternalLoader()
    store = _Store(bars)
    builder = FactorFrameBuilder(store=store, external_loader=loader, cache_ttl_sec=60)

    out = builder.build(
        symbol="XAUUSD+",
        timeframe="M5",
        limit=3,
        as_of="2026-01-01 00:10:00",
    )

    assert store.calls == [
        {
            "symbol": "XAUUSD+",
            "timeframe": "M5",
            "start": None,
            "end": None,
            "limit": 3,
        }
    ]
    assert isinstance(out.index, pd.DatetimeIndex)
    assert list(out["close"]) == [100.5, 101.5, 102.5]
    assert "time" in out.columns
    assert list(out["GLD_tonnes_chg_5d"]) == [1.0, 2.0, 3.0]
    assert list(out["hours_to_fomc"]) == [-24.0, 0.0, 24.0]
    assert loader.calls[0]["as_of"] == "2026-01-01 00:10:00"


def test_factor_frame_enrichment_failure_degrades_to_ohlcv_only():
    class _BrokenLoader:
        def align_to_bars(self, bar_df: pd.DataFrame, as_of=None) -> pd.DataFrame:
            raise RuntimeError("external db locked")

    idx = pd.date_range("2026-01-01", periods=2, freq="5min")
    bars = pd.DataFrame({"close": [1.0, 2.0]}, index=idx)
    builder = FactorFrameBuilder(store=_Store(bars), external_loader=_BrokenLoader())

    out = builder.enrich_bars(bars)

    assert isinstance(out.index, pd.DatetimeIndex)
    assert list(out["close"]) == [1.0, 2.0]
    assert "GLD_tonnes_chg_5d" not in out.columns
