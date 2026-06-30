"""Move external research tables out of the legacy ctrader_data.duckdb file.

K-line bars now live in monthly databases. The remaining non-price research
tables belong in data/external_data.duckdb, while ctrader_data.duckdb stays as
a cold compatibility source.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.core.db import DUCKDB_BARS_LEGACY, DUCKDB_EXTERNAL, connect_duckdb
from data.store import DataStore

EXTERNAL_TABLES = (
    "cot_gold",
    "etf_holdings",
    "cb_gold",
    "macro_daily",
    "etf_daily",
)


def _table_names(path: Path) -> set[str]:
    if not path.exists():
        return set()
    con = connect_duckdb(path, read_only=True)
    try:
        return {row[0] for row in con.execute("SHOW TABLES").fetchall()}
    finally:
        con.close()


def migrate(
    *,
    source: Path = DUCKDB_BARS_LEGACY,
    target: Path = DUCKDB_EXTERNAL,
    dry_run: bool = False,
) -> dict:
    if not source.exists():
        raise FileNotFoundError(f"source not found: {source}")

    # Initialize target external schema using the normal store path only when
    # applying. Dry-run must not create a new database file.
    if not dry_run:
        DataStore(str(target))

    source_tables = _table_names(source)
    src = connect_duckdb(source, read_only=True)
    dst = connect_duckdb(target, read_only=dry_run) if target.exists() else None
    try:
        result: dict[str, dict] = {}
        for table in EXTERNAL_TABLES:
            if table not in source_tables:
                result[table] = {"source_rows": 0, "target_rows_before": 0, "copied": 0, "status": "missing_source"}
                continue
            source_rows = int(src.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            target_rows_before = 0
            if dst is not None:
                try:
                    target_rows_before = int(dst.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                except Exception:
                    target_rows_before = 0
            copied = 0
            if source_rows and not dry_run:
                df = src.execute(f'SELECT * FROM "{table}"').df()
                if dst is None:
                    dst = connect_duckdb(target)
                dst.register("_external_migrate_df", df)
                try:
                    dst.execute(f'INSERT OR REPLACE INTO "{table}" SELECT * FROM _external_migrate_df')
                finally:
                    dst.unregister("_external_migrate_df")
                copied = len(df)
            target_rows_after = target_rows_before
            if dst is not None:
                try:
                    target_rows_after = int(dst.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                except Exception:
                    target_rows_after = target_rows_before
            result[table] = {
                "source_rows": source_rows,
                "target_rows_before": target_rows_before,
                "target_rows_after": target_rows_after,
                "copied": copied,
                "status": "dry_run" if dry_run else "ok",
            }
        return result
    finally:
        src.close()
        if dst is not None:
            dst.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write external_data.duckdb")
    parser.add_argument("--source", type=Path, default=DUCKDB_BARS_LEGACY)
    parser.add_argument("--target", type=Path, default=DUCKDB_EXTERNAL)
    args = parser.parse_args()

    result = migrate(source=args.source, target=args.target, dry_run=not args.apply)
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"External data migration {mode}")
    for table, stats in result.items():
        print(
            f"{table}: source={stats['source_rows']} "
            f"target_before={stats['target_rows_before']} "
            f"target_after={stats.get('target_rows_after', 0)} "
            f"copied={stats['copied']} status={stats['status']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
