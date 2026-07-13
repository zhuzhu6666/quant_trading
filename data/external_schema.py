"""External data schema helpers.

Keeps external research data auditable and point-in-time safe.  Raw source
tables keep their original columns, with release/fetch metadata added for
feature alignment and health reporting.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from backend.core.db import DUCKDB_EXTERNAL, connect_duckdb, duckdb_readonly_connection

PARSER_VERSION = "external_schema_v1"
REFRESH_AUDIT_STALE_AFTER_SEC = 15 * 60


def utc_epoch_for_date(date_str: str, *, hour: int = 0, minute: int = 0) -> float:
    dt = datetime.strptime(str(date_str)[:10], "%Y-%m-%d").replace(
        hour=hour,
        minute=minute,
        tzinfo=timezone.utc,
    )
    return dt.timestamp()


def cot_release_at(report_date: str) -> float:
    """Conservative COT availability: Tuesday report becomes usable Friday UTC."""
    dt = datetime.strptime(str(report_date)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return (dt + timedelta(days=3, hours=21, minutes=30)).timestamp()


def etf_release_at(filing_date: str | None, holding_date: str) -> float:
    """Use SEC filing date when known; fall back to 45 days after month end."""
    if filing_date:
        return utc_epoch_for_date(filing_date, hour=21, minute=30)
    dt = datetime.strptime(str(holding_date)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return (dt + timedelta(days=45, hours=21, minutes=30)).timestamp()


def macro_release_at(date_str: str) -> float:
    """Daily FRED series are usable from the following UTC day in backtests."""
    dt = datetime.strptime(str(date_str)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return (dt + timedelta(days=1)).timestamp()


def cb_release_at(date_str: str) -> float:
    """Conservative availability time for quarterly central-bank data.

    The World Gold Council publishes the quarterly series after the quarter
    closes.  A 45-day lag keeps live/backtest joins point-in-time safe when
    the source only exposes the observation quarter-end date.
    """
    dt = datetime.strptime(str(date_str)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return (dt + timedelta(days=45, hours=21, minutes=30)).timestamp()


def ensure_external_schema(db_path: str | Path = DUCKDB_EXTERNAL) -> None:
    conn = connect_duckdb(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS external_refresh_audit (
                run_id VARCHAR PRIMARY KEY,
                source VARCHAR NOT NULL,
                started_at DOUBLE NOT NULL,
                finished_at DOUBLE,
                status VARCHAR NOT NULL,
                rows INTEGER DEFAULT 0,
                latest_date VARCHAR,
                latest_release_at DOUBLE,
                error VARCHAR
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS external_raw_metadata (
                id VARCHAR PRIMARY KEY,
                source VARCHAR NOT NULL,
                source_url VARCHAR,
                raw_path VARCHAR NOT NULL,
                sha256 VARCHAR,
                fetched_at DOUBLE NOT NULL,
                parser_version VARCHAR
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS macro_daily (
                series VARCHAR NOT NULL,
                date VARCHAR NOT NULL,
                value DOUBLE,
                release_at DOUBLE DEFAULT 0,
                fetched_at DOUBLE DEFAULT 0,
                source VARCHAR DEFAULT 'unknown',
                PRIMARY KEY (series, date)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS etf_holdings (
                symbol VARCHAR NOT NULL,
                date VARCHAR NOT NULL,
                total_tonnes DOUBLE,
                total_shares DOUBLE,
                aum_usd DOUBLE,
                release_at DOUBLE DEFAULT 0,
                fetched_at DOUBLE DEFAULT 0,
                source VARCHAR DEFAULT 'unknown',
                PRIMARY KEY (symbol, date)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cb_gold (
                country VARCHAR NOT NULL,
                date VARCHAR NOT NULL,
                total_tonnes DOUBLE,
                monthly_chg_tonnes DOUBLE,
                release_at DOUBLE DEFAULT 0,
                fetched_at DOUBLE DEFAULT 0,
                source VARCHAR DEFAULT 'unknown',
                PRIMARY KEY (country, date)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cot_gold (
                report_date VARCHAR NOT NULL,
                open_interest BIGINT,
                mm_long BIGINT, mm_short BIGINT, mm_spread BIGINT,
                pm_long BIGINT, pm_short BIGINT,
                swap_long BIGINT, swap_short BIGINT,
                other_long BIGINT, other_short BIGINT,
                release_at DOUBLE DEFAULT 0,
                fetched_at DOUBLE DEFAULT 0,
                source VARCHAR DEFAULT 'unknown',
                PRIMARY KEY (report_date)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS etf_daily (
                symbol VARCHAR NOT NULL,
                date VARCHAR NOT NULL,
                close DOUBLE,
                release_at DOUBLE DEFAULT 0,
                fetched_at DOUBLE DEFAULT 0,
                source VARCHAR DEFAULT 'unknown',
                PRIMARY KEY (symbol, date)
            )
            """
        )
        conn.execute("ALTER TABLE macro_daily ADD COLUMN IF NOT EXISTS release_at DOUBLE DEFAULT 0")
        conn.execute("ALTER TABLE macro_daily ADD COLUMN IF NOT EXISTS fetched_at DOUBLE DEFAULT 0")
        conn.execute("ALTER TABLE macro_daily ADD COLUMN IF NOT EXISTS source VARCHAR DEFAULT 'unknown'")

        for table in ("cot_gold", "etf_holdings", "cb_gold", "etf_daily"):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS release_at DOUBLE DEFAULT 0")
            conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS fetched_at DOUBLE DEFAULT 0")
            conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS source VARCHAR DEFAULT 'unknown'")

        now = time.time()
        conn.execute(
            """
            UPDATE cot_gold
            SET release_at = CASE WHEN COALESCE(release_at, 0) <= 0 THEN ? ELSE release_at END,
                fetched_at = CASE WHEN COALESCE(fetched_at, 0) <= 0 THEN ? ELSE fetched_at END,
                source = CASE WHEN COALESCE(source, '') IN ('', 'unknown') THEN 'cftc' ELSE source END
            WHERE report_date IS NOT NULL
            """,
            [0.0, now],
        )
        cot_rows = conn.execute("SELECT report_date FROM cot_gold WHERE COALESCE(release_at, 0) <= 0").fetchall()
        for (report_date,) in cot_rows:
            conn.execute(
                "UPDATE cot_gold SET release_at=? WHERE report_date=?",
                [cot_release_at(str(report_date)), report_date],
            )

        etf_rows = conn.execute("SELECT symbol, date FROM etf_holdings WHERE COALESCE(release_at, 0) <= 0").fetchall()
        for symbol, date in etf_rows:
            conn.execute(
                "UPDATE etf_holdings SET release_at=?, fetched_at=CASE WHEN COALESCE(fetched_at,0)<=0 THEN ? ELSE fetched_at END, source=CASE WHEN COALESCE(source,'') IN ('','unknown') THEN 'sec_edgar' ELSE source END WHERE symbol=? AND date=?",
                [etf_release_at(None, str(date)), now, symbol, date],
            )
    finally:
        conn.close()


def start_refresh_audit(source: str, db_path: str | Path = DUCKDB_EXTERNAL) -> str:
    ensure_external_schema(db_path)
    run_id = f"{source}_{uuid.uuid4().hex[:16]}"
    conn = connect_duckdb(db_path)
    try:
        # A process can be killed before its finally block runs.  The global
        # refresh lock prevents a live overlap, so an older same-source row is
        # safe to close when a new run starts after a short grace period.
        now = time.time()
        conn.execute(
            """
            UPDATE external_refresh_audit
            SET finished_at=?, status='abandoned',
                error=COALESCE(error, 'superseded stale refresh subprocess')
            WHERE source=? AND status='running' AND started_at < ?
            """,
            [now, source, now - 60.0],
        )
        conn.execute(
            "INSERT INTO external_refresh_audit (run_id, source, started_at, status) VALUES (?, ?, ?, 'running')",
            [run_id, source, now],
        )
    finally:
        conn.close()
    return run_id


def finish_refresh_audit(
    run_id: str,
    *,
    status: str,
    rows: int = 0,
    latest_date: str | None = None,
    latest_release_at: float | None = None,
    error: str | None = None,
    db_path: str | Path = DUCKDB_EXTERNAL,
) -> None:
    conn = connect_duckdb(db_path)
    try:
        conn.execute(
            """
            UPDATE external_refresh_audit
            SET finished_at=?, status=?, rows=?, latest_date=?, latest_release_at=?, error=?
            WHERE run_id=?
            """,
            [time.time(), status, int(rows or 0), latest_date, latest_release_at, error, run_id],
        )
    finally:
        conn.close()


def reconcile_stale_refresh_audits(
    db_path: str | Path = DUCKDB_EXTERNAL,
    *,
    stale_after_sec: float = REFRESH_AUDIT_STALE_AFTER_SEC,
) -> int:
    """Close refresh rows left running by a killed or timed-out subprocess."""
    if not Path(db_path).exists():
        return 0
    ensure_external_schema(db_path)
    now = time.time()
    cutoff = now - max(60.0, float(stale_after_sec))
    conn = connect_duckdb(db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM external_refresh_audit WHERE status='running' AND started_at < ?",
            [cutoff],
        ).fetchone()
        stale_count = int(count[0] if count else 0)
        conn.execute(
            """
            UPDATE external_refresh_audit
            SET finished_at=?, status='abandoned',
                error=COALESCE(error, 'refresh subprocess exited or timed out')
            WHERE status='running' AND started_at < ?
            """,
            [now, cutoff],
        )
        return stale_count
    finally:
        conn.close()


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def record_raw_file(
    source: str,
    raw_path: str | Path,
    *,
    source_url: str | None = None,
    fetched_at: float | None = None,
    parser_version: str = PARSER_VERSION,
    db_path: str | Path = DUCKDB_EXTERNAL,
) -> None:
    ensure_external_schema(db_path)
    path = Path(raw_path)
    conn = connect_duckdb(db_path)
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO external_raw_metadata
            (id, source, source_url, raw_path, sha256, fetched_at, parser_version)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                f"{source}:{path.as_posix()}",
                source,
                source_url,
                path.as_posix(),
                sha256_file(path) if path.exists() else None,
                float(fetched_at or time.time()),
                parser_version,
            ],
        )
    finally:
        conn.close()


def latest_audit_by_source(db_path: str | Path = DUCKDB_EXTERNAL) -> dict[str, dict[str, Any]]:
    if not Path(db_path).exists():
        return {}
    try:
        with duckdb_readonly_connection(db_path, snapshot_first=True) as conn:
            rows = conn.execute(
                """
                SELECT source, started_at, finished_at, status, rows, latest_date, latest_release_at, error
                FROM (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY source
                        ORDER BY
                            CASE
                                WHEN status='running' AND started_at >= ? THEN 3
                                WHEN status IN ('success', 'skipped') THEN 2
                                WHEN status NOT IN ('running', 'abandoned') THEN 1
                                ELSE 0
                            END DESC,
                            started_at DESC
                    ) rn
                    FROM external_refresh_audit
                )
                WHERE rn=1
                """,
                [time.time() - REFRESH_AUDIT_STALE_AFTER_SEC],
            ).fetchall()
    except Exception:
        return {}
    return {
        str(r[0]): {
            "source": r[0],
            "started_at": r[1],
            "finished_at": r[2],
            "status": r[3],
            "rows": r[4],
            "latest_date": r[5],
            "latest_release_at": r[6],
            "error": r[7],
        }
        for r in rows
    }
