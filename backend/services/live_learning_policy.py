"""Committed live learning-policy projection and short-lived caching."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LiveLearningPolicyRuntime:
    connection_factory: Any
    load_controls: Any
    cache: dict[str, Any]
    cache_lock: Any
    warning: Any
    now: Any


_POLICY_SPECS = {
    "entry_cluster": {
        "actions": {
            "increase_same_direction_cooldown",
            "raise_pyramid_entry_threshold",
        },
        "limit": 20,
    },
    "entry_quality": {
        "actions": {
            "raise_weak_signal_threshold",
            "require_factor_agreement",
            "suppress_recent_worst_factor",
        },
        "limit": 50,
    },
    "event_window": {
        "actions": {
            "tighten_event_window_sizing",
            "extend_event_post_window_review",
        },
        "limit": 50,
    },
}


def _evidence(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "{}")
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _common(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "suggestion_id": str(item.get("suggestion_id") or ""),
        "scope_key": str(item.get("scope_key") or ""),
        "action": str(item.get("action") or ""),
        "confidence": float(item.get("confidence") or 0.0),
        "reason": str(item.get("reason") or ""),
        "governance_authority": str(item.get("governance_authority") or ""),
        "committed_mutation_id": str(item.get("committed_mutation_id") or ""),
    }


def _project_control(kind: str, row: Any) -> dict[str, Any]:
    item = dict(row)
    evidence = _evidence(item.get("evidence_json") or {})
    projected = _common(item)
    if kind == "entry_cluster":
        scope_key = projected["scope_key"]
        threshold = 3 if scope_key.endswith("_ge_3") else 2 if scope_key.endswith("_ge_2") else 1
        projected.update(
            min_same_direction_open_count=threshold,
            evidence={
                "sample_count": evidence.get("sample_count"),
                "bad_rate": evidence.get("bad_rate"),
                "avg_reward": evidence.get("avg_reward"),
            },
        )
    elif kind == "entry_quality":
        controls = evidence.get("recommended_controls") or {}
        projected.update(
            min_abs_signal_score=float(controls.get("min_abs_signal_score") or 0.0),
            max_factor_conflict_ratio=float(
                controls.get("max_factor_conflict_ratio") or 0.0
            ),
            strong_signal_override=float(
                controls.get("strong_signal_override") or 0.0
            ),
            suppressed_factor=str(
                controls.get("suppressed_factor")
                or item.get("scope_key")
                or ""
            ),
            evidence={
                "sample_count": evidence.get("sample_count"),
                "bad_rate": evidence.get("bad_rate"),
                "avg_reward": evidence.get("avg_reward"),
                "worst_factor": evidence.get("worst_factor"),
            },
        )
    else:
        event_name, _, window_bucket = projected["scope_key"].rpartition(":")
        projected.update(
            event_name=event_name,
            window_bucket=window_bucket,
            evidence={
                "sample_count": evidence.get("sample_count"),
                "bad_rate": evidence.get("bad_rate"),
                "avg_reward": evidence.get("avg_reward"),
            },
        )
    return projected


def load_active_learning_policy(
    kind: str,
    *,
    runtime: LiveLearningPolicyRuntime,
    now_ts: float | None = None,
) -> dict[str, Any]:
    if kind not in _POLICY_SPECS:
        raise ValueError(f"unsupported_live_learning_policy:{kind}")
    now = float(runtime.now() if now_ts is None else now_ts)
    with runtime.cache_lock:
        cached = runtime.cache.get("value") or {}
        if float(runtime.cache.get("expires_at") or 0.0) > now:
            return copy.deepcopy(cached)

    controls: list[dict[str, Any]] = []
    spec = _POLICY_SPECS[kind]
    try:
        conn = runtime.connection_factory(read_only=True)
        try:
            rows = runtime.load_controls(
                conn,
                scope_type=kind,
                allowed_actions=set(spec["actions"]),
                limit=int(spec["limit"]),
            )
        finally:
            conn.close()
        controls = [_project_control(kind, row) for row in rows]
    except Exception as exc:
        runtime.warning(f"[live] {kind} learning policy unavailable: {{}}", exc)

    value = {
        "active": bool(controls),
        "controls": controls,
        "source": "policy_suggestion",
        "loaded_at": now,
    }
    if kind == "entry_cluster":
        value["min_same_direction_open_count"] = min(
            [
                int(item.get("min_same_direction_open_count") or 999)
                for item in controls
            ]
            or [0]
        )
    with runtime.cache_lock:
        runtime.cache["value"] = copy.deepcopy(value)
        runtime.cache["expires_at"] = now + 60.0
    return value
