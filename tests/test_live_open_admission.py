from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from backend.services import live_service
from backend.services.live_open_admission import (
    evaluate_final_open_admission,
    probe_postgres_authority,
)
from backend.services.live_safety_state import (
    activate_no_new_risk_latch,
    no_new_risk_latch_status,
    reset_safety_state_for_tests,
)


@pytest.fixture(autouse=True)
def _isolated_open_admission_state(monkeypatch, tmp_path):
    reset_safety_state_for_tests()
    monkeypatch.setenv("QUANT_SAFETY_STATE_DIR", str(tmp_path / "safety"))
    monkeypatch.setattr(live_service, "_generation_controller_enabled", lambda: False)
    monkeypatch.setattr(live_service, "_process_shutdown_requested", False)
    monkeypatch.setattr(
        live_service,
        "_live_safety_watchdog_probe",
        lambda: {"unknown_execution_count": 0},
    )
    live_service._live_state_update(
        loop_running=False,
        accepting_new_risk=True,
        session_state_status="available",
        circuit_breaker=False,
    )
    yield
    reset_safety_state_for_tests()


def _fresh_session(now_ts: float) -> dict:
    return {
        "status": "open_confirmed",
        "can_open_positions": True,
        "broker_connected": True,
        "now_ts": now_ts,
    }


def _fresh_quote(now_ts: float) -> dict:
    return {
        "bid": 3999.9,
        "ask": 4000.1,
        "mid": 4000.0,
        "ts": now_ts,
    }


def test_watchdog_stale_facts_are_the_only_retryable_latch():
    activate_no_new_risk_latch(
        reason="safety_freshness_failed",
        actor="system:safety_watchdog",
        metadata={"blockers": ["account_freshness_stale"]},
        cause="safety_freshness",
        cause_id="safety_watchdog",
    )

    assert live_service._watchdog_freshness_retry_eligible(
        ("no_new_risk_latched", "accepting_new_risk_false")
    )


@pytest.mark.parametrize(
    "safety_blocker",
    ["unresolved_execution_intent", "unknown_execution_status_unavailable"],
)
def test_watchdog_execution_uncertainty_is_never_retryable(safety_blocker):
    activate_no_new_risk_latch(
        reason="safety_freshness_failed",
        actor="system:safety_watchdog",
        metadata={"blockers": [safety_blocker]},
        cause="safety_freshness",
        cause_id="safety_watchdog",
    )

    assert not live_service._watchdog_freshness_retry_eligible(
        ("no_new_risk_latched", "accepting_new_risk_false")
    )


def test_current_unknown_execution_projection_blocks_retry(monkeypatch):
    activate_no_new_risk_latch(
        reason="safety_freshness_failed",
        actor="system:safety_watchdog",
        metadata={"blockers": ["positions_freshness_stale"]},
        cause="safety_freshness",
        cause_id="safety_watchdog",
    )
    monkeypatch.setattr(
        live_service,
        "_live_safety_watchdog_probe",
        lambda: {"unknown_execution_count": 1},
    )

    assert not live_service._watchdog_freshness_retry_eligible(
        ("no_new_risk_latched", "accepting_new_risk_false")
    )


def test_watchdog_freshness_is_not_retryable_with_an_incident_cause():
    activate_no_new_risk_latch(
        reason="safety_freshness_failed",
        actor="system:safety_watchdog",
        metadata={"blockers": ["positions_freshness_stale"]},
        cause="safety_freshness",
        cause_id="safety_watchdog",
    )
    activate_no_new_risk_latch(
        reason="operator incident",
        actor="operator:test",
        cause="incident_control",
        cause_id="runtime_incident_mode",
    )

    assert not live_service._watchdog_freshness_retry_eligible(
        ("no_new_risk_latched", "accepting_new_risk_false")
    )


def test_pending_open_retry_reuses_same_bar_and_original_gate(monkeypatch):
    signal_gate = SimpleNamespace(passed=True, reason="passed")
    composite = SimpleNamespace(direction=-1)
    pipeline = {
        "pending_open_retry": {
            "bar": {
                "time": 1_767_225_600.0,
                "timeframe": "M5",
                "close": 4_000.0,
            },
            "factor_values": {"atr_ratio": 0.001},
            "composite": composite,
            "gate_result": signal_gate,
        }
    }
    frame = pd.DataFrame(
        {"open": [4_001.0], "high": [4_002.0], "low": [3_999.0], "close": [4_000.0]},
        index=pd.DatetimeIndex([pd.Timestamp("2026-01-01T00:00:00Z")]),
    )
    calls = []
    monkeypatch.setattr(live_service, "_factor_pipeline", pipeline)
    monkeypatch.setattr(live_service, "_should_send_orders", lambda _broker: True)
    monkeypatch.setattr(
        live_service,
        "_run_open_trade_pipeline",
        lambda **kwargs: calls.append(kwargs)
        or SimpleNamespace(passed=True, reason="passed"),
    )
    live_service._live_state_update(
        account={"balance": 10_000.0},
        positions=[],
    )

    live_service._retry_pending_open_trade(
        bridge=SimpleNamespace(),
        frame=frame,
        last_bar=frame.iloc[-1],
        broker="ctrader",
        tick=41,
        log=lambda _message: None,
        stop_requested=lambda: False,
    )

    assert len(calls) == 1
    assert calls[0]["gate_result"] is signal_gate
    assert calls[0]["composite"] is composite
    assert "pending_open_retry" not in pipeline


def test_pending_open_retry_is_discarded_when_closed_bar_advances(monkeypatch):
    pipeline = {
        "pending_open_retry": {
            "bar": {"time": 1.0, "timeframe": "M5"},
        }
    }
    frame = pd.DataFrame(
        {"open": [4_001.0], "high": [4_002.0], "low": [3_999.0], "close": [4_000.0]},
        index=pd.DatetimeIndex([pd.Timestamp("2026-01-01T00:05:00Z")]),
    )
    monkeypatch.setattr(live_service, "_factor_pipeline", pipeline)

    live_service._retry_pending_open_trade(
        bridge=SimpleNamespace(),
        frame=frame,
        last_bar=frame.iloc[-1],
        broker="ctrader",
        tick=42,
        log=lambda _message: None,
        stop_requested=lambda: False,
    )

    assert "pending_open_retry" not in pipeline


def test_final_open_admission_requires_fresh_pg_session_and_spot_facts():
    now_ts = 1000.0

    result = evaluate_final_open_admission(
        postgres={"ok": True, "observed_at": now_ts},
        market_session=_fresh_session(now_ts),
        spot_quote=_fresh_quote(now_ts),
        now_ts=now_ts,
    )

    assert result.ok is True
    assert result.blockers == ()


@pytest.mark.parametrize(
    ("quote", "expected"),
    [
        ({"bid": 4000.0, "ask": 0.0, "mid": 4000.0, "ts": 1000.0}, "spot_quote_bid_ask_invalid"),
        ({"bid": 4000.0, "ask": 4000.0, "mid": 4000.0, "ts": 1000.0}, "spot_quote_spread_invalid"),
    ],
)
def test_final_open_admission_requires_trainable_quote_inputs(quote, expected):
    result = evaluate_final_open_admission(
        postgres={"ok": True, "observed_at": 1000.0},
        market_session=_fresh_session(1000.0),
        spot_quote=quote,
        now_ts=1000.0,
    )

    assert result.ok is False
    assert expected in result.blockers


@pytest.mark.parametrize(
    ("postgres", "session", "quote", "expected"),
    [
        (
            {"ok": False, "observed_at": 1000.0},
            _fresh_session(1000.0),
            _fresh_quote(1000.0),
            "state_pg_unavailable",
        ),
        (
            {"ok": True, "observed_at": 1000.0},
            _fresh_session(980.0),
            _fresh_quote(1000.0),
            "market_session_stale",
        ),
        (
            {"ok": True, "observed_at": 1000.0},
            _fresh_session(1000.0),
            _fresh_quote(980.0),
            "spot_quote_stale",
        ),
    ],
)
def test_final_open_admission_fails_closed_for_each_authority(
    postgres,
    session,
    quote,
    expected,
):
    result = evaluate_final_open_admission(
        postgres=postgres,
        market_session=session,
        spot_quote=quote,
        now_ts=1000.0,
    )

    assert result.ok is False
    assert expected in result.blockers


def test_postgres_authority_probe_is_read_only_and_closes_connection():
    calls: list[str] = []

    class _Cursor:
        def fetchone(self):
            return (1,)

    class _Conn:
        def execute(self, sql):
            calls.append(sql)
            return _Cursor()

        def close(self):
            calls.append("close")

    result = probe_postgres_authority(lambda: _Conn(), now=lambda: 1000.0)

    assert result["ok"] is True
    assert calls == ["SELECT 1", "close"]


def test_final_open_postgres_connection_has_bounded_connect_and_statement_timeouts(
    monkeypatch,
):
    from backend.core import db as db_module
    from backend.core import state_store
    from psycopg import conninfo

    captured: dict = {}
    connection = object()
    monkeypatch.setattr(db_module, "state_pg_enabled", lambda: True)
    monkeypatch.setattr(db_module, "state_pg_dsn", lambda: "dbname=state")

    def _make_conninfo(dsn, **kwargs):
        captured["dsn"] = dsn
        captured["conninfo_kwargs"] = kwargs
        return "bounded-dsn"

    def _connect(dsn, **kwargs):
        captured["connect_dsn"] = dsn
        captured["connect_kwargs"] = kwargs
        return connection

    monkeypatch.setattr(conninfo, "make_conninfo", _make_conninfo)
    monkeypatch.setattr(state_store, "connect_state_store", _connect)

    result = live_service._get_final_open_probe_conn()

    assert result is connection
    assert captured["dsn"] == "dbname=state"
    assert captured["conninfo_kwargs"] == {
        "connect_timeout": 2,
        "options": "-c statement_timeout=2000 -c lock_timeout=1000",
    }
    assert captured["connect_dsn"] == "bounded-dsn"
    assert captured["connect_kwargs"] == {"read_only": True}


def test_runtime_postgres_failure_latches_no_new_risk_and_skips_open_rpc(monkeypatch):
    now_ts = time.time()
    order = MagicMock()
    logs: list[str] = []
    monkeypatch.setattr(
        live_service,
        "_get_final_open_probe_conn",
        lambda: (_ for _ in ()).throw(RuntimeError("postgres offline")),
    )
    monkeypatch.setattr(live_service, "_submit_open_trade_order", order)

    submitted = live_service._submit_open_trade_candidate(
        bridge=SimpleNamespace(get_spot_quote=lambda: _fresh_quote(now_ts)),
        attr_engine=None,
        broker="ctrader",
        cfg=SimpleNamespace(),
        bar={"time": now_ts},
        tick=21,
        account={},
        positions=[],
        composite=SimpleNamespace(direction=1),
        gate_result=SimpleNamespace(),
        candidate=SimpleNamespace(
            direction_name="LONG",
            volume=100.0,
            nursery_reservation_id="",
            market_session=_fresh_session(now_ts),
        ),
        current_price=4000.0,
        log=logs.append,
    )

    assert submitted is False
    order.assert_not_called()
    assert no_new_risk_latch_status()["active"] is True
    admission = live_service._live_state_get("final_open_admission")
    assert "state_pg_unavailable" in admission["blockers"]
    assert live_service._live_state_get("accepting_new_risk") is False
    assert any("final_open_admission" in item for item in logs)


def test_postgres_probe_never_holds_broker_admission_lock(monkeypatch):
    probe_entered = threading.Event()
    release_probe = threading.Event()
    finished = threading.Event()

    def _probe(**_kwargs):
        probe_entered.set()
        assert release_probe.wait(timeout=2.0)
        return {
            "ok": False,
            "blockers": ("state_pg_unavailable",),
            "postgres": {"error": "postgres timeout"},
            "spot_quote": {},
        }

    monkeypatch.setattr(live_service, "_probe_final_open_admission", _probe)
    monkeypatch.setattr(live_service, "_submit_open_trade_order", MagicMock())

    def _submit():
        try:
            live_service._submit_open_trade_candidate(
                bridge=SimpleNamespace(),
                attr_engine=None,
                broker="ctrader",
                cfg=SimpleNamespace(),
                bar={},
                tick=22,
                account={},
                positions=[],
                composite=SimpleNamespace(direction=1),
                gate_result=SimpleNamespace(),
                candidate=SimpleNamespace(
                    direction_name="LONG",
                    volume=100.0,
                    nursery_reservation_id="",
                ),
                current_price=4000.0,
                log=lambda _message: None,
            )
        finally:
            finished.set()

    thread = threading.Thread(target=_submit)
    thread.start()
    assert probe_entered.wait(timeout=1.0)
    assert live_service._OPEN_TRADE_ADMISSION_LOCK.acquire(timeout=0.2) is True
    live_service._OPEN_TRADE_ADMISSION_LOCK.release()

    release_probe.set()
    thread.join(timeout=2.0)
    assert finished.is_set() is True
