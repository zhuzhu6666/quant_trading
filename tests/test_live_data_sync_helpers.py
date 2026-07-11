import pandas as pd

from backend.services.live_data_sync_helpers import (
    BAR_FRESHNESS_THRESHOLDS,
    classify_decision_bar_freshness,
    classify_bar_freshness,
    dataframe_to_store_bars,
)


def test_classify_bar_freshness_detects_missing_closed_m5_bar():
    now = 1_783_396_219.0  # 2026-07-07 11:50:19 Asia/Shanghai
    latest = {
        "M1": 1_783_395_840.0,  # 11:44, missing 11:49
        "M5": 1_783_395_600.0,  # 11:40, missing 11:45
        "M15": 0,
        "H1": "bad",
    }

    result = classify_bar_freshness(latest, now=now)

    assert result["fresh_tfs"] == []
    assert result["stale_tfs"] == ["M1", "M5", "M15", "M30", "H1", "H4", "D1"]
    assert result["observed_bar_ts_by_tf"] == {
        "M1": 1_783_395_840.0,
        "M5": 1_783_395_600.0,
    }
    assert result["expected_bar_ts_by_tf"]["M5"] == 1_783_395_900.0
    assert result["missing_closed_bars_by_tf"]["M5"] == 1


def test_classify_decision_bar_freshness_accepts_latest_closed_bar():
    now = 1_783_396_219.0

    stale = classify_decision_bar_freshness(
        latest_ts=1_783_395_600.0,
        timeframe="M5",
        now=now,
    )
    fresh = classify_decision_bar_freshness(
        latest_ts=1_783_395_900.0,
        timeframe="M5",
        now=now,
    )

    assert stale["fresh"] is False
    assert stale["expected_closed_bar_ts"] == 1_783_395_900.0
    assert stale["missing_closed_bars"] == 1
    assert fresh["fresh"] is True


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
