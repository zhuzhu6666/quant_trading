from pathlib import Path


def test_miniprogram_dangerous_live_calls_require_explicit_confirmation():
    source = (Path(__file__).resolve().parents[1] / "miniprogram_v2/services/live.js").read_text(encoding="utf-8")

    assert "startTradingLoop requires explicit confirmation" in source
    assert "emergencyCloseAll requires explicit confirmation" in source
    assert "options.confirmed" in source


def test_web_dangerous_live_calls_use_confirmed_flag():
    source = (Path(__file__).resolve().parents[1] / "web_frontend/src/api/client.ts").read_text(encoding="utf-8")

    assert 'confirmed ? { "X-Confirm": "start-live" }' in source
    assert 'confirmed ? { "X-Confirm": "emergency" }' in source
