#!/usr/bin/env python3
"""Check or explicitly repair the canonical SQLite experiments schema."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.core.db import (  # noqa: E402
    EXPERIMENTS_DB,
    init_experiments_db,
    validate_experiments_db_schema,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check or explicitly repair data/experiments.db schema."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Read-only check (default).")
    mode.add_argument("--apply", action="store_true", help="Apply the operator-owned schema repair.")
    args = parser.parse_args(argv)

    try:
        if args.apply:
            init_experiments_db(EXPERIMENTS_DB)
        validate_experiments_db_schema(EXPERIMENTS_DB)
    except (OSError, RuntimeError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "schema": "experiments.sqlite.v1",
                    "path": str(EXPERIMENTS_DB),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2

    print(
        json.dumps(
            {
                "ok": True,
                "schema": "experiments.sqlite.v1",
                "path": str(EXPERIMENTS_DB),
                "mode": "apply" if args.apply else "check",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
