from __future__ import annotations

import time

import pytest

import backend.runtime.factor_governance_orchestrator as governance_module
from backend.runtime.factor_governance_orchestrator import FactorGovernanceOrchestrator
from backend.services.factor_identity import (
    canonical_factor_id,
    factor_definition_fingerprint,
)
from config import runtime_config as rc
from config.runtime_config import RuntimeConfig
from risk.governor import RiskGovernor
from risk.policy_service import RiskPolicyService, RiskVerdict


@pytest.fixture(autouse=True)
def _reset_runtime_config():
    rc.reset_for_tests()
    yield
    rc.reset_for_tests()


class _AllowRisk:
    def __init__(self) -> None:
        self.actions: list[str] = []

    def evaluate(self, action: str, _context: dict) -> RiskVerdict:
        self.actions.append(action)
        return RiskVerdict(True, "ok")


def _runtime_config(disabled_at: float) -> RuntimeConfig:
    return RuntimeConfig(
        autonomy_mode="demo_nursery",
        factor_signal_config={
            "pin_bar": {
                "enabled": False,
                "lifecycle_status": "QUARANTINE",
                "role": "alpha",
                "source": "builtin",
                "disabled_at": disabled_at,
            },
        },
        factor_portfolio_weights={"pin_bar": 0.1},
        factor_governance_auto_restore_enabled=True,
        factor_governance_restore_cooldown_days=7,
        factor_governance_restore_health_threshold=60.0,
        factor_governance_restore_max_weakness=0.65,
        factor_governance_max_restores_per_cycle=1,
        factor_health_min_n_obs=100,
    )


def _catalog(now: float, *, health_updated_at: float | None = None, shadow: dict | None = None) -> list[dict]:
    return [{
        "factor_id": "pin_bar",
        "source": "builtin",
        "lifecycle_origin": "builtin",
        "role": "alpha",
        "lifecycle_status": "QUARANTINED",
        "lifecycle_terminal_at": now - 8 * 86400,
        "governance_action": "retire_factor",
        "health_status": "WATCH",
        "health_score": 65.0,
        "health_n_obs": 2000,
        "health_updated_at": health_updated_at if health_updated_at is not None else now,
        "factor_governance_shadow": shadow or {},
    }]


@pytest.mark.parametrize(
    ("disabled_age_days", "health_updated_at_offset", "shadow"),
    [
        (1, 0, {}),
        (8, -1, {}),
        (8, 0, {"sample_count": 3, "avg_weakness_score": 0.8, "weakness_score": 0.8}),
    ],
)
def test_quarantined_builtin_alpha_requires_cooldown_fresh_health_and_cleared_model(
    disabled_age_days, health_updated_at_offset, shadow
):
    now = time.time()
    disabled_at = now - disabled_age_days * 86400
    rc.replace(_runtime_config(disabled_at))
    orchestrator = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())

    actions = orchestrator._restore_quarantined_builtin_alpha(
        _catalog(now, health_updated_at=disabled_at + health_updated_at_offset, shadow=shadow),
        {"run_id": "restore_gate_test"},
    )

    assert actions == []


def test_coordinator_off_does_not_bypass_with_legacy_quarantine_patch(monkeypatch):
    now = time.time()
    rc.replace(RuntimeConfig(
        autonomy_mode="live_candidate",
        factor_signal_config={
            "pin_bar": {"enabled": True, "lifecycle_status": "ACTIVE", "role": "alpha"},
        },
        factor_portfolio_weights={"pin_bar": 0.1},
    ))
    orchestrator = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    captured: list[dict] = []

    def apply_patch(patch, *, source, run_id):
        captured.append(patch)
        return {"ok": True}

    monkeypatch.setattr(orchestrator, "_apply_runtime_patch", apply_patch)
    monkeypatch.setattr(
        orchestrator,
        "_audit_action",
        lambda _run, _item, _action, status, *_args, **_kwargs: {
            "status": status
        },
    )
    # A pending experiment may defer another exploratory weight change, but
    # must never defer severe risk tightening.
    monkeypatch.setattr(orchestrator, "_factor_has_pending_effect", lambda _factor_id: True)

    actions = orchestrator._disable_weak_live_alpha(
        [{
            "factor_id": "pin_bar",
            "source": "builtin",
            "role": "alpha",
            "eligible_for_live": True,
            "health_score": 20.0,
            "health_status": "DECAYING",
            "factor_governance_shadow": {},
        }],
        {"run_id": "disable_test"},
    )

    assert actions == [{"status": "blocked_by_evidence"}]
    assert captured == []


def test_typed_governance_quarantines_builtin_through_lifecycle(monkeypatch):
    import backend.services.governance_control_plans as control_plans

    rc.replace(
        RuntimeConfig(
            autonomy_mode="live_candidate",
            factor_signal_config={
                "pin_bar": {
                    "enabled": True,
                    "lifecycle_status": "ACTIVE",
                    "role": "alpha",
                    "source": "builtin",
                }
            },
            factor_portfolio_weights={"pin_bar": 0.1},
        )
    )
    calls: list[dict] = []

    class _FakeLifecycle:
        def __init__(self, _db_path, adapter=None):
            self.adapter = adapter

        def quarantine(self, **kwargs):
            calls.append(dict(kwargs))
            current = rc.shared().to_dict()
            current["factor_signal_config"]["pin_bar"].update(
                {"enabled": False, "lifecycle_status": "QUARANTINED"}
            )
            current["factor_portfolio_weights"]["pin_bar"] = 0.0
            rc.replace(RuntimeConfig.from_dict(current))
            return {
                "ok": True,
                "status": "committed",
                "lifecycle_stage": "QUARANTINED",
            }

    monkeypatch.setattr(control_plans, "governance_coordinator_mode", lambda: "dual_record")
    monkeypatch.setattr(governance_module, "FactorLifecycleService", _FakeLifecycle)
    orchestrator = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    monkeypatch.setattr(orchestrator, "_factor_has_pending_effect", lambda _factor_id: True)
    monkeypatch.setattr(
        orchestrator,
        "_apply_runtime_patch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("generic mutation must not run")
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "_audit_action",
        lambda *_args, **_kwargs: {"status": "applied"},
    )

    actions = orchestrator._disable_weak_live_alpha(
        [
            {
                "factor_id": "pin_bar",
                "source": "builtin",
                "role": "alpha",
                "eligible_for_live": True,
                "health_score": 20.0,
                "health_status": "DECAYING",
                "factor_governance_shadow": {},
            }
        ],
        {"run_id": "typed_builtin_quarantine"},
    )

    assert actions == [{"status": "applied"}]
    assert calls[0]["expression"] == "pin_bar"
    assert rc.shared().factor_signal_config["pin_bar"]["lifecycle_status"] == "QUARANTINED"
    assert rc.shared().factor_portfolio_weights["pin_bar"] == 0.0


def test_balanced_demo_requires_three_mature_disable_cycles(monkeypatch):
    import backend.services.governance_control_plans as control_plans

    now = time.time()
    cfg = RuntimeConfig(
        autonomy_mode="demo_autonomous",
        factor_signal_config={
            "pin_bar": {
                "enabled": True,
                "lifecycle_status": "ACTIVE",
                "role": "alpha",
                "source": "builtin",
                "health_gate_exempt": True,
            },
            "alpha_b": {"enabled": True, "lifecycle_status": "ACTIVE", "role": "alpha", "health_gate_exempt": True},
            "alpha_c": {"enabled": True, "lifecycle_status": "ACTIVE", "role": "alpha", "health_gate_exempt": True},
            "alpha_d": {"enabled": True, "lifecycle_status": "ACTIVE", "role": "alpha", "health_gate_exempt": True},
        },
        factor_portfolio_weights={
            "pin_bar": 0.1,
            "alpha_b": 0.1,
            "alpha_c": 0.1,
            "alpha_d": 0.1,
        },
    )
    rc.replace(cfg)
    monkeypatch.setattr(rc, "bounded_demo_mode_active", lambda _cfg=None: True)
    monkeypatch.setattr(
        control_plans,
        "governance_coordinator_mode",
        lambda: "dual_record",
    )
    orchestrator = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    profile = orchestrator._governance_profile(cfg)
    catalog = [
        {
            "factor_id": "pin_bar",
            "source": "builtin",
            "role": "alpha",
            "eligible_for_live": True,
            "health_score": 15.0,
            "health_status": "DECAYING",
            "health_n_obs": 1000,
            "health_updated_at": now,
            "weight": 0.1,
            "factor_governance_shadow": {},
        }
    ]
    monkeypatch.setattr(
        orchestrator,
        "_advance_disable_evidence_streaks",
        lambda candidates, **_kwargs: {
            "pin_bar": {**candidates["pin_bar"], "streak": 2}
        },
    )
    assert (
        orchestrator._disable_weak_live_alpha(
            catalog,
            {"run_id": "demo-streak-2"},
            cfg=cfg,
            profile=profile,
        )
        == []
    )

    calls: list[dict] = []

    class _FakeLifecycle:
        def __init__(self, _db_path, adapter=None):
            self.adapter = adapter

        def quarantine(self, **kwargs):
            calls.append(dict(kwargs))
            return {"ok": True, "status": "committed"}

    monkeypatch.setattr(governance_module, "FactorLifecycleService", _FakeLifecycle)
    monkeypatch.setattr(
        orchestrator,
        "_advance_disable_evidence_streaks",
        lambda candidates, **_kwargs: {
            "pin_bar": {**candidates["pin_bar"], "streak": 3}
        },
    )
    monkeypatch.setattr(
        orchestrator,
        "_audit_action",
        lambda *_args, **_kwargs: {"status": "applied"},
    )
    actions = orchestrator._disable_weak_live_alpha(
        catalog,
        {"run_id": "demo-streak-3"},
        cfg=cfg,
        profile=profile,
    )
    assert actions == [{"status": "applied"}]
    assert calls[0]["evidence_refs"]["evidence_streak"] == 3


def test_balanced_demo_quarantine_cannot_break_directional_contract(monkeypatch):
    now = time.time()
    signal_cfg = {
        name: {
            "enabled": True,
            "lifecycle_status": "ACTIVE",
            "role": "alpha",
            "source": "builtin",
            "health_gate_exempt": True,
        }
        for name in ("pin_bar", "alpha_b", "alpha_c")
    }
    cfg = RuntimeConfig(
        autonomy_mode="demo_autonomous",
        factor_signal_config=signal_cfg,
        factor_portfolio_weights={name: 0.1 for name in signal_cfg},
    )
    rc.replace(cfg)
    monkeypatch.setattr(rc, "bounded_demo_mode_active", lambda _cfg=None: True)
    orchestrator = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    profile = orchestrator._governance_profile(cfg)
    monkeypatch.setattr(
        orchestrator,
        "_advance_disable_evidence_streaks",
        lambda candidates, **_kwargs: {
            "pin_bar": {**candidates["pin_bar"], "streak": 3}
        },
    )
    monkeypatch.setattr(
        governance_module,
        "FactorLifecycleService",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("lifecycle mutation must not be created")
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "_audit_action",
        lambda _run, _item, _action, status, *_args, **_kwargs: {
            "status": status
        },
    )

    actions = orchestrator._disable_weak_live_alpha(
        [
            {
                "factor_id": "pin_bar",
                "source": "builtin",
                "role": "alpha",
                "eligible_for_live": True,
                "health_score": 15.0,
                "health_status": "DECAYING",
                "health_n_obs": 1000,
                "health_updated_at": now,
                "weight": 0.1,
                "factor_governance_shadow": {},
            }
        ],
        {"run_id": "demo-guard-block"},
        cfg=cfg,
        profile=profile,
    )

    assert actions == [{"status": "blocked_by_directional_portfolio_guard"}]


def test_balanced_demo_small_model_sample_never_hard_quarantines(monkeypatch):
    now = time.time()
    cfg = RuntimeConfig(autonomy_mode="demo_autonomous")
    monkeypatch.setattr(rc, "bounded_demo_mode_active", lambda _cfg=None: True)
    orchestrator = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    captured: list[dict] = []
    monkeypatch.setattr(
        orchestrator,
        "_advance_disable_evidence_streaks",
        lambda candidates, **_kwargs: captured.append(candidates) or {},
    )
    actions = orchestrator._disable_weak_live_alpha(
        [
            {
                "factor_id": "pin_bar",
                "source": "builtin",
                "role": "alpha",
                "eligible_for_live": True,
                "health_score": 50.0,
                "health_status": "WATCH",
                "health_n_obs": 2000,
                "health_updated_at": now,
                "weight": 0.1,
                "factor_governance_shadow": {
                    "sample_count": 6,
                    "weak_sample_count": 6,
                    "avg_weakness_score": 0.99,
                },
            }
        ],
        {"run_id": "demo-small-model"},
        cfg=cfg,
        profile=orchestrator._governance_profile(cfg),
    )
    assert actions == []
    assert captured == [{}]


def test_demo_disable_streak_advances_only_for_unique_evidence_cycles(
    tmp_path,
):
    from backend.core.db import connect_sqlite

    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    conn.execute(
        """CREATE TABLE runtime_kv (
           key TEXT PRIMARY KEY,
           value_json TEXT NOT NULL DEFAULT '{}',
           updated_at REAL NOT NULL DEFAULT 0.0
        )"""
    )
    conn.commit()
    conn.close()

    orchestrator = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    orchestrator.overlay.db_path = db_path
    first = {
        "pin_bar": {
            "reason": "persistent_severe_health",
            "evidence_cycle_id": "health:1",
        }
    }
    assert orchestrator._advance_disable_evidence_streaks(
        first,
        now=10.0,
    )["pin_bar"]["streak"] == 1
    assert orchestrator._advance_disable_evidence_streaks(
        first,
        now=11.0,
    )["pin_bar"]["streak"] == 1
    second = {
        "pin_bar": {
            "reason": "persistent_severe_health",
            "evidence_cycle_id": "health:2",
        }
    }
    assert orchestrator._advance_disable_evidence_streaks(
        second,
        now=12.0,
    )["pin_bar"]["streak"] == 2
    assert orchestrator._advance_disable_evidence_streaks(
        {},
        now=13.0,
    ) == {}
    third = {
        "pin_bar": {
            "reason": "persistent_severe_health",
            "evidence_cycle_id": "health:3",
        }
    }
    assert orchestrator._advance_disable_evidence_streaks(
        third,
        now=14.0,
    )["pin_bar"]["streak"] == 1


def test_typed_demo_governance_reenrolls_terminal_builtin_as_new_shadow(monkeypatch):
    import backend.services.governance_control_plans as control_plans

    now = time.time()
    rc.replace(_runtime_config(now - 8 * 86400))
    monkeypatch.setattr(control_plans, "governance_coordinator_mode", lambda: "enforce")
    calls: list[dict] = []

    class _FakeLifecycle:
        def __init__(self, _db_path, adapter=None, **_options):
            self.adapter = adapter

        def reenroll_quarantined_builtin(self, **kwargs):
            calls.append(dict(kwargs))
            current = rc.shared().to_dict()
            current["factor_signal_config"]["pin_bar"].update(
                {"enabled": True, "lifecycle_status": "SHADOW"}
            )
            current["factor_portfolio_weights"]["pin_bar"] = 0.0
            rc.replace(RuntimeConfig.from_dict(current))
            return {
                "ok": True,
                "status": "committed",
                "lifecycle_stage": "SHADOW",
                "generation": 2,
            }

    monkeypatch.setattr(governance_module, "FactorLifecycleService", _FakeLifecycle)
    orchestrator = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    monkeypatch.setattr(orchestrator, "_factor_has_pending_effect", lambda _factor_id: False)
    monkeypatch.setattr(
        orchestrator,
        "_audit_action",
        lambda *_args, **_kwargs: {"status": "applied"},
    )
    monkeypatch.setattr(
        orchestrator,
        "_apply_runtime_patch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("terminal lifecycle must not be rewritten")
        ),
    )

    actions = orchestrator._restore_quarantined_builtin_alpha(
        _catalog(now),
        {"run_id": "terminal_builtin_restore"},
    )

    assert actions == [{"status": "applied"}]
    assert len(calls) == 1
    assert calls[0]["name"] == "pin_bar"
    assert calls[0]["v16"].target_agent == "factor_governance"
    assert rc.shared().factor_signal_config["pin_bar"]["enabled"] is True
    assert rc.shared().factor_signal_config["pin_bar"]["lifecycle_status"] == "SHADOW"
    assert rc.shared().factor_portfolio_weights["pin_bar"] == 0.0


def test_prepared_discovered_gp_candidate_reaches_preflight_and_activation(monkeypatch):
    now = time.time()
    factor_id = "dsl_auto_reachable"
    cfg = RuntimeConfig(
        autonomy_mode="demo_autonomous",
        factor_signal_config={
            factor_id: {
                "enabled": True,
                "lifecycle_status": "PROMOTION_PREPARED",
                "role": "alpha",
                "source": "discovered",
            },
        },
        factor_portfolio_weights={factor_id: 0.0},
        factor_governance_new_factor_weight=0.05,
    )
    rc.replace(cfg)
    expression = "dxy"
    catalog = [{
        "factor_id": factor_id,
        "lifecycle_factor_id": canonical_factor_id(expression),
        "lifecycle_origin": "dsl",
        "lifecycle_status": "PROMOTION_PREPARED",
        "runtime_admission": "projection_acknowledged",
        "lifecycle_expression": expression,
        "lifecycle_definition_fingerprint": factor_definition_fingerprint(
            expression
        ),
        "lifecycle_artifact_hash": "a" * 64,
        "source": "discovered",
        "role": "alpha",
        "health_status": "HEALTHY",
        "health_score": 80.0,
        "health_n_obs": 2000,
        "health_updated_at": now,
        "canary": {"stage": "ACTIVE"},
        "shadow_perf": {
            "oos_bars": 150,
            "n_valid": 1500,
            "cumulative_pnl": 0.01,
            "hit_rate": 0.53,
            "max_drawdown": 0.01,
        },
    }]
    calls: list[dict] = []
    lifecycle_options: list[dict] = []

    class _FakeLifecycle:
        def __init__(self, _db_path, adapter=None, **options):
            self.adapter = adapter
            lifecycle_options.append(dict(options))

        def get_state(self, **_kwargs):
            return {"lifecycle_stage": "PROMOTION_PREPARED"}

        def activate(self, **kwargs):
            calls.append(dict(kwargs))
            return {"ok": True, "status": "committed", "lifecycle_stage": "ACTIVE"}

    monkeypatch.setattr(governance_module, "FactorLifecycleService", _FakeLifecycle)
    orchestrator = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    monkeypatch.setattr(orchestrator, "_factor_has_pending_effect", lambda _factor_id: False)
    monkeypatch.setattr(
        orchestrator,
        "_audit_action",
        lambda _run, item, action, status, *_args, **_kwargs: {
            "factor_id": item["factor_id"],
            "action": action,
            "status": status,
        },
    )

    preflight = orchestrator._expansion_preflight(
        catalog,
        cfg=cfg,
        profile=orchestrator._governance_profile(cfg),
        redundancy_report={},
    )
    actions = orchestrator._promote_shadow_candidates(
        catalog,
        {"run_id": "prepared_gp_activation"},
    )

    assert preflight["reasons"]["shadow_promotion"] == [factor_id]
    assert actions == [{
        "factor_id": factor_id,
        "action": "promote_factor",
        "status": "applied",
    }]
    assert len(calls) == 1
    assert calls[0]["name"] == factor_id
    assert calls[0]["weight"] == 0.05
    assert lifecycle_options == [{"health_stale_after_sec": 900.0}]

    catalog[0]["runtime_admission"] = "awaiting_projection_ack"
    assert orchestrator._is_dsl_promotion_lifecycle_candidate(catalog[0]) is False


def test_balanced_demo_restores_qualified_active_zero_weight_before_lifecycle_work(
    monkeypatch,
):
    now = time.time()
    cfg = RuntimeConfig(
        autonomy_mode="demo_autonomous",
        factor_signal_config={
            "pin_bar": {
                "enabled": True,
                "lifecycle_status": "ACTIVE",
                "role": "alpha",
                "source": "builtin",
            }
        },
        factor_portfolio_weights={"pin_bar": 0.0},
    )
    rc.replace(cfg)
    monkeypatch.setattr(rc, "bounded_demo_mode_active", lambda _cfg=None: True)
    orchestrator = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    monkeypatch.setattr(orchestrator, "_factor_has_pending_effect", lambda _factor_id: False)
    calls: list[dict] = []
    monkeypatch.setattr(
        governance_module.FactorWeightChangeService,
        "execute",
        lambda _self, **kwargs: calls.append(dict(kwargs))
        or {"ok": True, "status": "applied", "projection_ready": True},
    )
    monkeypatch.setattr(
        orchestrator,
        "_audit_action",
        lambda *_args, **_kwargs: {"status": "applied"},
    )
    catalog = _catalog(now)
    catalog[0].update({"enabled": True, "lifecycle_status": "ACTIVE"})

    actions = orchestrator._restore_active_zero_weight_alpha(
        catalog,
        {"run_id": "active-zero-restore"},
        v16_authority={
            "command_id": "cmd-1",
            "candidate_id": "candidate-1",
            "posterior_fingerprint": "posterior-1",
            "evidence_fingerprint": "evidence-1",
        },
    )

    assert actions == [{"status": "applied"}]
    assert len(calls) == 1
    assert calls[0]["weight_policy_weights"] == {"pin_bar": 0.05}
    assert calls[0]["v16_command_id"] == "cmd-1"
    assert calls[0]["v16_candidate_id"] == "candidate-1"
    assert calls[0]["v16_posterior_fingerprint"] == "posterior-1"
    assert calls[0]["v16_evidence_fingerprint"] == "evidence-1"


def test_risk_policy_dispatches_factor_restore_and_honors_freeze():
    governor = RiskGovernor()
    service = RiskPolicyService(governor=governor)

    assert service.evaluate("restore_factor_live", {"autonomy_mode": "demo_nursery"}).allowed is True

    governor.set_override("force_factor_restore_freeze", True)
    verdict = service.evaluate("restore_factor_live", {"autonomy_mode": "demo_nursery"})
    assert verdict.allowed is False
    assert verdict.reason == "force_factor_restore_freeze"


def test_healthy_builtin_shadow_is_activated_with_governed_initial_weight(monkeypatch):
    rc.replace(RuntimeConfig(
        autonomy_mode="demo_nursery",
        factor_signal_config={
            "htf_trend_alignment": {
                "enabled": True,
                "lifecycle_status": "SHADOW",
                "role": "alpha",
                "source": "builtin",
            },
        },
        factor_portfolio_weights={"htf_trend_alignment": 0.0},
        factor_governance_builtin_activation_enabled=True,
        factor_governance_builtin_activation_min_health_score=70.0,
        factor_governance_builtin_activation_min_n_obs=500,
        factor_governance_builtin_activation_weight=0.05,
        factor_governance_max_builtin_activations_per_cycle=1,
    ))

    class _FakeLifecycle:
        stage = ""

        def __init__(self, _db_path, adapter=None, **_options):
            self.adapter = adapter

        def get_state(self, **_kwargs):
            return {"lifecycle_stage": self.stage} if self.stage else {}

        def register_shadow(self, **_kwargs):
            type(self).stage = "SHADOW"
            return {"ok": True, "status": "committed", "lifecycle_stage": "SHADOW"}

        def prepare_promotion(self, **_kwargs):
            type(self).stage = "PROMOTION_PREPARED"
            return {
                "ok": True,
                "status": "committed",
                "lifecycle_stage": "PROMOTION_PREPARED",
            }

        def activate(self, **kwargs):
            type(self).stage = "ACTIVE"
            current = rc.shared().to_dict()
            current["factor_signal_config"]["htf_trend_alignment"].update(
                {"enabled": True, "lifecycle_status": "ACTIVE"}
            )
            current["factor_portfolio_weights"]["htf_trend_alignment"] = kwargs[
                "weight"
            ]
            rc.replace(RuntimeConfig.from_dict(current))
            return {"ok": True, "status": "applied", "lifecycle_stage": "ACTIVE"}

    monkeypatch.setattr(governance_module, "FactorLifecycleService", _FakeLifecycle)
    monkeypatch.setattr(
        FactorGovernanceOrchestrator,
        "_audit_action",
        lambda _self, _run, item, action, status, *_args, **_kwargs: {
            "factor_id": item["factor_id"], "action": action, "status": status
        },
    )
    orchestrator = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    # This unit test exercises the activation contract, not the production
    # application-effect ledger.  Keep the latter isolated so a missing or
    # unavailable PostgreSQL authority correctly remains fail-closed in
    # production without making the unit test depend on live state.
    monkeypatch.setattr(orchestrator, "_factor_has_pending_effect", lambda _factor_id: False)
    catalog = [{
            "factor_id": "htf_trend_alignment",
            "source": "discovered",
            "lifecycle_origin": "builtin",
            "role": "alpha",
            "enabled": True,
            "lifecycle_status": "SHADOW",
            "lifecycle_generation": 2,
            "health_score": 75.0,
            "health_status": "HEALTHY",
            "health_n_obs": 1000,
            "health_updated_at": time.time(),
            "factor_governance_shadow": {},
        }]
    first = orchestrator._activate_healthy_builtin_shadow(
        catalog, {"run_id": "builtin_activation_test_1"}
    )
    second = orchestrator._activate_healthy_builtin_shadow(
        catalog, {"run_id": "builtin_activation_test_2"}
    )
    catalog[0]["lifecycle_status"] = "PROMOTION_PREPARED"
    actions = orchestrator._activate_healthy_builtin_shadow(
        catalog, {"run_id": "builtin_activation_test_3"}
    )

    assert first[0]["status"] == "shadow_registered"
    assert second[0]["status"] == "promotion_prepared"
    assert actions == [{
        "factor_id": "htf_trend_alignment", "action": "promote_factor", "status": "applied"
    }]
    assert rc.shared().factor_signal_config["htf_trend_alignment"]["lifecycle_status"] == "ACTIVE"
    assert rc.shared().factor_portfolio_weights["htf_trend_alignment"] == 0.05


def test_builtin_shadow_stays_observation_only_below_health_gate():
    rc.replace(RuntimeConfig(
        factor_signal_config={
            "htf_trend_alignment": {
                "enabled": True,
                "lifecycle_status": "SHADOW",
                "autonomous_activation": True,
                "role": "alpha",
                "source": "builtin",
            },
        },
        factor_portfolio_weights={"htf_trend_alignment": 0.0},
    ))
    orchestrator = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    actions = orchestrator._activate_healthy_builtin_shadow(
        [{
            "factor_id": "htf_trend_alignment",
            "source": "builtin",
            "role": "alpha",
            "enabled": True,
            "lifecycle_status": "SHADOW",
            "health_score": 69.9,
            "health_status": "WATCH",
            "health_n_obs": 1000,
            "factor_governance_shadow": {},
        }],
        {"run_id": "builtin_activation_gate_test"},
    )
    assert actions == []
    assert rc.shared().factor_signal_config["htf_trend_alignment"]["lifecycle_status"] == "SHADOW"
