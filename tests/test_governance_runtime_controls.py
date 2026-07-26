from __future__ import annotations

import sqlite3
import inspect
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.db import STATE_DB, STATE_DB_DDL
from backend.services.governance_control_plans import (
    AutonomyControlPlan,
    IncidentControlPlan,
    OperatorGovernancePausePlan,
)
from backend.services.governance_expansion_control import (
    GovernanceExpansionControlService,
)
from backend.services.governance_mutation_coordinator import (
    GovernanceMutationCoordinator,
    GovernanceMutationPlan,
    classify_governance_risk,
)
from config import runtime_config


@pytest.fixture(autouse=True)
def _reset_runtime_config():
    runtime_config.reset_for_tests()
    yield
    runtime_config.reset_for_tests()


def _set_coordinator_mode(monkeypatch, mode: str) -> None:
    from backend.core import static_feature_flags

    monkeypatch.setattr(
        static_feature_flags,
        "shared_static_feature_flags",
        lambda: SimpleNamespace(governance_mutation_coordinator_v2_mode=mode),
    )


def test_runtime_control_plans_do_not_accept_caller_risk_reduction() -> None:
    for plan_type in (
        AutonomyControlPlan,
        IncidentControlPlan,
        OperatorGovernancePausePlan,
    ):
        assert "risk_reduction" not in {item.name for item in fields(plan_type)}


def test_invalid_runtime_incident_mode_is_treated_as_frozen() -> None:
    from backend.services.incident_controls import RuntimeIncidentControlService

    assert (
        RuntimeIncidentControlService._target_mode_for_playbook(
            "latency_spike",
            "low",
            "typo_mode",
        )
        == "frozen"
    )


def test_runtime_control_risk_is_derived_from_before_after() -> None:
    incident = classify_governance_risk(
        {"runtime_incident_mode": "normal"},
        {"runtime_incident_mode": "no_new_risk"},
    )
    revoke = classify_governance_risk(
        {
            "autonomy_mode": "live_autonomous",
            "live_autonomy_unlocked": True,
            "live_autonomy_unlock_id": "unlock-1",
        },
        {
            "autonomy_mode": "live_candidate",
            "live_autonomy_unlocked": False,
            "live_autonomy_unlock_id": "",
        },
    )
    unlock = classify_governance_risk(
        {
            "autonomy_mode": "live_candidate",
            "live_autonomy_unlocked": False,
            "live_autonomy_unlock_id": "",
        },
        {
            "autonomy_mode": "live_autonomous",
            "live_autonomy_unlocked": True,
            "live_autonomy_unlock_id": "unlock-2",
        },
    )
    pause = classify_governance_risk(
        {"governance_expansion_paused": False},
        {"governance_expansion_paused": True},
    )
    unfreeze = classify_governance_risk(
        {"autonomy_expansion_frozen": True},
        {"autonomy_expansion_frozen": False},
    )

    assert incident.risk_class == "risk_tightening"
    assert revoke.risk_class == "risk_tightening"
    assert unlock.risk_class == "risk_expanding"
    assert pause.risk_class == "risk_tightening"
    assert unfreeze.risk_class == "risk_expanding"


def test_caller_risk_reduction_cannot_exempt_autonomy_unfreeze(
    tmp_path, monkeypatch
) -> None:
    from backend.services.runtime_config_mutation import RuntimeConfigMutationService

    _set_coordinator_mode(monkeypatch, "dual_record")
    runtime_config.replace(
        runtime_config.RuntimeConfig(
            autonomy_mode="live_candidate",
            autonomy_expansion_frozen=True,
        )
    )
    result = RuntimeConfigMutationService(tmp_path / "state.db").apply_patch(
        {"autonomy_expansion_frozen": False},
        source="pytest_auto_unfreeze",
        actor="system:pytest",
        action="auto_unfreeze_expansionary_autonomy",
        risk_reduction=True,
        audit=False,
    )

    assert result["ok"] is True
    assert result["caller_risk_reduction_ignored"] is True
    assert result["risk_classification"]["risk_class"] == "risk_expanding"
    assert runtime_config.shared().autonomy_expansion_frozen is False
    conn = sqlite3.connect(tmp_path / "state.db")
    try:
        status = conn.execute(
            "SELECT status FROM governance_mutation_intent"
        ).fetchone()[0]
    finally:
        conn.close()
    assert status == "committed"


def test_operator_pause_is_typed_and_resume_requires_confirmation(
    tmp_path, monkeypatch
) -> None:
    _set_coordinator_mode(monkeypatch, "off")
    service = GovernanceExpansionControlService(tmp_path / "state.db")

    paused = service.set_paused(
        True,
        actor="operator:pytest",
        reason="incident review",
    )
    blocked = service.set_paused(
        False,
        actor="operator:pytest",
        reason="resume",
    )
    resumed = service.set_paused(
        False,
        actor="operator:pytest",
        reason="evidence verified",
        confirm_resume=True,
    )

    assert paused["ok"] is True
    assert paused["mutation"]["risk_classification"]["risk_class"] == "risk_tightening"
    assert blocked["status"] == "confirm_resume_required"
    assert resumed["ok"] is True
    assert resumed["mutation"]["risk_classification"]["risk_class"] == "risk_expanding"
    assert runtime_config.shared().governance_expansion_paused is False


def test_bounded_demo_operator_control_is_v16_exempt_in_production(
    tmp_path, monkeypatch
) -> None:
    coordinator = GovernanceMutationCoordinator(tmp_path / "state.db")
    monkeypatch.setattr(
        GovernanceMutationCoordinator,
        "production_state",
        property(lambda _self: True),
    )
    plan = GovernanceMutationPlan(
        patch={"runtime_incident_mode": "normal"},
        source="operator_incident_control",
        actor="operator:pytest",
    )

    claim = coordinator._claim_v16(
        plan,
        {
            "risk_classification": {"risk_class": "risk_expanding"},
            "operator_bounded_demo_control_exempt": True,
        },
    )

    assert claim["allowed"] is True
    assert claim["status"] == "operator_bounded_demo_control_exempt"


def test_persisted_operator_pause_blocks_stale_process_expansion(
    tmp_path, monkeypatch
) -> None:
    from backend.services import runtime_config_mutation as module
    from backend.services.runtime_config_overlay import RuntimeConfigOverlayService

    _set_coordinator_mode(monkeypatch, "off")
    db_path = tmp_path / "state.db"
    RuntimeConfigOverlayService(db_path).apply_patch(
        {"governance_expansion_paused": True},
        source="operator_governance_expansion_control",
    )
    # Simulate a stale worker projection while PostgreSQL/overlay already owns
    # the committed operator pause.
    runtime_config.replace(
        runtime_config.RuntimeConfig(governance_expansion_paused=False)
    )
    monkeypatch.setattr(module, "is_state_db_path", lambda _path: True)
    monkeypatch.setattr(
        "backend.services.runtime_config_startup.load_yaml_runtime_config",
        lambda: (runtime_config.RuntimeConfig(), {}),
    )

    result = module.RuntimeConfigMutationService(db_path).apply_patch(
        {"factor_portfolio_weights": {"new_alpha": 0.1}},
        source="factor_governance_promote",
        actor="system:factor_governance",
        action="promote_factor",
        audit=False,
    )

    assert result["ok"] is False
    assert result["status"] == "blocked_governance_expansion_paused"


def test_autonomous_actor_cannot_modify_operator_pause(tmp_path) -> None:
    result = GovernanceExpansionControlService(tmp_path / "state.db").set_paused(
        True,
        actor="system:autonomous_learning",
        reason="not authorized",
    )
    assert result["ok"] is False
    assert result["status"] == "operator_required"

    missing_reason = GovernanceExpansionControlService(
        tmp_path / "reason.db"
    ).set_paused(
        True,
        actor="operator:pytest",
        reason="",
    )
    assert missing_reason["ok"] is False
    assert missing_reason["status"] == "reason_required"


def test_incident_tightening_keeps_local_latch_when_governance_store_fails(
    monkeypatch,
) -> None:
    from backend.services import incident_controls as module

    active = {"value": False}

    def activate(**_kwargs):
        active["value"] = True
        return {"active": True, "event_id": "latch-1"}

    monkeypatch.setattr(module, "is_state_db_path", lambda _path: True)
    monkeypatch.setattr(module, "activate_no_new_risk_latch", activate)
    monkeypatch.setattr(
        module,
        "no_new_risk_latch_status",
        lambda **_kwargs: {"active": active["value"], "state": "active"},
    )
    monkeypatch.setattr(
        module,
        "append_safety_outbox",
        lambda **_kwargs: {"event_id": "outbox-1"},
    )
    monkeypatch.setattr(
        IncidentControlPlan,
        "execute",
        lambda self, _db_path: {
            "ok": False,
            "status": "reserve_failed",
            "reason": "postgres unavailable",
        },
    )
    runtime_config.replace(
        runtime_config.RuntimeConfig(runtime_incident_mode="normal")
    )

    result = module.RuntimeIncidentControlService(STATE_DB).set_mode(
        "no_new_risk",
        actor="system:health",
        reason="heartbeat stale",
    )

    assert result["ok"] is True
    assert result["status"] == "local_safety_latched_projection_pending"
    assert result["local_safety_effective"] is True
    assert result["governance_projection_pending"] is True
    assert result["safety_outbox"]["event_id"] == "outbox-1"


def test_incident_tightening_keeps_local_latch_when_risk_policy_fails(
    monkeypatch,
) -> None:
    from backend.services import incident_controls as module

    active = {"value": False}

    def activate(**_kwargs):
        active["value"] = True
        return {"active": True, "event_id": "latch-policy"}

    monkeypatch.setattr(module, "is_state_db_path", lambda _path: True)
    monkeypatch.setattr(module, "activate_no_new_risk_latch", activate)
    monkeypatch.setattr(
        module,
        "no_new_risk_latch_status",
        lambda **_kwargs: {"active": active["value"], "state": "active"},
    )
    monkeypatch.setattr(
        module,
        "append_safety_outbox",
        lambda **_kwargs: {"event_id": "outbox-policy"},
    )
    monkeypatch.setattr(
        module.RiskPolicyService,
        "shared",
        classmethod(lambda cls: (_ for _ in ()).throw(RuntimeError("policy down"))),
    )
    runtime_config.replace(
        runtime_config.RuntimeConfig(runtime_incident_mode="normal")
    )

    result = module.RuntimeIncidentControlService(STATE_DB).set_mode(
        "frozen",
        actor="system:health",
        reason="safety heartbeat lost",
    )

    assert result["ok"] is True
    assert result["status"] == "local_safety_latched_policy_projection_pending"
    assert result["local_safety_effective"] is True
    assert result["risk_verdict"]["reason"] == "risk_policy_unavailable"


def test_local_latch_cannot_be_cleared_while_governance_projection_is_normal(
    monkeypatch,
) -> None:
    from backend.services import incident_controls as module

    monkeypatch.setattr(module, "is_state_db_path", lambda _path: True)
    monkeypatch.setattr(
        module,
        "no_new_risk_latch_status",
        lambda **_kwargs: {"active": True, "state": "active", "reason": "pg_down"},
    )
    executed = {"value": False}

    def should_not_execute(self, _db_path):
        executed["value"] = True
        return {"ok": True}

    monkeypatch.setattr(IncidentControlPlan, "execute", should_not_execute)
    runtime_config.replace(
        runtime_config.RuntimeConfig(runtime_incident_mode="normal")
    )
    service = module.RuntimeIncidentControlService(STATE_DB)

    status = service.status()
    result = service.set_mode(
        "normal",
        actor="api:ops.incident_control",
        reason="attempted direct clear",
        confirm_thaw=True,
    )

    assert status["configured_mode"] == "normal"
    assert status["effective_mode"] == "no_new_risk"
    assert status["local_latch_overrode_mode"] is True
    assert result["ok"] is False
    assert result["status"] == "governance_projection_recovery_required"
    assert result["local_safety_effective"] is True
    assert executed["value"] is False


def test_live_autonomy_revoke_keeps_local_latch_when_pg_fails(monkeypatch) -> None:
    from backend.services import live_autonomy as module

    active = {"value": False}

    def activate(**_kwargs):
        active["value"] = True
        return {"active": True, "event_id": "latch-2"}

    monkeypatch.setattr(module, "is_state_db_path", lambda _path: True)
    monkeypatch.setattr(module, "activate_no_new_risk_latch", activate)
    monkeypatch.setattr(
        module,
        "no_new_risk_latch_status",
        lambda **_kwargs: {"active": active["value"], "state": "active"},
    )
    monkeypatch.setattr(
        module,
        "append_safety_outbox",
        lambda **_kwargs: {"event_id": "outbox-2"},
    )
    monkeypatch.setattr(
        AutonomyControlPlan,
        "execute",
        lambda self, _db_path: {
            "ok": False,
            "status": "reserve_failed",
            "reason": "postgres unavailable",
        },
    )
    monkeypatch.setattr(
        module.LiveAutonomyService,
        "_build_readiness",
        staticmethod(lambda: {}),
    )
    monkeypatch.setattr(
        module.ProposalRegistryService,
        "status",
        lambda self: {},
    )
    monkeypatch.setattr(
        module.LiveAutonomyService,
        "_record_event",
        lambda self, **_kwargs: (_ for _ in ()).throw(RuntimeError("pg down")),
    )
    runtime_config.replace(
        runtime_config.RuntimeConfig(
            autonomy_mode="live_autonomous",
            live_autonomy_unlocked=True,
            live_autonomy_unlock_id="unlock-1",
        )
    )

    result = module.LiveAutonomyService(STATE_DB).revoke(
        actor="api:ops.autonomy.live_unlock",
        reason="operator revoke",
    )

    assert result["ok"] is True
    assert result["status"] == "local_safety_latched_projection_pending"
    assert result["local_safety_effective"] is True
    assert result["governance_projection_pending"] is True
    assert result["safety_outbox"]["event_id"] == "outbox-2"


def test_learning_auto_unfreeze_uses_typed_plan_and_retains_freeze_when_blocked(
    tmp_path, monkeypatch
) -> None:
    from backend.services import autonomous_learning as module
    from backend.services.backend_readiness import BackendReadinessService

    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            "INSERT INTO evolution_events(timestamp, event_type, payload_json) VALUES (1.0, 'learning_closure_verification_passed', '{}')"
        )
        conn.commit()
    finally:
        conn.close()
    runtime_config.replace(
        runtime_config.RuntimeConfig(
            autonomy_mode="live_candidate",
            autonomy_expansion_frozen=True,
        )
    )
    monkeypatch.setattr(
        BackendReadinessService,
        "build",
        lambda self: {
            "learning_repair": {"ok": True},
            "replay": {"ok": True},
            "config_runtime_drift": {"drift": False, "semantic_drift": False},
            "execution_semantics": {"blocking_components": []},
        },
    )
    captured = {}

    def blocked(self, _db_path):
        captured["plan"] = self
        return {"ok": False, "status": "blocked_v16_command_required"}

    monkeypatch.setattr(AutonomyControlPlan, "execute", blocked)

    result = module.maybe_auto_unfreeze_learning_repair(db_path=db_path)

    assert result["ok"] is False
    assert result["status"] == "freeze_retained_mutation_failed"
    assert captured["plan"].patch == {"autonomy_expansion_frozen": False}
    assert captured["plan"].scope_type == "autonomy_control"
    assert runtime_config.shared().autonomy_expansion_frozen is True


def test_learning_auto_unfreeze_persists_field_only_after_typed_mutation(
    tmp_path, monkeypatch
) -> None:
    from backend.services import autonomous_learning as module
    from backend.services.backend_readiness import BackendReadinessService
    from backend.services.release_control import ReleaseControlService

    _set_coordinator_mode(monkeypatch, "off")
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            "INSERT INTO evolution_events(timestamp, event_type, payload_json) VALUES (2.0, 'learning_closure_verification_passed', '{}')"
        )
        conn.commit()
    finally:
        conn.close()
    runtime_config.replace(
        runtime_config.RuntimeConfig(
            autonomy_mode="live_candidate",
            autonomy_expansion_frozen=True,
        )
    )
    monkeypatch.setattr(
        BackendReadinessService,
        "build",
        lambda self: {
            "learning_repair": {"ok": True},
            "replay": {"ok": True},
            "config_runtime_drift": {"drift": False, "semantic_drift": False},
            "execution_semantics": {"blocking_components": []},
        },
    )
    monkeypatch.setattr(
        ReleaseControlService,
        "start_release",
        lambda self, **_kwargs: {"run_id": "release-1"},
    )
    monkeypatch.setattr(
        ReleaseControlService,
        "finish_release",
        lambda self, _run_id, **_kwargs: {"run_id": "release-1", "status": "completed"},
    )

    result = module.maybe_auto_unfreeze_learning_repair(db_path=db_path)

    assert result["ok"] is True
    assert result["status"] == "auto_unfrozen"
    assert result["mutation"]["risk_classification"]["risk_class"] == "risk_expanding"
    assert result["mutation"]["v16_authority"]["status"] == "isolated_test_state"
    assert runtime_config.shared().autonomy_expansion_frozen is False


def test_incident_api_requires_step_up_only_for_thaw(monkeypatch) -> None:
    from backend.api import ops

    current = {"mode": "normal"}
    step_up_calls: list[str | None] = []

    class FakeIncidentControl:
        def status(self):
            return {"mode": current["mode"]}

        def set_mode(self, mode, **_kwargs):
            current["mode"] = mode
            return {"ok": True, "status": "applied", "target_mode": mode}

    monkeypatch.setattr(
        ops,
        "RuntimeIncidentControlService",
        FakeIncidentControl,
    )
    monkeypatch.setattr(
        ops,
        "require_recent_step_up",
        lambda authorization: step_up_calls.append(authorization) or "operator",
    )

    tightened = ops.set_incident_control(
        ops.IncidentControlRequest(mode="no_new_risk", reason="fault"),
        "operator",
        authorization="Bearer local-jwt",
    )
    thawed = ops.set_incident_control(
        ops.IncidentControlRequest(
            mode="normal",
            reason="verified",
            confirm_thaw=True,
        ),
        "operator",
        authorization="Bearer auth-v2",
    )

    assert tightened["ok"] is True
    assert thawed["ok"] is True
    assert step_up_calls == ["Bearer auth-v2"]


def test_safety_latch_precedes_governance_and_broker_reduction_has_no_coordinator() -> None:
    from backend.services.incident_controls import RuntimeIncidentControlService
    from backend.services.live_autonomy import LiveAutonomyService

    incident_source = inspect.getsource(RuntimeIncidentControlService.set_mode)
    revoke_source = inspect.getsource(LiveAutonomyService.revoke)
    assert incident_source.index("activate_no_new_risk_latch(") < incident_source.index(
        "plan.execute("
    )
    assert revoke_source.index("activate_no_new_risk_latch(") < revoke_source.index(
        "plan.execute("
    )

    root = Path(__file__).resolve().parents[1]
    for relative in (
        "backend/services/live_emergency.py",
        "backend/services/live_safety_plane.py",
        "execution/ctrader_bridge.py",
    ):
        source = (root / relative).read_text(encoding="utf-8")
        assert "GovernanceMutationCoordinator" not in source
        assert "governance_control_plans" not in source
