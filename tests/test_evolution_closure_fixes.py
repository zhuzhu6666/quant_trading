from __future__ import annotations

from dataclasses import dataclass


from alpha.registry_adapter import RegistryAdapter
from backend.runtime import evolution_orchestrator as evo
from backend.services import live_service
from config import runtime_config as rc
from deployment.canary import ACTIVE, CANARY_5, CANARY_50, CanaryEvalContext


@dataclass
class _Stats:
    n_trades: int


class _Attr:
    def get_all_factor_stats(self):
        return {"foo": _Stats(n_trades=60)}


class _AWE:
    _blend_baselines = {}

    def adapt(self, *args, **kwargs):
        return {"foo": {"weight": 0.0, "reason": "test_disable"}}


class _Adapter:
    def __init__(self, source="shadow"):
        self._meta = {"foo": {"source": source, "score": 1.0}}

    def get_meta(self, name):
        return dict(self._meta[name])


def test_scheduled_awe_adapt_publishes_runtime_patch(monkeypatch):
    rc.reset_for_tests()
    rc.patch({"awe_min_trades": 1, "factor_portfolio_weights": {"foo": 1.0}})

    monkeypatch.setattr(
        live_service,
        "_factor_pipeline",
        {"attribution": _Attr(), "awe": _AWE(), "engine": None},
    )

    live_service._scheduled_awe_adapt()

    assert rc.shared().factor_portfolio_weights == {"foo": 0.0}


def test_canary_intermediate_stage_does_not_execute_promotion(monkeypatch):
    saved = {}
    monkeypatch.setattr(RegistryAdapter, "shared", staticmethod(lambda: _Adapter("shadow")))
    monkeypatch.setattr(evo, "_load_canary_states", lambda: {})
    monkeypatch.setattr(evo, "_save_canary_states", lambda states: saved.update(states))
    monkeypatch.setattr(
        evo,
        "_load_canary_ctx_from_log",
        lambda name, score: CanaryEvalContext(oos_bars=10, oos_pnl=0.005),
    )

    promotions, rollbacks, stay = evo._run_canary_evaluation("XAUUSD+", "M5", 1000)

    assert promotions == []
    assert rollbacks == []
    assert stay == ["foo"]
    assert saved["foo"]["stage"] == CANARY_5


def test_canary_only_active_stage_executes_promotion(monkeypatch):
    saved = {}
    monkeypatch.setattr(RegistryAdapter, "shared", staticmethod(lambda: _Adapter("shadow")))
    monkeypatch.setattr(
        evo,
        "_load_canary_states",
        lambda: {"foo": {"stage": CANARY_50, "oos_bars": 60, "cumulative_pnl": 0.006}},
    )
    monkeypatch.setattr(evo, "_save_canary_states", lambda states: saved.update(states))
    monkeypatch.setattr(
        evo,
        "_load_canary_ctx_from_log",
        lambda name, score: CanaryEvalContext(oos_bars=100, oos_pnl=0.01),
    )

    promotions, rollbacks, stay = evo._run_canary_evaluation("XAUUSD+", "M5", 1000)

    assert promotions == ["foo"]
    assert rollbacks == []
    assert stay == []
    assert saved["foo"]["stage"] == ACTIVE


def test_canary_restores_legacy_lowercase_shadow(monkeypatch):
    saved = {}
    monkeypatch.setattr(RegistryAdapter, "shared", staticmethod(lambda: _Adapter("shadow")))
    monkeypatch.setattr(evo, "_load_canary_states", lambda: {"foo": {"stage": "shadow"}})
    monkeypatch.setattr(evo, "_save_canary_states", lambda states: saved.update(states))
    monkeypatch.setattr(
        evo,
        "_load_canary_ctx_from_log",
        lambda name, score: CanaryEvalContext(oos_bars=10, oos_pnl=0.005),
    )

    promotions, _, stay = evo._run_canary_evaluation("XAUUSD+", "M5", 1000)

    assert promotions == []
    assert stay == ["foo"]
    assert saved["foo"]["stage"] == CANARY_5
