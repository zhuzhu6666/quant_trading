#!/usr/bin/env python3
"""Build a read-only reconstruction plan for the persisted RuntimeConfig overlay.

The command never writes state.  It classifies every top-level legacy overlay
control against the selected target autonomy mode so an operator can decide
which controls may remain protective and which require a typed, committed
governance mutation before restart.
"""

from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.core.db import get_state_pg_conn
from backend.services.governance_mutation_coordinator import (
    classify_governance_risk,
)
from config.runtime_config import RuntimeConfig


REQUIRED_OVERLAY_COLUMNS = {
    "overlay_id",
    "overlay_json",
    "overlay_hash",
    "source",
    "mutation_id",
    "legacy_authority_json",
    "updated_at",
}


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): deepcopy(item) for key, item in value.items()}
    return {}


def _loads_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return _object(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return _object(parsed)


def _deep_merge(base: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    result = _object(base)
    for key, value in patch.items():
        if isinstance(value, Mapping) and isinstance(result.get(str(key)), Mapping):
            result[str(key)] = _deep_merge(result[str(key)], value)
        else:
            result[str(key)] = deepcopy(value)
    return result


def _action_for_control(
    *,
    desired_value: Any,
    target_base_value: Any,
    desired_risk_class: str,
) -> str:
    if desired_value == target_base_value:
        return "drop_legacy_override"
    if desired_risk_class == "risk_tightening":
        return "legacy_quarantine_or_typed_mutation"
    if desired_risk_class == "no_change":
        return "drop_or_typed_committed_mutation"
    return "typed_plan_and_evidence_review_required"


def build_reconstruction_plan(
    *,
    base_config: Mapping[str, Any],
    overlay_row: Mapping[str, Any],
    target_mode: str,
) -> dict[str, Any]:
    overlay = _loads_object(overlay_row.get("overlay_json"))
    legacy_authority = _loads_object(overlay_row.get("legacy_authority_json"))
    base = _object(base_config)
    current_effective = _deep_merge(base, overlay)
    target_base = deepcopy(base)

    controls: list[dict[str, Any]] = []
    action_counts: dict[str, int] = {}
    risk_counts: dict[str, int] = {}
    for key in sorted(overlay):
        current_classification = classify_governance_risk(
            {key: base.get(key)},
            {key: current_effective.get(key)},
        ).to_dict()
        desired_value = (
            target_mode if key == "autonomy_mode" else current_effective.get(key)
        )
        desired_classification = classify_governance_risk(
            {key: target_base.get(key)},
            {key: desired_value},
        ).to_dict()
        risk_class = str(desired_classification.get("risk_class") or "unknown")
        action = _action_for_control(
            desired_value=desired_value,
            target_base_value=target_base.get(key),
            desired_risk_class=risk_class,
        )
        action_counts[action] = action_counts.get(action, 0) + 1
        risk_counts[risk_class] = risk_counts.get(risk_class, 0) + 1
        controls.append(
            {
                "key": key,
                "current_classification": current_classification,
                "target_classification": desired_classification,
                "recommended_action": action,
                "requires_committed_mutation": action.startswith("typed_"),
            }
        )

    mutation_id = str(overlay_row.get("mutation_id") or "")
    return {
        "schema_version": "runtime_overlay_reconstruction_plan.v1",
        "read_only": True,
        "target_mode": target_mode,
        "overlay": {
            "overlay_id": str(overlay_row.get("overlay_id") or ""),
            "overlay_hash": str(overlay_row.get("overlay_hash") or ""),
            "source": str(overlay_row.get("source") or ""),
            "mutation_id": mutation_id,
            "updated_at": float(overlay_row.get("updated_at") or 0.0),
            "legacy_authority_present": bool(legacy_authority),
            "control_count": len(overlay),
        },
        "release_base_mode": str(base.get("autonomy_mode") or ""),
        "restart_authority_ready": bool(mutation_id) or (
            bool(legacy_authority)
            and not any(item["requires_committed_mutation"] for item in controls)
        ),
        "risk_counts": risk_counts,
        "action_counts": action_counts,
        "controls": controls,
    }


def read_overlay_row() -> dict[str, Any]:
    conn = get_state_pg_conn(read_only=True)
    try:
        columns = {
            str(row["column_name"])
            for row in conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema=current_schema()
                  AND table_name='runtime_config_overlay'
                """
            ).fetchall()
        }
        missing = sorted(REQUIRED_OVERLAY_COLUMNS - columns)
        if missing:
            raise RuntimeError(
                "runtime_config_overlay schema is incomplete: " + ", ".join(missing)
            )
        row = conn.execute(
            """
            SELECT overlay_id, overlay_json, overlay_hash, source, mutation_id,
                   legacy_authority_json, updated_at
            FROM runtime_config_overlay
            ORDER BY updated_at DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            raise RuntimeError("runtime_config_overlay is empty")
        return dict(row)
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", default="config/settings.yaml")
    parser.add_argument(
        "--target-mode",
        required=True,
        choices=("demo_autonomous", "demo_nursery"),
    )
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()

    base = RuntimeConfig.from_yaml(Path(args.settings)).to_dict()
    plan = build_reconstruction_plan(
        base_config=base,
        overlay_row=read_overlay_row(),
        target_mode=args.target_mode,
    )
    print(
        json.dumps(
            plan,
            ensure_ascii=False,
            sort_keys=True,
            indent=None if args.compact else 2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
