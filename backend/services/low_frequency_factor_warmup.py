"""PIT-safe daily history for low-frequency live factor normalization."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from alpha.factor_cadence import LOW_FREQUENCY_DAILY_FACTORS
from alpha.registry import factor_registry
from backend.services.live_data_sync_helpers import expected_closed_bar_ts
from data.factor_frame import FactorFrameBuilder


def build_low_frequency_factor_snapshots(
    *,
    signal_config: dict[str, dict[str, Any]] | None,
    as_of: Any,
    symbol: str = "XAUUSD+",
    frame_builder: Any | None = None,
) -> dict[str, Any]:
    """Rebuild normalizer history from closed D1 bars and PIT external data.

    Intraday warmup frames contain many repeated copies of one daily external
    value.  The normalizer deliberately samples those factors only when their
    value changes, so an M5-only warmup cannot satisfy a 30-observation macro
    window.  This helper uses the same ``FactorFrameBuilder`` and registered
    factor callables as live scoring, but evaluates them on closed D1 history.
    """

    configs = dict(signal_config or {})
    factor_names = sorted(set(configs) & LOW_FREQUENCY_DAILY_FACTORS)
    if not factor_names:
        return {
            "schema_version": "low_frequency_factor_warmup.v1",
            "snapshots": [],
            "factor_counts": {},
            "raw_factor_counts": {},
            "factor_errors": {},
            "latest_values": {},
            "daily_bar_count": 0,
        }

    max_window = max(
        int((configs.get(name) or {}).get("window", 100) or 100)
        for name in factor_names
    )
    # Leave enough headroom for rolling factor lookbacks before normalization.
    daily_limit = max(160, max_window + 60)
    builder = frame_builder or FactorFrameBuilder(cache_ttl_sec=0.0)
    daily_frame = builder.build(
        symbol=symbol,
        timeframe="D1",
        limit=daily_limit,
        as_of=as_of,
    )
    if hasattr(as_of, "timestamp"):
        as_of_epoch = float(as_of.timestamp())
    elif isinstance(as_of, str):
        as_of_epoch = datetime.fromisoformat(
            as_of.strip().replace("Z", "+00:00")
        ).timestamp()
    else:
        as_of_epoch = float(as_of)
    closed_bar_epoch = expected_closed_bar_ts(now=as_of_epoch, timeframe="D1")
    if closed_bar_epoch > 0:
        daily_frame = daily_frame.loc[
            [
                float(index.timestamp()) <= closed_bar_epoch
                if hasattr(index, "timestamp")
                else float(index) <= closed_bar_epoch
                for index in daily_frame.index
            ]
        ]
    if daily_frame is None or daily_frame.empty:
        return {
            "schema_version": "low_frequency_factor_warmup.v1",
            "snapshots": [],
            "factor_counts": {},
            "raw_factor_counts": {},
            "factor_errors": {},
            "latest_values": {},
            "daily_bar_count": 0,
        }

    snapshots: list[dict[str, float]] = [dict() for _ in range(len(daily_frame))]
    factor_counts: dict[str, int] = {}
    raw_factor_counts: dict[str, int] = {}
    factor_errors: dict[str, str] = {}
    latest_values: dict[str, float] = {}
    for name in factor_names:
        factor_fn = factor_registry.get(name)
        if factor_fn is None:
            continue
        try:
            values = factor_fn(daily_frame)
        except Exception as exc:
            factor_errors[name] = f"{type(exc).__name__}: {str(exc)[:180]}"
            continue
        raw_count = 0
        independent_count = 0
        previous: float | None = None
        for index, value in enumerate(values):
            if value is None:
                continue
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(numeric):
                continue
            snapshots[index][name] = numeric
            raw_count += 1
            if previous is None or abs(previous - numeric) > 1e-12:
                independent_count += 1
                previous = numeric
        raw_factor_counts[name] = raw_count
        factor_counts[name] = independent_count
        if previous is not None:
            latest_values[name] = previous

    return {
        "schema_version": "low_frequency_factor_warmup.v1",
        "snapshots": [snapshot for snapshot in snapshots if snapshot],
        "factor_counts": factor_counts,
        "raw_factor_counts": raw_factor_counts,
        "factor_errors": factor_errors,
        "latest_values": latest_values,
        "daily_bar_count": len(daily_frame),
    }
