"""Runtime factor selection helpers.

The live path is config-driven, but promoted discovered factors should remain
eligible for the main alpha chain without being hard-coded into runtime config.
Shadow factors stay in the shadow evaluator.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field

from alpha.registry import factor_registry
from .factor_identity import canonical_factor_id


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass
class RuntimeFactorSelection:
    selected_factor_ids: list[str] = field(default_factory=list)
    excluded_factor_ids: list[str] = field(default_factory=list)
    reason_excluded: dict[str, str] = field(default_factory=dict)


def runtime_factor_enabled(config: dict[str, object] | None) -> bool:
    """Resolve the existing config default once at the selection boundary.

    RuntimeConfig historically omits ``enabled`` for ordinary builtin
    factors. Missing and explicit ``None`` therefore retain that existing
    default; only an explicit ``False`` disables the factor. Consumers must
    use this helper instead of applying different truthiness rules.
    """
    return not (isinstance(config, dict) and config.get("enabled") is False)


def configured_factor_ids(config: dict[str, dict] | None) -> list[str] | None:
    if not config:
        return None
    names: list[str] = []
    for name, cfg in config.items():
        if str(name).startswith("_"):
            continue
        if isinstance(cfg, dict) and not runtime_factor_enabled(cfg):
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


def _has_active_health_evidence(
    name: str,
    health: dict[str, object],
    *,
    now: float | None = None,
) -> bool:
    """Require the same fail-closed health class used by ACTIVE admission."""

    item = health.get(name)
    if item is None:
        return False
    try:
        from config.runtime_config import shared as runtime_config

        cfg = runtime_config()
        min_score = float(getattr(cfg, "factor_health_healthy_threshold", 70.0))
        min_n_obs = int(getattr(cfg, "factor_health_min_n_obs", 100))
        min_abs_ic = float(
            getattr(cfg, "factor_health_ic_active_threshold", 0.02)
        )
    except Exception:
        min_score = 70.0
        min_n_obs = 100
        min_abs_ic = 0.02
    try:
        status = str(getattr(item, "status", "UNKNOWN") or "UNKNOWN").upper()
        score = float(getattr(item, "score", 0.0) or 0.0)
        n_obs = int(getattr(item, "n_obs", 0) or 0)
        rolling_ic = abs(float(getattr(item, "rolling_ic", 0.0) or 0.0))
        updated_at = float(getattr(item, "updated_at", 0.0) or 0.0)
    except (TypeError, ValueError):
        return False
    checked_at = float(time.time() if now is None else now)
    age = checked_at - updated_at if updated_at > 0.0 else float("inf")
    return bool(
        status == "HEALTHY"
        and score >= min_score
        and n_obs >= min_n_obs
        and rolling_ic >= min_abs_ic
        and -5.0 <= age <= 180.0
    )


def _discovered_admission_reason(name: str, cfg: object) -> str:
    """Require the committed ACTIVE projection produced by lifecycle v2."""

    if not isinstance(cfg, dict):
        return "explicit_enabled_config_required"
    if cfg.get("enabled") is not True:
        return "explicit_enabled_config_required"
    if str(cfg.get("lifecycle_status") or "").upper() != "ACTIVE":
        return "lifecycle_not_active"
    if not str(cfg.get("committed_mutation_id") or "").strip():
        return "committed_mutation_required"
    expression = str(cfg.get("expression") or "").strip()
    factor_id = str(cfg.get("factor_id") or "").strip()
    definition_fingerprint = str(cfg.get("definition_fingerprint") or "").strip().lower()
    artifact_hash = str(cfg.get("artifact_hash") or "").strip().lower()
    if not expression or not factor_id or not _SHA256_RE.fullmatch(definition_fingerprint):
        return "stable_factor_identity_required"
    if not _SHA256_RE.fullmatch(artifact_hash):
        return "stable_artifact_required"
    try:
        if canonical_factor_id(expression) != factor_id:
            return "stable_factor_identity_mismatch"
    except Exception:
        return "stable_factor_identity_invalid"
    try:
        weight = float(cfg.get("weight"))
    except (TypeError, ValueError):
        weight = 0.0
    if weight <= 0.0:
        return "explicit_positive_weight_required"
    return ""


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
    config = dict(config or {})
    for name in names:
        if name in dead:
            continue
        if factor_registry.get(name) is None:
            continue
        if not _has_active_health_evidence(name, health):
            continue
        cfg = config.get(name)
        admission_reason = _discovered_admission_reason(name, cfg)
        if admission_reason:
            continue
        explicit = True
        try:
            meta = adapter.get_meta(name) if hasattr(adapter, "get_meta") else {}
        except Exception:
            # Discovered factors require a complete Registry projection.  A
            # store/adapter failure must never make them look like built-ins.
            continue
        lifecycle = str((meta or {}).get("lifecycle_status") or (meta or {}).get("status") or "").upper()
        lifecycle_priority = 2 if lifecycle in {"LIVE", "ACTIVE", "CANARY_100"} else (1 if lifecycle.startswith("CANARY") else 0)
        score = max(
            _score((meta or {}).get("health_score")),
            _score((meta or {}).get("fitness")),
            _score((meta or {}).get("oos_score")),
            _score((meta or {}).get("score")),
        )
        active.append((100 + lifecycle_priority if explicit else lifecycle_priority, score, str(name)))
    budget = _discovered_budget()
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
                admission_reason = _discovered_admission_reason(name, config.get(name))
                reasons[name] = admission_reason or (
                    "discovered_runtime_budget"
                    if _has_active_health_evidence(name, health)
                    else "active_health_invalid_or_stale"
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
            meta_unavailable = False
            try:
                meta = adapter.get_meta(name) if hasattr(adapter, "get_meta") else {}
            except Exception:
                meta = {}
                meta_unavailable = True
            source = str(meta.get("source") or "builtin")
            if (
                meta_unavailable
                and role == "alpha"
                and not bool((cfg or {}).get("health_gate_exempt", False))
            ):
                excluded.append(name)
                reasons[name] = "registry_metadata_unavailable"
                continue
            if source == SOURCE_DISCOVERED:
                admission_reason = _discovered_admission_reason(name, cfg)
                if admission_reason:
                    excluded.append(name)
                    reasons[name] = admission_reason
                    continue
                if name not in selected_discovered:
                    excluded.append(name)
                    reasons[name] = (
                        "discovered_runtime_budget"
                        if _has_active_health_evidence(name, health)
                        else "active_health_invalid_or_stale"
                    )
                    continue
            if lifecycle in {
                "DEAD",
                "SHADOW",
                "PROMOTION_PREPARED",
                "QUARANTINE",
                "QUARANTINED",
                "RETIRED",
            }:
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
        # Registry/health/lifecycle is an admission authority for directional
        # alpha.  If that authority is unavailable, keep context/gate/sizing
        # inputs available but reject every non-exempt alpha instead of
        # falling back to the original configured list.
        admitted = []
        for name in names:
            cfg = config.get(name) if isinstance(config.get(name), dict) else {}
            role = str((cfg or {}).get("role") or "alpha").lower()
            if role == "alpha" and not bool((cfg or {}).get("health_gate_exempt", False)):
                excluded.append(name)
                reasons[name] = "factor_admission_unavailable"
                continue
            admitted.append(name)
        names = admitted

    return RuntimeFactorSelection(
        selected_factor_ids=names,
        excluded_factor_ids=sorted(set(excluded)),
        reason_excluded=reasons,
    )
