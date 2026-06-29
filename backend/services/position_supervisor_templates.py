from __future__ import annotations

from copy import deepcopy
from typing import Any


SCHEMA_VERSION = "position_supervisor_template.v1"
DEFAULT_TEMPLATE_ID = "position_supervisor:default.v1"
CONSERVATIVE_TEMPLATE_ID = "position_supervisor:conservative.v1"


_TEMPLATES: dict[str, dict[str, Any]] = {
    DEFAULT_TEMPLATE_ID: {
        "schema_version": SCHEMA_VERSION,
        "template_id": DEFAULT_TEMPLATE_ID,
        "template_version": "default.v1",
        "template_role": "baseline",
        "status": "active",
        "description": "Keep current supervisor behavior unchanged.",
        "thresholds": {
            "min_thesis_break_seconds": 0.0,
            "broken_holding_efficiency_threshold": 1.00,
            "giveback_reduce_threshold": 0.70,
            "giveback_tighten_threshold": 0.35,
            "profit_capture_min_threshold": 0.35,
            "time_decay_reduce_threshold": 0.35,
            "timeout_tighten_ratio": 0.80,
            "timeout_reduce_ratio": 0.80,
            "weakening_holding_efficiency_threshold": 0.45,
            "near_take_profit_progress": 0.92,
            "near_stop_loss_progress": 0.85,
            "near_stop_loss_efficiency_threshold": 0.25,
        },
        "risk_boundary": {
            "approval_path": "built_in_default",
            "can_auto_deploy": True,
            "auto_deploy_modes": ["demo_autonomous"],
            "requires_offline_replay": False,
        },
    },
    CONSERVATIVE_TEMPLATE_ID: {
        "schema_version": SCHEMA_VERSION,
        "template_id": CONSERVATIVE_TEMPLATE_ID,
        "template_version": "conservative.v1",
        "template_role": "reduce_early_small_loss_exits",
        "status": "candidate",
        "description": "Delay early thesis-broken full exits and prefer tighten/reduce evidence first.",
        "thresholds": {
            "min_thesis_break_seconds": 300.0,
            "broken_holding_efficiency_threshold": 0.12,
            "giveback_reduce_threshold": 0.78,
            "giveback_tighten_threshold": 0.42,
            "profit_capture_min_threshold": 0.28,
            "time_decay_reduce_threshold": 0.28,
            "timeout_tighten_ratio": 0.88,
            "timeout_reduce_ratio": 0.90,
            "weakening_holding_efficiency_threshold": 0.38,
            "near_take_profit_progress": 0.95,
            "near_stop_loss_progress": 0.90,
            "near_stop_loss_efficiency_threshold": 0.18,
        },
        "risk_boundary": {
            "approval_path": "offline_replay_then_human_review",
            "can_auto_deploy": False,
            "auto_deploy_modes": ["demo_autonomous"],
            "requires_offline_replay": True,
        },
    },
}


def list_position_supervisor_templates() -> list[dict[str, Any]]:
    return [deepcopy(item) for item in _TEMPLATES.values()]


def get_position_supervisor_template(template_id: str | None = None) -> dict[str, Any]:
    key = str(template_id or DEFAULT_TEMPLATE_ID)
    if key not in _TEMPLATES:
        key = DEFAULT_TEMPLATE_ID
    return deepcopy(_TEMPLATES[key])


def normalize_position_supervisor_template(template: dict[str, Any] | str | None = None) -> dict[str, Any]:
    if template is None or template == "":
        return get_position_supervisor_template(DEFAULT_TEMPLATE_ID)
    if isinstance(template, str):
        return get_position_supervisor_template(template)
    base = get_position_supervisor_template(str(template.get("template_id") or DEFAULT_TEMPLATE_ID))
    merged = deepcopy(base)
    thresholds = dict(base.get("thresholds") or {})
    thresholds.update(dict(template.get("thresholds") or {}))
    merged.update({k: deepcopy(v) for k, v in template.items() if k != "thresholds"})
    merged["thresholds"] = thresholds
    merged["schema_version"] = str(merged.get("schema_version") or SCHEMA_VERSION)
    merged["template_id"] = str(merged.get("template_id") or DEFAULT_TEMPLATE_ID)
    return merged
