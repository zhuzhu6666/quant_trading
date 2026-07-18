from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


from alpha.registry_adapter import RegistryAdapter
from backend.runtime import evolution_orchestrator as evo
from backend.services import live_service
from backend.services import shadow_service
from config import runtime_config as rc
from deployment.canary import ACTIVE, CANARY_5, CANARY_20, CANARY_50, PROBATION, QUARANTINED, CanaryEvalContext


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
        self.promoted = []
        self.registered = []

    def get_meta(self, name):
        return dict(self._meta[name])

    def promote(self, name, new_source, reason=""):
        self.promoted.append((name, new_source, reason))
        self._meta[name]["source"] = new_source
        return True

    def register_runtime(self, name, func, source, description=""):
        self.registered.append((name, source, description))
        self._meta[name] = {"source": source, "score": 0.0, "description": description}
        return True


class _RiskVerdict:
    def __init__(self, allowed=True, reason="ok"):
        self.allowed = allowed
        self.reason = reason

    def to_dict(self):
        return {"allowed": self.allowed, "reason": self.reason}


class _RiskPolicy:
    def __init__(self, allowed=True, reason="ok"):
        self.allowed = allowed
        self.reason = reason
        self.calls = []

    def evaluate(self, action, context):
        self.calls.append((action, context))
        return _RiskVerdict(self.allowed, self.reason)


def test_scheduled_awe_adapt_publishes_runtime_patch(monkeypatch):
    monkeypatch.setenv("QUANT_ALLOW_PYTEST_STATE_OVERLAY_WRITE", "1")
    rc.reset_for_tests()
    rc.patch({
        "autonomy_expansion_frozen": False,
        "awe_min_trades": 1,
        "factor_portfolio_weights": {"foo": 1.0, "bar": 2.0},
    })

    class _MutationService:
        def apply_patch(self, patch, **kwargs):
            cfg = rc.shared().to_dict()
            weights = dict(cfg.get("factor_portfolio_weights") or {})
            weights.update(patch["factor_portfolio_weights"])
            rc.patch({"factor_portfolio_weights": weights})
            return {"ok": True, "status": "applied", "version": 1}

    monkeypatch.setattr(
        "backend.services.runtime_config_mutation.RuntimeConfigMutationService",
        lambda: _MutationService(),
    )
    monkeypatch.setattr(
        "backend.services.experience_prior.ExperiencePriorService.priors",
        lambda self: {},
    )
    monkeypatch.setattr(
        "backend.services.learning_experiment_admission.LearningExperimentAdmissionService.evaluate",
        lambda self, **kwargs: {"allowed": True, "status": "admitted"},
    )
    monkeypatch.setattr(
        "backend.services.learning_experiment_admission.LearningExperimentAdmissionService.reserve_batch",
        lambda self, decisions, **kwargs: {
            "status": "reserved",
            "admissions": {
                name: {"allowed": True, "status": "reserved", "reservation_id": f"res_{name}"}
                for name in decisions
            },
            "reservations": {name: f"res_{name}" for name in decisions},
        },
    )
    monkeypatch.setattr(
        "backend.services.learning_experiment_admission.LearningExperimentAdmissionService.finalize_reservation",
        lambda self, reservation_id, **kwargs: {"status": "consumed"},
    )
    monkeypatch.setattr(
        "backend.services.factor_weight_change.FactorWeightChangeService._replay_admission",
        lambda self, decisions: {"required": True, "allowed": True, "evidence_grade": "A"},
    )
    monkeypatch.setattr(
        "backend.services.learning_application_state.LearningApplicationStateService.prepare",
        lambda self, **kwargs: "app_test",
    )
    monkeypatch.setattr(
        "backend.services.learning_application_state.LearningApplicationStateService.transition",
        lambda self, application_id, **kwargs: {"ok": True, "application_id": application_id},
    )
    risk = _RiskPolicy(allowed=True)
    monkeypatch.setattr(
        "risk.policy_service.RiskPolicyService.shared",
        staticmethod(lambda: risk),
    )
    monkeypatch.setattr(
        "backend.services.v16_command_gate.V16CommandGate.authorize",
        lambda *_args, **_kwargs: {"allowed": True, "status": "test_authorized"},
    )

    monkeypatch.setattr(
        live_service,
        "_factor_pipeline",
        {"attribution": _Attr(), "awe": _AWE(), "engine": None},
    )

    live_service._scheduled_awe_adapt()

    assert rc.shared().factor_portfolio_weights == {"foo": 0.0, "bar": 2.0}
    assert risk.calls[0][0] == "update_weight"
    assert risk.calls[0][1]["proposed_weights"] == {"foo": 0.0}


def test_scheduled_awe_adapt_risk_block_prevents_runtime_patch(monkeypatch):
    monkeypatch.setenv("QUANT_ALLOW_PYTEST_STATE_OVERLAY_WRITE", "1")
    rc.reset_for_tests()
    rc.patch({
        "autonomy_expansion_frozen": False,
        "awe_min_trades": 1,
        "factor_portfolio_weights": {"foo": 1.0},
    })
    writes = []

    class _MutationService:
        def apply_patch(self, patch, **kwargs):
            writes.append((patch, kwargs))
            return {"ok": True}

    risk = _RiskPolicy(allowed=False, reason="risk_budget_exhausted")
    monkeypatch.setattr(
        "backend.services.runtime_config_mutation.RuntimeConfigMutationService",
        lambda: _MutationService(),
    )
    monkeypatch.setattr(
        "backend.services.experience_prior.ExperiencePriorService.priors",
        lambda self: {},
    )
    monkeypatch.setattr(
        "backend.services.learning_experiment_admission.LearningExperimentAdmissionService.evaluate",
        lambda self, **kwargs: {"allowed": True, "status": "admitted"},
    )
    monkeypatch.setattr(
        "backend.services.learning_experiment_admission.LearningExperimentAdmissionService.reserve_batch",
        lambda self, decisions, **kwargs: {
            "status": "reserved",
            "admissions": {
                name: {"allowed": True, "status": "reserved", "reservation_id": f"res_{name}"}
                for name in decisions
            },
            "reservations": {name: f"res_{name}" for name in decisions},
        },
    )
    monkeypatch.setattr(
        "backend.services.learning_experiment_admission.LearningExperimentAdmissionService.release_reservations",
        lambda self, reservation_ids: None,
    )
    monkeypatch.setattr(
        "backend.services.factor_weight_change.FactorWeightChangeService._replay_admission",
        lambda self, decisions: {"required": True, "allowed": True, "evidence_grade": "A"},
    )
    monkeypatch.setattr(
        "risk.policy_service.RiskPolicyService.shared",
        staticmethod(lambda: risk),
    )
    monkeypatch.setattr(
        "backend.services.v16_command_gate.V16CommandGate.authorize",
        lambda *_args, **_kwargs: {"allowed": True, "status": "test_authorized"},
    )
    monkeypatch.setattr(
        live_service,
        "_factor_pipeline",
        {"attribution": _Attr(), "awe": _AWE(), "engine": None},
    )

    live_service._scheduled_awe_adapt()

    assert writes == []
    assert rc.shared().factor_portfolio_weights == {"foo": 1.0}
    assert risk.calls[0][0] == "update_weight"


def test_scheduled_awe_adapt_skips_while_expansion_is_frozen(monkeypatch):
    rc.reset_for_tests()
    rc.patch({"autonomy_mode": "live_candidate", "autonomy_expansion_frozen": True, "awe_min_trades": 1})

    class _ExplodingAttribution:
        def get_all_factor_stats(self):
            raise AssertionError("frozen AWE must not evaluate or mutate weights")

    monkeypatch.setattr(
        live_service,
        "_factor_pipeline",
        {"attribution": _ExplodingAttribution(), "awe": object(), "engine": None},
    )

    live_service._scheduled_awe_adapt()


def test_legacy_param_tune_entrypoint_is_removed():
    assert not hasattr(live_service, "_scheduled_param_tune")


def test_canary_intermediate_stage_does_not_execute_promotion(monkeypatch):
    rc.patch({"autonomy_expansion_frozen": False})
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


def test_canary_stage_does_not_advance_while_expansion_is_frozen(monkeypatch):
    saved = {}
    rc.patch({"autonomy_mode": "live_candidate", "autonomy_expansion_frozen": True})
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
    assert saved["foo"]["stage"] == "SHADOW"


def test_demo_canary_advances_even_when_global_expansion_freeze_is_configured(monkeypatch):
    saved = {}
    rc.patch({"autonomy_mode": "demo_nursery", "autonomy_expansion_frozen": True})
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


def test_canary_canary50_enters_probation_without_execution(monkeypatch):
    rc.patch({"autonomy_expansion_frozen": False})
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

    assert promotions == []
    assert rollbacks == []
    assert stay == ["foo"]
    assert saved["foo"]["stage"] == PROBATION


def test_canary_only_active_stage_executes_promotion(monkeypatch):
    rc.patch({"autonomy_expansion_frozen": False})
    saved = {}
    monkeypatch.setattr(RegistryAdapter, "shared", staticmethod(lambda: _Adapter("shadow")))
    monkeypatch.setattr(
        evo,
        "_load_canary_states",
        lambda: {"foo": {"stage": PROBATION, "oos_bars": 100, "cumulative_pnl": 0.01}},
    )
    monkeypatch.setattr(evo, "_save_canary_states", lambda states: saved.update(states))
    monkeypatch.setattr(
        evo,
        "_load_canary_ctx_from_log",
        lambda name, score: CanaryEvalContext(oos_bars=120, oos_pnl=0.012),
    )

    promotions, rollbacks, stay = evo._run_canary_evaluation("XAUUSD+", "M5", 1000)

    assert promotions == ["foo"]
    assert rollbacks == []
    assert stay == []
    assert saved["foo"]["stage"] == ACTIVE


def test_canary_restores_legacy_lowercase_shadow(monkeypatch):
    rc.patch({"autonomy_expansion_frozen": False})
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

def test_discovered_factor_is_demoted_until_canary_is_active(monkeypatch):
    rc.patch({"autonomy_expansion_frozen": False})
    saved = {}
    monkeypatch.setattr(RegistryAdapter, "shared", staticmethod(lambda: _Adapter("discovered")))
    monkeypatch.setattr(
        evo,
        "_load_canary_states",
        lambda: {"foo": {"stage": CANARY_20, "oos_bars": 30, "cumulative_pnl": 0.004}},
    )
    monkeypatch.setattr(evo, "_save_canary_states", lambda states: saved.update(states))
    monkeypatch.setattr(
        evo,
        "_load_canary_ctx_from_log",
        lambda name, score: CanaryEvalContext(oos_bars=60, oos_pnl=0.006),
    )

    promotions, rollbacks, stay = evo._run_canary_evaluation("XAUUSD+", "M5", 1000)

    assert promotions == []
    assert rollbacks == ["foo"]
    assert stay == []
    assert saved["foo"]["stage"] == CANARY_50


def test_canary_rollback_count_and_history_survive_cycle(monkeypatch):
    rc.patch({"autonomy_expansion_frozen": False})
    saved = {}
    monkeypatch.setattr(RegistryAdapter, "shared", staticmethod(lambda: _Adapter("shadow")))
    monkeypatch.setattr(
        evo,
        "_load_canary_states",
        lambda: {
            "foo": {
                "stage": CANARY_5,
                "oos_bars": 10,
                "cumulative_pnl": 0.002,
                "rollback_count": 2,
                "events": [{"event": "prior"}],
            }
        },
    )
    monkeypatch.setattr(evo, "_save_canary_states", lambda states: saved.update(states))
    monkeypatch.setattr(
        evo,
        "_load_canary_ctx_from_log",
        lambda name, score: CanaryEvalContext(oos_bars=10, oos_pnl=-0.001),
    )

    _, rollbacks, _ = evo._run_canary_evaluation("XAUUSD+", "M5", 1000)

    assert rollbacks == ["foo"]
    assert saved["foo"]["stage"] == QUARANTINED
    assert saved["foo"]["rollback_count"] == 3
    assert len(saved["foo"]["events"]) > 1


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
    assert len(perf.evidence_hash) == 64
    assert len(perf.dataset_hash) == 64
    assert perf.new_evidence_bars == len(df)

    repeated = evaluate_factor(
        df,
        "mom",
        momentum_factor,
        symbol="XAUUSD+",
        timeframe="M5",
        previous_perf=perf,
    )
    assert repeated is not None
    assert repeated.evidence_hash == perf.evidence_hash
    assert repeated.new_evidence_bars == 0


def test_canary_requires_new_evidence_between_stage_promotions():
    from deployment.canary import CanaryDirector

    director = CanaryDirector()
    first = CanaryEvalContext(
        oos_bars=100,
        oos_pnl=0.02,
        additional_metrics={
            "evidence_hash": "evidence-1",
            "dataset_hash": "dataset-1",
            "evidence_end_at": "2026-07-10T10:00:00Z",
            "new_evidence_bars": 100,
            "hit_rate": 0.6,
            "n_active": 100,
        },
    )
    assert director.check_promotion("fresh_alpha", first) == "promote"
    assert director.promote("fresh_alpha") is True
    assert director.get_stage("fresh_alpha") == CANARY_5

    # The same aggregate window cannot be consumed again for CANARY_20.
    assert director.check_promotion("fresh_alpha", first) == "stay"
    assert director.get_state("fresh_alpha").fresh_evidence_bars == 0

    second = CanaryEvalContext(
        oos_bars=110,
        oos_pnl=0.022,
        additional_metrics={
            **first.additional_metrics,
            "evidence_hash": "evidence-2",
            "dataset_hash": "dataset-2",
            "evidence_end_at": "2026-07-10T10:50:00Z",
            "new_evidence_bars": 10,
        },
    )
    assert director.check_promotion("fresh_alpha", second) == "stay"

    third = CanaryEvalContext(
        oos_bars=115,
        oos_pnl=0.024,
        additional_metrics={
            **second.additional_metrics,
            "evidence_hash": "evidence-3",
            "dataset_hash": "dataset-3",
            "evidence_end_at": "2026-07-10T11:15:00Z",
            "new_evidence_bars": 5,
        },
    )
    assert director.check_promotion("fresh_alpha", third) == "promote"
    assert director.promote("fresh_alpha") is True
    assert director.get_stage("fresh_alpha") == CANARY_20


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


def test_collect_learning_suggestions_keeps_approved_observational(tmp_path, monkeypatch):
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
            ("a4", "baz", "boost_small", 0.6, "auto_approved", 9_999_999_300),
            ("a5", "baz", "downweight", 0.6, "pending_review", 9_999_999_400),
        ],
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(evo._time, "time", lambda: 10_000_000_000)
    summary, biases = evo._collect_learning_suggestions(max_age_days=30)

    assert summary["foo"]["proposed"] == 1
    assert summary["foo"]["approved"] == 1
    assert summary["bar"]["approved"] == 1
    assert summary["baz"]["proposed"] == 1
    assert summary["baz"]["approved"] == 1
    assert biases == {}

    original = {"foo": 0.4, "bar": 0.3, "baz": 0.3}
    adjusted, applied = evo._apply_learning_biases(original, biases)
    assert adjusted == original
    assert applied == {}


def test_collect_learning_suggestions_requires_applied_authority_for_biases(tmp_path, monkeypatch):
    import sqlite3

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(evo, "_CANARY_DB", db_path)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE policy_suggestion (
            suggestion_id TEXT PRIMARY KEY,
            scope_type TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            action TEXT NOT NULL,
            confidence REAL DEFAULT 0.0,
            reason TEXT DEFAULT '',
            evidence_json TEXT DEFAULT '{}',
            status TEXT DEFAULT 'proposed',
            reviewed_at REAL DEFAULT 0.0,
            created_at REAL NOT NULL DEFAULT 0.0,
            applied_mutation_id TEXT DEFAULT ''
        );
        CREATE TABLE governance_mutation_intent (
            mutation_id TEXT PRIMARY KEY,
            status TEXT NOT NULL
        );
        """
    )
    rows = [
        ("legacy_tighten", "foo", "downweight", 0.8, "applied", "", 9_999_999_100),
        ("legacy_expand", "bar", "boost_small", 0.8, "applied", "", 9_999_999_200),
        ("committed_expand", "baz", "boost_small", 0.8, "applied", "mut_committed", 9_999_999_300),
        ("prepared_tighten", "qux", "downweight", 0.8, "applied", "mut_prepared", 9_999_999_400),
        ("approved_tighten", "watch", "downweight", 0.8, "approved", "", 9_999_999_500),
    ]
    conn.executemany(
        """
        INSERT INTO policy_suggestion
        (suggestion_id, scope_type, scope_key, action, confidence, status,
         applied_mutation_id, reviewed_at, created_at)
        VALUES (?, 'factor', ?, ?, ?, ?, ?, ?, ?)
        """,
        [(*row[:-1], row[-1], row[-1]) for row in rows],
    )
    conn.executemany(
        "INSERT INTO governance_mutation_intent (mutation_id, status) VALUES (?, ?)",
        [("mut_committed", "committed"), ("mut_prepared", "prepared")],
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(evo._time, "time", lambda: 10_000_000_000)
    summary, dual_biases = evo._collect_learning_suggestions(
        max_age_days=30,
        coordinator_mode="dual_record",
    )

    assert summary["watch"]["approved"] == 1
    assert set(dual_biases) == {"foo", "baz"}
    assert dual_biases["foo"]["governance_authorities"] == ["legacy_quarantined"]
    assert dual_biases["foo"]["committed_mutation_ids"] == []
    assert dual_biases["foo"]["multiplier"] < 1.0
    assert dual_biases["baz"]["governance_authorities"] == ["committed_mutation"]
    assert dual_biases["baz"]["committed_mutation_ids"] == ["mut_committed"]
    assert dual_biases["baz"]["multiplier"] > 1.0
    assert "bar" not in dual_biases
    assert "qux" not in dual_biases
    assert "watch" not in dual_biases

    _summary, enforce_biases = evo._collect_learning_suggestions(
        max_age_days=30,
        coordinator_mode="enforce",
    )
    assert set(enforce_biases) == {"baz"}
    adjusted, applied = evo._apply_learning_biases(
        {"baz": 0.5, "other": 0.5},
        enforce_biases,
    )
    assert adjusted["baz"] > 0.5
    assert adjusted["other"] < 0.5
    assert applied["baz"]["committed_mutation_ids"] == ["mut_committed"]


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


def test_apply_learning_biases_uses_current_runtime_weight_for_snapshot_only_factor():
    adjusted, applied = evo._apply_learning_biases(
        {"foo": 0.6, "bar": 0.4},
        {
            "dsl_auto_hot": {"multiplier": 0.82, "action": "downweight", "suggestion_ids": ["s_hot"]},
        },
        base_weights={"dsl_auto_hot": 0.3},
    )

    assert "dsl_auto_hot" in adjusted
    assert abs(sum(adjusted.values()) - 1.0) < 1e-6
    assert applied["dsl_auto_hot"]["old_weight"] == 0.3
    assert applied["dsl_auto_hot"]["biased_weight"] == 0.246
    assert applied["dsl_auto_hot"]["suggestion_ids"] == ["s_hot"]


def test_apply_learning_biases_uses_source_weight_when_runtime_weight_missing():
    adjusted, applied = evo._apply_learning_biases(
        {"foo": 1.0},
        {
            "dsl_auto_hot": {
                "multiplier": 0.82,
                "action": "downweight",
                "suggestion_ids": ["s_hot"],
                "source_weight": 0.3,
            },
        },
    )

    assert "dsl_auto_hot" in adjusted
    assert abs(sum(adjusted.values()) - 1.0) < 1e-6
    assert applied["dsl_auto_hot"]["old_weight"] == 0.3
    assert applied["dsl_auto_hot"]["biased_weight"] == 0.246


def test_evolution_shadow_register_blocked_by_risk_policy(monkeypatch):
    import risk.policy_service as policy_service

    policy = _RiskPolicy(allowed=False, reason="drawdown_too_high_for_new_factor")
    monkeypatch.setattr(policy_service.RiskPolicyService, "shared", staticmethod(lambda: policy))
    monkeypatch.setattr(evo, "_emit_evolution_story", lambda *args, **kwargs: None)

    class _Expr:
        expression = "rank(close)"

    assert evo._register_shadow_factors([_Expr()]) == 0
    assert policy.calls[0][0] == "register_factor"


def test_evolution_shadow_register_skips_invalid_dsl_before_registration(monkeypatch):
    import risk.policy_service as policy_service
    from alpha.factor_identity import factor_definition_fingerprint
    import backend.services.governance_control_plans as control_plans

    adapter = _Adapter()
    policy = _RiskPolicy(allowed=True)
    stories = []
    monkeypatch.setattr(policy_service.RiskPolicyService, "shared", staticmethod(lambda: policy))
    monkeypatch.setattr(RegistryAdapter, "shared", staticmethod(lambda: adapter))
    monkeypatch.setattr(control_plans, "governance_coordinator_mode", lambda: "off")
    monkeypatch.setattr(evo, "_emit_evolution_story", lambda event, payload: stories.append((event, payload)))

    class _BadExpr:
        name = "dsl_auto_bad"
        expression = "rank(close"

    class _GoodExpr:
        name = "dsl_auto_good"
        expression = "rank(close)"

    assert evo._register_shadow_factors([_BadExpr(), _GoodExpr()]) == 1
    stable_name = f"dsl_auto_{factor_definition_fingerprint('rank(close)')}"
    assert adapter.registered == [(stable_name, "shadow", "rank(close)")]
    assert stories[0][0] == "shadow_register_invalid_dsl_skipped"
    assert stories[0][1]["factor"] == "dsl_auto_bad"


def test_evolution_shadow_register_commits_lifecycle_before_registry_in_enforce(
    monkeypatch,
):
    import risk.policy_service as policy_service
    from alpha.factor_identity import factor_definition_fingerprint
    import backend.services.factor_lifecycle_service as lifecycle_module
    import backend.services.governance_control_plans as control_plans

    adapter = _Adapter()
    policy = _RiskPolicy(allowed=True)
    committed = []
    monkeypatch.setattr(policy_service.RiskPolicyService, "shared", staticmethod(lambda: policy))
    monkeypatch.setattr(RegistryAdapter, "shared", staticmethod(lambda: adapter))
    monkeypatch.setattr(control_plans, "governance_coordinator_mode", lambda: "enforce")

    class _Lifecycle:
        def __init__(self, *args, **kwargs):
            assert kwargs["adapter"] is adapter

        def register_shadow(self, **kwargs):
            committed.append(kwargs)
            return {"ok": True, "status": "committed", "mutation_id": "mut-shadow"}

    monkeypatch.setattr(lifecycle_module, "FactorLifecycleService", _Lifecycle)

    class _Expr:
        name = "human_label_is_not_identity"
        expression = "rank(close)"

    assert evo._register_shadow_factors([_Expr()]) == 1
    fingerprint = factor_definition_fingerprint("rank(close)")
    assert committed[0]["name"] == f"dsl_auto_{fingerprint}"
    assert committed[0]["artifact_hash"] == fingerprint
    assert committed[0]["idempotency_key"] == f"evolution-shadow:{fingerprint}"
    assert adapter.registered == []


def test_shadow_promote_blocked_by_risk_policy(monkeypatch):
    import risk.policy_service as policy_service

    adapter = _Adapter("shadow")
    policy = _RiskPolicy(allowed=False, reason="drawdown_too_high_for_promotion")
    monkeypatch.setattr(shadow_service, "_get_adapter", lambda: adapter)
    monkeypatch.setattr(policy_service.RiskPolicyService, "shared", staticmethod(lambda: policy))

    result = shadow_service.promote("foo")

    assert result["ok"] is False
    assert "risk_policy_block" in result["error"]
    assert adapter.promoted == []


def test_canary_evaluation_is_bounded_and_rotates_oldest_candidates(monkeypatch):
    class _ManyAdapter:
        def __init__(self):
            self._meta = {
                f"factor_{idx:02d}": {"source": "shadow", "score": float(idx)}
                for idx in range(12)
            }

        def get_meta(self, name):
            return dict(self._meta[name])

    saved = {}
    states = {
        f"factor_{idx:02d}": {"stage": "SHADOW", "updated_at": float(idx)}
        for idx in range(12)
    }
    states["factor_11"]["stage"] = "PROBATION"
    monkeypatch.setenv("QUANT_CANARY_EVALUATION_LIMIT", "10")
    monkeypatch.setattr(RegistryAdapter, "shared", staticmethod(_ManyAdapter))
    monkeypatch.setattr(evo, "_load_canary_states", lambda: states)
    monkeypatch.setattr(evo, "_save_canary_states", lambda value: saved.update(value))
    monkeypatch.setattr(evo, "_emit_evolution_story", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        evo,
        "_load_canary_ctx_from_log",
        lambda name, score: CanaryEvalContext(oos_bars=0, oos_pnl=0.0),
    )

    promotions, rollbacks, stay = evo._run_canary_evaluation("XAUUSD+", "M5", 1000)

    assert promotions == []
    assert rollbacks == []
    assert len(stay) == 10
    assert set(saved) == {"factor_11", *(f"factor_{idx:02d}" for idx in range(9))}
