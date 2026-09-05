"""Tests for live_service circuit breaker (drawdown protection)."""
import pytest

from backend.services import live_service
from risk.runtime_policy import RiskLimitSnapshot


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """Reset circuit_breaker + session stats between tests."""
    monkeypatch.setattr(live_service, "bounded_demo_mode_active", lambda: False)
    live_service._reset_session_state_for_new_day()
    live_service._live_state_update(session_start_balance=1000.0)
    yield
    live_service._reset_session_state_for_new_day()
    live_service._live_state_update(session_start_balance=1000.0)


def test_circuit_breaker_starts_false():
    assert live_service._live_state_get("circuit_breaker") is False
    assert live_service._live_state_get("circuit_reason") == ""


def test_circuit_breaker_triggers_at_5_percent_drawdown():
    live_service._live_state_update(session_pnl=-50.0, session_start_balance=1000.0)

    result = live_service._evaluate_daily_drawdown(
        risk_limits=RiskLimitSnapshot(max_daily_loss_pct=5.0)
    )

    assert result["tripped"] is True
    assert result["dd_pct"] == 5.0
    assert live_service._live_state_get("circuit_breaker") is True
    assert "daily drawdown" in live_service._live_state_get("circuit_reason")


def test_circuit_breaker_uses_risk_limit_snapshot_threshold():
    live_service._live_state_update(session_pnl=-40.0, session_start_balance=1000.0)

    result = live_service._evaluate_daily_drawdown(
        risk_limits=RiskLimitSnapshot(max_daily_loss_pct=4.0)
    )

    assert result["tripped"] is True
    assert result["risk_limits"]["max_daily_loss_pct"] == 4.0
    assert live_service._live_state_get("circuit_breaker") is True


def test_demo_drawdown_is_observed_without_tripping_circuit(monkeypatch):
    monkeypatch.setattr(live_service, "bounded_demo_mode_active", lambda: True)
    live_service._live_state_update(
        session_pnl=-500.0,
        session_start_balance=1000.0,
        circuit_breaker=True,
        circuit_reason="stale live circuit",
    )

    result = live_service._evaluate_daily_drawdown(
        risk_limits=RiskLimitSnapshot(max_daily_loss_pct=5.0)
    )

    assert result["tripped"] is False
    assert result["observed_tripped"] is True
    assert result["observed_reason"] == "daily drawdown 50.0%"
    assert live_service._live_state_get("circuit_breaker") is False
    assert live_service._live_state_get("circuit_reason") == ""


def test_demo_consecutive_loss_observation_survives_tick_evaluation(monkeypatch):
    monkeypatch.setattr(live_service, "bounded_demo_mode_active", lambda: True)
    live_service._live_state_update(
        session_pnl=-10.0,
        session_start_balance=1000.0,
        session_consecutive_loss=8,
    )

    result = live_service._evaluate_daily_drawdown(
        risk_limits=RiskLimitSnapshot(
            max_consecutive_losses=8,
            max_daily_loss_pct=50.0,
        )
    )

    assert result["tripped"] is False
    assert result["observed_tripped"] is True
    assert result["observed_reason"] == "consecutive losses 8"
    assert live_service._live_state_get("session_circuit_observation") == {
        "triggered": True,
        "reason": "consecutive losses 8",
        "enforced": False,
    }


def test_circuit_breaker_does_not_trip_below_5_percent():
    live_service._live_state_update(session_pnl=-40.0, session_start_balance=1000.0)

    result = live_service._evaluate_daily_drawdown()

    assert result["tripped"] is False
    assert live_service._live_state_get("circuit_breaker") is False


def test_positive_pnl_does_not_trigger_breaker():
    live_service._live_state_update(session_pnl=100.0, session_start_balance=1000.0)

    result = live_service._evaluate_daily_drawdown()

    assert result["tripped"] is False
    assert live_service._live_state_get("circuit_breaker") is False


def test_breaker_resets_on_new_day():
    live_service._live_state_update(
        circuit_breaker=True,
        circuit_reason="daily drawdown 5.2%",
        session_pnl=-52.0,
        session_trades=4,
        session_losing=3,
        session_consecutive_loss=3,
        session_max_drawdown_pct=5.2,
    )

    live_service._reset_session_state_for_new_day()

    assert live_service._live_state_get("circuit_breaker") is False
    assert live_service._live_state_get("circuit_reason") == ""
    assert live_service._live_state_get("session_pnl") == 0.0
    assert live_service._live_state_get("session_trades") == 0
    assert live_service._live_state_get("session_max_drawdown_pct") == 0.0



def test_set_factor_snapshot_writes_both_views():
    votes = {"rsi_14": {"signal": 0.2, "direction": 1}}
    composite = {"direction": 1, "score": 0.8}

    live_service._set_factor_snapshot(votes, composite)

    assert live_service._live_state_get("last_factor_votes", clone=True) == votes
    assert live_service._live_state_get("last_composite", clone=True) == composite


def test_pending_close_ids_track_deal_wait_without_touching_pnl():
    before = live_service._live_state_get("session_pnl")
    live_service._live_state_update(session_pending_close_add=99001)
    live_service._live_state_update(session_pending_close_add=[99002, 99001])
    ids = live_service._live_state_get("session_pending_close_ids")
    assert ids.count(99001) == 1
    assert ids.count(99002) == 1
    assert live_service._live_state_get("session_pnl") == before
    assert live_service._live_state_get("session_consecutive_loss") == 0
    live_service._live_state_update(session_pending_close_remove=[99001])
    ids = live_service._live_state_get("session_pending_close_ids")
    assert 99001 not in ids
    assert 99002 in ids
    live_service._live_state_update(session_pending_close_remove=[99002])
    assert 99002 not in live_service._live_state_get("session_pending_close_ids")
