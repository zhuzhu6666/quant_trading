#!/usr/bin/env python
"""Dukascopy incremental puller for monthly tick DuckDB files."""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
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
DOWNLOAD_TIMEOUT_SEC = float(os.getenv("DUKASCOPY_DOWNLOAD_TIMEOUT_SEC", "8"))
MAX_CONSECUTIVE_MISSES = int(os.getenv("DUKASCOPY_MAX_CONSECUTIVE_MISSES", "4"))
LOOKBACK_HOURS = int(os.getenv("DUKASCOPY_LOOKBACK_HOURS", "6"))


@dataclass(frozen=True)
class DownloadResult:
    available: bool
    path: Path
    status: str
    size: int = 0
    error: str = ""
    cached: bool = False


def _hour_url(year: int, month0: int, day: int, hour: int) -> str:
    return f"{BASE}/{year}/{month0:02d}/{day:02d}/{hour:02d}h_ticks.bi5"


def _hour_path(year: int, month0: int, day: int, hour: int) -> Path:
    return RAW_ROOT / f"{year}/{month0:02d}/{day:02d}/{hour:02d}h_ticks.bi5"


def download_hour(year: int, month0: int, day: int, hour: int) -> DownloadResult:
    url = _hour_url(year, month0, day, hour)
    local = _hour_path(year, month0, day, hour)
    if local.exists() and local.stat().st_size > 0:
        return DownloadResult(True, local, "cached", local.stat().st_size, cached=True)
    if local.exists():
        local.unlink()
    local.parent.mkdir(parents=True, exist_ok=True)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_SEC) as response:
            payload = response.read()
        if not payload:
            return DownloadResult(False, local, "empty_response")
        tmp = local.with_suffix(local.suffix + ".tmp")
        tmp.write_bytes(payload)
        tmp.replace(local)
        return DownloadResult(True, local, "downloaded", len(payload))
    except urllib.error.HTTPError as exc:
        return DownloadResult(False, local, f"http_{exc.code}", error=str(exc))
    except Exception as exc:
        return DownloadResult(False, local, type(exc).__name__, error=str(exc))


def import_hour(
    year: int,
    month0: int,
    day: int,
    hour: int,
    *,
    previous_latest: float | None,
) -> tuple[int, int, float | None]:
    local = _hour_path(year, month0, day, hour)
    base_ts, records = decode_bi5(local)
    if not records:
        return 0, 0, None
    imported = write_hour_records(
        records,
        base_ts=base_ts,
        year=year,
        month=month0 + 1,
    )
    new_records = sum(1 for row in records if previous_latest is None or float(row[1]) > previous_latest)
    return imported, new_records, float(records[-1][1])


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
        if LOOKBACK_HOURS > 0:
            start -= timedelta(hours=LOOKBACK_HOURS)

    stop = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    before_count, _before_max_ts = monthly_summary()
    total_imported = 0
    total_new = 0
    attempted = 0
    missing = 0
    consecutive_misses = 0
    first_failures: list[str] = []
    current = start.replace(minute=0, second=0, microsecond=0)
    while current < stop:
        if should_skip_market_hour(current):
            current += timedelta(hours=1)
            continue
        year, month0, day, hour = current.year, current.month - 1, current.day, current.hour
        attempted += 1
        result = download_hour(year, month0, day, hour)
        if result.available:
            consecutive_misses = 0
            imported, new_records, _max_record_ts = import_hour(
                year,
                month0,
                day,
                hour,
                previous_latest=last_ts,
            )
            total_imported += imported
            total_new += new_records
        else:
            missing += 1
            consecutive_misses += 1
            if len(first_failures) < 8:
                first_failures.append(f"{current.isoformat()} {result.status} {result.error}".strip())
            print(
                f"[dukascopy_tick] missing {current.isoformat()} status={result.status} error={result.error}",
                file=sys.stderr,
                flush=True,
            )
            if MAX_CONSECUTIVE_MISSES > 0 and consecutive_misses >= MAX_CONSECUTIVE_MISSES:
                print(
                    f"[dukascopy_tick] stop after {consecutive_misses} consecutive unavailable hours",
                    file=sys.stderr,
                    flush=True,
                )
                break
        current += timedelta(hours=1)

    count, max_ts = monthly_summary()
    refresh_legacy_ticks_pointer(max_ts)
    net_rows = count - before_count
    latest = datetime.fromtimestamp(max_ts, tz=timezone.utc) if max_ts else "N/A"
    if first_failures:
        print("[dukascopy_tick] first unavailable hours: " + " | ".join(first_failures), file=sys.stderr, flush=True)
    print(
        f"✅ Dukascopy 月库增量完成: +{total_new} new ticks "
        f"(net_rows={net_rows:+d}, {total_imported} rows imported/replaced, "
        f"attempted={attempted}, missing={missing})",
        flush=True,
    )
    print(f"   DB: {count:,} ticks | 最新: {latest}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
