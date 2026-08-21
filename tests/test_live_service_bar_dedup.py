"""Same-bar open dedup: a bar that already filled must not open twice.

Covers the duplicate-open incidents of 2026-08-10 (stale decision bar repair
replays + pending open retry re-entry opening a second position for the same
signal).  Distinct bars may still open concurrently; this only blocks the
same bar.
"""
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.services import live_service
from backend.services.live_safety_state import reset_safety_state_for_tests
from config import runtime_config as rc


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch, tmp_path):
    monkeypatch.setenv("QUANT_SAFETY_STATE_DIR", str(tmp_path / "safety"))
    reset_safety_state_for_tests()
    rc.reset_for_tests()
    monkeypatch.setattr(
        live_service._LIVE_LOOP_CONTROLLER,
        "accepting_new_risk",
        lambda _generation_id: True,
    )
    live_service._local_positions.clear()
    live_service._live_state["account"] = None
    live_service._live_state["positions"] = []
    live_service._live_state["market_session"] = None
    live_service._live_state["spot_quote"] = None
    live_service._live_state["last_processed_decision_bar_ts"] = 0.0
    live_service._process_shutdown_requested = False
    live_service._live_state_update(
        loop_running=False,
        accepting_new_risk=False,
        session_state_status="unknown",
    )
    yield
    reset_safety_state_for_tests()
    live_service._local_positions.clear()
    live_service._process_shutdown_requested = False
    live_service._live_state_update(
        loop_running=False,
        accepting_new_risk=False,
        session_state_status="unknown",
    )


def _fake_bridge(position_id=12345, order_id=99):
    bridge = MagicMock()
    result = MagicMock()
    result.success = True
    result.order_id = order_id
    result.position_id = position_id
    result.comment = "ok"
    bridge.market_buy.return_value = result
    bridge.market_sell.return_value = result
    return bridge


def _open_pipeline_kwargs(bridge, logs, bar_ts=None):
    return {
        "bridge": bridge,
        "pipeline": {},
        "broker": "ctrader",
        "cfg": SimpleNamespace(),
        "bar": {"time": bar_ts if bar_ts is not None else time.time()},
        "factor_values": {},
        "composite": SimpleNamespace(direction=1, score=0.8),
        "gate_result": SimpleNamespace(passed=True, reason="pass"),
        "account": {"balance": 10000.0, "equity": 10000.0},
        "positions": [],
        "attr_engine": None,
        "current_price": 4000.0,
        "atr_price": 4.0,
        "pending_open_attach_ids": [],
        "send": True,
        "tick": 8,
        "log": logs.append,
    }


def test_open_pipeline_blocks_same_bar_second_open(monkeypatch):
    """Same bar already filled -> blocked before candidate, never re-opens."""
    bridge = _fake_bridge()
    logs = []
    bar_ts = time.time()
    monkeypatch.setattr(
        live_service, "_bar_open_already_recorded", lambda ts: ts == bar_ts
    )
    prepare = MagicMock()
    monkeypatch.setattr(live_service, "_prepare_open_trade_candidate", prepare)

    gate = live_service._run_open_trade_pipeline(
        **_open_pipeline_kwargs(bridge, logs, bar_ts=bar_ts)
    )

    assert gate.passed is False
    assert gate.reason == "bar_already_opened"
    assert getattr(gate, "retryable_watchdog_freshness", False) is False
    assert any("bar_already_opened" in item for item in logs)
    prepare.assert_not_called()
    bridge.market_buy.assert_not_called()
    bridge.market_sell.assert_not_called()


def test_open_pipeline_allows_distinct_bar_without_open_record(monkeypatch):
    """A bar with no prior canonical open proceeds to candidate prep."""
    bridge = _fake_bridge()
    logs = []
    prepare = MagicMock(
        return_value=SimpleNamespace(order_block={"order_blocked": False})
    )
    monkeypatch.setattr(live_service, "_bar_open_already_recorded", lambda ts: False)
    monkeypatch.setattr(live_service, "_prepare_open_trade_candidate", prepare)
    monkeypatch.setattr(
        live_service,
        "_submit_open_trade_candidate",
        lambda **_kwargs: False,
    )

    gate = live_service._run_open_trade_pipeline(
        **_open_pipeline_kwargs(bridge, logs)
    )

    # reached candidate prep: the dedup guard did not block a fresh bar
    prepare.assert_called_once()
    assert not any("bar_already_opened" in item for item in logs)
    bridge.market_buy.assert_not_called()


def test_bar_open_already_recorded_queries_canonical_decision_stream(monkeypatch):
    """The guard queries canonical decisions for an open on the same bar ts."""
    def _fake_scan(conn, **kwargs):
        class _Row(dict):
            pass

        yield _Row(event_type="skip")
        yield _Row(event_type="open")

    monkeypatch.setattr(live_service, "iter_decision_rows", _fake_scan)
    monkeypatch.setattr(live_service, "logger", MagicMock())

    assert live_service._bar_open_already_recorded(1786383900.0) is True


def test_bar_open_already_recorded_canonical_window(monkeypatch):
    """Canonical branch: an open decision within the +-5s window dedupes the bar."""
    def _fake_scan(conn, **kwargs):
        class _Row(dict):
            pass

        yield _Row(event_type="skip")
        yield _Row(event_type="open")

    monkeypatch.setattr(live_service, "canonical_ready", lambda conn: True)
    monkeypatch.setattr(live_service, "iter_decision_rows", _fake_scan)
    monkeypatch.setattr(live_service, "logger", MagicMock())

    assert live_service._bar_open_already_recorded(1786383900.0) is True


def test_bar_open_already_recorded_canonical_no_open(monkeypatch):
    """Canonical branch: no open decision in the window -> allow the bar."""
    def _fake_scan(conn, **kwargs):
        return iter([])

    monkeypatch.setattr(live_service, "canonical_ready", lambda conn: True)
    monkeypatch.setattr(live_service, "iter_decision_rows", _fake_scan)
    monkeypatch.setattr(live_service, "logger", MagicMock())

    assert live_service._bar_open_already_recorded(1786383900.0) is False


def test_bar_open_already_recorded_fail_open_on_error(monkeypatch):
    """Canonical scan unreadable -> fail-open (dedup guard is not a risk gate)."""
    def _boom(conn, **kwargs):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(live_service, "iter_decision_rows", _boom)
    monkeypatch.setattr(live_service, "logger", MagicMock())

    assert live_service._bar_open_already_recorded(1786383900.0) is False
    assert live_service._bar_open_already_recorded(0.0) is False
    assert live_service._bar_open_already_recorded(None) is False


def test_bar_open_already_recorded_fail_open_on_canonical_error(monkeypatch):
    """Canonical window unreadable -> fail-open because dedup is not a risk gate."""
    def _boom_scan(conn, **kwargs):
        raise RuntimeError("canonical down")

    monkeypatch.setattr(live_service, "canonical_ready", lambda conn: True)
    monkeypatch.setattr(live_service, "iter_decision_rows", _boom_scan)
    monkeypatch.setattr(live_service, "logger", MagicMock())

    assert live_service._bar_open_already_recorded(1786383900.0) is False
