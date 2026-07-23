"""Pure helpers for live scheduler data sync."""

from __future__ import annotations

import math
from typing import Any, Mapping


# Keep maintenance pulls one minute behind M5 decision boundaries. Running at
# :00/:05/... contends for the primary cTrader bridge exactly while final
# account/position admission facts are being validated.
DATA_SYNC_CRON = "1-56/5 * * * *"
DATA_SYNC_INTERVAL_SECONDS = 5 * 60


BAR_FRESHNESS_THRESHOLDS: dict[str, float] = {
    "M1": 180,
    "M5": 900,
    "M15": 1800,
    "M30": 3600,
    "H1": 7200,
    "H4": 28800,
    "D1": 172800,
}

TIMEFRAME_SECONDS: dict[str, int] = {
    "M1": 60,
    "M2": 120,
    "M3": 180,
    "M4": 240,
    "M5": 300,
    "M10": 600,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
}



def timeframe_seconds(timeframe: str | None) -> int:
    return int(TIMEFRAME_SECONDS.get(str(timeframe or "").upper(), 0) or 0)


def expected_closed_bar_ts(
    *,
    now: float,
    timeframe: str,
    close_grace_seconds: float = 0.0,
) -> float:
    seconds = timeframe_seconds(timeframe)
    if seconds <= 0:
        return 0.0
    current_bar_start = math.floor(float(now) / seconds) * seconds
    grace = max(0.0, float(close_grace_seconds or 0.0))
    if float(now) - current_bar_start < grace:
        return float(current_bar_start - (2 * seconds))
    return float(current_bar_start - seconds)


def classify_decision_bar_freshness(
    *,
    latest_ts: Any,
    timeframe: str,
    now: float,
    close_grace_seconds: float = 0.0,
) -> dict[str, Any]:
    tf = str(timeframe or "").upper()
    seconds = timeframe_seconds(tf)
    try:
        latest = float(latest_ts or 0.0)
    except Exception:
        latest = 0.0
    expected = expected_closed_bar_ts(
        now=float(now),
        timeframe=tf,
        close_grace_seconds=close_grace_seconds,
    )
    age = max(0.0, float(now) - latest) if latest > 0 else float("inf")
    missing_bars = 0
    if seconds > 0 and expected > 0 and latest > 0 and latest < expected:
        missing_bars = int(max(1, round((expected - latest) / seconds)))
    fresh = bool(latest > 0 and (expected <= 0 or latest >= expected))
    return {
        "schema_version": "decision_bar_freshness.v1",
        "timeframe": tf,
        "timeframe_seconds": seconds,
        "latest_bar_ts": latest,
        "expected_closed_bar_ts": expected,
        "age_seconds": age,
        "missing_closed_bars": missing_bars,
        "fresh": fresh,
    }


def classify_bar_freshness(
    latest_by_tf: Mapping[str, Any],
    *,
    now: float,
    thresholds: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    thresholds = thresholds or BAR_FRESHNESS_THRESHOLDS
    stale_tfs: list[str] = []
    fresh_tfs: list[str] = []
    observed_bar_ts_by_tf: dict[str, float] = {}

    expected_bar_ts_by_tf: dict[str, float] = {}
    missing_closed_bars_by_tf: dict[str, int] = {}

    for tf, max_age in thresholds.items():
        try:
            row_ts = float(latest_by_tf.get(tf) or 0.0)
        except Exception:
            row_ts = 0.0
        if row_ts > 0:
            observed_bar_ts_by_tf[tf] = row_ts

        freshness = classify_decision_bar_freshness(
            latest_ts=row_ts,
            timeframe=tf,
            now=now,
        )
        expected_ts = float(freshness.get("expected_closed_bar_ts", 0.0) or 0.0)
        if expected_ts > 0:
            expected_bar_ts_by_tf[tf] = expected_ts
        missing_closed_bars_by_tf[tf] = int(freshness.get("missing_closed_bars", 0) or 0)

        is_fresh = bool(freshness.get("fresh", False))
        if not is_fresh and expected_ts <= 0:
            is_fresh = row_ts > 0 and (now - row_ts) < float(max_age)
        if is_fresh:
            fresh_tfs.append(tf)
        else:
            stale_tfs.append(tf)

    return {
        "stale_tfs": stale_tfs,
        "fresh_tfs": fresh_tfs,
        "observed_bar_ts_by_tf": observed_bar_ts_by_tf,
        "expected_bar_ts_by_tf": expected_bar_ts_by_tf,
        "missing_closed_bars_by_tf": missing_closed_bars_by_tf,
    }


def dataframe_to_store_bars(df) -> list[dict[str, Any]]:
    bars: list[dict[str, Any]] = []
    if df is None or getattr(df, "empty", False):
        return bars
    for idx, row in df.iterrows():
        ts = int(idx.timestamp())
        bars.append(
            {
                "time": ts,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": int(row["volume"]),
                "spread": 0,
            }
        )
    return bars
