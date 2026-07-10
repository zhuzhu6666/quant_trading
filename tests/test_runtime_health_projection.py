from __future__ import annotations

from backend.services.runtime_health_projection import RuntimeHealthProjectionService


def test_runtime_health_projection_round_trip(tmp_path):
    service = RuntimeHealthProjectionService(tmp_path / "runtime.db")

    published = service.publish(
        market_session={"status": "closed", "reason": "weekend"},
        ctrader_connected=True,
        live_loop_running=True,
    )
    latest = service.latest(max_age_seconds=60)

    assert published["ok"] is True
    assert latest["status"] == "fresh"
    assert latest["ctrader"]["status"] == "connected"
    assert latest["market_session"]["status"] == "closed"
    assert latest["live_loop"]["running"] is True
    assert latest["boundary"]["does_not_authorize_trading"] is True


def test_runtime_health_projection_missing_store_is_nonfatal(tmp_path):
    result = RuntimeHealthProjectionService(tmp_path / "missing" / "runtime.db").latest()

    assert result["ok"] is False
    assert result["status"] == "unavailable"

