from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


ACTIVE_CONFLICT_STATUSES = {"proposed", "approved", "applied"}


def _loads(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except Exception:
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _text(row: dict[str, Any], key: str) -> str:
    return str(row.get(key) or "").strip()


def _evidence(row: dict[str, Any]) -> dict[str, Any]:
    return _loads(row.get("evidence") or row.get("evidence_json"))


def _factor_from_template(row: dict[str, Any], evidence: dict[str, Any]) -> str:
    factor = str(evidence.get("factor_id") or "").strip()
    if factor:
        return factor
    scope_key = _text(row, "scope_key")
    if ":" in scope_key:
        return scope_key.split(":", 1)[0].strip()
    return scope_key


def _factor_from_entry_quality(row: dict[str, Any], evidence: dict[str, Any]) -> str:
    for key in ("suppressed_factor", "factor_id", "worst_factor", "factor"):
        value = str(evidence.get(key) or "").strip()
        if value:
            return value
    controls = evidence.get("recommended_controls") or {}
    if isinstance(controls, dict):
        value = str(controls.get("suppressed_factor") or controls.get("factor_id") or "").strip()
        if value:
            return value
    return _text(row, "scope_key")


def control_surface(row: dict[str, Any]) -> str:
    scope_type = _text(row, "scope_type")
    scope_key = _text(row, "scope_key")
    action = _text(row, "action")
    evidence = _evidence(row)
    if scope_type == "position_supervisor_template":
        return "position_supervisor_template"
    if scope_type == "parameter_template" and action == "switch_parameter_template":
        factor = _factor_from_template(row, evidence)
        return f"factor:{factor}" if factor else "parameter_template:global"
    if scope_type == "factor":
        return f"factor:{scope_key}"
    if scope_type == "entry_quality" and action == "suppress_recent_worst_factor":
        factor = _factor_from_entry_quality(row, evidence)
        return f"factor:{factor}" if factor else "entry_quality:global"
    if scope_type == "event_window":
        return f"event_window:{scope_key}"
    if scope_type == "entry_quality":
        return f"entry_quality:{scope_key or 'global'}"
    return f"{scope_type}:{scope_key}:{action}"


def _position_supervisor_priority(row: dict[str, Any]) -> int:
    action = _text(row, "action")
    scope_key = _text(row, "scope_key")
    evidence = _evidence(row)
    candidate = evidence.get("candidate_template") or {}
    candidate_id = ""
    if isinstance(candidate, dict):
        candidate_id = str(candidate.get("template_id") or candidate.get("template_version") or "")
    target = str(evidence.get("target_template_id") or scope_key or candidate_id)
    if "auto_tpsl" in target:
        return 200
    priority = {
        "tighten_mfe_capture_protection": 100,
        "tighten_profit_protection": 80,
        "relax_thesis_break": 50,
        "increase_min_hold_window": 45,
        "switch_position_supervisor_template": 10,
    }.get(action, 10)
    if scope_key == "position_supervisor:profit_protection.v1":
        priority += 10
    return priority


def _factor_surface_priority(row: dict[str, Any]) -> int:
    action = _text(row, "action")
    scope_type = _text(row, "scope_type")
    if scope_type == "parameter_template" and action == "switch_parameter_template":
        return 120
    if scope_type == "entry_quality" and action == "suppress_recent_worst_factor":
        return 110
    if action == "downweight":
        return 90
    if action == "boost_small":
        return 20
    return 10


def suggestion_priority(row: dict[str, Any], surface: str) -> tuple[int, float, float, float]:
    if surface == "position_supervisor_template":
        priority = _position_supervisor_priority(row)
    elif surface.startswith("factor:"):
        priority = _factor_surface_priority(row)
    else:
        priority = 10
    return (
        priority,
        _safe_float(row.get("confidence")),
        _safe_float(row.get("reviewed_at")),
        _safe_float(row.get("created_at")),
    )


@dataclass(frozen=True)
class GovernanceConflictDecision:
    suggestion_id: str
    surface: str
    decision: str
    reason: str
    winner_id: str = ""


class GovernanceConflictResolver:
    """Pure resolver for high-risk policy suggestion control surfaces."""

    def resolve(self, suggestions: list[dict[str, Any]]) -> dict[str, Any]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in suggestions:
            status = _text(row, "status").lower()
            suggestion_id = _text(row, "suggestion_id")
            if not suggestion_id or status not in ACTIVE_CONFLICT_STATUSES:
                continue
            surface = control_surface(row)
            if not surface:
                continue
            groups.setdefault(surface, []).append(row)

        winners: list[dict[str, Any]] = []
        superseded: list[dict[str, Any]] = []
        decisions: dict[str, dict[str, Any]] = {}
        for surface, rows in groups.items():
            ordered = sorted(rows, key=lambda row: suggestion_priority(row, surface), reverse=True)
            winner = ordered[0]
            winner_id = _text(winner, "suggestion_id")
            winners.append({"suggestion_id": winner_id, "surface": surface})
            decisions[winner_id] = {
                "suggestion_id": winner_id,
                "surface": surface,
                "decision": "winner",
                "reason": "highest_priority_active_suggestion",
                "winner_id": winner_id,
            }
            for row in ordered[1:]:
                suggestion_id = _text(row, "suggestion_id")
                reason = f"superseded by {winner_id} on {surface}"
                item = {
                    "suggestion_id": suggestion_id,
                    "surface": surface,
                    "decision": "superseded",
                    "reason": reason,
                    "winner_id": winner_id,
                }
                superseded.append(item)
                decisions[suggestion_id] = item
        return {"winners": winners, "superseded": superseded, "decisions": decisions}
