"""Unified point-in-time factor frame builder.

This module is the shared data boundary for live factor calculation, factor
health, and evolution. It preserves the raw OHLCV contract while enriching the
frame with point-in-time external features and event buckets.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import pandas as pd

from data.external_loader import ExternalDataLoader
from data.store import DataStore

logger = logging.getLogger(__name__)

_LAST_ENRICHMENT_STATUS: dict[str, Any] = {
    "ok": True,
    "updated_at": 0.0,
    "error": "",
}


@dataclass
class _CacheEntry:
    key: tuple[Any, ...]
    value: pd.DataFrame
    created_at: float


class FactorFrameBuilder:
    """Build and enrich factor calculation frames.

    The public contract is intentionally small: ``build`` loads bars and
    enriches them, while ``enrich_bars`` accepts an already loaded OHLCV frame.
    Both return a DataFrame with a DatetimeIndex and a ``time`` column.
    """

    def __init__(
        self,
        *,
        store: DataStore | None = None,
        external_loader: ExternalDataLoader | None = None,
        cache_ttl_sec: float = 300.0,
    ) -> None:
        self.store = store or DataStore()
        self.external_loader = external_loader or ExternalDataLoader()
        self.cache_ttl_sec = float(cache_ttl_sec)
        self._cache: _CacheEntry | None = None

    def build(
        self,
        symbol: str = "XAUUSD+",
        timeframe: str = "M5",
        limit: int | None = None,
        start: str | None = None,
        end: str | None = None,
        as_of: Any | None = None,
    ) -> pd.DataFrame:
        bars = self.store.load_bars(symbol, timeframe, start=start, end=end, limit=limit)
        return self.enrich_bars(bars, as_of=as_of)

    def enrich_bars(self, bar_df: pd.DataFrame, as_of: Any | None = None) -> pd.DataFrame:
        frame = self._normalize_bar_frame(bar_df)
        if frame.empty:
            return frame

        key = self._cache_key(frame, as_of)
        now = time.time()
        if (
            self._cache is not None
            and self._cache.key == key
            and now - self._cache.created_at <= self.cache_ttl_sec
        ):
            return self._cache.value.copy()

        try:
            ext = self.external_loader.align_to_bars(frame, as_of=as_of)
        except Exception as exc:
            logger.warning("[FactorFrameBuilder] external enrichment failed: %s", exc)
            _LAST_ENRICHMENT_STATUS.update(
                {
                    "ok": False,
                    "updated_at": now,
                    "error": str(exc),
                }
            )
            return frame

        if ext is None or ext.empty:
            enriched = frame
        else:
            ext = ext.copy()
            duplicate_cols = [c for c in ext.columns if c in frame.columns]
            if duplicate_cols:
                ext = ext.drop(columns=duplicate_cols)
            enriched = frame.join(ext, how="left")

        self._cache = _CacheEntry(key=key, value=enriched.copy(), created_at=now)
        _LAST_ENRICHMENT_STATUS.update({"ok": True, "updated_at": now, "error": ""})
        return enriched

    @staticmethod
    def _normalize_bar_frame(bar_df: pd.DataFrame) -> pd.DataFrame:
        if bar_df is None:
            return pd.DataFrame()
        frame = bar_df.copy()
        if frame.empty:
            return frame

        if "time" in frame.columns:
            if pd.api.types.is_numeric_dtype(frame["time"]):
                idx = pd.to_datetime(frame["time"], unit="s", errors="coerce", utc=True)
            else:
                idx = pd.to_datetime(frame["time"], errors="coerce", utc=True)
            frame.index = pd.DatetimeIndex(idx).tz_convert(None)
        elif isinstance(frame.index, pd.DatetimeIndex):
            frame.index = pd.DatetimeIndex(frame.index)
            if frame.index.tz is not None:
                frame.index = frame.index.tz_convert(None)
            frame["time"] = frame.index.astype("int64") / 1_000_000_000
        else:
            frame.index = pd.to_datetime(frame.index, errors="coerce", utc=True)
            frame.index = pd.DatetimeIndex(frame.index).tz_convert(None)
            frame["time"] = frame.index.astype("int64") / 1_000_000_000

        frame = frame[~frame.index.isna()].sort_index()
        if "time" not in frame.columns:
            frame["time"] = frame.index.astype("int64") / 1_000_000_000
        return frame

    @staticmethod
    def _cache_key(frame: pd.DataFrame, as_of: Any | None) -> tuple[Any, ...]:
        idx = frame.index
        first = idx[0].isoformat() if len(idx) else ""
        last = idx[-1].isoformat() if len(idx) else ""
        return (len(frame), first, last, str(as_of or ""))


def latest_factor_frame_status() -> dict[str, Any]:
    return dict(_LAST_ENRICHMENT_STATUS)
