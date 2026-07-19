import numpy as np
import time
from types import SimpleNamespace

from alpha.registry import factor_registry
from alpha.registry_adapter import RegistryAdapter
from alpha.runtime_factor_selection import active_discovered_factor_ids, select_runtime_factors
from backend.services.factor_identity import canonical_factor_id, factor_definition_fingerprint


def _active_discovered_config(expression: str) -> dict:
    fingerprint = factor_definition_fingerprint(expression)
    return {
        "enabled": True,
        "source": "discovered",
        "lifecycle_status": "ACTIVE",
        "committed_mutation_id": f"mutation:{fingerprint[:12]}",
        "expression": expression,
        "factor_id": canonical_factor_id(expression),
        "definition_fingerprint": fingerprint,
        "artifact_hash": fingerprint,
        "weight": 0.1,
    }


def test_discovered_runtime_budget_prefers_explicit_and_high_score(monkeypatch):
    names = ["disc_a", "disc_b", "disc_c", "disc_d"]

    def _one(df):
        return np.ones(len(df))

    for name in names:
        factor_registry._factors[name] = _one

    class _Adapter:
        def list_by_source(self, source):
            return names if source == "discovered" else []

        def dead_names(self):
            return []

        def get_meta(self, name):
            return {"source": "discovered", "score": {"disc_a": 0.1, "disc_b": 0.9, "disc_c": 0.5, "disc_d": 0.0}[name]}

        def all_statuses(self):
            return [
                SimpleNamespace(
                    factor=name,
                    status="HEALTHY",
                    score=80.0,
                    n_obs=100,
                    rolling_ic=0.03,
                    updated_at=time.time(),
                )
                for name in names + ["rsi_14"]
            ]

    monkeypatch.setenv("QUANT_RUNTIME_DISCOVERED_FACTOR_BUDGET", "2")
    monkeypatch.setattr(RegistryAdapter, "shared", staticmethod(lambda: _Adapter()))
    config = {
        "rsi_14": {"enabled": True},
        **{
            name: _active_discovered_config(f"ts_mean(close, {index + 2})")
            for index, name in enumerate(names)
        },
    }
    try:
        active = active_discovered_factor_ids(config)
        selection = select_runtime_factors(config)
    finally:
        for name in names:
            factor_registry._factors.pop(name, None)

    assert active == ["disc_b", "disc_c"]
    assert selection is not None
    assert "disc_b" in selection.selected_factor_ids
    assert "disc_c" in selection.selected_factor_ids
    assert selection.reason_excluded["disc_a"] == "discovered_runtime_budget"
    assert selection.reason_excluded["disc_d"] == "discovered_runtime_budget"


def test_discovered_factor_without_committed_active_projection_is_excluded(monkeypatch):
    name = "disc_legacy"
    factor_registry._factors[name] = lambda df: np.ones(len(df))

    class _Adapter:
        def list_by_source(self, source):
            return [name] if source == "discovered" else []

        def dead_names(self):
            return []

        def get_meta(self, _name):
            return {"source": "discovered"}

        def all_statuses(self):
            return [
                SimpleNamespace(
                    factor=name,
                    status="HEALTHY",
                    score=80.0,
                    n_obs=100,
                    rolling_ic=0.03,
                    updated_at=time.time(),
                )
            ]

    monkeypatch.setattr(RegistryAdapter, "shared", staticmethod(lambda: _Adapter()))
    try:
        selection = select_runtime_factors({name: {"enabled": True, "lifecycle_status": "ACTIVE"}})
    finally:
        factor_registry._factors.pop(name, None)

    assert selection is not None
    assert name not in selection.selected_factor_ids
    assert selection.reason_excluded[name] == "committed_mutation_required"


def test_alpha_without_health_evidence_is_fail_closed(monkeypatch):
    class _Adapter:
        def list_by_source(self, source):
            return []

        def dead_names(self):
            return []

        def all_statuses(self):
            return [SimpleNamespace(factor="measured", status="WATCH", n_obs=12)]

    monkeypatch.setattr(RegistryAdapter, "shared", staticmethod(lambda: _Adapter()))
    selection = select_runtime_factors({
        "measured": {"role": "alpha"},
        "unknown": {"role": "alpha"},
        "context_only": {"role": "context"},
    })

    assert selection is not None
    assert selection.selected_factor_ids == ["measured", "context_only"]
    assert selection.reason_excluded["unknown"] == "missing_health_evidence"


def test_registry_adapter_failure_rejects_alpha_but_keeps_non_directional_roles(monkeypatch):
    monkeypatch.setattr(
        RegistryAdapter,
        "shared",
        staticmethod(lambda: (_ for _ in ()).throw(RuntimeError("registry unavailable"))),
    )

    selection = select_runtime_factors(
        {
            "alpha_a": {"role": "alpha"},
            "context_a": {"role": "context"},
            "gate_a": {"role": "gate"},
        }
    )

    assert selection is not None
    assert selection.selected_factor_ids == ["context_a", "gate_a"]
    assert selection.reason_excluded["alpha_a"] == "factor_admission_unavailable"


def test_registry_metadata_failure_cannot_reclassify_discovered_alpha_as_builtin(monkeypatch):
    name = "disc_registry_error"
    factor_registry._factors[name] = lambda df: np.ones(len(df))

    class _Adapter:
        def list_by_source(self, source):
            return [name] if source == "discovered" else []

        def dead_names(self):
            return []

        def all_statuses(self):
            return [
                SimpleNamespace(
                    factor=name,
                    status="HEALTHY",
                    score=80.0,
                    n_obs=100,
                    rolling_ic=0.03,
                    updated_at=time.time(),
                )
            ]

        def get_meta(self, _name):
            raise RuntimeError("registry metadata unavailable")

    monkeypatch.setattr(RegistryAdapter, "shared", staticmethod(lambda: _Adapter()))
    try:
        selection = select_runtime_factors(
            {name: _active_discovered_config("ts_mean(close, 7)")}
        )
    finally:
        factor_registry._factors.pop(name, None)

    assert selection is not None
    assert name not in selection.selected_factor_ids
    assert selection.reason_excluded[name] == "registry_metadata_unavailable"


def test_prepared_builtin_is_observation_only_not_live_admitted(monkeypatch):
    name = "prepared_builtin"
    factor_registry._factors[name] = lambda df: np.ones(len(df))

    class _Adapter:
        def list_by_source(self, _source):
            return []

        def dead_names(self):
            return []

        def get_meta(self, _name):
            return {"source": "builtin"}

        def all_statuses(self):
            return [
                SimpleNamespace(
                    factor=name,
                    status="HEALTHY",
                    score=85.0,
                    n_obs=500,
                    rolling_ic=0.04,
                    updated_at=time.time(),
                )
            ]

    monkeypatch.setattr(RegistryAdapter, "shared", staticmethod(lambda: _Adapter()))
    try:
        selection = select_runtime_factors(
            {
                name: {
                    "enabled": True,
                    "role": "alpha",
                    "source": "builtin",
                    "lifecycle_status": "PROMOTION_PREPARED",
                }
            }
        )
    finally:
        factor_registry._factors.pop(name, None)

    assert selection is not None
    assert name not in selection.selected_factor_ids
    assert selection.reason_excluded[name] == "lifecycle_not_live"


def test_active_discovered_factor_requires_fresh_healthy_evidence(monkeypatch):
    name = "disc_stale_health"
    factor_registry._factors[name] = lambda df: np.ones(len(df))

    class _Adapter:
        def list_by_source(self, source):
            return [name] if source == "discovered" else []

        def dead_names(self):
            return []

        def get_meta(self, _name):
            return {"source": "discovered"}

        def all_statuses(self):
            return [
                SimpleNamespace(
                    factor=name,
                    status="WATCH",
                    score=69.0,
                    n_obs=500,
                    rolling_ic=0.05,
                    updated_at=time.time(),
                )
            ]

    monkeypatch.setattr(RegistryAdapter, "shared", staticmethod(lambda: _Adapter()))
    try:
        selection = select_runtime_factors(
            {name: _active_discovered_config("ts_mean(close, 11)")}
        )
    finally:
        factor_registry._factors.pop(name, None)

    assert name not in selection.selected_factor_ids
    assert selection.reason_excluded[name] == "active_health_invalid_or_stale"
