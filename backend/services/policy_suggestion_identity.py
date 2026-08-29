"""Deterministic identities for semantic policy suggestions.

Policy suggestions are projections of evidence, not per-run events.  The
identity therefore excludes only fields that describe the write occurrence
and keeps evidence, qualification, status, and lifecycle identifiers intact.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


# Keep this list deliberately narrow.  In particular, evidence dates, sample
# ids, hashes, before/after values, mutation ids, and state changes are
# semantic and must remain part of the suggestion identity.
OCCURRENCE_KEYS = frozenset(
    {
        "run_id",
        "evolution_run_id",
        "cycle_id",
        "trace_id",
        "request_id",
        "correlation_id",
        "audit_id",
        "decision_id",
        "created_at",
        "updated_at",
        "generated_at",
        "published_at",
        "heartbeat_at",
        "loaded_at",
        "catalog_ts",
        "write_timestamp",
    }
)


def normalize_policy_suggestion_value(value: Any) -> Any:
    """Normalize JSON-like evidence while removing occurrence-only fields."""

    if isinstance(value, dict):
        return {
            str(key): normalize_policy_suggestion_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key).lower() not in OCCURRENCE_KEYS
        }
    if isinstance(value, tuple):
        return [normalize_policy_suggestion_value(item) for item in value]
    if isinstance(value, list):
        return [normalize_policy_suggestion_value(item) for item in value]
    if isinstance(value, set):
        return sorted(
            (normalize_policy_suggestion_value(item) for item in value),
            key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, default=str),
        )
    return value


def deterministic_policy_suggestion_id(
    *,
    writer: str,
    scope_type: str,
    scope_key: str,
    action: str,
    evidence: Any,
    status: str,
    qualification_fingerprint: str = "",
    prefix: str = "psg",
) -> str:
    """Build an id stable across scheduler retries and process restarts."""

    identity = {
        "writer": str(writer or ""),
        "scope_type": str(scope_type or ""),
        "scope_key": str(scope_key or ""),
        "action": str(action or ""),
        "evidence": normalize_policy_suggestion_value(evidence or {}),
        "status": str(status or ""),
        "qualification_fingerprint": str(qualification_fingerprint or ""),
    }
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()
    return f"{prefix}_{digest}"


__all__ = ["deterministic_policy_suggestion_id", "normalize_policy_suggestion_value"]
