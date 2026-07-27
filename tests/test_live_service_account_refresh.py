"""Tests for live loop background account/positions refresh.

The legacy loop keeps a compatibility refresh worker while the Phase2 safety
plane flag is off. It must publish only explicit fresh broker reconciliations
using the broker's observed_at; HTTP or worker fetch time is never a fact.
"""
import threading
import time
from types import SimpleNamespace
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
    live_service._live_state["account_reconciled"] = None
    live_service._live_state["account_reconcile_id"] = None
    live_service._live_state["account_event"] = None
    live_service._live_state["account_event_updated_at"] = None
    live_service._live_state["account_event_reason"] = None
    live_service._live_state["positions"] = []
    live_service._live_state["positions_reconciled"] = []
    live_service._live_state["positions_reconcile_id"] = None
    live_service._live_state["positions_component_facts"] = {}
    live_service._live_state["positions_event"] = []
    live_service._live_state["positions_event_updated_at"] = None
    live_service._live_state["positions_event_reason"] = None
    live_service._live_state["account_updated_at"] = None
    live_service._live_state["positions_updated_at"] = None
    live_service._refresh_thread = None
    live_service._ACCOUNT_CACHE.clear()
    live_service._POSITIONS_CACHE.clear()
    live_service._probe_ctrader_cache = None
    yield
    live_service._live_state["account"] = None
    live_service._live_state["account_reconciled"] = None
    live_service._live_state["account_reconcile_id"] = None
    live_service._live_state["account_event"] = None
    live_service._live_state["account_event_updated_at"] = None
    live_service._live_state["account_event_reason"] = None
    live_service._live_state["positions"] = []
    live_service._live_state["positions_reconciled"] = []
    live_service._live_state["positions_reconcile_id"] = None
    live_service._live_state["positions_component_facts"] = {}
    live_service._live_state["positions_event"] = []
    live_service._live_state["positions_event_updated_at"] = None
    live_service._live_state["positions_event_reason"] = None
    live_service._live_state["account_updated_at"] = None
    live_service._live_state["positions_updated_at"] = None
    live_service._refresh_thread = None
    live_service._ACCOUNT_CACHE.clear()
    live_service._POSITIONS_CACHE.clear()
    live_service._probe_ctrader_cache = None


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
    b.reconcile_account.side_effect = lambda **_kwargs: SimpleNamespace(
        status="fresh",
        reconcile_id="account-refresh-test",
        observed_at=time.time(),
        account=dict(b.account_info.return_value),
    )
    b.reconcile_positions.side_effect = lambda **_kwargs: SimpleNamespace(
        status="fresh",
        reconcile_id="positions-refresh-test",
        observed_at=time.time(),
        positions=tuple(b.get_positions.return_value),
    )
    return b


def _known_position_components(observed_at: float) -> dict:
    return {
        name: {
            "state": "known",
            "source": "ctrader_reconcile",
            "observed_at": observed_at,
            "reason_code": None,
            "known_position_ids": [42],
            "unknown_position_ids": [],
        }
        for name in ("identity", "protection", "price", "pnl")
    }


def test_http_reads_preserve_fresh_broker_observation_timestamp(monkeypatch):
    from backend.services.api_fact_views import account_fact_payload, positions_fact_payload

    observed_at = time.time() - 2.0

    class _Bridge:
        is_connected = True

        def reconcile_account(self, *, force=True, allow_cache_fallback=False):
            return SimpleNamespace(
                status="fresh",
                reconcile_id="account-fresh-r1",
                observed_at=observed_at,
                account={"balance": 1000.0, "equity": 1001.0, "currency": "USD"},
            )

        def reconcile_positions(self, *, force=True, allow_cache_fallback=False):
            return SimpleNamespace(
                status="fresh",
                reconcile_id="positions-fresh-r1",
                observed_at=observed_at,
                positions=({"position_id": 42, "symbol": "XAUUSD+", "volume": 100.0},),
                components=_known_position_components(observed_at),
            )

    bridge = _Bridge()
    monkeypatch.setattr(live_service, "_get_ctrader", lambda: (bridge, None, False))
    monkeypatch.setattr(
        live_service,
        "_enrich_positions_with_path_metrics",
        lambda positions, **_kwargs: list(positions),
    )
    monkeypatch.setattr(live_service, "_loop_thread", None)
    live_service._live_state["loop_running"] = False

    account = live_service.get_account("ctrader")
    positions = live_service.get_positions("ctrader")

    assert live_service._live_state["account_updated_at"] == observed_at
    assert live_service._live_state["positions_updated_at"] == observed_at
    assert account["reconcile_status"] == "fresh"
    assert positions["reconcile_status"] == "fresh"
    assert account_fact_payload(account, now=observed_at + 3)["_fact"]["state"] == "known"
    assert positions_fact_payload(positions, now=observed_at + 3)["_fact"]["state"] == "known"


def test_ctrader_events_never_rejuvenate_reconciled_account_or_positions(monkeypatch):
    from backend.services.api_fact_views import account_fact_payload, positions_fact_payload

    now = time.time()
    broker_observed_at = now - 20.0

    class _Bridge:
        def add_event_listener(self, listener):
            self.listener = listener

    bridge = _Bridge()
    monkeypatch.setattr(
        live_service,
        "_enrich_positions_with_path_metrics",
        lambda positions, **_kwargs: list(positions),
    )
    monkeypatch.setattr(live_service, "_phase2_v2_active", lambda: False)
    monkeypatch.setattr(live_service, "_probe_ctrader", lambda: ("connected", None))
    monkeypatch.setattr(
        live_service,
        "loop_status",
        lambda: {
            "running": True,
            "phase": "running",
            "ready": True,
            "accepting_new_risk": True,
            "blockers": [],
            "safety": {},
            "safety_heartbeat_age_sec": None,
        },
    )
    authoritative_account = {
        "ok": True,
        "broker": "ctrader",
        "balance": 1000.0,
        "equity": 1000.0,
    }
    authoritative_positions = [{"position_id": 42, "symbol": "XAUUSD+"}]
    live_service._live_state_update(
        _diag={"bridge_ready": True},
        account=dict(authoritative_account),
        account_reconciled=dict(authoritative_account),
        account_updated_at=broker_observed_at,
        account_reconcile_id="account-r1",
        positions=list(authoritative_positions),
        positions_reconciled=list(authoritative_positions),
        positions_updated_at=broker_observed_at,
        positions_reconcile_id="positions-r1",
    )

    live_service._install_ctrader_live_listener(bridge)
    bridge.listener(
        "account",
        {
            "account": {"balance": 1000.0, "equity": 0.0},
            "reason": "equity_recompute",
        },
    )
    bridge.listener(
        "positions",
        {"positions": [{"position_id": 99}], "reason": "execution_event"},
    )

    assert live_service._live_state_get("account", clone=True) == authoritative_account
    assert live_service._live_state_get("positions", clone=True) == authoritative_positions
    assert live_service._live_state_get("account_updated_at") == broker_observed_at
    assert live_service._live_state_get("positions_updated_at") == broker_observed_at
    assert live_service._live_state_get("account_event", clone=True)["equity"] == 0.0
    assert live_service._live_state_get("positions_event", clone=True)[0]["position_id"] == 99

    readiness = live_service.get_live_readiness("ctrader")
    assert readiness["ok"] is False
    assert readiness["account_ready"] is False
    assert readiness["positions_ready"] is False
    assert "account_reconcile_stale" in readiness["reasons"]
    assert "positions_reconcile_stale" in readiness["reasons"]
    account_payload = {**authoritative_account, "readiness": readiness}
    positions_payload = {
        "ok": True,
        "broker": "ctrader",
        "positions": authoritative_positions,
        "readiness": readiness,
    }
    assert account_fact_payload(account_payload, now=now)["_fact"]["state"] == "stale"
    assert positions_fact_payload(positions_payload, now=now)["_fact"]["state"] == "stale"


def test_readiness_cannot_be_green_when_loop_or_safety_blocks_new_risk(monkeypatch):
    now = time.time()
    monkeypatch.setattr(live_service, "_phase2_v2_active", lambda: True)
    monkeypatch.setattr(live_service, "_probe_ctrader", lambda: ("connected", None))
    monkeypatch.setattr(
        live_service,
        "loop_status",
        lambda: {
            "running": True,
            "phase": "degraded",
            "ready": False,
            "accepting_new_risk": False,
            "blockers": ["position_reconcile_failed"],
            "safety_heartbeat_age_sec": 1.0,
            "safety": {
                "reconciliation_state": "failed",
                "blockers": ["position_reconcile_failed"],
                "unknown_execution_count": 0,
                "accepting_new_risk": False,
            },
        },
    )
    live_service._live_state_update(
        _diag={"bridge_ready": True},
        account={"ok": True, "broker": "ctrader", "balance": 1000.0},
        account_reconciled={"ok": True, "broker": "ctrader", "balance": 1000.0},
        account_updated_at=now,
        account_reconcile_id="account-r1",
        positions=[],
        positions_reconciled=[],
        positions_updated_at=now,
        positions_reconcile_id="positions-r1",
    )

    readiness = live_service.get_live_readiness("ctrader")

    assert readiness["ok"] is False
    assert readiness["state"] == "degraded"
    assert readiness["safety_ready"] is False
    assert readiness["loop_accepting_new_risk"] is False
    assert "position_reconcile_failed" in readiness["reasons"]
    assert "safety_position_reconcile_not_fresh" in readiness["reasons"]


def test_readiness_recovers_missed_bridge_edge_from_accepting_generation(monkeypatch):
    now = time.time()
    monkeypatch.setattr(live_service, "_phase2_v2_active", lambda: True)
    monkeypatch.setattr(live_service, "_probe_ctrader", lambda: ("connected", None))
    monkeypatch.setattr(
        live_service,
        "loop_status",
        lambda: {
            "running": True,
            "phase": "running",
            "ready": True,
            "accepting_new_risk": True,
            "blockers": [],
            "safety_heartbeat_age_sec": 1.0,
            "safety": {
                "reconciliation_state": "fresh",
                "blockers": [],
                "unknown_execution_count": 0,
                "accepting_new_risk": True,
            },
        },
    )
    live_service._live_state_update(
        _diag={"bridge_ready": False},
        account_reconciled={"ok": True, "broker": "ctrader", "balance": 1000.0},
        account_updated_at=now,
        account_reconcile_id="account-r1",
        positions_reconciled=[],
        positions_updated_at=now,
        positions_reconcile_id="positions-r1",
    )

    readiness = live_service.get_live_readiness("ctrader")

    assert readiness["ok"] is True
    assert readiness["bridge_ready"] is True
    assert "bridge_not_ready" not in readiness["reasons"]

    monkeypatch.setattr(
        live_service,
        "_probe_ctrader",
        lambda: ("disconnected", "socket_closed"),
    )
    disconnected = live_service.get_live_readiness("ctrader")
    assert disconnected["ok"] is False
    assert disconnected["bridge_ready"] is False
    assert "bridge_not_ready" in disconnected["reasons"]


def test_http_reads_do_not_rejuvenate_non_fresh_broker_cache(monkeypatch):
    from backend.services.api_fact_views import account_fact_payload, positions_fact_payload

    now = time.time()
    old_observation = now - 120.0

    class _Bridge:
        is_connected = True

        def reconcile_account(self, *, force=True, allow_cache_fallback=False):
            return SimpleNamespace(
                status="cache",
                reconcile_id="account-cache-r1",
                observed_at=old_observation,
                account={"balance": 999.0, "equity": 999.0},
            )

        def reconcile_positions(self, *, force=True, allow_cache_fallback=False):
            return SimpleNamespace(
                status="cache",
                reconcile_id="positions-cache-r1",
                observed_at=old_observation,
                positions=({"position_id": 99, "symbol": "XAUUSD+"},),
            )

        def account_info(self):
            raise AssertionError("HTTP fact reads must use explicit reconcile")

        def get_positions(self, _symbol=None):
            raise AssertionError("HTTP fact reads must use explicit reconcile")

    bridge = _Bridge()
    monkeypatch.setattr(live_service, "_get_ctrader", lambda: (bridge, None, False))
    monkeypatch.setattr(
        live_service,
        "_enrich_positions_with_path_metrics",
        lambda positions, **_kwargs: list(positions),
    )
    monkeypatch.setattr(live_service, "_loop_thread", None)
    live_service._live_state.update(
        {
            "loop_running": False,
            "account": {"ok": True, "broker": "ctrader", "balance": 1000.0, "equity": 1000.0},
            "account_reconciled": {"ok": True, "broker": "ctrader", "balance": 1000.0, "equity": 1000.0},
            "account_updated_at": old_observation,
            "account_reconcile_id": "account-old-r1",
            "positions": [{"position_id": 42, "symbol": "XAUUSD+", "volume": 100.0}],
            "positions_reconciled": [{"position_id": 42, "symbol": "XAUUSD+", "volume": 100.0}],
            "positions_updated_at": old_observation,
            "positions_reconcile_id": "positions-old-r1",
        }
    )

    account = live_service.get_account("ctrader")
    positions = live_service.get_positions("ctrader")

    assert account["balance"] == 1000.0
    assert positions["positions"][0]["position_id"] == 42
    assert account["reconcile_status"] == "failed"
    assert positions["reconcile_status"] == "failed"
    assert live_service._live_state["account_updated_at"] == old_observation
    assert live_service._live_state["positions_updated_at"] == old_observation
    assert account_fact_payload(account, now=now)["_fact"]["state"] == "stale"
    assert positions_fact_payload(positions, now=now)["_fact"]["state"] == "stale"


def test_refresh_account_positions_writes_cache(monkeypatch):
    monkeypatch.setattr(
        live_service,
        "_lookup_open_decision_context",
        lambda _position_id: {"entry_ts": 0.0, "timeframe": "M5", "source": ""},
    )
    monkeypatch.setattr(live_service, "_load_recovery_position_row", lambda _position_id: None)
    monkeypatch.setattr(live_service, "_lookup_entry_decision_id", lambda _position_id: None)
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
    assert live_service._live_state["account_reconciled"]["balance"] == 10000.0
    assert live_service._live_state["positions_reconciled"][0]["position_id"] == 42
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


def test_legacy_refresh_cadence_sustains_fifteen_second_fact_window(monkeypatch):
    import inspect

    clock = {"now": 100.0}
    monkeypatch.setattr(live_service.time, "time", lambda: clock["now"])
    monkeypatch.setattr(
        live_service,
        "_enrich_positions_with_path_metrics",
        lambda positions, **_kwargs: list(positions),
    )

    class _Bridge:
        is_connected = True

        def __init__(self):
            self.account_calls = 0
            self.position_calls = 0

        def reconcile_account(self, **_kwargs):
            self.account_calls += 1
            return SimpleNamespace(
                status="fresh",
                reconcile_id=f"account-{self.account_calls}",
                observed_at=clock["now"],
                account={"balance": 1000.0, "equity": 1000.0},
            )

        def reconcile_positions(self, **_kwargs):
            self.position_calls += 1
            return SimpleNamespace(
                status="fresh",
                reconcile_id=f"positions-{self.position_calls}",
                observed_at=clock["now"],
                positions=(),
            )

    bridge = _Bridge()
    live_service._refresh_account_positions_sync(bridge, "ctrader")
    clock["now"] = 104.9
    live_service._refresh_account_positions_sync(bridge, "ctrader")
    assert (bridge.account_calls, bridge.position_calls) == (1, 1)
    clock["now"] = 105.1
    live_service._refresh_account_positions_sync(bridge, "ctrader")
    assert (bridge.account_calls, bridge.position_calls) == (2, 1)
    clock["now"] = 110.1
    live_service._refresh_account_positions_sync(bridge, "ctrader")

    assert (bridge.account_calls, bridge.position_calls) == (3, 2)
    assert clock["now"] - live_service._live_state["account_updated_at"] < 15.0
    assert clock["now"] - live_service._live_state["positions_updated_at"] < 15.0
    # The five-second worker cadence must leave latency headroom below the
    # 15-second account freshness safety contract.
    assert live_service._ACCOUNT_REFRESH_MIN_INTERVAL <= 5.0
    assert live_service._POSITION_RECONCILE_MIN_INTERVAL <= 10.0
    assert (
        inspect.signature(live_service.kickoff_account_refresh)
        .parameters["interval_sec"]
        .default
        <= 5.0
    )


def test_refresh_account_positions_fills_single_position_pnl_from_account_equity(monkeypatch):
    monkeypatch.setattr(
        live_service,
        "_lookup_open_decision_context",
        lambda _position_id: {"entry_ts": 0.0, "timeframe": "M5", "source": ""},
    )
    monkeypatch.setattr(live_service, "_load_recovery_position_row", lambda _position_id: None)
    monkeypatch.setattr(live_service, "_lookup_entry_decision_id", lambda _position_id: None)
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
    bridge.reconcile_account.side_effect = RuntimeError("network blip")
    bridge.reconcile_positions.side_effect = RuntimeError("network blip")
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

        def reconcile_account(self, *, force=True, allow_cache_fallback=False):
            self.account_calls += 1
            return SimpleNamespace(
                status="fresh",
                reconcile_id="account-skip-position",
                observed_at=time.time(),
                account={
                    "balance": 10000.0,
                    "equity": 10000.0,
                    "currency": "USD",
                    "margin": 0.0,
                    "margin_free": 0.0,
                    "leverage": 100,
                },
            )

        def reconcile_positions(self, *, force=True, allow_cache_fallback=False):
            self.position_calls += 1
            return SimpleNamespace(
                status="fresh",
                reconcile_id="positions-should-not-run",
                observed_at=time.time(),
                positions=({"position_id": 43, "symbol_id": 1, "type": "buy", "volume": 100.0},),
            )

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

        def reconcile_account(self, *, force=True, allow_cache_fallback=False):
            raise AssertionError("account refresh should be skipped")

        def reconcile_positions(self, *, force=True, allow_cache_fallback=False):
            return SimpleNamespace(
                status="failed",
                reconcile_id="positions-failed",
                observed_at=0.0,
                positions=(),
                error_code="timeout",
            )

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
    src_path = (
        _Path(__file__).resolve().parent.parent
        / "backend"
        / "services"
        / "live_loop_tick_runtime.py"
    )
    src = src_path.read_text(encoding="utf-8")
    lines = src.splitlines()
    # Locate the extracted tick body used by _run_loop's main while loop.
    # Phase2 runs broker reconciliation inline on the single mutation thread;
    # this regression assertion applies only to the compatibility loop, which
    # still owns the background refresh worker.
    main_loop_idx = next(
        i for i, ln in enumerate(lines)
        if "def run_legacy_live_loop_tick_body" in ln
    )
    helper_end = next(
        i for i, ln in enumerate(lines[main_loop_idx + 1:], start=main_loop_idx + 1)
        if ln.startswith("def run_live_loop_tick_body")
    )
    branch_text = "\n".join(lines[main_loop_idx:helper_end])
    kickoff_pos = branch_text.find("kickoff_account_refresh")
    # cTrader reads from local DataStore now
    warmup_pos = branch_text.find("warmup_from_local_db")
    assert kickoff_pos > 0, "kickoff_account_refresh not found in cTrader main-loop block"
    assert warmup_pos > 0, "warmup_from_local_db not found in cTrader main-loop block"
    assert kickoff_pos < warmup_pos, (
        "REGRESSION: kickoff_account_refresh is AFTER _warmup_from_local_db in "
        "the cTrader live tick body. It must be BEFORE so the cache writer still "
        "runs when warmup returns None."
    )
