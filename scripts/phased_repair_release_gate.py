#!/usr/bin/env python3
"""Read-only preflight for phased repair feature-flag transitions."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.phased_repair_release_gate import (
    TARGET_EXPECTED_FLAGS,
    collect_phased_release_preflight,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--target",
        choices=tuple(TARGET_EXPECTED_FLAGS),
        default="supervisor_enforce",
    )
    parser.add_argument("--required-hours", type=float, default=24.0)
    parser.add_argument("--max-gap-sec", type=float, default=75.0)
    args = parser.parse_args()
    result = collect_phased_release_preflight(
        target=args.target,
        required_hours=args.required_hours,
        max_gap_sec=args.max_gap_sec,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
