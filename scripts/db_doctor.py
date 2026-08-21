"""数据库体检脚本.

检查:
  1. 文件角色是否和预期数据库引擎匹配
  2. DuckDB / SQLite 是否可按正确驱动打开
  3. 关键 schema 是否存在 (避免 lots/volume 这类历史漂移)

用法:
    .venv/bin/python scripts/db_doctor.py
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.core.db import (  # noqa: E402
    DUCKDB_BARS,
    DUCKDB_BARS_LEGACY,
    DUCKDB_EXTERNAL,
    DUCKDB_EVENTS,
    DUCKDB_TRADES,
    EXPERIMENTS_DB,
    connect_duckdb,
    connect_sqlite,
    get_state_pg_conn,
    init_experiments_db,
    init_state_db,
    state_table_columns,
)
from alpha.attribution_engine import _ensure_trades_duckdb_schema  # noqa: E402


def _check_duckdb(path: Path, required: dict[str, set[str]]) -> list[str]:
    problems: list[str] = []
    tmp_dir: Path | None = None
    if not path.exists():
        return [f"missing file: {path.name}"]
    try:
        con = connect_duckdb(path, read_only=True)
    except Exception as exc:
        if "Could not set lock" not in str(exc):
            return [f"open failed via DuckDB: {exc}"]
        tmp_dir = Path(tempfile.mkdtemp(prefix="db_doctor_"))
        snapshot = tmp_dir / path.name
        try:
            shutil.copy2(path, snapshot)
            con = connect_duckdb(snapshot, read_only=True)
        except Exception as snap_exc:
            return [f"open failed via DuckDB: {snap_exc}"]
    try:
        tables = {row[0] for row in con.execute("SHOW TABLES").fetchall()}
        for table, cols in required.items():
            if table not in tables:
                problems.append(f"missing table {table}")
                continue
            actual = {row[1] for row in con.execute(f"PRAGMA table_info('{table}')").fetchall()}
            missing = sorted(cols - actual)
            if missing:
                problems.append(f"{table} missing columns: {', '.join(missing)}")
    finally:
        con.close()
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    return problems


def _check_sqlite(path: Path, required: dict[str, set[str]]) -> list[str]:
    problems: list[str] = []
    if not path.exists():
        return [f"missing file: {path.name}"]
    try:
        con = connect_sqlite(path, read_only=True)
    except Exception as exc:
        return [f"open failed via SQLite: {exc}"]
    try:
        cur = con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cur.fetchall()}
        for table, cols in required.items():
            if table not in tables:
                problems.append(f"missing table {table}")
                continue
            actual = {row[1] for row in con.execute(f"PRAGMA table_info('{table}')").fetchall()}
            missing = sorted(cols - actual)
            if missing:
                problems.append(f"{table} missing columns: {', '.join(missing)}")
    finally:
        con.close()
    return problems


def _check_postgres_state(required: dict[str, set[str]]) -> list[str]:
    """Check the canonical runtime state schema through PostgreSQL only."""
    problems: list[str] = []
    try:
        con = get_state_pg_conn(read_only=True)
    except Exception as exc:
        return [f"connect failed via PostgreSQL: {exc}"]
    try:
        for table, expected_columns in required.items():
            try:
                actual = set(state_table_columns(con, table))
            except Exception as exc:
                problems.append(f"{table} schema lookup failed: {exc}")
                continue
            if not actual:
                problems.append(f"missing table {table}")
                continue
            missing = sorted(expected_columns - actual)
            if missing:
                problems.append(f"{table} missing columns: {', '.join(missing)}")
    finally:
        con.close()
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="数据库体检")
    parser.add_argument("--repair", action="store_true", help="先执行标准修复，再体检")
    args = parser.parse_args()

    if args.repair:
        init_state_db()
        init_experiments_db()
        _ensure_trades_duckdb_schema()

    checks: list[tuple[str, list[str]]] = [
        (
            DUCKDB_BARS.name,
            _check_duckdb(
                DUCKDB_BARS,
                {
                    "bars": {"symbol", "timeframe", "time", "open", "high", "low", "close", "volume", "spread"},
                },
            ),
        ),
        (
            DUCKDB_EXTERNAL.name,
            _check_duckdb(
                DUCKDB_EXTERNAL,
                {
                    "cot_gold": {"report_date", "release_at", "fetched_at", "source"},
                    "etf_holdings": {"symbol", "date", "release_at", "fetched_at", "source"},
                    "macro_daily": {"series", "date", "value", "release_at", "fetched_at", "source"},
                    "external_refresh_audit": {"source", "started_at", "status"},
                    "external_raw_metadata": {"source", "raw_path", "sha256", "fetched_at"},
                },
            ),
        ),
        (
            DUCKDB_BARS_LEGACY.name,
            _check_duckdb(DUCKDB_BARS_LEGACY, {}),
        ),
        (
            DUCKDB_TRADES.name,
            _check_duckdb(
                DUCKDB_TRADES,
                {
                    "trades": {"position_id", "direction", "volume", "open_ts", "open_price", "status"},
                    "trade_executions": {"trade_id", "exec_ts", "exec_type", "price", "volume"},
                    "trade_factor_attributions": {"trade_id", "factor_name", "marginal_contribution"},
                },
            ),
        ),
        (
            DUCKDB_EVENTS.name,
            _check_duckdb(DUCKDB_EVENTS, {"events": {"date", "type", "importance"}}),
        ),
        (
            "runtime (PostgreSQL)",
            _check_postgres_state(
                {
                    "ctrader_deals": {"deal_id", "position_id"},
                },
            ),
        ),
        (
            EXPERIMENTS_DB.name,
            _check_sqlite(EXPERIMENTS_DB, {"experiments": {"run_id", "status"}}),
        ),
    ]

    has_error = False
    print("Database doctor")
    print("=" * 60)
    for name, problems in checks:
        if problems:
            has_error = True
            print(f"[FAIL] {name}")
            for item in problems:
                print(f"  - {item}")
        else:
            print(f"[ OK ] {name}")
    return 1 if has_error else 0


if __name__ == "__main__":
    raise SystemExit(main())

