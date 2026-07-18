from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from backend.services.live_autonomy import LiveAutonomyService, ensure_live_autonomy_unlock_table
from backend.services.proposal_registry import ProposalRegistryService, ensure_proposal_registry_table
from backend.core.db import connect_sqlite
from config import runtime_config as rc


def _ready_payload() -> dict:
    now = time.time()
    return {
        "ready_for_frontend": True,
        "generated_at": now,
        "blockers": [],
        "live": {
            "ctrader": {"status": "connected"},
            "loop": {"running": True},
            "readiness": {"ok": True},
        },
        "execution_semantics": {"effective_send_orders": True},
        "incident_control": {"mode": "normal"},
        "release": {
            "ok": True,
            "latest_release": {"rollback_ref": {"snapshot_hash": "snap1"}, "created_at": now},
        },
        "replay": {"ok": True, "age_seconds": 10.0, "stale_after_seconds": 86400.0},
        "autonomy_health": {"posture": "full"},
    }


def test_live_unlock_evaluation_blocks_missing_replay_and_release(tmp_path):
    db_path = tmp_path / "state.db"
    ensure_live_autonomy_unlock_table(db_path)
    ensure_proposal_registry_table(db_path)
    readiness = _ready_payload()
    readiness["replay"] = {"ok": False}
    readiness["release"] = {"ok": True, "latest_release": {"rollback_ref": {}}}

    result = LiveAutonomyService(db_path).evaluate(readiness=readiness, refresh_proposals=False, persist=False)

    assert result["ok"] is False
    components = {item["component"] for item in result["blockers"]}
    assert "replay" in components
    assert "release" in components


def test_live_unlock_success_persists_overlay_and_event(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    ensure_live_autonomy_unlock_table(db_path)
    ensure_proposal_registry_table(db_path)
    rc.replace(rc.RuntimeConfig(autonomy_mode="live_candidate", live_autonomy_unlocked=False, live_autonomy_unlock_id=""))
    monkeypatch.setattr("backend.services.runtime_config_mutation.is_state_db_path", lambda _path: False)

    result = LiveAutonomyService(db_path).unlock(
        readiness=_ready_payload(),
        confirm=True,
        actor="test",
        reason="unit test",
    )

    assert result["ok"] is True
    assert rc.shared().autonomy_mode == "live_autonomous"
    assert rc.shared().live_autonomy_unlocked is True
    assert rc.shared().live_autonomy_unlock_id
    conn = connect_sqlite(db_path)
    try:
        row = conn.execute("SELECT status, action FROM live_autonomy_unlock_event ORDER BY created_at DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    assert tuple(row) == ("unlocked", "unlock")


def test_live_autonomy_revoke_returns_to_candidate(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    ensure_live_autonomy_unlock_table(db_path)
    rc.replace(rc.RuntimeConfig(autonomy_mode="live_autonomous", live_autonomy_unlocked=True, live_autonomy_unlock_id="unlock1"))
    monkeypatch.setattr("backend.services.runtime_config_mutation.is_state_db_path", lambda _path: False)
    monkeypatch.setattr(LiveAutonomyService, "_build_readiness", staticmethod(_ready_payload))

    result = LiveAutonomyService(db_path).revoke(actor="test", reason="unit test")

    assert result["ok"] is True
    assert rc.shared().autonomy_mode == "live_candidate"
    assert rc.shared().live_autonomy_unlocked is False
    assert rc.shared().live_autonomy_unlock_id == ""


def test_live_unlock_evaluation_blocks_stale_replay_evidence(tmp_path):
    db_path = tmp_path / "state.db"
    ensure_live_autonomy_unlock_table(db_path)
    ensure_proposal_registry_table(db_path)
    readiness = _ready_payload()
    readiness["replay"] = {"ok": True, "age_seconds": 90000.0, "stale_after_seconds": 86400.0}

    result = LiveAutonomyService(db_path).evaluate(readiness=readiness, refresh_proposals=False, persist=False)

    assert result["ok"] is False
    assert result["evidence_freshness"]["status"] == "stale"
    assert any(item["component"] == "replay" and item["status"] == "stale_evidence" for item in result["blockers"])


def test_live_unlock_treats_null_age_without_timestamp_as_missing(tmp_path):
    db_path = tmp_path / "state.db"
    ensure_live_autonomy_unlock_table(db_path)
    ensure_proposal_registry_table(db_path)
    readiness = _ready_payload()
    readiness["replay"] = {
        "ok": True,
        "age_seconds": None,
        "stale_after_seconds": 86400.0,
    }

    result = LiveAutonomyService(db_path).evaluate(
        readiness=readiness,
        refresh_proposals=False,
        persist=False,
    )

    replay = result["evidence_freshness"]["items"]["replay"]
    assert replay["status"] == "missing_timestamp"
    assert replay["age_seconds"] is None
    assert result["ok"] is False


def test_live_autonomy_status_degrades_live_mode_when_evidence_is_stale(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    ensure_live_autonomy_unlock_table(db_path)
    ensure_proposal_registry_table(db_path)
    rc.replace(rc.RuntimeConfig(autonomy_mode="live_autonomous", live_autonomy_unlocked=True, live_autonomy_unlock_id="unlock1"))
    stale = _ready_payload()
    stale["replay"] = {"ok": True, "age_seconds": 90000.0, "stale_after_seconds": 86400.0}
    monkeypatch.setattr(LiveAutonomyService, "_build_readiness", staticmethod(lambda: stale))

    status = LiveAutonomyService(db_path).status()

    assert status["operational_posture"]["status"] == "degraded"
    assert status["operational_posture"]["recommended_incident_mode"] == "no_new_risk"


def test_budget_breach_writes_event_that_registry_routes_to_incident_tighten(tmp_path):
    db_path = tmp_path / "state.db"
    ensure_live_autonomy_unlock_table(db_path)
    ensure_proposal_registry_table(db_path)
    readiness = _ready_payload()
    readiness["session"] = {"daily_loss_pct": 5.0, "drawdown_pct": 1.0, "trades": 1}
    readiness["risk_limits"] = {"max_daily_loss_pct": 5.0, "max_drawdown_pct": 15.0, "max_daily_trades": 20}

    result = LiveAutonomyService(db_path).evaluate(
        readiness=readiness,
        refresh_proposals=False,
        persist=True,
        actor="test",
    )
    registry = ProposalRegistryService(db_path).refresh()
    proposals = ProposalRegistryService(db_path).latest(limit=10)["items"]
    budget_proposal = next(item for item in proposals if item["source_ref_type"] == "live_autonomy_unlock_event")

    assert result["ok"] is False
    assert result["budget_breach_response"]["recommended_incident_mode"] == "no_new_risk"
    assert registry["refreshed_count"] >= 1
    assert budget_proposal["control_surface"] == "incident_control"
    assert budget_proposal["route_recommendation"] == "tighten_incident"


@pytest.fixture(autouse=True)
def _reset_runtime_config():
    original = rc.shared()
    try:
        yield
    finally:
        rc.replace(original)
