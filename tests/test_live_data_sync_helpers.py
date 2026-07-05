import math

import pandas as pd

from backend.services.live_data_sync_helpers import (
    BAR_FRESHNESS_THRESHOLDS,
    classify_bar_freshness,
    classify_tick_freshness,
    dataframe_to_store_bars,
)


def test_classify_bar_freshness_preserves_threshold_order_and_observed_times():
    now = 1_000_000.0
    latest = {
        "M1": now - 30,
        "M5": now - BAR_FRESHNESS_THRESHOLDS["M5"] - 1,
        "M15": 0,
        "H1": "bad",
    }

    result = classify_bar_freshness(latest, now=now)

    assert result["fresh_tfs"] == ["M1"]
    assert result["stale_tfs"] == ["M5", "M15", "M30", "H1", "H4", "D1"]
    assert result["observed_bar_ts_by_tf"] == {
        "M1": now - 30,
        "M5": now - BAR_FRESHNESS_THRESHOLDS["M5"] - 1,
    }


def test_classify_tick_freshness_treats_missing_ticks_as_stale_advisory():
    missing = classify_tick_freshness(0, now=1_000.0)
    fresh = classify_tick_freshness(900.0, now=1_000.0)
    stale = classify_tick_freshness(100.0, now=1_000.0)

    assert missing["stale"] is True
    assert math.isinf(missing["age_seconds"])
    assert fresh == {"latest_ts": 900.0, "stale": False, "age_seconds": 100.0}
    assert stale == {"latest_ts": 100.0, "stale": True, "age_seconds": 900.0}


def test_dataframe_to_store_bars_matches_datastore_payload_shape():
    df = pd.DataFrame(
        [
            {"open": 1.1, "high": 1.2, "low": 1.0, "close": 1.15, "volume": 3.0},
            {"open": 2.1, "high": 2.2, "low": 2.0, "close": 2.15, "volume": 4.0},
        ],
        index=pd.to_datetime(["2026-07-04T00:00:00Z", "2026-07-04T00:01:00Z"]),
    )

    bars = dataframe_to_store_bars(df)

    assert bars == [
        {
            "time": 1783123200,
            "open": 1.1,
            "high": 1.2,
            "low": 1.0,
            "close": 1.15,
            "volume": 3,
            "spread": 0,
        },
        {
            "time": 1783123260,
            "open": 2.1,
            "high": 2.2,
            "low": 2.0,
            "close": 2.15,
            "volume": 4,
            "spread": 0,
        },
    ]


def test_dataframe_to_store_bars_handles_empty_input():
    assert dataframe_to_store_bars(None) == []
    assert dataframe_to_store_bars(pd.DataFrame()) == []
