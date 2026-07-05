#!/usr/bin/env python3
"""Clear the active autonomous runtime overlay back to the YAML base config."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.runtime_config_overlay import RuntimeConfigOverlayService
from backend.services.runtime_config_startup import load_yaml_runtime_config


def main() -> int:
    parser = argparse.ArgumentParser(description="Clear runtime_config_overlay and snapshot YAML base.")
    parser.add_argument("--source", default="cleanup_runtime_overlay")
    parser.add_argument("--run-id", default="")
    parser.add_argument(
        "--confirm-clean-active-overlay",
        action="store_true",
        help="Actually clear the active production overlay. Without this flag the command only prints status.",
    )
    args = parser.parse_args()

    service = RuntimeConfigOverlayService()
    status = service.status()
    if not args.confirm_clean_active_overlay:
        print(json.dumps({"dry_run": True, "overlay_status": status}, ensure_ascii=False, sort_keys=True))
        return 0

    base_cfg, _yaml_cfg = load_yaml_runtime_config()
    result = service.clear_overlay_to_base(
        base_cfg,
        source=args.source,
        run_id=args.run_id,
    )
    print(json.dumps({"dry_run": False, "before": status, "result": result}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
