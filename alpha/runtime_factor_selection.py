"""Runtime factor selection helpers.

The live path is config-driven, but promoted discovered factors should remain
eligible for the main alpha chain without being hard-coded into runtime config.
Shadow factors stay in the shadow evaluator.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from alpha.registry import factor_registry


@dataclass
class RuntimeFactorSelection:
    selected_factor_ids: list[str] = field(default_factory=list)
    excluded_factor_ids: list[str] = field(default_factory=list)
    reason_excluded: dict[str, str] = field(default_factory=dict)


def configured_factor_ids(config: dict[str, dict] | None) -> list[str] | None:
    if not config:
        return None
    names: list[str] = []
    for name, cfg in config.items():
        if str(name).startswith("_"):
            continue
        if isinstance(cfg, dict) and cfg.get("enabled") is False:
            continue
        names.append(str(name))
    return names


def _discovered_budget() -> int:
    try:
        return max(1, min(int(os.getenv("QUANT_RUNTIME_DISCOVERED_FACTOR_BUDGET", "24")), 128))
    except Exception:
        return 24


def _score(value: object) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0


def _health_evidence(adapter: object) -> dict[str, object]:
    """Return persisted health rows keyed by factor.

    An absent row is deliberately different from a low score: live alpha may
    remain active while WATCH/DECAYING, but it must not enter directional
    scoring without any measured observations at all.
    """
    try:
        statuses = adapter.all_statuses() if hasattr(adapter, "all_statuses") else []
    except Exception:
        statuses = []
    return {
        str(getattr(item, "factor", "") or ""): item
        for item in statuses
        if str(getattr(item, "factor", "") or "")
    }


def _has_health_evidence(name: str, health: dict[str, object]) -> bool:
    item = health.get(name)
    if item is None:
        return False
    status = str(getattr(item, "status", "UNKNOWN") or "UNKNOWN").upper()
    try:
        n_obs = int(getattr(item, "n_obs", 0) or 0)
    except Exception:
        n_obs = 0
    return n_obs > 0 and status not in {"", "UNKNOWN"}


def active_discovered_factor_ids(config: dict[str, dict] | None = None) -> list[str]:
    try:
        from alpha.registry_adapter import RegistryAdapter, SOURCE_DISCOVERED

        adapter = RegistryAdapter.shared()
        names = list(adapter.list_by_source(SOURCE_DISCOVERED))
        dead = set(adapter.dead_names()) if hasattr(adapter, "dead_names") else set()
        health = _health_evidence(adapter)
    except Exception:
        return []

    active: list[tuple[int, float, str]] = []
    explicitly_configured: set[str] = set()
    config = dict(config or {})
    for name in names:
        if name in dead:
            continue
        if factor_registry.get(name) is None:
            continue
        if not _has_health_evidence(name, health):
            continue
        cfg = config.get(name)
        if isinstance(cfg, dict) and cfg.get("enabled") is False:
            continue
        explicit = isinstance(cfg, dict)
        if explicit:
            explicitly_configured.add(name)
        try:
            meta = adapter.get_meta(name) if hasattr(adapter, "get_meta") else {}
        except Exception:
            meta = {}
        lifecycle = str((meta or {}).get("lifecycle_status") or (meta or {}).get("status") or "").upper()
        lifecycle_priority = 2 if lifecycle in {"LIVE", "ACTIVE", "CANARY_100"} else (1 if lifecycle.startswith("CANARY") else 0)
        score = max(
            _score((meta or {}).get("health_score")),
            _score((meta or {}).get("fitness")),
            _score((meta or {}).get("oos_score")),
            _score((meta or {}).get("score")),
        )
        active.append((100 + lifecycle_priority if explicit else lifecycle_priority, score, str(name)))
    budget = max(_discovered_budget(), len(explicitly_configured))
    active.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return [name for _, _, name in active[:budget]]


def runtime_factor_ids(config: dict[str, dict] | None) -> list[str] | None:
    selection = select_runtime_factors(config)
    return None if selection is None else selection.selected_factor_ids


def runtime_factor_budget_status(config: dict[str, dict] | None) -> dict[str, object]:
    selection = select_runtime_factors(config)
    if selection is None:
        return {
            "ok": False,
            "schema_version": "runtime_factor_budget.v1",
            "status": "missing_config",
            "selected_count": 0,
            "budget_excluded_count": 0,
            "discovered_budget": _discovered_budget(),
        }
    budget_excluded = [
        name for name, reason in selection.reason_excluded.items()
        if reason == "discovered_runtime_budget"
    ]
    return {
        "ok": True,
        "schema_version": "runtime_factor_budget.v1",
        "status": "bounded" if budget_excluded else "within_budget",
        "selected_count": len(selection.selected_factor_ids),
        "selected_factor_ids": list(selection.selected_factor_ids),
        "excluded_count": len(selection.excluded_factor_ids),
        "budget_excluded_count": len(budget_excluded),
        "budget_excluded_sample": sorted(budget_excluded)[:20],
        "discovered_budget": _discovered_budget(),
        "boundary": {
            "registry_evidence_retained": True,
            "cold_factors_remain_shadow_or_research": True,
            "does_not_delete_factor_artifacts": True,
        },
    }


def select_runtime_factors(config: dict[str, dict] | None) -> RuntimeFactorSelection | None:
    configured = configured_factor_ids(config)
    if configured is None:
        return None

    names = list(configured)
    seen = set(names)
    excluded: list[str] = []
    reasons: dict[str, str] = {}

    config = dict(config or {})
    for name, cfg in config.items():
        if isinstance(cfg, dict) and cfg.get("enabled") is False:
            excluded.append(str(name))
            reasons[str(name)] = "disabled_by_runtime_config"

    selected_discovered = active_discovered_factor_ids(config)
    for name in selected_discovered:
        if name not in seen:
            names.append(name)
            seen.add(name)
    try:
        from alpha.registry_adapter import RegistryAdapter, SOURCE_DISCOVERED, SOURCE_SHADOW

        adapter = RegistryAdapter.shared()
        health = _health_evidence(adapter)
        for name in adapter.list_by_source(SOURCE_DISCOVERED):
            if name not in selected_discovered and name not in reasons:
                excluded.append(name)
                reasons[name] = (
                    "discovered_runtime_budget"
                    if _has_health_evidence(name, health)
                    else "missing_health_evidence"
                )
        for name in adapter.list_by_source(SOURCE_SHADOW):
            if name not in reasons:
                excluded.append(name)
                reasons[name] = "shadow_only"
        dead_names = set(adapter.dead_names())
        if dead_names:
            names = [name for name in names if name not in dead_names]
            seen = set(names)
        for name in dead_names:
            if name not in reasons:
                excluded.append(name)
                reasons[name] = "lifecycle_dead"

        admitted: list[str] = []
        for name in names:
            cfg = config.get(name) if isinstance(config.get(name), dict) else {}
            role = str((cfg or {}).get("role") or "alpha").lower()
            lifecycle = str((cfg or {}).get("lifecycle_status") or "").upper()
            if lifecycle in {"DEAD", "SHADOW", "QUARANTINE", "QUARANTINED"}:
                excluded.append(name)
                reasons[name] = "lifecycle_not_live"
                continue
            if role == "alpha" and not bool((cfg or {}).get("health_gate_exempt", False)):
                if not _has_health_evidence(name, health):
                    excluded.append(name)
                    reasons[name] = "missing_health_evidence"
                    continue
            admitted.append(name)
        names = admitted
    except Exception:
        pass

    return RuntimeFactorSelection(
        selected_factor_ids=names,
        excluded_factor_ids=sorted(set(excluded)),
        reason_excluded=reasons,
    )
