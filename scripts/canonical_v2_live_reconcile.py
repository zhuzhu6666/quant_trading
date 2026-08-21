#!/usr/bin/env python3
"""Read-only live integrity check for the runtime/canonical authority boundary.

``canonical_v2`` is the immutable fact source and ``runtime`` is the mutable
operational source.  They are intentionally not row-for-row mirrors.  This
command checks both catalogs plus canonical payload/relation integrity within
the requested observation window; it never reads a deleted source or creates
a fallback comparison path.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.core.db import get_state_pg_conn  # noqa: E402
from scripts.verify_state_pg_parity import build_report  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since-epoch",
        type=float,
        default=time.time() - 6 * 3600,
        help="Count canonical events observed after this epoch (default: six hours).",
    )
    args = parser.parse_args(argv)

    conn = get_state_pg_conn(read_only=True)
    try:
        report = build_report(
            conn,
            since_epoch=args.since_epoch,
        )
    finally:
        conn.rollback()
        conn.close()

    report["schema_version"] = "canonical_v2_live_fact_check.v1"
    report["mode"] = "read_only"
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
