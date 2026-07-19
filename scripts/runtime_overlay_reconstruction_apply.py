#!/usr/bin/env python3
"""Explicitly clear an unbound legacy overlay back to the release base.

This is a one-time operator release boundary, not an autonomous governance
shortcut.  It only accepts the demo_autonomous release target, installs a
durable no-new-risk latch before writing, backs up the exact legacy row, and
uses an overlay-hash compare-and-swap.  The latch is intentionally not cleared;
the controlled restart verification owns that later decision.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.core.db import STATE_DB
from backend.services.live_safety_state import activate_no_new_risk_latch
from backend.services.runtime_config_overlay import RuntimeConfigOverlayService
from config.runtime_config import RuntimeConfig


def _backup_payload(latest: dict[str, Any], *, actor: str, reason: str) -> dict[str, Any]:
    return {
        "schema_version": "runtime_overlay_operator_backup.v1",
        "actor": actor,
        "reason": reason,
        "captured_at": time.time(),
        "overlay_id": latest.get("overlay_id", ""),
        "overlay": latest.get("overlay") or {},
        "overlay_hash": latest.get("overlay_hash", ""),
        "source": latest.get("source", ""),
        "run_id": latest.get("run_id", ""),
        "mutation_id": latest.get("mutation_id", ""),
        "legacy_authority": latest.get("legacy_authority") or {},
        "updated_at": latest.get("updated_at", 0.0),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", default="config/settings.yaml")
    parser.add_argument("--target-mode", required=True, choices=("demo_autonomous",))
    parser.add_argument("--expected-overlay-hash", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--backup-dir", default="data/runtime_overlay_backups")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    actor = str(args.actor or "").strip()
    reason = str(args.reason or "").strip()
    if not actor.startswith("operator:"):
        parser.error("--actor must start with operator:")
    if not reason:
        parser.error("--reason is required")

    base = RuntimeConfig.from_yaml(Path(args.settings))
    if str(base.autonomy_mode or "") != args.target_mode:
        raise RuntimeError(
            "release_base_mode_mismatch:"
            f"expected={args.target_mode}:actual={base.autonomy_mode}"
        )
    service = RuntimeConfigOverlayService(STATE_DB)
    latest = service.latest()
    current_hash = str(latest.get("overlay_hash") or "")
    if current_hash != str(args.expected_overlay_hash):
        raise RuntimeError(
            "overlay_hash_changed:"
            f"expected={args.expected_overlay_hash}:actual={current_hash}"
        )
    if str(latest.get("mutation_id") or ""):
        raise RuntimeError("overlay_already_mutation_bound")

    preview = {
        "ok": True,
        "status": "ready_to_apply" if args.apply else "dry_run",
        "target_mode": args.target_mode,
        "overlay_hash": current_hash,
        "control_keys": sorted((latest.get("overlay") or {}).keys()),
        "apply_requested": bool(args.apply),
    }
    if not args.apply:
        print(json.dumps(preview, ensure_ascii=False, sort_keys=True, indent=2))
        return 0

    run_id = f"runtime-overlay-reconstruction-{int(time.time())}"
    latch = activate_no_new_risk_latch(
        reason="runtime_overlay_reconstruction_in_progress",
        actor=actor,
        correlation_id=run_id,
        metadata={
            "target_mode": args.target_mode,
            "expected_overlay_hash": current_hash,
            "operator_reason": reason,
        },
        cause="release_reconstruction",
        cause_id=run_id,
    )

    backup_dir = Path(args.backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"{run_id}.json"
    with backup_path.open("x", encoding="utf-8") as handle:
        json.dump(
            _backup_payload(latest, actor=actor, reason=reason),
            handle,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        handle.write("\n")

    result = service.clear_overlay_to_base(
        base,
        source="operator_runtime_overlay_reconstruction",
        run_id=run_id,
        expected_overlay_hash=current_hash,
    )
    print(
        json.dumps(
            {
                **preview,
                "status": "cleared_to_release_base",
                "run_id": run_id,
                "backup_path": str(backup_path),
                "latch_event_id": latch.get("event_id", ""),
                "result": result,
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
