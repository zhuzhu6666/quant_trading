"""Tests for live_service._process_tick + _local_positions tracking.

audit 2026-06-10: SL/TP local mirror + non-blocking tick reads.
Task 1 added _local_positions / _track_local_sl_tp.
Task 2 added tick-level tests for _process_tick (no sync broker reads + amend).
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
    yield
    live_service._local_positions.clear()
    live_service._live_state["account"] = None
    live_service._live_state["positions"] = []


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


# ── Task 2: _process_tick tests (audit 2026-06-10) ───────────────────


def test_process_tick_does_not_call_account_info_or_get_positions_synchronously(monkeypatch):
    """Tick must read from _live_state cache, not call bridge.account_info / get_positions.

    Audit 2026-06-10: those two sync calls ate 30s+ of Twisted reactor time per tick
    and blocked FastAPI's 40-thread pool. Tick is decision-only; reads come from cache.
    """
    bridge = _fake_bridge()
    strategy = MagicMock()
    strategy.on_bar.return_value = _fake_signal(direction=0)  # no signal
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
        # If _process_tick still calls these, test fails
        m.setattr("time.sleep", lambda s: None)
        live_service._process_tick(bridge, strategy, df_new, last_bar, "ctrader", tick=1, log=log_fn)

    bridge.account_info.assert_not_called()
    bridge.get_positions.assert_not_called()


def test_process_tick_calls_amend_after_market_buy(monkeypatch):
    """Long signal → market_buy fills → amend_position_sltp pushes SL/TP to server.

    The amend must include the sl/tp we computed from the signal's sl_atr/tp_atr.
    """
    bridge = _fake_bridge(position_id=777, order_id=99)
    strategy = MagicMock()
    strategy.on_bar.return_value = _fake_signal(direction=1, atr=7.0, sl_atr=2.0, tp_atr=3.0, price=4500.0)
    strategy.last_atr = 7.0

    live_service._live_state["account"] = {"ok": True, "balance": 10000.0, "equity": 10000.0}
    live_service._live_state["positions"] = []

    df_new = _make_df()
    log_fn = MagicMock()

    with monkeypatch.context() as m:
        m.setattr("os.getenv", lambda k, d="": "1" if k == "CTRADER_SEND_ORDERS" else d)
        m.setattr("time.sleep", lambda s: None)
        live_service._process_tick(bridge, strategy, df_new, df_new.iloc[-1], "ctrader", tick=1, log=log_fn)

    bridge.market_buy.assert_called_once()
    bridge.amend_position_sltp.assert_called_once()
    call_args = bridge.amend_position_sltp.call_args
    # position_id=777 (from market_buy result), sl=4500-14=4486, tp=4500+21=4521
    assert call_args.kwargs.get("position_id") == 777 or (call_args.args and call_args.args[0] == 777)
    assert abs(call_args.kwargs.get("sl", call_args.args[1] if len(call_args.args) > 1 else 0) - 4486.0) < 0.01
    assert abs(call_args.kwargs.get("tp", call_args.args[2] if len(call_args.args) > 2 else 0) - 4521.0) < 0.01


def test_process_tick_amend_failure_keeps_old_sltp(monkeypatch):
    """If amend returns success=False, we should not crash and the position's
    SL/TP should be re-attempted next tick (local tracking not updated)."""
    bridge = _fake_bridge(position_id=555, order_id=99)
    bridge.amend_position_sltp.return_value = MagicMock(success=False, position_id=555, comment="rejected")
    strategy = MagicMock()
    strategy.on_bar.return_value = _fake_signal(direction=1)
    strategy.last_atr = 7.0
    live_service._live_state["account"] = {"ok": True, "balance": 10000.0, "equity": 10000.0}
    live_service._live_state["positions"] = []
    log_fn = MagicMock()
    with monkeypatch.context() as m:
        m.setattr("os.getenv", lambda k, d="": "1" if k == "CTRADER_SEND_ORDERS" else d)
        m.setattr("time.sleep", lambda s: None)
        live_service._process_tick(bridge, strategy, _make_df(), _make_df().iloc[-1], "ctrader", tick=1, log=log_fn)
    # amend was attempted; no entry was tracked (no SL/TP known to be on server)
    assert 555 not in live_service._local_positions
    # The log must record failure
    assert any("AMEND" in str(c) or "FAILED" in str(c) for c in log_fn.call_args_list)


def test_process_tick_dry_run_does_not_call_amend(monkeypatch):
    """When CTRADER_SEND_ORDERS != 1, neither market_buy nor amend should fire."""
    bridge = _fake_bridge()
    strategy = MagicMock()
    strategy.on_bar.return_value = _fake_signal(direction=1)
    strategy.last_atr = 7.0
    live_service._live_state["account"] = {"ok": True, "balance": 10000.0, "equity": 10000.0}
    live_service._live_state["positions"] = []
    log_fn = MagicMock()
    with monkeypatch.context() as m:
        m.setattr("os.getenv", lambda k, d="": "0" if k == "CTRADER_SEND_ORDERS" else d)
        m.setattr("time.sleep", lambda s: None)
        live_service._process_tick(bridge, strategy, _make_df(), _make_df().iloc[-1], "ctrader", tick=1, log=log_fn)
    bridge.market_buy.assert_not_called()
    bridge.amend_position_sltp.assert_not_called()


def test_process_tick_calls_amend_after_market_sell(monkeypatch):
    """SHORT signal → market_sell fills → amend_position_sltp pushes SL/TP to server.

    Catches sign errors in the SHORT sl/tp formula (sl = price + dist, tp = price - dist).
    With price=4500, atr=7, sl_atr=2, tp_atr=3: sl=4500+14=4514, tp=4500-21=4479.
    """
    bridge = _fake_bridge(position_id=888, order_id=99)
    strategy = MagicMock()
    strategy.on_bar.return_value = _fake_signal(direction=-1, atr=7.0, sl_atr=2.0, tp_atr=3.0, price=4500.0)
    strategy.last_atr = 7.0
    live_service._live_state["account"] = {"ok": True, "balance": 10000.0, "equity": 10000.0}
    live_service._live_state["positions"] = []
    log_fn = MagicMock()
    with monkeypatch.context() as m:
        m.setattr("os.getenv", lambda k, d="": "1" if k == "CTRADER_SEND_ORDERS" else d)
        m.setattr("time.sleep", lambda s: None)
        live_service._process_tick(bridge, strategy, _make_df(), _make_df().iloc[-1], "ctrader", tick=1, log=log_fn)
    bridge.market_sell.assert_called_once()
    bridge.amend_position_sltp.assert_called_once()
    call_args = bridge.amend_position_sltp.call_args
    assert call_args.kwargs.get("position_id") == 888 or (call_args.args and call_args.args[0] == 888)
    assert abs(call_args.kwargs.get("sl", call_args.args[1] if len(call_args.args) > 1 else 0) - 4514.0) < 0.01
    assert abs(call_args.kwargs.get("tp", call_args.args[2] if len(call_args.args) > 2 else 0) - 4479.0) < 0.01


def test_process_tick_unwraps_positions_envelope(monkeypatch):
    """If _live_state['positions'] is the wrapped dict shape (from /api/live/positions
    endpoint), the shim must unwrap it to a list before iterating. Without the shim,
    `p.get("position_id")` on a non-dict raises AttributeError."""
    bridge = _fake_bridge()
    bridge.market_buy.return_value = MagicMock(success=True, order_id=99, position_id=0, comment="ok")
    # NO signal — this test verifies the unwrap happens during read (not order path).
    strategy = MagicMock()
    strategy.on_bar.return_value = _fake_signal(direction=0)  # no signal
    strategy.last_atr = 7.0
    live_service._live_state["account"] = {"ok": True, "balance": 10000.0, "equity": 10000.0}
    # Envelope shape: {"ok": True, "positions": [...]} — what the endpoint actually writes
    live_service._live_state["positions"] = {"ok": True, "positions": [{"position_id": 1, "type": "buy", "volume": 0.01}]}
    log_fn = MagicMock()
    with monkeypatch.context() as m:
        m.setattr("time.sleep", lambda s: None)
        # Should not raise — the shim unwraps the dict to a list
        live_service._process_tick(bridge, strategy, _make_df(), _make_df().iloc[-1], "ctrader", tick=1, log=log_fn)
    # Tick log should report 1 position (the one inside the envelope)
    tick_log_call = [c for c in log_fn.call_args_list if "positions=1" in str(c)]
    assert len(tick_log_call) == 1


def test_process_tick_amend_exception_does_not_crash(monkeypatch):
    """If amend_position_sltp raises (network blip, broker contract change),
    the tick must log and continue — not crash the live loop."""
    bridge = _fake_bridge(position_id=999, order_id=99)
    bridge.amend_position_sltp.side_effect = RuntimeError("network blip")
    strategy = MagicMock()
    strategy.on_bar.return_value = _fake_signal(direction=1)
    strategy.last_atr = 7.0
    live_service._live_state["account"] = {"ok": True, "balance": 10000.0, "equity": 10000.0}
    live_service._live_state["positions"] = []
    log_fn = MagicMock()
    with monkeypatch.context() as m:
        m.setattr("os.getenv", lambda k, d="": "1" if k == "CTRADER_SEND_ORDERS" else d)
        m.setattr("time.sleep", lambda s: None)
        # Must not raise
        live_service._process_tick(bridge, strategy, _make_df(), _make_df().iloc[-1], "ctrader", tick=1, log=log_fn)
    # 999 not in _local_positions (amend raised before _track_local_sl_tp)
    assert 999 not in live_service._local_positions
    # Log captured the exception
    assert any("amend exception" in str(c) or "network blip" in str(c) for c in log_fn.call_args_list)


def test_process_tick_amend_falls_back_to_cached_position_id(monkeypatch):
    """When market_buy returns position_id=0 (broker didn't echo back), the tick
    must fall back to the latest cached position from _live_state and amend that.

    Catches regression where the fallback picks the wrong position or skips the amend.
    """
    bridge = _fake_bridge(position_id=0, order_id=99)  # market_buy returns position_id=0
    strategy = MagicMock()
    strategy.on_bar.return_value = _fake_signal(direction=1)
    strategy.last_atr = 7.0
    live_service._live_state["account"] = {"ok": True, "balance": 10000.0, "equity": 10000.0}
    # Pre-populate cache with a known position
    live_service._live_state["positions"] = [
        {"position_id": 888, "type": "buy", "volume": 0.01, "price_open": 4500.0}
    ]
    log_fn = MagicMock()
    with monkeypatch.context() as m:
        m.setattr("os.getenv", lambda k, d="": "1" if k == "CTRADER_SEND_ORDERS" else d)
        m.setattr("time.sleep", lambda s: None)
        live_service._process_tick(bridge, strategy, _make_df(), _make_df().iloc[-1], "ctrader", tick=1, log=log_fn)
    bridge.market_buy.assert_called_once()
    bridge.amend_position_sltp.assert_called_once()
    call_args = bridge.amend_position_sltp.call_args
    amend_pid = call_args.kwargs.get("position_id") or (call_args.args[0] if call_args.args else None)
    assert amend_pid == 888, f"expected fallback to cached pos 888, got {amend_pid}"
    # And the local tracker should now have 888
    assert 888 in live_service._local_positions
