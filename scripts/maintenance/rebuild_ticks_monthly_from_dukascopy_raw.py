#!/usr/bin/env python
"""Rebuild monthly tick DuckDB files from cached Dukascopy .bi5 files."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

import duckdb
import pandas as pd


ROOT = Path(__file__).resolve().parent.parent.parent
os.chdir(str(ROOT))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.tick_monthly import (  # noqa: E402
    DEFAULT_SYMBOL,
    MONTHLY_ROOT,
    RAW_ROOT,
    decode_bi5,
    ensure_ticks_schema,
    iter_month_db_paths,
    month_db_path,
    parse_bi5_base,
)


ROWS_PER_BATCH = 500_000


def iter_raw_files(raw_root: Path) -> list[Path]:
    return sorted(raw_root.glob("**/*h_ticks.bi5"))


def flush_month(conn: duckdb.DuckDBPyConnection, rows: list[tuple]) -> int:
    if not rows:
        return 0
    df = pd.DataFrame(rows, columns=["symbol", "time", "bid", "ask", "last", "volume"])
    conn.execute("INSERT INTO ticks SELECT * FROM df")
    return len(rows)


def create_index(conn: duckdb.DuckDBPyConnection) -> None:
    try:
        conn.execute("SET threads=1")
        conn.execute("SET preserve_insertion_order=false")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ticks_sym_time ON ticks(symbol, time)")
    except Exception as exc:
        print(f"index creation skipped: {exc}", flush=True)


def rebuild(raw_root: Path, monthly_root: Path, symbol: str, batch_rows: int, replace: bool, index: bool) -> None:
    raw_files = iter_raw_files(raw_root)
    if not raw_files:
        raise RuntimeError(f"no .bi5 files found under {raw_root}")
    if replace and monthly_root.exists():
        shutil.rmtree(monthly_root)
    monthly_root.mkdir(parents=True, exist_ok=True)

    started = time.time()
    total = 0
    current_key: tuple[int, int] | None = None
    current_conn: duckdb.DuckDBPyConnection | None = None
    rows: list[tuple] = []
    month_totals: dict[tuple[int, int], int] = {}

    def close_current() -> None:
        nonlocal current_conn, rows, total
        if current_conn is None or current_key is None:
            return
        total += flush_month(current_conn, rows)
        month_totals[current_key] = month_totals.get(current_key, 0) + len(rows)
        rows = []
        if index:
            create_index(current_conn)
        current_conn.execute("CHECKPOINT")
        current_conn.close()
        current_conn = None

    for idx, path in enumerate(raw_files, 1):
        year, month0, _day, _hour, _base_ts = parse_bi5_base(path)
        key = (year, month0 + 1)
        if current_key != key:
            close_current()
            current_key = key
            db_path = month_db_path(year, month0 + 1, monthly_root)
            current_conn = duckdb.connect(str(db_path))
            ensure_ticks_schema(current_conn)

        _base_ts, decoded = decode_bi5(path, symbol)
        rows.extend(decoded)
        if len(rows) >= batch_rows and current_conn is not None and current_key is not None:
            wrote = flush_month(current_conn, rows)
            total += wrote
            month_totals[current_key] = month_totals.get(current_key, 0) + wrote
            rows.clear()
            elapsed = max(time.time() - started, 0.001)
            print(
                f"progress files={idx}/{len(raw_files)} rows={total:,} "
                f"rate={total / elapsed:,.0f}/s",
                flush=True,
            )
    close_current()

    print("rebuilt monthly tick DBs", flush=True)
    print(f"total rows={total:,} months={len(month_totals)}", flush=True)
    for db_path in iter_month_db_paths(monthly_root):
        conn = duckdb.connect(str(db_path), read_only=True)
        count, min_ts, max_ts, max_volume = conn.execute(
            "SELECT COUNT(*), MIN(time), MAX(time), MAX(volume) FROM ticks"
        ).fetchone()
        conn.close()
        print(f"{db_path.name}: rows={count:,} min={min_ts} max={max_ts} max_volume={max_volume}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", default=str(RAW_ROOT))
    parser.add_argument("--monthly-root", default=str(MONTHLY_ROOT))
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL)
    parser.add_argument("--batch-rows", type=int, default=ROWS_PER_BATCH)
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--index", action="store_true")
    args = parser.parse_args()

    rebuild(
        raw_root=Path(args.raw_root),
        monthly_root=Path(args.monthly_root),
        symbol=args.symbol,
        batch_rows=max(int(args.batch_rows), 10_000),
        replace=bool(args.replace),
        index=bool(args.index),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
