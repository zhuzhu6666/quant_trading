#!/usr/bin/env python3
"""Run a pgBackRest backup/check and publish a sanitized health observation.

This script is designed for the versioned systemd templates under
``deployment/``.  It does not configure PostgreSQL, create a stanza, or
restore data.  Those are explicit operator actions because they require a
real object-store bucket and PostgreSQL host paths.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.core.db import get_state_pg_conn
from backend.services.postgres_backup_health import PostgresBackupHealthService


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _latest_backup(info: Any, stanza: str) -> dict[str, Any]:
    """Extract stable, non-secret summary fields from pgBackRest JSON output."""
    entries = info if isinstance(info, list) else [info]
    entry = next(
        (
            item for item in entries
            if isinstance(item, dict) and str(item.get("name") or "") == stanza
        ),
        next((item for item in entries if isinstance(item, dict)), {}),
    )
    backups = entry.get("backup") if isinstance(entry, dict) else []
    if not isinstance(backups, list):
        backups = []
    latest = max(
        (item for item in backups if isinstance(item, dict)),
        key=lambda item: _safe_float((item.get("timestamp") or {}).get("stop")),
        default={},
    )
    timestamp = latest.get("timestamp") if isinstance(latest.get("timestamp"), dict) else {}
    status_value = entry.get("status") if isinstance(entry, dict) else {}
    if isinstance(status_value, dict):
        stanza_ok = int(status_value.get("code") or 0) == 0
        stanza_status = str(status_value.get("message") or "ok")
    else:
        stanza_ok = str(status_value or "ok").lower() == "ok"
        stanza_status = str(status_value or "ok")
    archive = entry.get("archive") if isinstance(entry, dict) else []
    if not isinstance(archive, list):
        archive = []
    return {
        "stanza_status": stanza_status,
        "stanza_ok": stanza_ok,
        "backup_total": len(backups),
        "latest_backup": {
            "label": str(latest.get("label") or ""),
            "type": str(latest.get("type") or ""),
            "started_at": _safe_float(timestamp.get("start")),
            "completed_at": _safe_float(timestamp.get("stop")),
        },
        "archive": {
            "repository_ranges": [
                {
                    "min": str(item.get("min") or ""),
                    "max": str(item.get("max") or ""),
                }
                for item in archive
                if isinstance(item, dict)
            ],
        },
    }


def _archive_runtime_status() -> dict[str, Any]:
    """Read PostgreSQL's archive view; an unavailable read is never hidden."""
    try:
        conn = get_state_pg_conn(read_only=True)
        try:
            row = conn.execute(
                """
                SELECT current_setting('archive_mode', true) AS archive_mode,
                       last_archived_wal,
                       EXTRACT(EPOCH FROM last_archived_time) AS last_archived_at,
                       EXTRACT(EPOCH FROM clock_timestamp() - last_archived_time) AS archive_lag_seconds,
                       failed_count,
                       EXTRACT(EPOCH FROM last_failed_time) AS last_failed_at
                FROM pg_stat_archiver
                """
            ).fetchone()
        finally:
            conn.close()
    except Exception as exc:
        return {
            "status": "unavailable",
            "error": f"{type(exc).__name__}: {exc}",
        }
    item = dict(row or {})
    archive_mode = str(item.get("archive_mode") or "off")
    last_archived_at = _safe_float(item.get("last_archived_at"))
    return {
        "status": "available" if archive_mode in {"on", "always"} and last_archived_at else "degraded",
        "archive_mode": archive_mode,
        "last_archived_wal": str(item.get("last_archived_wal") or ""),
        "last_archived_at": last_archived_at,
        "archive_lag_seconds": _safe_float(item.get("archive_lag_seconds")),
        "failed_count": int(item.get("failed_count") or 0),
        "last_failed_at": _safe_float(item.get("last_failed_at")),
    }


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def _info(pgbackrest_bin: str, stanza: str) -> Any:
    result = subprocess.run(
        [pgbackrest_bin, f"--stanza={stanza}", "--output=json", "info"],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def build_observation(*, pgbackrest_info: Any, stanza: str, command: str, archive_status: dict[str, Any]) -> dict[str, Any]:
    backup = _latest_backup(pgbackrest_info, stanza)
    healthy = (
        backup["stanza_ok"]
        and bool(backup["latest_backup"]["label"])
        and archive_status.get("status") == "available"
    )
    return {
        "ok": healthy,
        "schema_version": "postgres_backup_health.v1",
        "status": "healthy" if healthy else "degraded",
        "stanza": stanza,
        "last_command": command,
        "backup": backup,
        "postgres_archive": archive_status,
        "observed_at": time.time(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stanza", default=os.environ.get("PGBACKREST_STANZA", "quant-state-v1"))
    parser.add_argument("--type", choices=("full", "diff", "check", "report"), default="diff")
    parser.add_argument("--pgbackrest-bin", default=os.environ.get("PGBACKREST_BIN", "pgbackrest"))
    parser.add_argument("--no-publish", action="store_true")
    args = parser.parse_args()

    pgbackrest_bin = shutil.which(args.pgbackrest_bin) or args.pgbackrest_bin
    observation: dict[str, Any]
    exit_code = 0
    try:
        if args.type != "report":
            _run([pgbackrest_bin, f"--stanza={args.stanza}", "check"])
        if args.type in {"full", "diff"}:
            _run([pgbackrest_bin, f"--stanza={args.stanza}", f"--type={args.type}", "backup"])
        observation = build_observation(
            pgbackrest_info=_info(pgbackrest_bin, args.stanza),
            stanza=args.stanza,
            command=args.type,
            archive_status=_archive_runtime_status(),
        )
    except Exception as exc:
        exit_code = 2
        observation = {
            "ok": False,
            "schema_version": "postgres_backup_health.v1",
            "status": "unavailable",
            "stanza": args.stanza,
            "last_command": args.type,
            "errors": [f"{type(exc).__name__}: {exc}"],
            "postgres_archive": _archive_runtime_status(),
            "observed_at": time.time(),
        }

    if not args.no_publish:
        try:
            observation = PostgresBackupHealthService().publish(observation)
        except Exception as exc:
            print(json.dumps({"ok": False, "publish_error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
            return 3
    print(json.dumps(observation, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
