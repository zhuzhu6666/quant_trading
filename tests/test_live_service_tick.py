"""Tests for live_service._local_positions tracking (Task 1 of cTrader SL/TP refactor).

audit 2026-06-10: SL/TP local mirror — bridge.amend_position_sltp() success is
recorded here so the live loop knows what SL/TP sit on the server and can
reconcile on next tick / broker rejection. Task 2 will add tick-level tests.
"""
import threading
import time

import pytest

from backend.services import live_service


@pytest.fixture(autouse=True)
def _reset_state():
    """Reset module-level state between tests."""
    live_service._local_positions.clear()
    live_service._live_state["account"] = None
    live_service._live_state["positions"] = []
    yield
    live_service._local_positions.clear()
    live_service._live_state["account"] = None
    live_service._live_state["positions"] = []


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
    amend_result = MagicMock()
    amend_result.success = True
    amend_result.position_id = position_id
    amend_result.comment = "amend ok"
    bridge.amend_position_sltp.return_value = amend_result
    return bridge


def _fake_signal(direction=1, atr=7.0, sl_atr=2.0, tp_atr=3.0, price=4500.0):
    sig = MagicMock()
    sig.direction = direction
    sig.atr = atr
    sig.sl_atr = sl_atr
    sig.tp_atr = tp_atr
    sig.price = price
    sig.strength = 1.0
    sig.strategy = "test"
    return sig


def test_local_positions_initially_empty():
    assert live_service._local_positions == {}


def test_track_local_position_adds_entry():
    live_service._track_local_sl_tp(position_id=42, sl=4486.0, tp=4521.0)
    assert 42 in live_service._local_positions
    entry = live_service._local_positions[42]
    assert entry.sl == 4486.0
    assert entry.tp == 4521.0
    assert abs(time.time() - entry.updated_at) < 5
