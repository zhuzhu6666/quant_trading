#!/usr/bin/env python
"""Rebuild ticks.duckdb from cached Dukascopy .bi5 files."""

from __future__ import annotations

import argparse
import lzma
import os
import struct
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd


PIPET = 0.001
RECORD = struct.Struct(">3I2f")
ROWS_PER_BATCH = 500_000


def iter_raw_files(raw_root: Path) -> list[Path]:
    return sorted(raw_root.glob("**/*h_ticks.bi5"))


def parse_base_ts(path: Path) -> int:
    hour_name = path.name.split("h_ticks.bi5", 1)[0]
    hour = int(hour_name)
    day = int(path.parent.name)
    month0 = int(path.parent.parent.name)
    year = int(path.parent.parent.parent.name)
    return int(datetime(year, month0 + 1, day, hour, tzinfo=timezone.utc).timestamp())


def decode_file(path: Path, symbol: str) -> list[tuple]:
    if not path.exists() or path.stat().st_size <= 0:
        return []
    try:
        decoded = lzma.decompress(path.read_bytes())
    except Exception as exc:
        print(f"skip decode failed: {path} ({exc})", flush=True)
        return []

    base = parse_base_ts(path)
    records = []
    usable = len(decoded) - (len(decoded) % RECORD.size)
    for offset in range(0, usable, RECORD.size):
        tick_ms, ask_raw, bid_raw, ask_volume, bid_volume = RECORD.unpack_from(decoded, offset)
        if bid_raw <= 0 or ask_raw <= 0:
            continue
        volume = float(max(ask_volume, 0.0) + max(bid_volume, 0.0))
        records.append(
            (
                symbol,
                base + tick_ms / 1000.0,
                bid_raw * PIPET,
                ask_raw * PIPET,
                (bid_raw + ask_raw) * PIPET / 2.0,
                volume,
            )
        )
    return records


def flush_batch(conn, rows: list[tuple]) -> int:
    if not rows:
        return 0
    df = pd.DataFrame(rows, columns=["symbol", "time", "bid", "ask", "last", "volume"])
    conn.execute("INSERT INTO ticks SELECT * FROM df")
    return len(rows)


def rebuild(raw_root: Path, output: Path, symbol: str, batch_rows: int) -> None:
    raw_files = iter_raw_files(raw_root)
    if not raw_files:
        raise RuntimeError(f"no .bi5 files found under {raw_root}")

    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError(f"output already exists: {output}")

    conn = duckdb.connect(str(output))
    conn.execute(
        """
        CREATE TABLE ticks (
            symbol VARCHAR NOT NULL,
            time DOUBLE NOT NULL,
            bid DOUBLE,
            ask DOUBLE,
            last DOUBLE,
            volume DOUBLE DEFAULT 0
        )
        """
    )

    started = time.time()
    rows: list[tuple] = []
    total = 0
    for idx, path in enumerate(raw_files, 1):
        decoded = decode_file(path, symbol)
        rows.extend(decoded)
        if len(rows) >= batch_rows:
            total += flush_batch(conn, rows)
            rows.clear()
            elapsed = max(time.time() - started, 0.001)
            print(
                f"progress files={idx}/{len(raw_files)} rows={total:,} "
                f"rate={total / elapsed:,.0f}/s",
                flush=True,
            )
    total += flush_batch(conn, rows)
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ticks_sym_time ON ticks(symbol, time)")
    except Exception as exc:
        print(f"index creation skipped: {exc}", flush=True)
    conn.execute("CHECKPOINT")
    summary = conn.execute(
        "SELECT COUNT(*), MIN(time), MAX(time), MIN(volume), MAX(volume), AVG(volume) FROM ticks"
    ).fetchone()
    conn.close()
    print(f"rebuilt {output}", flush=True)
    print(f"summary {summary}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", default="data/dukascopy_raw/XAUUSD")
    parser.add_argument("--output", default="data/ticks.rebuilt.duckdb")
    parser.add_argument("--symbol", default="XAUUSD+")
    parser.add_argument("--batch-rows", type=int, default=ROWS_PER_BATCH)
    args = parser.parse_args()

    rebuild(
        raw_root=Path(args.raw_root),
        output=Path(args.output),
        symbol=args.symbol,
        batch_rows=max(int(args.batch_rows), 10_000),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
