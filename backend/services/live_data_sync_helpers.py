"""Pure helpers for live scheduler data sync."""

from __future__ import annotations

from typing import Any, Mapping


BAR_FRESHNESS_THRESHOLDS: dict[str, float] = {
    "M1": 600,
    "M5": 900,
    "M15": 1800,
    "M30": 3600,
    "H1": 7200,
    "H4": 28800,
    "D1": 172800,
}

TICK_ADVISORY_MAX_AGE_SECONDS = 600.0


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

    for tf, max_age in thresholds.items():
        try:
            row_ts = float(latest_by_tf.get(tf) or 0.0)
        except Exception:
            row_ts = 0.0
        if row_ts > 0:
            observed_bar_ts_by_tf[tf] = row_ts
        if row_ts > 0 and (now - row_ts) < float(max_age):
            fresh_tfs.append(tf)
        else:
            stale_tfs.append(tf)

    return {
        "stale_tfs": stale_tfs,
        "fresh_tfs": fresh_tfs,
        "observed_bar_ts_by_tf": observed_bar_ts_by_tf,
    }


def classify_tick_freshness(
    latest_ts: Any,
    *,
    now: float,
    max_age_seconds: float = TICK_ADVISORY_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    try:
        tick_latest = float(latest_ts or 0.0)
    except Exception:
        tick_latest = 0.0
    tick_stale = tick_latest == 0 or (now - tick_latest) > float(max_age_seconds)
    tick_age = (now - tick_latest) if tick_latest > 0 else float("inf")
    return {
        "latest_ts": tick_latest,
        "stale": tick_stale,
        "age_seconds": tick_age,
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
