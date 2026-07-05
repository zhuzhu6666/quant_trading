"""Runtime factor selection helpers.

The live path is config-driven, but promoted discovered factors should remain
eligible for the main alpha chain without being hard-coded into runtime config.
Shadow factors stay in the shadow evaluator.
"""
from __future__ import annotations

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


def active_discovered_factor_ids(config: dict[str, dict] | None = None) -> list[str]:
    try:
        from alpha.registry_adapter import RegistryAdapter, SOURCE_DISCOVERED

        adapter = RegistryAdapter.shared()
        names = list(adapter.list_by_source(SOURCE_DISCOVERED))
        dead = set(adapter.dead_names()) if hasattr(adapter, "dead_names") else set()
    except Exception:
        return []

    active: list[str] = []
    config = dict(config or {})
    for name in names:
        if name in dead:
            continue
        if factor_registry.get(name) is None:
            continue
        cfg = config.get(name)
        if isinstance(cfg, dict) and cfg.get("enabled") is False:
            continue
        active.append(name)
    return active


def runtime_factor_ids(config: dict[str, dict] | None) -> list[str] | None:
    selection = select_runtime_factors(config)
    return None if selection is None else selection.selected_factor_ids


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

    for name in active_discovered_factor_ids(config):
        if name not in seen:
            names.append(name)
            seen.add(name)
    try:
        from alpha.registry_adapter import RegistryAdapter, SOURCE_SHADOW

        adapter = RegistryAdapter.shared()
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
    except Exception:
        pass

    return RuntimeFactorSelection(
        selected_factor_ids=names,
        excluded_factor_ids=sorted(set(excluded)),
        reason_excluded=reasons,
    )
