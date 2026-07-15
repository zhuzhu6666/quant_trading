from backend.services.market_session import maintenance_wait_evidence


def _session(**patch):
    payload = {
        "status": "open_pending_quote",
        "api_available": True,
        "broker_connected": True,
        "evidence": ["market_data_stale"],
    }
    payload.update(patch)
    return payload


def test_maintenance_wait_is_active_before_75_minutes():
    result = maintenance_wait_evidence(
        _session(), latest_market_data_ts=1000.0, now_ts=1000.0 + 74 * 60, grace_seconds=4500.0
    )
    assert result["active"] is True
    assert result["remaining_seconds"] == 60.0


def test_maintenance_wait_expires_at_75_minutes_and_remains_expired_afterward():
    at_limit = maintenance_wait_evidence(
        _session(), latest_market_data_ts=1000.0, now_ts=1000.0 + 75 * 60, grace_seconds=4500.0
    )
    after_limit = maintenance_wait_evidence(
        _session(), latest_market_data_ts=1000.0, now_ts=1000.0 + 76 * 60, grace_seconds=4500.0
    )
    assert at_limit["active"] is False
    assert after_limit["active"] is False


def test_maintenance_wait_requires_healthy_api_and_pending_quote_state():
    disconnected = maintenance_wait_evidence(
        _session(broker_connected=False),
        latest_market_data_ts=1000.0,
        now_ts=1100.0,
        grace_seconds=4500.0,
    )
    open_market = maintenance_wait_evidence(
        _session(status="open"),
        latest_market_data_ts=1000.0,
        now_ts=1100.0,
        grace_seconds=4500.0,
    )
    assert disconnected["active"] is False
    assert open_market["active"] is False


def test_connected_stale_market_data_can_enter_bounded_maintenance_wait():
    result = maintenance_wait_evidence(
        _session(status="broker_connected_market_data_stale"),
        latest_market_data_ts=1000.0,
        now_ts=1100.0,
        grace_seconds=4500.0,
    )
    assert result["active"] is True


def test_broker_error_disables_maintenance_wait():
    result = maintenance_wait_evidence(
        _session(evidence=["broker_market_closed_error"]),
        latest_market_data_ts=1000.0,
        now_ts=1100.0,
        grace_seconds=4500.0,
    )
    assert result["active"] is False
