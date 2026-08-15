"""Canonical bounded projection for supervisor evidence payloads.

Supervisor events retain their identifiers, actions, outcomes, risk facts and
numeric evidence. Recursive prior decisions and execution candidates are
not event facts; they are represented by scalar/reference fields at the
lifecycle boundary so one event cannot contain the complete history of its
predecessors.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


_PRIOR_SUPERVISOR_SNAPSHOT_KEYS = frozenset({"latest_supervisor", "latest_protection"})


def strip_recursive_supervisor_snapshots(value: Any) -> Any:
    """Remove prior supervisor snapshots before a trace payload is archived.

    A supervisor trace is an occurrence, not a container for the previous
    occurrence.  Keep the branch explicit in the archived payload so readers
    can distinguish an intentionally omitted prior snapshot from missing
    evidence, but never carry the nested object forward.
    """
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if key in _PRIOR_SUPERVISOR_SNAPSHOT_KEYS and isinstance(item, (Mapping, list)):
                sanitized[key] = {
                    "omitted": True,
                    "reason": "recursive_prior_supervisor_snapshot",
                }
            else:
                sanitized[key] = strip_recursive_supervisor_snapshots(item)
        return sanitized
    if isinstance(value, list):
        return [strip_recursive_supervisor_snapshots(item) for item in value]
    return value


def compact_supervisor_mapping(
    value: Any,
    *,
    nested_keys: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Keep scalar evidence and explicitly allowed one-level projections."""
    if not isinstance(value, Mapping):
        return {}
    compact: dict[str, Any] = {}
    for raw_key, item in value.items():
        key = str(raw_key)
        if item is None or isinstance(item, (str, int, float, bool)):
            compact[key] = item
        elif isinstance(item, list) and all(
            part is None or isinstance(part, (str, int, float, bool))
            for part in item
        ):
            compact[key] = list(item)
        elif key in nested_keys and isinstance(item, Mapping):
            nested = compact_supervisor_mapping(item)
            if nested:
                compact[key] = nested
    return compact


def supervisor_payload_sha256(value: Any) -> str:
    """Hash the original JSON value before a bounded projection is stored."""
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def bounded_review_projection(review: Mapping[str, Any]) -> dict[str, Any]:
    """Project known recursive supervisor branches while keeping review facts."""
    projected = dict(review)
    if isinstance(review.get("inferred_close_supervisor"), Mapping):
        projected["inferred_close_supervisor"] = compact_supervisor_mapping(
            review["inferred_close_supervisor"],
            nested_keys=frozenset({"evidence", "execution", "risk", "controls"}),
        )
    domains = review.get("responsibility_domains")
    if isinstance(domains, Mapping):
        projected_domains = dict(domains)
        position_management = domains.get("position_management")
        if isinstance(position_management, Mapping):
            projected_pm = dict(position_management)
            supervisor = position_management.get("supervisor")
            if isinstance(supervisor, Mapping):
                projected_supervisor = compact_supervisor_mapping(
                    supervisor,
                    nested_keys=frozenset({"evidence", "execution", "risk", "controls"}),
                )
                projected_pm["supervisor"] = projected_supervisor
            projected_domains["position_management"] = projected_pm
        projected["responsibility_domains"] = projected_domains
    return projected
