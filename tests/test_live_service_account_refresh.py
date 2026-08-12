"""Tests for read-only account/position fact projections.

The serial Safety owner is the sole live broker-fact writer. HTTP reads may
consume or explicitly reconcile facts outside the live loop, but they must not
create durable recovery rows or rejuvenate stale observations.
"""
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

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
    live_service._live_state["loop_running"] = False
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
    live_service._live_state["loop_running"] = False
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
    live_service._live_state["loop_running"] = False
    live_service._ACCOUNT_CACHE.clear()
    live_service._POSITIONS_CACHE.clear()
    live_service._probe_ctrader_cache = None


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

    persist_values = []

    def _enrich(positions, **kwargs):
        persist_values.append(kwargs["persist"])
        return list(positions)

    monkeypatch.setattr(
        live_service,
        "_enrich_positions_with_path_metrics",
        _enrich,
    )
    monkeypatch.setattr(live_service, "_loop_thread", None)
    live_service._live_state["loop_running"] = False

    account = live_service.get_account("ctrader")
    positions = live_service.get_positions("ctrader")

    assert live_service._live_state["account_updated_at"] == observed_at
    assert live_service._live_state["positions_updated_at"] == observed_at
    assert account["reconcile_status"] == "fresh"
    assert positions["reconcile_status"] == "fresh"
    assert persist_values and all(value is False for value in persist_values)
    assert account_fact_payload(account, now=observed_at + 3)["_fact"]["state"] == "known"
    assert positions_fact_payload(positions, now=observed_at + 3)["_fact"]["state"] == "known"


def test_live_http_positions_read_existing_projection_without_recomputing(monkeypatch):
    projected = {
        "position_id": 42,
        "symbol": "XAUUSD+",
        "position_path_metrics_state": "known",
        "supervisor": {"action": "hold"},
    }
    live_service._live_state.update(
        {
            "loop_running": True,
            "broker": "ctrader",
            "positions_reconciled": [projected],
            "positions": [projected],
            "positions_updated_at": time.time(),
            "positions_reconcile_id": "positions-read-projection",
        }
    )
    monkeypatch.setattr(
        live_service,
        "_enrich_positions_with_path_metrics",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("HTTP reads must not recompute live projections")
        ),
    )
    monkeypatch.setattr(live_service, "_probe_ctrader", lambda: ("connected", None))

    result = live_service.get_positions("ctrader")

    assert result["positions"][0]["position_id"] == 42
    assert result["positions"][0]["supervisor"]["action"] == "hold"


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
    assert "safety_position_reconcile_not_fresh" not in readiness["reasons"]


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
