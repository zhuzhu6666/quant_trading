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


def test_recent_review_reentry_block_uses_consecutive_conflicting_losses(monkeypatch):
    now = 10_000.0
    rows = [
        {
            "review_id": "r2", "position_id": "p2", "outcome_label": "bad_loss",
            "failure_tags_json": '["factor_conflict", "thesis_broken"]',
            "review_json": '{"direction": -1}', "created_at": now - 60,
        },
        {
            "review_id": "r1", "position_id": "p1", "outcome_label": "bad_loss",
            "failure_tags_json": '["regime_mismatch"]',
            "review_json": '{"direction": -1}', "created_at": now - 600,
        },
    ]
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = rows
    monkeypatch.setattr("backend.core.db.get_state_pg_conn", lambda **_kwargs: conn)

    block = live_service._recent_review_reentry_block(
        symbol="XAUUSD+", direction=-1, now_ts=now,
    )

    assert block is not None
    assert block["reason"] == "repeated_conflicting_thesis_loss"
    assert block["review_ids"] == ["r2", "r1"]
    assert block["remaining_seconds"] == 3540.0


def test_recent_review_reentry_block_requires_consecutive_failures(monkeypatch):
    rows = [
        {
            "review_id": "win", "position_id": "p2", "outcome_label": "good_win",
            "failure_tags_json": "[]", "review_json": '{"direction": -1}',
            "created_at": 9_900.0,
        },
        {
            "review_id": "loss", "position_id": "p1", "outcome_label": "bad_loss",
            "failure_tags_json": '["factor_conflict"]',
            "review_json": '{"direction": -1}', "created_at": 9_000.0,
        },
    ]
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = rows
    monkeypatch.setattr("backend.core.db.get_state_pg_conn", lambda **_kwargs: conn)

    assert live_service._recent_review_reentry_block(
        symbol="XAUUSD+", direction=-1, now_ts=10_000.0,
    ) is None


@pytest.fixture(autouse=True)
def _reset_state():
    live_service._live_state["account"] = None
    live_service._live_state["positions"] = []
    live_service._live_state["account_updated_at"] = None
    live_service._live_state["positions_updated_at"] = None
    live_service._refresh_thread = None
    yield
    live_service._live_state["account"] = None
    live_service._live_state["positions"] = []
    live_service._live_state["account_updated_at"] = None
    live_service._live_state["positions_updated_at"] = None
    live_service._refresh_thread = None


def _fake_bridge(balance=10000.0, equity=10050.0, currency="USD"):
    b = MagicMock()
    b.account_info.return_value = {
        "balance": balance, "equity": equity, "currency": currency,
        "margin": 0.0, "margin_free": 0.0, "leverage": 100,
    }
    b.refresh_account_info.return_value = b.account_info.return_value
    b.get_positions.return_value = [
        {"position_id": 42, "symbol_id": 1, "type": "buy", "volume": 0.01,
         "price_open": 4500.0, "sl": 0.0, "tp": 0.0, "profit": 50.0,
         "swap": 0.0, "commission": 0.0}
    ]
    b.refresh_positions.return_value = b.get_positions.return_value
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
    # audit 2026-06-10: timestamps must be set so the WS snapshot knows the data is fresh
    assert live_service._live_state["account_updated_at"] is not None
    assert live_service._live_state["positions_updated_at"] is not None
    # timestamps should be very recent (within 5s of now)
    assert abs(time.time() - live_service._live_state["account_updated_at"]) < 5
    assert abs(time.time() - live_service._live_state["positions_updated_at"]) < 5
    pos = live_service._live_state["positions"]
    # positions stored as the wrapped endpoint format OR unwrapped list — accept either
    if isinstance(pos, dict):
        pos = pos.get("positions", [])
    cached = next(p for p in pos if p.get("position_id") == 42)
    assert cached["mfe"] == pytest.approx(50.0)
    assert cached["profit_capture_ratio"] == pytest.approx(1.0)
    assert cached["thesis_status"] in {"intact", "weakening"}


def test_refresh_account_positions_fills_single_position_pnl_from_account_equity():
    bridge = _fake_bridge(balance=503.24, equity=501.81)
    bridge.get_positions.return_value = [
        {"position_id": 88, "symbol_id": 1, "symbol": "XAUUSD", "type": "sell", "volume": 100.0,
         "price_open": 3968.85, "price_current": 3970.22, "sl": 3986.08, "tp": 3943.01,
         "profit": 0.0, "swap": 0.0, "commission": 0.0}
    ]
    bridge.refresh_positions.return_value = bridge.get_positions.return_value

    live_service._refresh_account_positions_sync(bridge, "ctrader")

    pos = live_service._live_state["positions"]
    cached = next(p for p in pos if p.get("position_id") == 88)
    assert cached["pnl"] == pytest.approx(-1.43)
    assert cached["profit"] == pytest.approx(-1.43)
    assert cached["unrealized_pnl"] == pytest.approx(-1.43)
    assert cached["netUnrealizedPnL"] == pytest.approx(-1.43)
    assert cached["pnl_source"] == "account_equity"


def test_refresh_account_positions_swallows_bridge_errors():
    """If bridge.account_info raises, we should NOT crash — just log and leave cache.
    Same pattern as the original tick code: best-effort write, never raise."""
    bridge = MagicMock()
    bridge.account_info.side_effect = RuntimeError("network blip")
    bridge.refresh_account_info.side_effect = RuntimeError("network blip")
    bridge.get_positions.side_effect = RuntimeError("network blip")
    bridge.refresh_positions.side_effect = RuntimeError("network blip")
    # Should NOT raise
    live_service._refresh_account_positions_sync(bridge, "ctrader")
    # Cache stays at whatever it was (None from fixture)
    assert live_service._live_state["account"] is None


def test_refresh_account_positions_skips_reconcile_when_positions_recent():
    class _Bridge:
        is_connected = True

        def __init__(self):
            self.account_calls = 0
            self.position_calls = 0

        def refresh_account_info(self):
            self.account_calls += 1
            return {
                "balance": 10000.0,
                "equity": 10000.0,
                "currency": "USD",
                "margin": 0.0,
                "margin_free": 0.0,
                "leverage": 100,
            }

        def refresh_positions(self, *, force=False, allow_cache_fallback=True):
            self.position_calls += 1
            return [{"position_id": 43, "symbol_id": 1, "type": "buy", "volume": 100.0}]

    bridge = _Bridge()
    live_service._live_state["account_updated_at"] = time.time() - 60.0
    live_service._live_state["positions_updated_at"] = time.time()
    live_service._live_state["positions"] = [{"position_id": 42, "symbol_id": 1, "type": "buy", "volume": 100.0}]

    live_service._refresh_account_positions_sync(bridge, "ctrader")

    assert bridge.account_calls == 1
    assert bridge.position_calls == 0
    assert live_service._live_state["positions"][0]["position_id"] == 42


def test_refresh_account_positions_skips_position_write_when_reconcile_not_fresh():
    class _Bridge:
        is_connected = True
        _last_reconcile_at = 10.0

        def refresh_account_info(self):
            raise AssertionError("account refresh should be skipped")

        def refresh_positions(self, *, force=False, allow_cache_fallback=True):
            return []

    live_service._live_state["account"] = {"balance": 10000.0, "equity": 10000.0}
    live_service._live_state["account_updated_at"] = time.time()
    live_service._live_state["positions"] = [{"position_id": 42, "symbol_id": 1, "type": "buy", "volume": 100.0}]
    live_service._live_state["positions_updated_at"] = time.time() - 300.0

    live_service._refresh_account_positions_sync(_Bridge(), "ctrader")

    assert live_service._live_state["positions"][0]["position_id"] == 42


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


# ── Regression: kickoff fires even when fetch_bars returns None ──────────
# audit 2026-06-10: cTrader broker demo doesn't return history bars.
# Previous code put kickoff_account_refresh inside the `else` branch
# (after fetch_bars succeeded), so it never ran in production. Fix: move
# kickoff above fetch_bars in _run_loop. This test reads the source file
# and asserts the call ordering — if anyone moves kickoff back inside the
# else, this test fails.
def test_kickoff_runs_even_when_fetch_bars_returns_none():
    """Regression: in the live tick body cTrader branch, kickoff_account_refresh
    must be called BEFORE _fetch_bars_with_retry. Otherwise the cTrader
    demo (which returns 0 history bars) will skip the kickoff forever.
    """
    from pathlib import Path as _Path
    src_path = str(_Path(__file__).resolve().parent.parent / "backend" / "services" / "live_service.py")
    src = open(src_path, encoding="utf-8").read()
    lines = src.splitlines()
    # Locate the extracted tick body used by _run_loop's main while loop.
    main_loop_idx = next(i for i, ln in enumerate(lines) if "def _run_live_loop_tick_body" in ln)
    helper_end = next(
        i for i, ln in enumerate(lines[main_loop_idx + 1:], start=main_loop_idx + 1)
        if ln.startswith("def _update_live_loop_risk_metrics")
    )
    branch_text = "\n".join(lines[main_loop_idx:helper_end])
    kickoff_pos = branch_text.find("kickoff_account_refresh")
    # cTrader reads from local DataStore now
    warmup_pos = branch_text.find("_warmup_from_local_db")
    assert kickoff_pos > 0, "kickoff_account_refresh not found in cTrader main-loop block"
    assert warmup_pos > 0, "_warmup_from_local_db not found in cTrader main-loop block"
    assert kickoff_pos < warmup_pos, (
        "REGRESSION: kickoff_account_refresh is AFTER _warmup_from_local_db in "
        "the cTrader live tick body. It must be BEFORE so the cache writer still "
        "runs when warmup returns None."
    )
