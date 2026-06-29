"""Helpers for Dukascopy tick monthly DuckDB files."""

from __future__ import annotations

import lzma
import os
import struct
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd


DEFAULT_SYMBOL = "XAUUSD+"
MONTHLY_ROOT = Path("data/ticks_monthly")
RAW_ROOT = Path("data/dukascopy_raw/XAUUSD")
PIPET = 0.001
RECORD = struct.Struct(">3I2f")


def month_db_path(year: int, month: int, root: Path = MONTHLY_ROOT) -> Path:
    return root / f"ticks_{year:04d}_{month:02d}.duckdb"


def iter_month_db_paths(root: Path = MONTHLY_ROOT) -> list[Path]:
    return sorted(root.glob("ticks_????_??.duckdb"))


def ensure_ticks_schema(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ticks (
            symbol VARCHAR NOT NULL,
            time DOUBLE NOT NULL,
            bid DOUBLE,
            ask DOUBLE,
            last DOUBLE,
            volume DOUBLE DEFAULT 0
        )
        """
    )


def parse_bi5_base(path: Path) -> tuple[int, int, int, int, int]:
    hour_name = path.name.split("h_ticks.bi5", 1)[0]
    hour = int(hour_name)
    day = int(path.parent.name)
    month0 = int(path.parent.parent.name)
    year = int(path.parent.parent.parent.name)
    base_ts = int(datetime(year, month0 + 1, day, hour, tzinfo=timezone.utc).timestamp())
    return year, month0, day, hour, base_ts


def decode_bi5(path: Path, symbol: str = DEFAULT_SYMBOL) -> tuple[int, list[tuple]]:
    if not path.exists() or path.stat().st_size <= 0:
        return 0, []
    year, month0, _day, _hour, base_ts = parse_bi5_base(path)
    try:
        decoded = lzma.decompress(path.read_bytes())
    except Exception:
        return base_ts, []

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
                base_ts + tick_ms / 1000.0,
                bid_raw * PIPET,
                ask_raw * PIPET,
                (bid_raw + ask_raw) * PIPET / 2.0,
                volume,
            )
        )
    return base_ts, records


def write_hour_records(
    records: list[tuple],
    *,
    base_ts: int,
    year: int,
    month: int,
    symbol: str = DEFAULT_SYMBOL,
    root: Path = MONTHLY_ROOT,
) -> int:
    if not records:
        return 0
    root.mkdir(parents=True, exist_ok=True)
    db_path = month_db_path(year, month, root)
    conn = duckdb.connect(str(db_path))
    try:
        ensure_ticks_schema(conn)
        conn.execute("BEGIN TRANSACTION")
        conn.execute(
            "DELETE FROM ticks WHERE symbol=? AND time>=? AND time<?",
            [symbol, float(base_ts), float(base_ts + 3600)],
        )
        df = pd.DataFrame(records, columns=["symbol", "time", "bid", "ask", "last", "volume"])
        conn.execute("INSERT INTO ticks SELECT * FROM df")
        conn.execute("COMMIT")
        return len(records)
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def latest_tick_ts(root: Path = MONTHLY_ROOT, fallback_db: Path | None = Path("data/ticks.duckdb")) -> float | None:
    latest: float | None = None
    for db_path in iter_month_db_paths(root):
        try:
            conn = duckdb.connect(str(db_path), read_only=True)
            row = conn.execute("SELECT MAX(time) FROM ticks").fetchone()
            conn.close()
            if row and row[0] is not None:
                latest = max(float(row[0]), latest or 0.0)
        except Exception:
            continue
    if latest is not None:
        return latest
    if fallback_db and fallback_db.exists():
        try:
            conn = duckdb.connect(str(fallback_db), read_only=True)
            row = conn.execute("SELECT MAX(time) FROM ticks").fetchone()
            conn.close()
            if row and row[0] is not None:
                return float(row[0])
        except Exception:
            return None
    return None


def monthly_summary(root: Path = MONTHLY_ROOT) -> tuple[int, float | None]:
    total = 0
    latest: float | None = None
    for db_path in iter_month_db_paths(root):
        try:
            conn = duckdb.connect(str(db_path), read_only=True)
            count, max_ts = conn.execute("SELECT COUNT(*), MAX(time) FROM ticks").fetchone()
            conn.close()
        except Exception:
            continue
        total += int(count or 0)
        if max_ts is not None:
            latest = max(float(max_ts), latest or 0.0)
    return total, latest


def refresh_legacy_ticks_pointer(
    latest_ts: float | None,
    *,
    root: Path = MONTHLY_ROOT,
    legacy_db: Path = Path("data/ticks.duckdb"),
) -> bool:
    """Point data/ticks.duckdb at the latest monthly DB on Linux servers.

    The function only updates an existing symlink or creates a missing one. It
    deliberately leaves a regular ticks.duckdb file untouched.
    """
    if latest_ts is None or os.name == "nt":
        return False
    latest_dt = datetime.fromtimestamp(float(latest_ts), tz=timezone.utc)
    target = month_db_path(latest_dt.year, latest_dt.month, root)
    if not target.exists():
        return False
    if legacy_db.exists() or legacy_db.is_symlink():
        if not legacy_db.is_symlink():
            return False
        legacy_db.unlink()
    legacy_db.parent.mkdir(parents=True, exist_ok=True)
    relative_target = Path("ticks_monthly") / target.name
    legacy_db.symlink_to(relative_target)
    return True
