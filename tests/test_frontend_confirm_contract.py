from pathlib import Path


def test_miniprogram_does_not_export_dangerous_live_mutations():
    source = (Path(__file__).resolve().parents[1] / "miniprogram_v2/services/live.js").read_text(encoding="utf-8")

    # The mini-program is status-only.  Risk-creating and emergency controls
    # live in the Web console, so the safest confirmation contract here is to
    # have no callable mutation export at all.
    assert "startTradingLoop" not in source
    assert "stopTradingLoop" not in source
    assert "emergencyCloseAll" not in source


def test_web_dangerous_live_calls_use_confirmed_flag():
    source = (Path(__file__).resolve().parents[1] / "web_frontend/src/api/client.ts").read_text(encoding="utf-8")

    assert 'confirmed ? { "X-Confirm": "start-live" }' in source
    assert 'confirmed ? { "X-Confirm": "emergency" }' in source
