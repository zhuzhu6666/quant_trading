from __future__ import annotations

import json

import pytest

from backend.core.db import STATE_DB
from backend.services.governance_control_plans import IncidentControlPlan
from backend.services.live_safety_state import (
    activate_no_new_risk_latch,
    clear_no_new_risk_latch,
    no_new_risk_latch_status,
    reset_safety_state_for_tests,
    resolve_broker_outcome_mutation,
    safety_latch_path,
    unresolved_broker_outcome_mutations,
)
from config import runtime_config
from risk.policy_service import RiskPolicyService


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch, tmp_path):
    reset_safety_state_for_tests()
    runtime_config.reset_for_tests()
    monkeypatch.setenv("QUANT_SAFETY_STATE_DIR", str(tmp_path / "safety"))
    yield
    runtime_config.reset_for_tests()
    reset_safety_state_for_tests()


def _causes() -> set[str]:
    return {
        str(item.get("cause") or "")
        for item in no_new_risk_latch_status()["causes"]
    }


def test_incident_release_preserves_broker_heartbeat_and_emergency_causes():
    activate_no_new_risk_latch(
        reason="operator incident",
        actor="operator:test",
        correlation_id="incident_control_1",
        cause="incident_control",
        cause_id="runtime_incident_mode",
    )
    activate_no_new_risk_latch(
        reason="broker_execution_outcome_unknown",
        actor="execution:ctrader_bridge",
        correlation_id="intent-1",
        metadata={"action": "close_position", "position_id": 501},
    )
    activate_no_new_risk_latch(
        reason="safety_freshness_failed",
        actor="system:safety_watchdog",
        cause="safety_freshness",
        cause_id="safety_watchdog",
    )
    activate_no_new_risk_latch(
        reason="emergency_close",
        actor="system:emergency_close",
        correlation_id="emergency-1",
    )

    released = clear_no_new_risk_latch(
        reason="governed incident thaw",
        actor="operator:test",
        cause="incident_control",
        cause_id="runtime_incident_mode",
    )

    assert released["released"] == 1
    assert released["active"] is True
    assert _causes() == {
        "broker_execution_unknown",
        "safety_freshness",
        "emergency_resume",
    }
    assert [item["intent_id"] for item in unresolved_broker_outcome_mutations()] == [
        "intent-1"
    ]
    last = json.loads(safety_latch_path().read_text(encoding="utf-8").splitlines()[-1])
    assert last["event"] == "release_cause"
    assert last["active"] is True


def test_broker_unknown_requires_terminal_evidence_and_releases_only_its_intent():
    for intent_id, position_id in (("intent-1", 501), ("intent-2", 502)):
        activate_no_new_risk_latch(
            reason="broker_execution_outcome_unknown",
            actor="execution:ctrader_bridge",
            correlation_id=intent_id,
            metadata={"action": "close_position", "position_id": position_id},
        )

    with pytest.raises(ValueError, match="confirmed_or_rejected"):
        resolve_broker_outcome_mutation(
            intent_id="intent-1",
            outcome="unknown",
            evidence={"reconcile_id": "r1"},
        )
    with pytest.raises(ValueError, match="requires_evidence"):
        resolve_broker_outcome_mutation(
            intent_id="intent-1",
            outcome="confirmed",
            evidence={},
        )

    result = resolve_broker_outcome_mutation(
        intent_id="intent-1",
        action="close_position",
        position_id=501,
        outcome="confirmed",
        evidence={"reconcile_id": "fresh-r1", "position_present": False},
    )

    assert result["status"] == "resolved"
    assert result["released"] == 1
    unresolved = unresolved_broker_outcome_mutations()
    assert [(item["intent_id"], item["position_id"]) for item in unresolved] == [
        ("intent-2", 502)
    ]
    assert no_new_risk_latch_status()["active"] is True


def test_process_local_unknown_survives_disk_failure_until_explicit_resolution(
    monkeypatch,
):
    from backend.services import live_safety_state as module

    append = module._append_fsynced
    monkeypatch.setattr(
        module,
        "_append_fsynced",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    with pytest.raises(Exception, match="persist_failed"):
        activate_no_new_risk_latch(
            reason="broker_execution_outcome_unknown",
            actor="execution:ctrader_bridge",
            correlation_id="intent-disk-failure",
            metadata={"action": "close_position", "position_id": 503},
        )

    assert unresolved_broker_outcome_mutations()[0]["intent_id"] == "intent-disk-failure"
    assert no_new_risk_latch_status()["state"] == "persistence_failed_fail_closed"

    monkeypatch.setattr(module, "_append_fsynced", append)
    resolved = resolve_broker_outcome_mutation(
        intent_id="intent-disk-failure",
        outcome="confirmed",
        evidence={"reconcile_id": "fresh-after-disk-repair", "position_present": False},
    )

    assert resolved["released"] == 1
    assert unresolved_broker_outcome_mutations() == []
    assert no_new_risk_latch_status()["active"] is False


def test_market_open_unknown_without_position_id_remains_unresolved_by_intent():
    activate_no_new_risk_latch(
        reason="broker_execution_outcome_unknown",
        actor="execution:ctrader_bridge",
        correlation_id="open-intent-no-position",
        metadata={"action": "market_open", "position_id": 0},
    )

    unresolved = unresolved_broker_outcome_mutations()

    assert unresolved == [
        {
            "intent_id": "open-intent-no-position",
            "status": "unknown",
            "action": "market_open",
            "position_id": 0,
            "created_at": unresolved[0]["created_at"],
            "evidence": {},
        }
    ]
    resolved = resolve_broker_outcome_mutation(
        intent_id="open-intent-no-position",
        outcome="rejected",
        evidence={"source": "fresh_order_history", "order_rejected": True},
    )
    assert resolved["released"] == 1
    assert unresolved_broker_outcome_mutations() == []


def test_legacy_v1_events_migrate_by_replay_without_rewrite():
    path = safety_latch_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "live_no_new_risk_latch.v1",
                "event_id": "legacy-unknown",
                "event": "activate",
                "active": True,
                "reason": "broker_execution_outcome_unknown",
                "actor": "execution:ctrader_bridge",
                "correlation_id": "legacy-intent",
                "metadata": {"action": "close_position", "position_id": 601},
                "created_at": 1.0,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    status = no_new_risk_latch_status()

    assert status["active"] is True
    assert status["legacy_records_replayed"] is True
    assert status["causes"][0]["cause"] == "broker_execution_unknown"
    assert unresolved_broker_outcome_mutations()[0]["intent_id"] == "legacy-intent"

    activate_no_new_risk_latch(
        reason="new incident projection",
        actor="operator:test",
        cause="incident_control",
        cause_id="runtime_incident_mode",
    )
    clear_no_new_risk_latch(
        reason="new governed thaw",
        actor="operator:test",
        cause="incident_control",
        cause_id="runtime_incident_mode",
    )
    assert unresolved_broker_outcome_mutations()[0]["intent_id"] == "legacy-intent"
    assert _causes() == {"broker_execution_unknown"}


def test_incident_thaw_commits_config_but_retains_independent_broker_blocker(
    monkeypatch,
):
    from backend.services import incident_controls as module

    monkeypatch.setattr(module, "is_state_db_path", lambda _path: True)
    monkeypatch.setattr(
        IncidentControlPlan,
        "execute",
        lambda self, _db_path: {"ok": True, "status": "applied", "mutation_id": "m-1"},
    )
    runtime_config.replace(
        runtime_config.RuntimeConfig(runtime_incident_mode="no_new_risk")
    )
    activate_no_new_risk_latch(
        reason="incident active",
        actor="operator:test",
        cause="incident_control",
        cause_id="runtime_incident_mode",
    )
    activate_no_new_risk_latch(
        reason="broker_execution_outcome_unknown",
        actor="execution:ctrader_bridge",
        correlation_id="intent-residual",
        metadata={"action": "close_position", "position_id": 701},
    )

    result = module.RuntimeIncidentControlService(STATE_DB).set_mode(
        "normal",
        actor="operator:test",
        reason="incident evidence cleared",
        confirm_thaw=True,
    )

    assert result["ok"] is False
    assert result["status"] == "governance_committed_safety_latch_retained"
    assert result["mutation"]["ok"] is True
    assert result["effective_mode"] == "no_new_risk"
    assert result["resume_required"] is True
    assert result["remaining_latch_causes"] == ["broker_execution_unknown"]
    assert no_new_risk_latch_status()["active"] is True


def test_configured_normal_cannot_use_incident_thaw_for_independent_cause(
    monkeypatch,
):
    from backend.services import incident_controls as module

    executed = {"value": False}
    monkeypatch.setattr(module, "is_state_db_path", lambda _path: True)
    monkeypatch.setattr(
        IncidentControlPlan,
        "execute",
        lambda self, _db_path: executed.update(value=True) or {"ok": True},
    )
    runtime_config.replace(runtime_config.RuntimeConfig(runtime_incident_mode="normal"))
    activate_no_new_risk_latch(
        reason="emergency_close",
        actor="system:emergency_close",
        correlation_id="emergency-blocker",
    )

    result = module.RuntimeIncidentControlService(STATE_DB).set_mode(
        "normal",
        actor="operator:test",
        reason="incident thaw",
        confirm_thaw=True,
    )

    assert result["status"] == "independent_safety_blockers_active"
    assert result["local_safety_effective"] is True
    assert result["governance_projection_pending"] is False
    assert executed["value"] is False


def test_risk_policy_audits_cause_specific_incident_release():
    verdict = RiskPolicyService.shared().evaluate(
        "set_incident_control",
        {
            "current_mode": "no_new_risk",
            "target_mode": "normal",
            "confirm_thaw": True,
            "local_latch_causes": ["broker_execution_unknown", "incident_control"],
            "release_cause": "incident_control",
        },
    )

    assert verdict.allowed is True
    assert verdict.audit_payload["release_cause"] == "incident_control"
    assert verdict.audit_payload["local_latch_causes"] == [
        "broker_execution_unknown",
        "incident_control",
    ]
