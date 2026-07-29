#!/usr/bin/env python3
"""Record a completed Windows logical-backup receipt.

This is invoked only by the root-owned SSH bridge after the Windows client has
validated its local custom-format archive. It never receives credentials,
writes backup bytes, or changes PostgreSQL archive settings.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.services.postgres_backup_health import PostgresBackupHealthService


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--completed-at", required=True, type=float)
    parser.add_argument("--byte-count", required=True, type=int)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args()
    receipt = PostgresBackupHealthService().record_windows_pull(
        completed_at=args.completed_at,
        byte_count=args.byte_count,
        sha256=args.sha256,
    )
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
