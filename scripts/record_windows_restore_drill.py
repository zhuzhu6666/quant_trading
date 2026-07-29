#!/usr/bin/env python3
"""Record an explicitly successful isolated restore drill reported by Windows.

The Windows client may call this only after ``verify_state_restore.py`` exits
successfully against an isolated restored database.  It stores no credentials,
does not restore or promote a database, and labels the result as client
reported rather than pretending the server inspected the offline host.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.services.postgres_backup_health import PostgresBackupHealthService


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verified-at", required=True, type=float)
    args = parser.parse_args()
    if args.verified_at <= 0 or args.verified_at > time.time() + 900.0:
        raise SystemExit("invalid restore verification timestamp")
    receipt = PostgresBackupHealthService().record_restore_drill(
        {
            "ok": True,
            "verification_source": "windows_client_reported",
            "state_schema": {"status": "healthy"},
            "table_counts": {"status": "observed_only"},
            "memory_integrity": {"status": "healthy"},
            "source_parity": "not_claimed_for_offline_logical_snapshot",
            "verified_at": args.verified_at,
        }
    )
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
