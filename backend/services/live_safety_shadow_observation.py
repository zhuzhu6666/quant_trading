"""Durable, broker-independent evidence for the Safety v2 shadow gate."""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping

from backend.services.live_safety_state import _append_fsynced, safety_outbox_path


SCHEMA_VERSION = "live_safety_shadow_observation.v1"


def safety_shadow_observation_path() -> Path:
    return safety_outbox_path().with_name("safety_shadow_observations.jsonl")


def append_safety_shadow_observation(
    *,
    payload: Mapping[str, Any],
    generation_id: str,
    broker: str,
    tick: int,
) -> dict[str, Any]:
    """Append one full-cycle observation without treating it as broker truth."""

    comparison = payload.get("comparison")
    comparison = dict(comparison) if isinstance(comparison, Mapping) else {}
    position_ids = sorted(
        {
            int(value)
            for value in list(payload.get("position_ids") or [])
            if str(value).strip() and int(value) > 0
        }
    )
    observed_at = time.time()
    record = {
        "schema_version": SCHEMA_VERSION,
        "observation_id": str(uuid.uuid4()),
        "observed_at": observed_at,
        "heartbeat_at": float(payload.get("heartbeat_at") or 0.0),
        "generation_id": str(generation_id or "legacy"),
        "broker": str(broker or ""),
        "tick": int(tick),
        "mode": str(payload.get("mode") or ""),
        "effective_mode": str(payload.get("effective_mode") or ""),
        "status": str(payload.get("status") or ""),
        "reconciliation_state": str(payload.get("reconciliation_state") or ""),
        "reconcile_id": str(payload.get("reconcile_id") or ""),
        "account_updated_at": float(payload.get("account_updated_at") or 0.0),
        "positions_updated_at": float(payload.get("positions_updated_at") or 0.0),
        "position_ids": position_ids,
        "unknown_execution_count": int(payload.get("unknown_execution_count") or 0),
        "accepting_new_risk": bool(payload.get("accepting_new_risk")),
        "forced_shadow": bool(payload.get("forced_shadow")),
        "blockers": sorted({str(item) for item in payload.get("blockers", []) or []}),
        "candidate_count": len(list(payload.get("candidates") or [])),
        "executed_count": len(list(payload.get("executed") or [])),
        "comparison": {
            key: comparison.get(key)
            for key in (
                "independent",
                "match",
                "enforce_eligible",
                "duplicate",
                "position_conflict",
                "actual_recorded",
                "pre_execution_match",
                "v2_vs_actual_match",
                "legacy_preview_vs_actual_match",
                "fingerprint",
                "actual_fingerprint",
            )
            if key in comparison
        },
    }
    _append_fsynced(safety_shadow_observation_path(), record)
    return record


def read_safety_shadow_observations(path: Path | None = None) -> list[dict[str, Any]]:
    source = path or safety_shadow_observation_path()
    if not source.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and item.get("schema_version") == SCHEMA_VERSION:
            records.append(item)
    return records


def evaluate_safety_shadow_gate(
    observations: Iterable[Mapping[str, Any]],
    *,
    required_hours: float = 24.0,
    max_gap_sec: float = 75.0,
    now: float | None = None,
) -> dict[str, Any]:
    """Evaluate continuous empty-account or complete-lifecycle shadow evidence."""

    checked_at = float(time.time() if now is None else now)
    items = sorted(
        (dict(item) for item in observations if str(item.get("mode") or "") == "shadow"),
        key=lambda item: float(item.get("observed_at") or 0.0),
    )
    if not items:
        return {
            "schema_version": "live_safety_shadow_gate.v1",
            "ok": False,
            "status": "evidence_missing",
            "checked_at": checked_at,
            "observation_count": 0,
            "blockers": ["shadow_observation_missing"],
        }
    required_sec = max(0.0, float(required_hours)) * 3600.0
    allowed_gap = max(1.0, float(max_gap_sec))
    window_start = 0
    unsafe_observation_count = 0
    last_reset_reasons: list[str] = []
    previous_at = 0.0
    for index, item in enumerate(items):
        item_at = float(item.get("observed_at") or 0.0)
        item_unsafe: list[str] = []
        if previous_at > 0 and item_at - previous_at > allowed_gap:
            window_start = index
            last_reset_reasons = ["observation_gap_exceeded"]
        previous_at = item_at
        comparison = item.get("comparison")
        comparison = dict(comparison) if isinstance(comparison, Mapping) else {}
        if str(item.get("reconciliation_state") or "") != "fresh":
            item_unsafe.append("reconciliation_not_fresh")
        if int(item.get("unknown_execution_count") or 0) != 0:
            item_unsafe.append("unknown_execution")
        if bool(item.get("forced_shadow")):
            item_unsafe.append("forced_shadow")
        for field in ("account_updated_at", "positions_updated_at"):
            updated_at = float(item.get(field) or 0.0)
            age = item_at - updated_at if updated_at > 0 else float("inf")
            if age < -5.0 or age > 15.0:
                item_unsafe.append(f"{field.removesuffix('_updated_at')}_freshness_invalid")
        if not comparison:
            item_unsafe.append("comparison_missing")
        if comparison and not bool(comparison.get("independent")):
            item_unsafe.append("comparison_not_independent")
        if comparison and not bool(comparison.get("match")):
            item_unsafe.append("candidate_mismatch")
        if comparison and not bool(comparison.get("enforce_eligible")):
            item_unsafe.append("comparison_not_enforce_eligible")
        if bool(comparison.get("duplicate")):
            item_unsafe.append("duplicate_candidate")
        if bool(comparison.get("position_conflict")):
            item_unsafe.append("position_conflict")
        if item.get("position_ids") and not bool(comparison.get("actual_recorded")):
            item_unsafe.append("legacy_actual_missing_for_position")
        if item_unsafe:
            unsafe_observation_count += 1
            window_start = index + 1
            last_reset_reasons = sorted(set(item_unsafe))

    window = items[window_start:]
    if not window:
        return {
            "schema_version": "live_safety_shadow_gate.v1",
            "ok": False,
            "status": "observing",
            "checked_at": checked_at,
            "observation_count": len(items),
            "continuous_observation_count": 0,
            "unsafe_observation_count": unsafe_observation_count,
            "last_reset_reasons": last_reset_reasons,
            "blockers": ["continuous_safe_window_missing"],
        }
    first_at = float(window[0].get("observed_at") or 0.0)
    last_at = float(window[-1].get("observed_at") or 0.0)
    gaps = [
        float(right.get("observed_at") or 0.0) - float(left.get("observed_at") or 0.0)
        for left, right in zip(window, window[1:])
    ]
    max_gap = max(gaps, default=0.0)
    duration_sec = max(0.0, last_at - first_at)
    all_position_ids: set[int] = set()
    completed_position_ids: set[int] = set()
    previously_open: set[int] = set()
    for item in window:
        current = {int(value) for value in item.get("position_ids", []) or [] if int(value) > 0}
        all_position_ids.update(current)
        completed_position_ids.update(previously_open - current)
        previously_open = current
    empty_account_window = not all_position_ids and duration_sec >= required_sec
    complete_lifecycle = bool(completed_position_ids)
    blockers: list[str] = []
    if not empty_account_window and not complete_lifecycle:
        blockers.append("duration_or_lifecycle_incomplete")
    if checked_at - last_at > allowed_gap or checked_at < last_at - 5.0:
        blockers.append("observation_stream_stale")
    return {
        "schema_version": "live_safety_shadow_gate.v1",
        "ok": not blockers,
        "status": "passed" if not blockers else "observing",
        "checked_at": checked_at,
        "observation_count": len(items),
        "continuous_observation_count": len(window),
        "unsafe_observation_count": unsafe_observation_count,
        "last_reset_reasons": last_reset_reasons,
        "first_observed_at": first_at,
        "last_observed_at": last_at,
        "duration_sec": duration_sec,
        "required_duration_sec": required_sec,
        "max_gap_sec": max_gap,
        "empty_account_window": empty_account_window,
        "complete_lifecycle": complete_lifecycle,
        "observed_position_ids": sorted(all_position_ids),
        "completed_position_ids": sorted(completed_position_ids),
        "blockers": sorted(set(blockers)),
    }
