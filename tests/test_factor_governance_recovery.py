from __future__ import annotations

import time

import pytest

import backend.runtime.factor_governance_orchestrator as governance_module
from backend.runtime.factor_governance_orchestrator import FactorGovernanceOrchestrator
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
        "role": "alpha",
        "lifecycle_status": "ACTIVE",
        "governance_action": "disable_factor_live",
        "health_status": "WATCH",
        "health_score": 65.0,
        "health_n_obs": 2000,
        "health_updated_at": health_updated_at if health_updated_at is not None else now,
        "factor_governance_shadow": shadow or {},
    }]


def test_quarantined_builtin_alpha_is_restored_after_fresh_recovery_evidence(monkeypatch):
    now = time.time()
    rc.replace(_runtime_config(now - 8 * 86400))
    risk = _AllowRisk()
    orchestrator = FactorGovernanceOrchestrator(risk_policy=risk)
    applied: list[dict] = []
    audited: list[tuple[str, str]] = []

    def apply_patch(patch, *, source, run_id):
        applied.append({"patch": patch, "source": source, "run_id": run_id})
        current = rc.shared().to_dict()
        current["factor_signal_config"].update(patch["factor_signal_config"])
        rc.replace(RuntimeConfig.from_dict(current))
        return {"ok": True, "status": "applied"}

    monkeypatch.setattr(orchestrator, "_apply_runtime_patch", apply_patch)
    monkeypatch.setattr(
        orchestrator,
        "_audit_action",
        lambda _run, item, action, status, *_args, **_kwargs: (
            audited.append((item["factor_id"], action, status))
            or {"factor_id": item["factor_id"], "action": action, "status": status}
        ),
    )

    actions = orchestrator._restore_quarantined_builtin_alpha(
        _catalog(now),
        {"run_id": "restore_test"},
    )

    assert [item["action"] for item in actions] == ["restore_factor_live"]
    assert actions[0]["status"] == "applied"
    assert risk.actions == ["restore_factor_live"]
    assert applied[0]["source"] == "factor_governance_restore_live"
    assert audited == [("pin_bar", "restore_factor_live", "applied")]
    restored = rc.shared().factor_signal_config["pin_bar"]
    assert restored["enabled"] is True
    assert restored["lifecycle_status"] == "ACTIVE"
    assert restored["restored_from"] == "QUARANTINE"


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


def test_disable_weak_live_alpha_always_enters_recoverable_quarantine(monkeypatch):
    now = time.time()
    rc.replace(RuntimeConfig(
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
    monkeypatch.setattr(orchestrator, "_audit_action", lambda *_args, **_kwargs: {"status": "applied"})
    monkeypatch.setattr(orchestrator, "_factor_has_pending_effect", lambda _factor_id: False)

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

    assert actions == [{"status": "applied"}]
    assert captured[0]["factor_signal_config"]["pin_bar"]["lifecycle_status"] == "QUARANTINE"
    assert captured[0]["factor_signal_config"]["pin_bar"]["disabled_at"] >= now


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
                "autonomous_activation": True,
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

    class _FakeWeightChange:
        def __init__(self, _db_path):
            pass

        def execute(self, **kwargs):
            current = rc.shared().to_dict()
            current["factor_signal_config"].update(
                kwargs["additional_patch"]["factor_signal_config"]
            )
            current["factor_portfolio_weights"].update(kwargs["weight_policy_weights"])
            rc.replace(RuntimeConfig.from_dict(current))
            return {"status": "applied", "proposed_weights": kwargs["weight_policy_weights"]}

    monkeypatch.setattr(governance_module, "FactorWeightChangeService", _FakeWeightChange)
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
    actions = orchestrator._activate_healthy_builtin_shadow(
        [{
            "factor_id": "htf_trend_alignment",
            "source": "builtin",
            "role": "alpha",
            "enabled": True,
            "lifecycle_status": "SHADOW",
            "health_score": 75.0,
            "health_status": "HEALTHY",
            "health_n_obs": 1000,
            "factor_governance_shadow": {},
        }],
        {"run_id": "builtin_activation_test"},
    )

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
