from types import SimpleNamespace

from backend.runtime.factor_governance_orchestrator import FactorGovernanceOrchestrator
from backend.services.runtime_config_overlay import RuntimeConfigOverlayService
from config import runtime_config as rc


class _AllowRisk:
    def evaluate(self, action, context):
        return SimpleNamespace(
            allowed=True,
            reason="ok",
            severity="info",
            required_mode=context.get("required_mode", "autonomous_governance"),
            audit_payload={"action": action},
            to_dict=lambda: {"allowed": True, "reason": "ok", "action": action},
        )


def test_orchestrator_promotes_eligible_shadow_without_human_approval(monkeypatch, tmp_path):
    rc.reset_for_tests()
    orch = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    orch.overlay = RuntimeConfigOverlayService(tmp_path / "state.db")
    promoted = []
    audited = []

    class _Adapter:
        def promote(self, name, new_source, reason=""):
            promoted.append((name, new_source, reason))
            return True

    monkeypatch.setattr(
        "alpha.registry_adapter.RegistryAdapter.shared",
        classmethod(lambda cls: _Adapter()),
    )
    monkeypatch.setattr(
        orch,
        "_audit_action",
        lambda run, item, action, status, evidence, verdict, **kwargs: audited.append(
            {"factor_id": item["factor_id"], "action": action, "status": status, "evidence": evidence}
        ) or audited[-1],
    )

    catalog = [{
        "factor_id": "shadow_alpha_1",
        "source": "shadow",
        "role": "alpha",
        "canary": {"stage": "ACTIVE"},
        "shadow_perf": {
            "oos_bars": 120,
            "n_valid": 100,
            "cumulative_pnl": 1.2,
            "hit_rate": 0.55,
            "max_drawdown": 0.01,
        },
        "health_status": "UNKNOWN",
        "health_score": 0.0,
    }]

    actions = orch._promote_shadow_candidates(catalog, {"run_id": "test-run"})

    assert promoted == [("shadow_alpha_1", "discovered", "autonomous_governance_v3")]
    assert actions[0]["status"] == "applied"
    cfg = rc.shared()
    assert cfg.factor_signal_config["shadow_alpha_1"]["source"] == "discovered"
    assert cfg.factor_portfolio_weights["shadow_alpha_1"] == 0.3


def test_orchestrator_does_not_promote_shadow_without_evidence():
    rc.reset_for_tests()
    orch = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    catalog = [{
        "factor_id": "weak_shadow",
        "source": "shadow",
        "role": "alpha",
        "shadow_perf": {"oos_bars": 10, "n_valid": 10, "cumulative_pnl": -1.0},
        "health_status": "UNKNOWN",
        "health_score": 0.0,
    }]

    assert orch._promote_shadow_candidates(catalog, {"run_id": "test-run"}) == []

def test_orchestrator_requires_active_canary_before_shadow_promotion():
    rc.reset_for_tests()
    orch = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    catalog = [{
        "factor_id": "premature_shadow",
        "source": "shadow",
        "role": "alpha",
        "canary": {"stage": "CANARY_50"},
        "shadow_perf": {
            "oos_bars": 500, "n_valid": 500, "cumulative_pnl": 2.0,
            "hit_rate": 0.70, "max_drawdown": 0.01,
        },
        "health_status": "HEALTHY",
        "health_score": 90.0,
    }]

    assert orch._promote_shadow_candidates(catalog, {"run_id": "test-run"}) == []


def test_orchestrator_downweights_from_model_weakness_evidence(monkeypatch, tmp_path):
    rc.reset_for_tests()
    rc.patch({
        "factor_signal_config": {
            "model_weak_factor": {"role": "alpha", "enabled": True, "tags": ["技术"]},
        },
        "factor_portfolio_weights": {"model_weak_factor": 0.3},
        "factor_governance_model_min_samples": 3,
        "factor_governance_model_weakness_threshold": 0.65,
        "awe_max_single_change": 0.15,
    })
    orch = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    orch.overlay = RuntimeConfigOverlayService(tmp_path / "state.db")
    audited = []
    monkeypatch.setattr(
        orch,
        "_audit_action",
        lambda run, item, action, status, evidence, verdict, **kwargs: audited.append(
            {"factor_id": item["factor_id"], "action": action, "status": status, "evidence": evidence}
        ) or audited[-1],
    )
    catalog = [{
        "factor_id": "model_weak_factor",
        "source": "builtin",
        "role": "alpha",
        "enabled": True,
        "eligible_for_live": True,
        "used_in_score": True,
        "weight": 0.3,
        "health_score": 0.0,
        "health_status": "UNKNOWN",
        "factor_governance_shadow": {
            "sample_count": 4,
            "weak_sample_count": 4,
            "avg_weakness_score": 0.78,
            "weakness_score": 0.8,
            "model_type": "factor_governance_lightgbm",
            "latest_inference_id": "fg_weak_1",
        },
    }]

    actions = orch._downweight_weak_alpha(catalog, {"run_id": "test-run"})

    assert actions[0]["status"] == "applied"
    assert rc.shared().factor_portfolio_weights["model_weak_factor"] == 0.255
    evidence = audited[0]["evidence"]
    assert evidence["health_weak"] is False
    assert evidence["model_governance"]["weak_for_downweight"] is True


def test_orchestrator_defers_factor_mutation_while_effect_is_pending(monkeypatch):
    rc.reset_for_tests()
    rc.patch({"factor_portfolio_weights": {"model_weak_factor": 0.3}})
    orch = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    monkeypatch.setattr(orch, "_factor_has_pending_effect", lambda factor_id: factor_id == "model_weak_factor")
    catalog = [{
        "factor_id": "model_weak_factor",
        "source": "builtin",
        "role": "alpha",
        "used_in_score": True,
        "weight": 0.3,
        "health_score": 10.0,
        "health_status": "DECAYING",
        "factor_governance_shadow": {},
    }]

    assert orch._downweight_weak_alpha(catalog, {"run_id": "test-run"}) == []
    assert rc.shared().factor_portfolio_weights["model_weak_factor"] == 0.3


def test_pending_effect_gate_releases_factor_after_final_effect(monkeypatch):
    row = {"application_status": "reinforced", "effect_status": "reinforced"}

    class _Result:
        def fetchone(self):
            return row

    class _Conn:
        def execute(self, *args, **kwargs):
            return _Result()

        def close(self):
            return None

    monkeypatch.setattr(
        "backend.runtime.factor_governance_orchestrator.get_state_pg_conn",
        lambda read_only: _Conn(),
    )

    assert FactorGovernanceOrchestrator._factor_has_pending_effect("rsi_14") is False

    row.update(application_status="applied", effect_status="mixed")
    assert FactorGovernanceOrchestrator._factor_has_pending_effect("rsi_14") is True

    row.update(application_status="observing", effect_status="reinforced")
    assert FactorGovernanceOrchestrator._factor_has_pending_effect("rsi_14") is True


def test_scoped_factor_rollback_preserves_unrelated_runtime_config():
    patch = FactorGovernanceOrchestrator._scoped_factor_rollback_patch(
        "rsi_14",
        {
            "factor_signal_config": {"rsi_14": {"enabled": True}},
            "factor_portfolio_weights": {"rsi_14": 0.4},
        },
        {
            "factor_signal_config": {"rsi_14": {"enabled": False}, "ema_slope": {"enabled": False}},
            "factor_portfolio_weights": {"rsi_14": 0.1, "ema_slope": 0.8},
        },
    )

    assert patch["factor_signal_config"]["rsi_14"]["enabled"] is True
    assert patch["factor_signal_config"]["ema_slope"]["enabled"] is False
    assert patch["factor_portfolio_weights"] == {"rsi_14": 0.4, "ema_slope": 0.8}
