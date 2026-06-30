#!/usr/bin/env python3
"""Split legacy bars into monthly DuckDB files.

Input:
  data/ctrader_data.duckdb:bars

Output:
  data/bars_monthly/bars_YYYY_MM.duckdb:bars
  data/bars.duckdb -> current month bars DB
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.core.db import (
    DUCKDB_BARS_LEGACY,
    bars_monthly_path,
    connect_duckdb,
    ensure_bars_table,
    refresh_current_bars_link,
)


def migrate(source: Path = DUCKDB_BARS_LEGACY, *, dry_run: bool = False) -> dict:
    src = connect_duckdb(source, read_only=True)
    try:
        months = src.execute(
            "SELECT DISTINCT strftime(to_timestamp(time), '%Y-%m') AS ym "
            "FROM bars ORDER BY ym"
        ).fetchall()

        total = 0
        files = 0
        for (ym,) in months:
            year, month = (int(part) for part in str(ym).split("-"))
            month_start = int(src_ts(year, month))
            next_start = int(src_ts(year + (month == 12), 1 if month == 12 else month + 1))
            target = bars_monthly_path(month_start)
            if dry_run:
                print(f"{ym}: {target}")
                continue

            df = src.execute(
                """
                SELECT symbol, timeframe, time, open, high, low, close,
                       COALESCE(volume, 0) AS volume,
                       COALESCE(spread, 0) AS spread
                FROM bars
                WHERE time >= ? AND time < ?
                """,
                [month_start, next_start],
            ).df()
            if df.empty:
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            dst = connect_duckdb(target)
            try:
                ensure_bars_table(dst)
                dst.register("bars_batch", df)
                dst.execute(
                    """
                    INSERT OR REPLACE INTO bars
                    (symbol, timeframe, time, open, high, low, close, volume, spread)
                    SELECT symbol, timeframe, time, open, high, low, close, volume, spread
                    FROM bars_batch
                    """
                )
                count = dst.execute("SELECT COUNT(*) FROM bars").fetchone()[0]
                total += int(count)
                files += 1
                print(f"{ym}: {target} rows={count} source_rows={len(df)}")
            finally:
                try:
                    dst.unregister("bars_batch")
                except Exception:
                    pass
                dst.close()
    finally:
        src.close()

    if not dry_run:
        refresh_current_bars_link()
    return {"files": files, "rows_seen": total}


def src_ts(year: int, month: int) -> float:
    from datetime import datetime, timezone

    return datetime(year, month, 1, tzinfo=timezone.utc).timestamp()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=str(DUCKDB_BARS_LEGACY))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = migrate(Path(args.source), dry_run=args.dry_run)
    print(result)


if __name__ == "__main__":
    main()
