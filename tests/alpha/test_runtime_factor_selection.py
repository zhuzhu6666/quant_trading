import numpy as np
from types import SimpleNamespace

from alpha.registry import factor_registry
from alpha.registry_adapter import RegistryAdapter
from alpha.runtime_factor_selection import active_discovered_factor_ids, select_runtime_factors


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
            return [SimpleNamespace(factor=name, status="HEALTHY", n_obs=100) for name in names + ["rsi_14"]]

    monkeypatch.setenv("QUANT_RUNTIME_DISCOVERED_FACTOR_BUDGET", "2")
    monkeypatch.setattr(RegistryAdapter, "shared", staticmethod(lambda: _Adapter()))
    config = {"rsi_14": {"enabled": True}, "disc_d": {"enabled": True, "source": "discovered"}}
    try:
        active = active_discovered_factor_ids(config)
        selection = select_runtime_factors(config)
    finally:
        for name in names:
            factor_registry._factors.pop(name, None)

    assert active == ["disc_d", "disc_b"]
    assert selection is not None
    assert "disc_d" in selection.selected_factor_ids
    assert "disc_b" in selection.selected_factor_ids
    assert selection.reason_excluded["disc_a"] == "discovered_runtime_budget"
    assert selection.reason_excluded["disc_c"] == "discovered_runtime_budget"


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
