def test_state_snapshot_reports_consecutive_not_total_session_losses(monkeypatch):
    from backend.services import live_service
    from backend.ws import endpoints

    monkeypatch.setattr(
        live_service,
        "_live_state",
        {
            "account": {"equity": 1000.0, "balance": 1000.0},
            "positions": [],
            "loop_started_at": 1.0,
            "session_losing": 17,
            "session_consecutive_loss": 2,
        },
    )
    monkeypatch.setattr(endpoints, "_live_loop_status", lambda: {"running": True})
    monkeypatch.setattr(endpoints, "_live_get_latest_price", lambda: 3300.0)
    monkeypatch.setattr(endpoints, "_read_closed_loop_status", lambda *_args: {})

    snapshot = endpoints._read_state_snapshot()

    assert snapshot["daily"]["loss"] == 17
    assert snapshot["risk"]["consecutive_loss"] == 2
    assert snapshot["_fact"]["contract"] == "live.state.v2"
    assert snapshot["_fact"]["state"] == "unknown"
