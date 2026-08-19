#!/usr/bin/env python3
"""Read-only capacity/growth observer for the runtime PostgreSQL state store.

P3 decision (2026-08-19): archival/partitioning DESIGN is deferred until real
DB growth increment is observed. This tool is the observation instrument: it
snapshots every runtime + canonical_v2 table's live row count and total size
and APPENDS a timestamped block to run_artifacts/capacity/observations.tsv so
the growth increment can be diffed over time.

Constraints honored:
- READ-ONLY against PostgreSQL (SELECT only; same guard as scripts/state_query.py).
- Memory/learning data is NEVER an archive candidate (user rule: 记忆不可归档).
  This tool only *measures*; it never deletes/archives/detaches anything.
- No product tables/services/threads added; the only side effect is a file append.

Usage:
    .venv/bin/python scripts/capacity_observe.py            # append + print digest
    .venv/bin/python scripts/capacity_observe.py --dry      # print only, no append
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.core.db import get_state_pg_conn, state_backend  # noqa: E402

OBSERVATIONS_DIR = PROJECT_ROOT / "run_artifacts" / "capacity"
OBSERVATIONS_FILE = OBSERVATIONS_DIR / "observations.tsv"

# Headline growth tables shown in the one-line digest (kept small on purpose).
DIGEST_TABLES = (
    "canonical_v2.event",
    "canonical_v2.payload_blob",
    "canonical_v2.training_sample_row",
    "runtime.brain_state_snapshot",
    "runtime.brain_action_plan_eval",
    "runtime.brain_action_plan_eval_payload",
    "runtime.brain_memory",
    "runtime.evolution_decision",
    "runtime.evolution_events",
    "runtime.learning_application_log",
    "runtime.learning_application_effect",
)


def _size_sql() -> str:
    return (
        "SELECT n.nspname AS schema_name, c.relname AS table_name, "
        "COALESCE(s.n_live_tup, 0) AS row_count, "
        "pg_total_relation_size(c.oid) AS size_bytes "
        "FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid "
        "WHERE n.nspname IN ('runtime', 'canonical_v2') AND c.relkind = 'r' "
        "ORDER BY n.nspname, c.relname"
    )


def _read_previous_observations() -> dict[str, int] | None:
    """Return {table: size_bytes} of the most recent observation block, or None.

    Only used by --json to compute growth deltas. Never deletes/rewrites the log.
    """
    if not OBSERVATIONS_FILE.exists():
        return None
    lines = OBSERVATIONS_FILE.read_text(encoding="utf-8").splitlines()
    ts = ""
    previous: dict[str, int] = {}
    for line in lines:
        line = line.strip()
        if line.startswith("# "):
            ts = line[2:]
            previous = {}
            continue
        parts = line.split("\t")
        if len(parts) >= 4 and parts[0] == ts and parts[1] == "schema.table":
            continue
        if len(parts) >= 4 and parts[0] == ts:
            previous[parts[1]] = int(parts[3] or 0)
    return previous if previous else None


def _json_snapshot(rows: list[dict]) -> dict:
    now = datetime.now(timezone.utc)
    previous = _read_previous_observations()
    total_rows = 0
    total_bytes = 0
    tables: list[dict] = []
    for row in sorted(rows, key=lambda r: f"{r['schema_name']}.{r['table_name']}"):
        table = f"{row['schema_name']}.{row['table_name']}"
        row_count = int(row["row_count"] or 0)
        size_bytes = int(row["size_bytes"] or 0)
        total_rows += row_count
        total_bytes += size_bytes
        prev_size = (previous or {}).get(table)
        delta_h = None
        delta_d = None
        if prev_size is not None:
            delta_h = (size_bytes - prev_size) / max(1.0, 24.0)  # 估算小时增速(bytes)
            delta_d = delta_h * 24.0
        tables.append({
            "table": table,
            "rows": row_count,
            "size_bytes": size_bytes,
            "size": _humanize(size_bytes),
            "delta_bytes_per_hour": delta_h,
            "delta_bytes_per_day": delta_d,
        })
    # 最老观测时间(算总增速)
    oldest_ts: str | None = None
    if previous:
        try:
            with open(OBSERVATIONS_FILE, encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("# "):
                        oldest_ts = line[2:].strip()
                        break
            if oldest_ts:
                start = datetime.fromisoformat(oldest_ts)
                span_hours = max(1.0, (now - start).total_seconds() / 3600.0)
            else:
                span_hours = None
        except Exception:
            span_hours = None
    else:
        span_hours = None
    return {
        "observed_at": now.replace(microsecond=0).isoformat(),
        "total_rows": total_rows,
        "total_bytes": total_bytes,
        "total_size": _humanize(total_bytes),
        "tables": tables,
        "spec": {
            "note": "read-only capacity snapshot; deltas approximate growth rate vs last observation",
            "archive_never": "memory/learning tables are never archive candidates (user rule)",
        },
    }


def _json_trend(rows: list[dict]) -> dict:
    """Compact trend view for the system_health dashboard."""
    snap = _json_snapshot(rows)
    top = sorted(
        (t for t in snap["tables"] if (t.get("delta_bytes_per_hour") or 0) > 0),
        key=lambda t: -(t["delta_bytes_per_hour"] or 0),
    )[:10]
    return {
        "observed_at": snap["observed_at"],
        "total_size": snap["total_size"],
        "total_rows": snap["total_rows"],
        "growing": [
            {
                "table": t["table"],
                "size": t["size"],
                "grow_delta_bytes": t["delta_bytes_per_day"] or 0,
            }
            for t in top
        ],
    }


def _humanize(size_bytes: int) -> str:
    value = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{int(size_bytes)}B"


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only capacity observer")
    parser.add_argument("--dry", action="store_true", help="print only, do not append to the log")
    parser.add_argument("--json", action="store_true", help="print JSON snapshot to stdout and exit")
    parser.add_argument("--trend", action="store_true", help="print compact growth trend JSON to stdout and exit")
    args = parser.parse_args()

    if state_backend() != "postgres":
        raise SystemExit(f"runtime state backend is not PostgreSQL: {state_backend()}")

    conn = get_state_pg_conn(read_only=True)
    try:
        rows = [dict(row) for row in conn.execute(_size_sql()).fetchall()]
    finally:
        conn.rollback()
        conn.close()

    if args.json:
        import json as _json
        print(_json.dumps(_json_snapshot(rows), ensure_ascii=False, sort_keys=True))
        return 0
    if args.trend:
        import json as _json
        print(_json.dumps(_json_trend(rows), ensure_ascii=False, sort_keys=True))
        return 0

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    lines = [f"# {now}", "ts\tschema.table\trows\tsize_bytes"]
    total_rows = 0
    total_bytes = 0
    for row in sorted(rows, key=lambda r: f"{r['schema_name']}.{r['table_name']}"):
        table = f"{row['schema_name']}.{row['table_name']}"
        row_count = int(row["row_count"] or 0)
        size_bytes = int(row["size_bytes"] or 0)
        total_rows += row_count
        total_bytes += size_bytes
        lines.append(f"{now}\t{table}\t{row_count}\t{size_bytes}")
    lines.append(f"{now}\t__TOTAL__\t{total_rows}\t{total_bytes}")

    digest = " | ".join(
        f"{t}={_humanize(int(r['size_bytes'] or 0))}" for t in DIGEST_TABLES for r in rows
        if f"{r['schema_name']}.{r['table_name']}" == t
    )
    print(f"[{now}] total {total_rows} rows / {_humanize(total_bytes)}")
    if digest:
        print(f"  headline: {digest}")

    if args.dry:
        return 0
    OBSERVATIONS_DIR.mkdir(parents=True, exist_ok=True)
    exists = OBSERVATIONS_FILE.exists()
    with open(OBSERVATIONS_FILE, "a", encoding="utf-8") as handle:
        if not exists:
            handle.write("ts\tschema.table\trows\tsize_bytes\n")
        handle.write("\n".join(lines[1:]) + "\n")
    print(f"appended -> {OBSERVATIONS_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
