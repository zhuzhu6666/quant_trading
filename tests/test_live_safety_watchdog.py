from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from backend.services import live_service
from backend.services.live_safety_state import (
    activate_no_new_risk_latch,
    no_new_risk_latch_status,
    reset_safety_state_for_tests,
    safety_outbox_path,
)
from config.runtime_config import release_recovered_overlay_authority_latches
from backend.services.live_safety_watchdog import (
    LiveSafetyWatchdog,
    evaluate_safety_freshness,
)
from backend.services.live_safety_plane import LiveSafetyPlane
from backend.services.live_safety_planner import SafetyPlan, safety_candidate


@pytest.fixture(autouse=True)
def _isolated_safety_state(monkeypatch, tmp_path):
    reset_safety_state_for_tests()
    monkeypatch.setenv("QUANT_SAFETY_STATE_DIR", str(tmp_path / "safety"))
    live_service._live_state_update(
        accepting_new_risk=True,
        safety_failure={},
        safety_plane={},
        account_updated_at=0.0,
        positions_updated_at=0.0,
    )
    yield
    reset_safety_state_for_tests()


def test_flag_off_freshness_is_explicitly_not_applicable():
    result = evaluate_safety_freshness(
        {
            "enabled": False,
            "running": True,
            "started_at": 1.0,
            "safety_heartbeat_at": 0.0,
            "account_updated_at": 0.0,
            "positions_updated_at": 0.0,
            "unknown_execution_count": None,
        },
        now=100.0,
    )

    assert result.ok is True
    assert result.state == "not_applicable"
    assert result.blockers == ()


def test_phase2_startup_grace_keeps_missing_timestamps_unknown_without_latching():
    result = evaluate_safety_freshness(
        {
            "enabled": True,
            "running": True,
            "started_at": 95.0,
            "safety_heartbeat_at": 0.0,
            "account_updated_at": 0.0,
            "positions_updated_at": 0.0,
            "unknown_execution_count": None,
        },
        now=100.0,
    )

    assert result.ok is True
    assert result.state == "startup_unknown"
    assert result.ages == {"safety": None, "account": None, "positions": None}


def test_phase2_stale_heartbeat_reconcile_and_unknown_intent_are_blockers():
    result = evaluate_safety_freshness(
        {
            "enabled": True,
            "running": True,
            "started_at": 1.0,
            "safety_heartbeat_at": 80.0,
            "account_updated_at": 79.0,
            "positions_updated_at": 78.0,
            "unknown_execution_count": 1,
        },
        now=100.0,
    )

    assert result.ok is False
    assert set(result.blockers) == {
        "safety_freshness_stale",
        "account_freshness_stale",
        "positions_freshness_stale",
        "unresolved_execution_intent",
    }


def test_watchdog_violation_durably_latches_no_new_risk():
    watchdog = LiveSafetyWatchdog(
        probe=lambda: {
            "enabled": True,
            "running": True,
            "started_at": 1.0,
            "safety_heartbeat_at": 80.0,
            "account_updated_at": 99.0,
            "positions_updated_at": 99.0,
            "unknown_execution_count": 0,
        },
        on_violation=live_service._on_live_safety_watchdog_violation,
        clock=lambda: 100.0,
    )

    result = watchdog.run_once()

    assert result.ok is False
    assert no_new_risk_latch_status()["active"] is True
    assert live_service._live_state_get("accepting_new_risk") is False
    assert safety_outbox_path().exists()


def test_watchdog_requires_consecutive_current_checks_before_recovery():
    now = {"value": 100.0}
    snapshot = {
        "enabled": True,
        "running": True,
        "started_at": 1.0,
        "safety_heartbeat_at": 100.0,
        "account_updated_at": 100.0,
        "positions_updated_at": 100.0,
        "unknown_execution_count": 0,
    }
    recovered: list[SafetyFreshnessResult] = []
    watchdog = LiveSafetyWatchdog(
        probe=lambda: snapshot,
        on_violation=lambda _result: None,
        on_recovery=recovered.append,
        recovery_checks=3,
        clock=lambda: now["value"],
    )

    watchdog.run_once()
    watchdog.run_once()
    assert recovered == []
    watchdog.run_once()
    assert len(recovered) == 1

    snapshot["unknown_execution_count"] = 1
    watchdog.run_once()
    snapshot["unknown_execution_count"] = 0
    watchdog.run_once()
    watchdog.run_once()
    assert len(recovered) == 1
    watchdog.run_once()
    assert len(recovered) == 2


def test_watchdog_recovery_releases_only_its_own_latch_cause():
    activate_no_new_risk_latch(
        reason="incident active",
        actor="operator:test",
        cause="incident_control",
        cause_id="runtime_incident_mode",
    )
    live_service._persist_safety_fail_closed(
        blockers=["unresolved_execution_intent"],
        source="safety_watchdog",
    )
    result = evaluate_safety_freshness(
        {
            "enabled": True,
            "running": True,
            "started_at": 1.0,
            "safety_heartbeat_at": 100.0,
            "account_updated_at": 100.0,
            "positions_updated_at": 100.0,
            "unknown_execution_count": 0,
        },
        now=100.0,
    )

    live_service._on_live_safety_watchdog_recovery(result)

    causes = {
        (item["cause"], item["cause_id"])
        for item in no_new_risk_latch_status()["causes"]
    }
    assert causes == {("incident_control", "runtime_incident_mode")}


def test_live_loop_cause_requires_normal_cycle_and_reconciled_facts(monkeypatch):
    now = time.time()
    activate_no_new_risk_latch(
        reason="live loop safety cycle failed",
        actor="system:live_loop",
        cause="safety_freshness",
        cause_id="live_loop",
    )
    live_service._live_state_update(
        loop_running=True,
        session_state_status="available",
        account_reconciled={"ok": True, "account_id": "acct-1"},
        account_reconcile_id="account-r1",
        positions_reconciled=[],
        positions_reconcile_id="positions-r1",
        safety_plane={
            "status": "completed",
            "accepting_new_risk": True,
        },
    )
    monkeypatch.setattr(
        live_service,
        "_live_safety_watchdog_probe",
        lambda: {
            "enabled": True,
            "running": True,
            "started_at": now - 60.0,
            "safety_heartbeat_at": now,
            "account_updated_at": now,
            "positions_updated_at": now,
            "unknown_execution_count": 0,
        },
    )
    result = evaluate_safety_freshness(
        {
            "enabled": True,
            "running": True,
            "started_at": now - 60.0,
            "safety_heartbeat_at": now,
            "account_updated_at": now,
            "positions_updated_at": now,
            "unknown_execution_count": 0,
        },
        now=now,
    )

    live_service._on_live_safety_watchdog_recovery(result)

    assert no_new_risk_latch_status()["active"] is False
    assert no_new_risk_latch_status()["causes"] == []


def test_watchdog_recovery_refreshes_legacy_live_admission_projection(monkeypatch):
    now = time.time()
    activate_no_new_risk_latch(
        reason="safety freshness failed",
        actor="system:safety_watchdog",
        cause="safety_freshness",
        cause_id="safety_watchdog",
    )
    live_service._live_state_update(
        loop_running=True,
        accepting_new_risk=False,
        session_state_status="available",
        safety_plane={
            "status": "completed",
            "accepting_new_risk": True,
            "blockers": [],
        },
        account_reconciled={"ok": True},
        account_updated_at=now,
        account_reconcile_id="account-r1",
        positions_reconciled=[],
        positions_updated_at=now,
        positions_reconcile_id="positions-r1",
        account_reconcile_failed_at=0.0,
        positions_reconcile_failed_at=0.0,
    )
    monkeypatch.setattr(
        live_service, "_generation_controller_enabled", lambda: False
    )
    result = evaluate_safety_freshness(
        {
            "enabled": True,
            "running": True,
            "started_at": now - 60.0,
            "safety_heartbeat_at": now,
            "account_updated_at": now,
            "positions_updated_at": now,
            "unknown_execution_count": 0,
        },
        now=now,
    )

    live_service._on_live_safety_watchdog_recovery(result)

    assert no_new_risk_latch_status()["active"] is False
    assert live_service._live_state_get("accepting_new_risk") is True


def test_live_loop_cause_stays_latched_when_safety_cycle_is_not_ready():
    now = time.time()
    activate_no_new_risk_latch(
        reason="live loop safety cycle failed",
        actor="system:live_loop",
        cause="safety_freshness",
        cause_id="live_loop",
    )
    live_service._live_state_update(
        loop_running=True,
        session_state_status="available",
        account_reconciled={"ok": True},
        account_reconcile_id="account-r1",
        positions_reconciled=[],
        positions_reconcile_id="positions-r1",
        safety_plane={"status": "completed", "accepting_new_risk": False},
    )
    result = evaluate_safety_freshness(
        {
            "enabled": True,
            "running": True,
            "started_at": now - 60.0,
            "safety_heartbeat_at": now,
            "account_updated_at": now,
            "positions_updated_at": now,
            "unknown_execution_count": 0,
        },
        now=now,
    )

    live_service._on_live_safety_watchdog_recovery(result)

    assert {
        (item["cause"], item["cause_id"])
        for item in no_new_risk_latch_status()["causes"]
    } == {("safety_freshness", "live_loop")}


def test_verified_overlay_recovery_releases_refresh_and_legacy_restore_causes():
    activate_no_new_risk_latch(
        reason="incident active",
        actor="operator:test",
        cause="incident_control",
        cause_id="runtime_incident_mode",
    )
    for cause_id in (
        "runtime_config_overlay_refresh",
        "legacy_restore:runtime_config_overlay",
    ):
        activate_no_new_risk_latch(
            reason="authority unverified",
            actor="system:test",
            cause="governance_authority",
            cause_id=cause_id,
        )

    assert release_recovered_overlay_authority_latches(
        {
            "overlay_hash": "verified-hash",
            "mutation_id": "mutation-1",
            "authority": {
                "authority": "committed_mutation",
                "checks": {"projection_current": True},
            },
        }
    )

    causes = {
        (item["cause"], item["cause_id"])
        for item in no_new_risk_latch_status()["causes"]
    }
    assert causes == {("incident_control", "runtime_incident_mode")}


def test_watchdog_releases_missing_supervisor_position_after_fresh_reconcile():
    now = time.time()
    activate_no_new_risk_latch(
        reason="incident active",
        actor="operator:test",
        cause="incident_control",
        cause_id="runtime_incident_mode",
    )
    activate_no_new_risk_latch(
        reason="safety freshness failed",
        actor="system:supervisor_tighten",
        metadata={
            "error": "amend_projection_unverified:position_missing_after_amend",
        },
        cause="safety_freshness",
        cause_id="supervisor_tighten",
    )
    live_service._live_state_update(
        positions_reconciled=[],
        positions_updated_at=now,
        positions_reconcile_id="reconcile-empty",
        safety_failure={"source": "supervisor_tighten"},
    )
    result = evaluate_safety_freshness(
        {
            "enabled": True,
            "running": True,
            "started_at": now - 60.0,
            "safety_heartbeat_at": now,
            "account_updated_at": now,
            "positions_updated_at": now,
            "unknown_execution_count": 0,
        },
        now=now,
    )

    live_service._on_live_safety_watchdog_recovery(result)

    causes = {
        (item["cause"], item["cause_id"])
        for item in no_new_risk_latch_status()["causes"]
    }
    assert causes == {("incident_control", "runtime_incident_mode")}
    assert live_service._live_state_get("safety_failure", clone=True) == {}


def test_watchdog_keeps_supervisor_cause_while_target_position_is_open():
    now = time.time()
    activate_no_new_risk_latch(
        reason="safety freshness failed",
        actor="system:supervisor_tighten",
        metadata={
            "error": "amend_projection_unverified:position_missing_after_amend",
            "position_id": 74,
        },
        cause="safety_freshness",
        cause_id="supervisor_tighten",
    )
    live_service._live_state_update(
        positions_reconciled=[{"position_id": 74}],
        positions_updated_at=now,
        positions_reconcile_id="reconcile-open",
    )
    result = evaluate_safety_freshness(
        {
            "enabled": True,
            "running": True,
            "started_at": now - 60.0,
            "safety_heartbeat_at": now,
            "account_updated_at": now,
            "positions_updated_at": now,
            "unknown_execution_count": 0,
        },
        now=now,
    )

    live_service._on_live_safety_watchdog_recovery(result)

    assert {
        (item["cause"], item["cause_id"])
        for item in no_new_risk_latch_status()["causes"]
    } == {("safety_freshness", "supervisor_tighten")}


def test_watchdog_records_its_own_cause_when_incident_latch_already_active():
    activate_no_new_risk_latch(
        reason="incident active",
        actor="operator:test",
        cause="incident_control",
        cause_id="runtime_incident_mode",
    )

    live_service._persist_safety_fail_closed(
        blockers=["safety_freshness_stale"],
        source="safety_watchdog",
    )

    causes = {
        (item["cause"], item["cause_id"])
        for item in no_new_risk_latch_status()["causes"]
    }
    assert causes == {
        ("incident_control", "runtime_incident_mode"),
        ("safety_freshness", "safety_watchdog"),
    }


def test_stale_watchdog_and_unknown_execution_block_open_but_protection_continues(monkeypatch):
    watchdog = LiveSafetyWatchdog(
        probe=lambda: {
            "enabled": True,
            "running": True,
            "started_at": 1.0,
            "safety_heartbeat_at": 80.0,
            "account_updated_at": 99.0,
            "positions_updated_at": 99.0,
            "unknown_execution_count": 1,
        },
        on_violation=live_service._on_live_safety_watchdog_violation,
        clock=lambda: 100.0,
    )
    freshness = watchdog.run_once()
    protected: list[dict] = []
    position = {
        "position_id": 904,
        "symbol": "XAUUSD+",
        "direction": -1,
        "volume": 100.0,
        "entry_price": 2400.0,
        "current_price": 2399.0,
    }

    class _Bridge:
        is_connected = True

        def unresolved_execution_intent_count(self):
            return 1

        def get_spot_quote(self):
            return {"bid": 2398.9, "ask": 2399.1, "mid": 2399.0, "ts": time.time()}

    reconcile = SimpleNamespace(
        status="fresh",
        reconcile_id="positions-risk-reduction-r1",
        observed_at=time.time(),
        generated_at=time.time(),
        positions=(position,),
    )
    monkeypatch.setattr(
        live_service,
        "_get_live_safety_plane",
        lambda _generation_id="": LiveSafetyPlane(mode="enforce", clock=lambda: 100.0),
    )
    candidate = safety_candidate(
        action="tighten",
        position_id=904,
        source="supervisor_tighten",
        controls={"target_stop_loss": 2399.5},
    )
    plan = SafetyPlan(candidates=(candidate,), arbitration=(), planned_at=100.0)
    monkeypatch.setattr(live_service, "_plan_live_safety_candidates", lambda **_kwargs: plan)
    monkeypatch.setattr(
        live_service,
        "_preview_legacy_live_safety_candidates",
        lambda **_kwargs: plan,
    )
    monkeypatch.setattr(
        live_service,
        "_execute_live_safety_candidate",
        lambda _candidate, *, positions, **_kwargs: (
            protected.extend(positions) or {"ok": True, "status": "dispatched"}
        ),
    )

    safety = live_service._run_live_safety_cycle(
        bridge=_Bridge(),
        broker="ctrader",
        tick=1,
        log=lambda _message: None,
        reconcile_result=reconcile,
    )

    assert freshness.ok is False
    assert no_new_risk_latch_status()["active"] is True
    assert live_service._open_trade_draining() is True
    assert safety["accepting_new_risk"] is False
    assert "unknown_execution" in safety["blockers"]
    assert [item["position_id"] for item in protected] == [904]


def test_safety_cycle_exception_retries_in_five_seconds_and_latches(monkeypatch):
    bridge = SimpleNamespace(is_connected=True)
    monkeypatch.setattr(live_service, "_phase2_v2_active", lambda: True)
    monkeypatch.setattr(live_service, "_generation_controller_enabled", lambda: False)
    monkeypatch.setattr(live_service, "_get_ctrader", lambda: (bridge, None, False))
    monkeypatch.setattr(live_service, "_explicit_position_reconcile", lambda _bridge: {})
    monkeypatch.setattr(
        live_service,
        "_run_live_safety_cycle",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("planner exploded")),
    )

    result = live_service._run_live_loop_tick_body(
        broker="ctrader",
        bridge_cfg=SimpleNamespace(),
        timeframe="M5",
        tick=9,
        recovery_bootstrapped=False,
        stop_requested=lambda: False,
        log=lambda _message: None,
    )

    assert result["wait_seconds"] == 5.0
    assert result["safety"]["accepting_new_risk"] is False
    assert result["safety"]["blockers"] == ["safety_cycle_exception"]
    assert no_new_risk_latch_status()["active"] is True


def test_v2_readiness_requires_fresh_account_positions_and_safety(monkeypatch):
    now = time.time()
    monkeypatch.setattr(live_service, "_phase2_v2_active", lambda: True)
    monkeypatch.setattr(live_service, "_probe_ctrader", lambda: ("connected", None))
    monkeypatch.setattr(
        live_service,
        "loop_status",
        lambda: {
            "running": True,
            "phase": "running",
            "ready": True,
            "accepting_new_risk": True,
            "blockers": [],
            "safety_heartbeat_age_sec": 16.0,
            "safety": {
                "unknown_execution_count": 0,
                "reconciliation_state": "fresh",
                "accepting_new_risk": True,
                "blockers": [],
            },
        },
    )
    live_service._live_state_update(
        _diag={"bridge_ready": True},
        account={"ok": True},
        account_reconciled={"ok": True},
        account_updated_at=now - 16.0,
        account_reconcile_id="account-stale-r1",
        positions=[],
        positions_reconciled=[],
        positions_updated_at=now - 16.0,
        positions_reconcile_id="positions-stale-r1",
    )

    readiness = live_service.get_live_readiness("ctrader")

    assert readiness["ok"] is False
    assert readiness["account_ready"] is False
    assert readiness["positions_ready"] is False
    assert readiness["safety_ready"] is False
    assert "safety_heartbeat_stale" in readiness["reasons"]
    assert "account_reconcile_stale" in readiness["reasons"]
    assert "positions_reconcile_stale" in readiness["reasons"]
