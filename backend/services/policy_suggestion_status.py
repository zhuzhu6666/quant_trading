"""Compatibility helpers for policy_suggestion governance statuses."""
from __future__ import annotations

from typing import Any


AUTONOMOUS_STATUSES = {
    "proposed",
    "auto_approved",
    "applied",
    "rolled_back",
    "blocked_by_risk",
    "superseded",
}


def normalize_policy_suggestion_status(row: dict[str, Any]) -> str:
    status = str(row.get("status") or "").strip().lower()
    action = str(row.get("action") or "").strip().lower()
    reason = str(row.get("reason") or "").strip().lower()
    review_note = str(row.get("review_note") or "").strip().lower()
    evidence = str(row.get("evidence_json") or "").strip().lower()
    text = " ".join([action, reason, review_note, evidence])

    if status in AUTONOMOUS_STATUSES:
        return status
    if status == "pending_review":
        return "proposed"
    if status == "approved":
        if (
            "demo_auto_approve" in action
            or "demo_autonomous" in text
            or "auto-approved" in text
            or "approved by governor" in text
            or "autonomous" in text
        ):
            return "auto_approved"
        return "legacy_approved"
    if status == "rejected":
        return "legacy_rejected"
    if status:
        return f"legacy_{status}"
    return "legacy_unknown"


def count_policy_suggestion_statuses(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    raw: dict[str, int] = {}
    normalized: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "unknown")
        raw[status] = raw.get(status, 0) + 1
        mapped = normalize_policy_suggestion_status(row)
        normalized[mapped] = normalized.get(mapped, 0) + 1
    return {"raw": raw, "normalized": normalized}
