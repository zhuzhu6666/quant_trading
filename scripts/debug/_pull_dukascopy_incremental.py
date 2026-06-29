#!/usr/bin/env python
"""Dukascopy incremental puller for monthly tick DuckDB files."""

from __future__ import annotations

import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent.parent
os.chdir(str(ROOT))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.tick_monthly import (  # noqa: E402
    RAW_ROOT,
    decode_bi5,
    latest_tick_ts,
    monthly_summary,
    refresh_legacy_ticks_pointer,
    write_hour_records,
)


BASE = "https://datafeed.dukascopy.com/datafeed/XAUUSD"


def download_hour(year: int, month0: int, day: int, hour: int) -> bool:
    url = f"{BASE}/{year}/{month0:02d}/{day:02d}/{hour:02d}h_ticks.bi5"
    local = RAW_ROOT / f"{year}/{month0:02d}/{day:02d}/{hour:02d}h_ticks.bi5"
    if local.exists() and local.stat().st_size > 0:
        return True
    local.parent.mkdir(parents=True, exist_ok=True)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as response:
            local.write_bytes(response.read())
        return True
    except Exception:
        return False


def import_hour(year: int, month0: int, day: int, hour: int) -> int:
    local = RAW_ROOT / f"{year}/{month0:02d}/{day:02d}/{hour:02d}h_ticks.bi5"
    base_ts, records = decode_bi5(local)
    if not records:
        return 0
    return write_hour_records(
        records,
        base_ts=base_ts,
        year=year,
        month=month0 + 1,
    )


def should_skip_market_hour(current: datetime) -> bool:
    if current.weekday() == 5:
        return True
    return current.weekday() == 6 and current.hour < 22


def main() -> int:
    last_ts = latest_tick_ts()
    if last_ts is None:
        start = datetime.now(timezone.utc) - timedelta(days=7)
    else:
        start = datetime.fromtimestamp(last_ts, tz=timezone.utc)

    now = datetime.now(timezone.utc)
    total = 0
    current = start.replace(minute=0, second=0, microsecond=0)
    while current < now:
        if should_skip_market_hour(current):
            current += timedelta(hours=1)
            continue
        year, month0, day, hour = current.year, current.month - 1, current.day, current.hour
        if download_hour(year, month0, day, hour):
            total += import_hour(year, month0, day, hour)
        current += timedelta(hours=1)

    count, max_ts = monthly_summary()
    refresh_legacy_ticks_pointer(max_ts)
    latest = datetime.fromtimestamp(max_ts, tz=timezone.utc) if max_ts else "N/A"
    print(f"✅ Dukascopy 月库增量完成: +{total} ticks", flush=True)
    print(f"   DB: {count:,} ticks | 最新: {latest}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
