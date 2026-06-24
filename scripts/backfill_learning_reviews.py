from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.services.learning_backfill import run_learning_backfill


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill missing learning reviews from ctrader_deals.")
    parser.add_argument("--limit", type=int, default=100, help="Maximum missing positions to inspect.")
    parser.add_argument("--allow-partial", action="store_true", help="Also backfill positions without open decision ledger.")
    parser.add_argument("--no-rebuild-learning", action="store_true", help="Skip experience/stat/suggestion rebuild.")
    args = parser.parse_args()

    result = run_learning_backfill(
        limit=args.limit,
        allow_partial=args.allow_partial,
        rebuild_learning=not args.no_rebuild_learning,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
