"""Frontend dangerous-live-call confirmation contracts.

The server deployment is a backend-only sparse checkout (no
``miniprogram_v2`` / ``web_frontend`` sources), so these contracts are
skipped when the source files are absent and enforced in full checkouts
(CI / desktop workbench).
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_miniprogram_does_not_export_dangerous_live_mutations():
    source_path = ROOT / "miniprogram_v2/services/live.js"
    if not source_path.exists():
        pytest.skip("miniprogram_v2 sources are not part of the server sparse checkout")
    source = source_path.read_text(encoding="utf-8")

    # The mini-program is status-only.  Risk-creating and emergency controls
    # live in the Web console, so the safest confirmation contract here is to
    # have no callable mutation export at all.
    assert "startTradingLoop" not in source
    assert "stopTradingLoop" not in source
    assert "emergencyCloseAll" not in source


def test_web_dangerous_live_calls_use_confirmed_flag():
    source_path = ROOT / "web_frontend/src/api/client.ts"
    if not source_path.exists():
        pytest.skip("web_frontend sources are not part of the server sparse checkout")
    source = source_path.read_text(encoding="utf-8")

    assert 'confirmed ? { "X-Confirm": "start-live" }' in source
    assert 'confirmed ? { "X-Confirm": "emergency" }' in source
