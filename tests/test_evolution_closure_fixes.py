from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


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


def test_shadow_trader_evaluate_factor_builds_virtual_perf():
    from alpha.shadow_trader import evaluate_factor

    df = pd.DataFrame({
        "open": np.linspace(100.0, 120.0, 80),
        "high": np.linspace(101.0, 121.0, 80),
        "low": np.linspace(99.0, 119.0, 80),
        "close": np.linspace(100.0, 120.0, 80),
        "volume": np.ones(80) * 100,
    })

    def momentum_factor(frame):
        return pd.Series(frame["close"]).diff(3).to_numpy()

    perf = evaluate_factor(df, "mom", momentum_factor, symbol="XAUUSD+", timeframe="M5")

    assert perf is not None
    assert perf.factor == "mom"
    assert perf.oos_bars > 0
    assert 0.0 <= perf.hit_rate <= 1.0


def test_canary_context_prefers_shadow_factor_perf(monkeypatch):
    from alpha.shadow_trader import ShadowPerf
    import alpha.shadow_trader as shadow_trader

    perf = ShadowPerf(
        factor="foo",
        source="shadow",
        symbol="XAUUSD+",
        timeframe="M5",
        oos_bars=88,
        cumulative_pnl=0.0123,
        hit_rate=0.61,
        max_drawdown=0.002,
        last_signal=0.7,
        n_valid=100,
        n_active=88,
    )
    monkeypatch.setattr(shadow_trader, "load_shadow_perf", lambda name: perf)

    ctx = evo._load_canary_ctx_from_log("foo", score=0.0)

    assert ctx.oos_bars == 88
    assert ctx.oos_pnl == 0.0123
    assert ctx.additional_metrics["source"] == "shadow_factor_perf"


def test_update_shadow_performance_uses_shadow_trader(monkeypatch):
    import alpha.shadow_trader as shadow_trader

    calls = []

    def fake_eval(df, **kwargs):
        calls.append(kwargs)
        return {"foo": object()}

    monkeypatch.setattr(shadow_trader, "evaluate_shadow_factors", fake_eval)
    monkeypatch.setattr(evo, "_emit_evolution_story", lambda *args, **kwargs: None)

    count = evo._update_shadow_performance(pd.DataFrame({"close": [1, 2, 3]}), "XAUUSD+", "M5")

    assert count == 1
    assert calls[0]["symbol"] == "XAUUSD+"
    assert calls[0]["timeframe"] == "M5"
    assert calls[0]["persist"] is True


def test_collect_learning_suggestions_separates_proposed_and_approved(tmp_path, monkeypatch):
    import sqlite3

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(evo, "_CANARY_DB", db_path)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS policy_suggestion (
            suggestion_id TEXT PRIMARY KEY,
            scope_type TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            action TEXT NOT NULL,
            confidence REAL DEFAULT 0.0,
            reason TEXT DEFAULT '',
            evidence_json TEXT DEFAULT '{}',
            status TEXT DEFAULT 'proposed',
            created_at REAL NOT NULL DEFAULT 0.0
        );
        """
    )
    conn.executemany(
        """
        INSERT INTO policy_suggestion
        (suggestion_id, scope_type, scope_key, action, confidence, status, created_at)
        VALUES (?, 'factor', ?, ?, ?, ?, ?)
        """,
        [
            ("a1", "foo", "downweight", 0.9, "proposed", 9_999_999_000),
            ("a2", "foo", "downweight", 0.8, "approved", 9_999_999_100),
            ("a3", "bar", "boost_small", 0.5, "approved", 9_999_999_200),
        ],
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(evo._time, "time", lambda: 10_000_000_000)
    summary, biases = evo._collect_learning_suggestions(max_age_days=30)

    assert summary["foo"]["proposed"] == 1
    assert summary["foo"]["approved"] == 1
    assert summary["bar"]["approved"] == 1
    assert biases["foo"]["multiplier"] < 1.0
    assert biases["bar"]["multiplier"] > 1.0
    assert biases["foo"]["suggestion_ids"] == ["a2"]


def test_apply_learning_biases_is_small_and_normalized():
    adjusted, applied = evo._apply_learning_biases(
        {"foo": 0.6, "bar": 0.4},
        {
            "foo": {"multiplier": 0.8, "action": "downweight", "suggestion_ids": ["s1"]},
            "bar": {"multiplier": 1.04, "action": "boost_small", "suggestion_ids": ["s2"]},
        },
    )

    assert set(adjusted) == {"foo", "bar"}
    assert abs(sum(adjusted.values()) - 1.0) < 1e-6
    assert adjusted["foo"] < 0.6
    assert adjusted["bar"] > 0.4
    assert applied["foo"]["multiplier"] == 0.8
    assert applied["bar"]["multiplier"] == 1.04
