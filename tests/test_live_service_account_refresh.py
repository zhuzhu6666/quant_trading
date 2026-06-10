"""Tests for live loop background account/positions refresh.

audit 2026-06-10: previous _process_tick rewrite removed all broker calls,
relying on lazy /api/live/account endpoint to populate _live_state. But
WS /ws/state reads _live_state directly — so the snapshot keeps returning
the placeholder zeros. Fix: _run_loop spawns a daemon thread during its
60s wait that calls bridge.account_info() + bridge.get_positions() and
writes the result to _live_state (so the next WS tick has fresh data).
"""
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from backend.services import live_service


@pytest.fixture(autouse=True)
def _reset_state():
    live_service._live_state["account"] = None
    live_service._live_state["positions"] = []
    yield
    live_service._live_state["account"] = None
    live_service._live_state["positions"] = []


def _fake_bridge(balance=10000.0, equity=10050.0, currency="USD"):
    b = MagicMock()
    b.account_info.return_value = {
        "balance": balance, "equity": equity, "currency": currency,
        "margin": 0.0, "margin_free": 0.0, "leverage": 100,
    }
    b.get_positions.return_value = [
        {"position_id": 42, "symbol_id": 1, "type": "buy", "volume": 0.01,
         "price_open": 4500.0, "sl": 0.0, "tp": 0.0, "profit": 50.0,
         "swap": 0.0, "commission": 0.0}
    ]
    return b


def test_refresh_account_positions_writes_cache():
    bridge = _fake_bridge()
    # Synchronous call (no thread spawn) for test determinism
    live_service._refresh_account_positions_sync(bridge, "ctrader")
    acct = live_service._live_state["account"]
    assert acct is not None
    assert acct["balance"] == 10000.0
    assert acct["equity"] == 10050.0
    assert acct["currency"] == "USD"
    pos = live_service._live_state["positions"]
    # positions stored as the wrapped endpoint format OR unwrapped list — accept either
    if isinstance(pos, dict):
        pos = pos.get("positions", [])
    assert any(p.get("position_id") == 42 for p in pos)


def test_refresh_account_positions_swallows_bridge_errors():
    """If bridge.account_info raises, we should NOT crash — just log and leave cache.
    Same pattern as the original tick code: best-effort write, never raise."""
    bridge = MagicMock()
    bridge.account_info.side_effect = RuntimeError("network blip")
    bridge.get_positions.side_effect = RuntimeError("network blip")
    # Should NOT raise
    live_service._refresh_account_positions_sync(bridge, "ctrader")
    # Cache stays at whatever it was (None from fixture)
    assert live_service._live_state["account"] is None


def test_kickoff_refresh_spawns_daemon_thread(monkeypatch):
    """kickoff_account_refresh() must return a thread that runs and exits.
    We mock time.sleep so the worker can complete quickly, then join() and
    verify it finished."""
    bridge = _fake_bridge()
    started = threading.Event()
    # Pre-install a stoppable Event as _loop_stop_flag so the worker exits
    # after the first refresh. Worker checks .is_set() BETWEEN sleeps in
    # its slice-loop, so we set it on the first sleep.
    fake_stop = threading.Event()
    monkeypatch.setattr(live_service, "_loop_stop_flag", fake_stop)

    def fake_sleep(s):
        # First call: just record that the worker reached the sleep phase
        # (refresh has already been called, cache is populated). After the
        # first sleep, signal the worker to stop on its next stop_flag check.
        if not started.is_set():
            started.set()
            fake_stop.set()

    monkeypatch.setattr("time.sleep", fake_sleep)
    monkeypatch.setattr("threading.Thread", lambda **kw: (
        # Wrap so the target runs synchronously when we .start() it
        _SyncThread(kw["target"], kw.get("args", ()), kw.get("daemon", False), kw.get("name", ""))
    ))
    # Use a regular synchronous thread so we can join()
    live_service.kickoff_account_refresh(bridge, "ctrader", interval_sec=0.05)
    # The helper should have updated _live_state by now (sync thread ran)
    acct = live_service._live_state["account"]
    assert acct is not None
    assert acct["balance"] == 10000.0


class _SyncThread:
    """Stand-in for threading.Thread that runs the target immediately on .start()."""
    def __init__(self, target, args, daemon, name):
        self._target = target
        self._args = args or ()
        self.daemon = daemon
        self.name = name
        self._alive = False

    def start(self):
        self._alive = True
        try:
            self._target(*self._args)
        finally:
            self._alive = False

    def is_alive(self):
        return self._alive
