#!/usr/bin/env python3
"""Check or refresh the canonical FastAPI OpenAPI snapshot."""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "tests" / "snapshots" / "openapi.json"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _render() -> str:
    from backend.app import app

    return json.dumps(app.openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--update", action="store_true")
    args = parser.parse_args()
    rendered = _render()
    if args.update:
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(rendered, encoding="utf-8")
        print(f"updated {SNAPSHOT.relative_to(ROOT)}")
        return 0
    if not SNAPSHOT.exists():
        raise SystemExit("OpenAPI snapshot is missing; run with --update")
    current = SNAPSHOT.read_text(encoding="utf-8")
    if current != rendered:
        diff = "".join(
            difflib.unified_diff(
                current.splitlines(keepends=True),
                rendered.splitlines(keepends=True),
                fromfile="committed OpenAPI",
                tofile="generated OpenAPI",
                n=2,
            )
        )
        raise SystemExit("OpenAPI snapshot is stale:\n" + diff[:12000])
    print("OpenAPI snapshot is current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
