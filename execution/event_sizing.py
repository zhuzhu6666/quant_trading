"""execution/event_sizing.py — 事件感知动态仓位

根据事件日历（importance ≥ 2）和 bar 时间戳计算仓位乘数。
只在高影响事件临近的一小时内降仓，避免把日内交易长时间锁死。

Events 表结构: (date TEXT, type TEXT, description TEXT, importance INTEGER)
  importance=3 → HIGH: FOMC, NFP, CPI
  importance=2 → MEDIUM: PCE

用法:
    es = EventSizing(db_path="data/events.duckdb")
    mult = es.get_multiplier(bar_time_epoch)  # 0.5 .. 1.0
    volume *= mult
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from backend.core.db import connect_duckdb

logger = logging.getLogger(__name__)

EVENT_SIZING_SCHEMA_VERSION = "event_sizing.short_window.v2"


@dataclass
class EventTier:
    """仓位乘数层级: max_hours_before 内适用 multiplier"""
    max_hours_before: float
    multiplier: float


@dataclass
class EventRecord:
    """预加载的事件记录"""
    dt: datetime             # UTC datetime (date + assumed release time)
    event_type: str          # "FOMC", "NFP", "CPI", "PCE"
    importance: int          # 2 or 3
    description: str = ""


# 默认事件发布时间 (UTC)
DEFAULT_EVENT_TIMES: dict[str, str] = {
    "FOMC": "19:00",
    "NFP":  "13:30",
    "CPI":  "13:30",
    "PCE":  "13:30",
}

# 默认乘数层级
DEFAULT_TIERS: dict[int, list[EventTier]] = {
    3: [  # HIGH: FOMC, NFP, CPI
        EventTier(max_hours_before=0.25, multiplier=0.5),
        EventTier(max_hours_before=1.0, multiplier=0.8),
    ],
    2: [  # MEDIUM: PCE / Fed speakers; record context without reducing min-size trades
        EventTier(max_hours_before=0.5, multiplier=1.0),
    ],
}


def _is_legacy_sqlite_events_path(db_path: str) -> bool:
    suffix = Path(db_path).suffix.lower()
    return suffix in {".db", ".sqlite", ".sqlite3"}


class EventSizing:
    """
    事件感知仓位乘数。

    从 events 表加载 importance≥2 的事件，根据 bar 时间戳
    与最近事件的距离返回仓位乘数 [0.5, 1.0]。

    None/disabled 时所有调用返回 1.0，向后兼容。
    """

    def __init__(
        self,
        db_path: str = "data/events.duckdb",
        enabled: bool = True,
        event_times: dict[str, str] | None = None,
        tiers: dict[int, list[EventTier]] | None = None,
    ):
        self.enabled = enabled
        self.event_times = event_times or DEFAULT_EVENT_TIMES
        self.tiers = tiers or DEFAULT_TIERS
        self._events: list[EventRecord] = []
        self._min_multiplier = 1.0

        if enabled:
            self._load_events(db_path)
            if self.tiers:
                self._min_multiplier = min(
                    t.multiplier
                    for tier_list in self.tiers.values()
                    for t in tier_list
                )

    def _load_events(self, db_path: str) -> None:
        """Load importance>=2 events.

        Production uses DuckDB (data/events.duckdb). SQLite fallback is kept for
        legacy unit tests and historical local event files.
        """
        if not Path(db_path).exists():
            logger.warning(f"[EventSizing] {db_path} 不存在, event sizing 禁用")
            self.enabled = False
            return

        try:
            try:
                conn = connect_duckdb(db_path, read_only=True)
                try:
                    cur = conn.execute(
                        "SELECT date, type, description, importance "
                        "FROM events WHERE importance >= 2"
                    )
                    rows = cur.fetchall()
                finally:
                    conn.close()
            except ValueError:
                if not _is_legacy_sqlite_events_path(db_path):
                    raise
                conn = sqlite3.connect(str(db_path))
                try:
                    rows = conn.execute(
                        "SELECT date, type, description, importance "
                        "FROM events WHERE importance >= 2"
                    ).fetchall()
                finally:
                    conn.close()

            for date_str, evt_type, desc, importance in rows:
                time_str = self.event_times.get(evt_type, "13:30")
                try:
                    dt = datetime.strptime(
                        f"{date_str} {time_str}", "%Y-%m-%d %H:%M"
                    ).replace(tzinfo=timezone.utc)
                    self._events.append(EventRecord(
                        dt=dt, event_type=evt_type,
                        importance=importance, description=desc,
                    ))
                except ValueError:
                    continue

            self._events.sort(key=lambda e: e.dt)
            logger.info(
                f"[EventSizing] Loaded {len(self._events)} events "
                f"(importance >= 2) from {db_path}"
            )
        except Exception as e:
            logger.warning(f"[EventSizing] Load failed: {e}")
            self.enabled = False

    def get_multiplier(self, bar_time: float) -> float:
        """
        计算仓位乘数。

        Args:
            bar_time: bar["time"] epoch seconds (UTC)

        Returns:
            float in [min_multiplier, 1.0]，1.0 = 无事件影响
        """
        if not self.enabled or not self._events:
            return 1.0

        try:
            bar_dt = datetime.fromtimestamp(bar_time, tz=timezone.utc)
        except (OSError, ValueError, OverflowError):
            return 1.0

        context = self.get_context(bar_time)
        try:
            return float(context.get("multiplier", 1.0))
        except (TypeError, ValueError):
            return 1.0

    @staticmethod
    def _window_bucket(hours_until: float, tier: EventTier | None) -> str:
        if hours_until < 0:
            return "post_0_5m"
        if tier is None:
            return ""
        if tier.max_hours_before <= 0.25:
            return "pre_0_15m"
        if tier.max_hours_before <= 0.5:
            return "pre_15_30m"
        if tier.max_hours_before <= 1.0:
            return "pre_30_60m"
        return f"pre_0_{tier.max_hours_before:g}h"

    def get_context(self, bar_time: float) -> dict[str, Any]:
        """Return the multiplier plus the event/tier that caused it."""
        if not self.enabled or not self._events:
            return {"enabled": self.enabled, "multiplier": 1.0, "event_near": False}

        try:
            bar_dt = datetime.fromtimestamp(bar_time, tz=timezone.utc)
        except (OSError, ValueError, OverflowError):
            return {"enabled": self.enabled, "multiplier": 1.0, "event_near": False}

        min_mult = 1.0
        causal: dict[str, Any] = {}
        nearest: dict[str, Any] = {}
        max_pre_window = max(
            (tier.max_hours_before for tiers in self.tiers.values() for tier in tiers),
            default=0.0,
        )
        for event in self._events:
            delta = event.dt - bar_dt
            hours_until = delta.total_seconds() / 3600.0

            # BUG-4 fix (audit 2026-06-21): 旧实现 post-event window 是 60 分钟,
            # 这导致事件已过 30-60 分钟后仍被误判降仓.
            # 缩短为 5 分钟 — 仅覆盖 bar 时间和事件时间的小范围错位.
            if hours_until < -(5.0 / 60.0):  # 5 min post-event
                continue
            if max_pre_window > 0 and hours_until > max_pre_window:
                continue

            candidate = {
                "event_type": event.event_type,
                "event": event.description or event.event_type,
                "event_importance": int(event.importance),
                "event_ts": event.dt.timestamp(),
                "hours_until_event": hours_until,
                "minutes_until_event": hours_until * 60.0,
                "is_post_event": hours_until < 0,
            }
            if not nearest or abs(hours_until) < abs(float(nearest.get("hours_until_event", 999999.0))):
                nearest = dict(candidate)

            tier_list = self.tiers.get(event.importance, [])
            for tier in tier_list:
                if hours_until <= tier.max_hours_before:
                    if tier.multiplier < min_mult:
                        min_mult = tier.multiplier
                        causal = {
                            **candidate,
                            "tier_max_hours_before": float(tier.max_hours_before),
                            "tier_multiplier": float(tier.multiplier),
                            "window_bucket": self._window_bucket(hours_until, tier),
                        }
                    break

        event_payload = causal or nearest
        return {
            "schema_version": EVENT_SIZING_SCHEMA_VERSION,
            "enabled": self.enabled,
            "multiplier": min_mult,
            "event_near": bool(event_payload),
            **event_payload,
        }

    def is_event_near(
        self, bar_time: float, hours_threshold: float = 72.0
    ) -> tuple[bool, Optional[str]]:
        """
        检查是否有事件在 hours_threshold 内。

        Returns:
            (is_near, event_description_or_None)
        """
        if not self.enabled or not self._events:
            return False, None

        try:
            bar_dt = datetime.fromtimestamp(bar_time, tz=timezone.utc)
        except (OSError, ValueError, OverflowError):
            return False, None

        for event in self._events:
            delta = event.dt - bar_dt
            hours_until = delta.total_seconds() / 3600.0
            if -1.0 <= hours_until <= hours_threshold:
                return True, event.description or event.event_type
        return False, None

    def stats(self) -> dict:
        """摘要信息"""
        return {
            "schema_version": EVENT_SIZING_SCHEMA_VERSION,
            "enabled": self.enabled,
            "total_events": len(self._events),
            "min_multiplier": self._min_multiplier,
            "max_pre_window_hours": max(
                (tier.max_hours_before for tiers in self.tiers.values() for tier in tiers),
                default=0.0,
            ),
        }

