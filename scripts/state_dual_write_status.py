"""Inspect or flush the PostgreSQL state dual-write outbox."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.core.db import STATE_DB  # noqa: E402
from backend.services.state_dual_write import flush_once, outbox_status  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="State dual-write outbox status")
    parser.add_argument("--flush-once", action="store_true", help="attempt one PostgreSQL flush batch")
    parser.add_argument("--limit", type=int, default=20, help="flush batch size")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args()

    result = {"status": outbox_status(STATE_DB)}
    if args.flush_once:
        result["flush"] = flush_once(db_path=STATE_DB, limit=args.limit)
        result["status_after"] = outbox_status(STATE_DB)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        status = result["status"]
        print("State dual-write outbox")
        print("=" * 60)
        print(f"enabled: {status['enabled']}")
        print(f"dsn_configured: {status['dsn_configured']}")
        print(f"counts: {status['counts']}")
        print(f"latest: {status['latest']}")
        if "flush" in result:
            print(f"flush: {result['flush']}")
            print(f"counts_after: {result['status_after']['counts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
