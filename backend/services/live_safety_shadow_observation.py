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
    first_at = float(items[0].get("observed_at") or 0.0)
    last_at = float(items[-1].get("observed_at") or 0.0)
    gaps = [
        float(right.get("observed_at") or 0.0) - float(left.get("observed_at") or 0.0)
        for left, right in zip(items, items[1:])
    ]
    max_gap = max(gaps, default=0.0)
    duration_sec = max(0.0, last_at - first_at)
    required_sec = max(0.0, float(required_hours)) * 3600.0
    unsafe: list[str] = []
    all_position_ids: set[int] = set()
    completed_position_ids: set[int] = set()
    previously_open: set[int] = set()
    for item in items:
        current = {int(value) for value in item.get("position_ids", []) or [] if int(value) > 0}
        all_position_ids.update(current)
        completed_position_ids.update(previously_open - current)
        previously_open = current
        comparison = item.get("comparison")
        comparison = dict(comparison) if isinstance(comparison, Mapping) else {}
        if str(item.get("reconciliation_state") or "") != "fresh":
            unsafe.append("reconciliation_not_fresh")
        if int(item.get("unknown_execution_count") or 0) != 0:
            unsafe.append("unknown_execution")
        if bool(item.get("forced_shadow")):
            unsafe.append("forced_shadow")
        if not comparison:
            unsafe.append("comparison_missing")
        if comparison and not bool(comparison.get("independent")):
            unsafe.append("comparison_not_independent")
        if comparison and not bool(comparison.get("match")):
            unsafe.append("candidate_mismatch")
        if comparison and not bool(comparison.get("enforce_eligible")):
            unsafe.append("comparison_not_enforce_eligible")
        if current and not bool(comparison.get("actual_recorded")):
            unsafe.append("legacy_actual_missing_for_position")
        if bool(comparison.get("duplicate")):
            unsafe.append("duplicate_candidate")
        if bool(comparison.get("position_conflict")):
            unsafe.append("position_conflict")
    empty_account_window = not all_position_ids and duration_sec >= required_sec
    complete_lifecycle = bool(completed_position_ids) and not unsafe
    blockers = sorted(set(unsafe))
    if max_gap > max(1.0, float(max_gap_sec)):
        blockers.append("observation_gap_exceeded")
    if not empty_account_window and not complete_lifecycle:
        blockers.append("duration_or_lifecycle_incomplete")
    return {
        "schema_version": "live_safety_shadow_gate.v1",
        "ok": not blockers,
        "status": "passed" if not blockers else "observing",
        "checked_at": checked_at,
        "observation_count": len(items),
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
