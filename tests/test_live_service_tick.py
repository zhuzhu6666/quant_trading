"""Tests for live_service._process_tick + _local_positions tracking.

audit 2026-06-10: SL/TP local mirror + non-blocking tick reads.
Task 1 added _local_positions / _track_local_sl_tp.
Task 2 added tick-level tests for _process_tick (no sync broker reads + amend).

2026-06-13: Removed old strategy-driven _process_tick tests.
Now _process_tick dispatches to Factor Takeover v4 pipeline.
Tests for the pipeline itself are in tests/alpha/.
"""
import threading
import time

import pytest
from unittest.mock import MagicMock

from backend.services import live_service


@pytest.fixture(autouse=True)
def _reset_state():
    """Reset module-level state between tests."""
    live_service._local_positions.clear()
    live_service._live_state["account"] = None
    live_service._live_state["positions"] = []
    live_service._pos_open_api_volume.clear()
    yield
    live_service._local_positions.clear()
    live_service._live_state["account"] = None
    live_service._live_state["positions"] = []
    live_service._pos_open_api_volume.clear()


def _make_df():
    """Tiny M15 OHLCV dataframe for tick tests."""
    import pandas as pd
    idx = pd.date_range("2026-06-10 10:00", periods=5, freq="15min", tz="UTC")
    return pd.DataFrame({
        "open":  [4495, 4500, 4502, 4503, 4500],
        "high":  [4501, 4503, 4504, 4505, 4502],
        "low":   [4494, 4499, 4500, 4502, 4498],
        "close": [4500, 4502, 4503, 4503, 4500],
        "volume": [100, 110, 120, 130, 140],
    }, index=idx)


def _fake_bridge(position_id=12345, order_id=99):
    """Mock CTraderBridge: market_buy/sell returns ok, amend accepts."""
    bridge = MagicMock()
    result = MagicMock()
    result.success = True
    result.order_id = order_id
    result.position_id = position_id
    result.comment = "ok"
    bridge.market_buy.return_value = result
    bridge.market_sell.return_value = result
    bridge.amend_position_sltp.return_value = MagicMock(
        success=True, position_id=position_id,
    )
    return bridge


def test_resolve_position_api_volume_prefers_refreshed_position():
    """Actual filled size should come from refreshed broker positions, not the request."""
    refreshed = [{"position_id": 12345, "volume": 130.0}]
    vol = live_service._resolve_position_api_volume(12345, refreshed, 100.0)
    assert vol == 130.0


# ── _local_positions ─────────────────────────────────────


def test_local_positions_initially_empty():
    assert live_service._local_positions == {}


def test_track_local_position_adds_entry():
    live_service._track_local_sl_tp(position_id=42, sl=4480.0, tp=4550.0)
    assert 42 in live_service._local_positions
    entry = live_service._local_positions[42]
    assert entry.position_id == 42
    assert entry.sl == 4480.0
    assert entry.tp == 4550.0


def test_process_tick_does_not_call_account_info_or_get_positions_synchronously(monkeypatch):
    """Live tick is decision-only; it should NOT call bridge.account_info() or
    get_positions() synchronously — those live in the background refresh."""
    bridge = _fake_bridge()
    strategy = MagicMock()
    strategy.on_bar.return_value = None  # no signal
    strategy.last_atr = 7.0

    # Pre-populate cache as if the last WS push / status update wrote it
    live_service._live_state["account"] = {
        "ok": True, "broker": "ctrader", "balance": 10000.0,
        "equity": 10000.0, "currency": "USD", "leverage": 100,
    }
    live_service._live_state["positions"] = []

    df_new = _make_df()
    last_bar = df_new.iloc[-1]
    log_fn = MagicMock()

    with monkeypatch.context() as m:
        m.setattr("backend.services.live_service._should_send_orders", lambda broker: False)
        m.setattr("time.sleep", lambda s: None)
        live_service._process_tick(bridge, strategy, df_new, last_bar, "ctrader", tick=1, log=log_fn)

    bridge.account_info.assert_not_called()
    bridge.get_positions.assert_not_called()


def test_process_tick_dry_run_does_not_call_amend(monkeypatch):
    """When _factor_pipeline is None, dry-run tick should not fire orders."""
    bridge = _fake_bridge()
    strategy = MagicMock()
    strategy.on_bar.return_value = None
    strategy.last_atr = 7.0

    live_service._live_state["account"] = {"ok": True, "balance": 10000.0, "equity": 10000.0}
    live_service._live_state["positions"] = []
    live_service._factor_pipeline = None
    log_fn = MagicMock()

    with monkeypatch.context() as m:
        m.setattr("backend.services.live_service._should_send_orders", lambda broker: False)
        m.setattr("time.sleep", lambda s: None)
        live_service._process_tick(bridge, strategy, _make_df(), _make_df().iloc[-1], "ctrader", tick=1, log=log_fn)

    bridge.market_buy.assert_not_called()
    bridge.market_sell.assert_not_called()
