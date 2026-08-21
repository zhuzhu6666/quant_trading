"""Replayed-close attribution recovery.

A recovery-replayed close used to hard-code ``contributions={}`` /
``attribution_integrity="missing"`` even when the attribution engine still
held the position's open context, or the durable open snapshot was sitting in
the row's own recovery metadata.  These tests pin the evidence order:
live engine → durable snapshot restore → conservative missing.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.services.live_position_lifecycle import (
    build_replayed_close_payloads,
    replay_close_attribution,
)


class _FakeAttrEngine:
    """Minimal contract surface: has_open / record_close / restore_open."""

    def __init__(self, *, live_open=False, restore_payload=None):
        self.live_open = live_open
        self.restore_payload = restore_payload
        self.recorded = []

    def has_open(self, pid):
        return self.live_open

    def record_close(self, pid, *, close_price, close_ts, real_pnl=None):
        self.recorded.append(int(pid))
        return {"momentum": 0.4}

    def restore_open(self, pid, payload):
        self.restore_payload = dict(payload or {})
        return bool(payload)


def test_live_engine_context_wins_with_full_integrity():
    engine = _FakeAttrEngine(live_open=True)
    contributions, integrity = replay_close_attribution(
        501,
        attr_engine=engine,
        close_price=4576.84,
        close_ts=1787316360.0,
        real_pnl={"net": -11.05},
        recovery_meta={"trade_attribution": {"open_price": 4564.16}},
    )
    assert integrity == "full"
    assert contributions == {"momentum": 0.4}
    assert engine.recorded == [501]
    # The durable snapshot must NOT be restored over a live context.
    assert engine.restore_payload is None


def test_durable_snapshot_restore_yields_recovered_integrity():
    engine = _FakeAttrEngine(live_open=False)
    snapshot = {"open_price": 4564.16, "direction": -1}
    contributions, integrity = replay_close_attribution(
        502,
        attr_engine=engine,
        close_price=4564.89,
        close_ts=1787315993.0,
        real_pnl={"net": 16.32},
        recovery_meta={"trade_attribution": snapshot},
    )
    assert integrity == "recovered"
    assert contributions == {"momentum": 0.4}
    # Restore went through the engine's own contract — same content, and the
    # helper never re-serialises or duplicates the snapshot itself.
    assert engine.restore_payload == snapshot
    assert snapshot == {"open_price": 4564.16, "direction": -1}


def test_no_evidence_stays_conservative_missing():
    engine = _FakeAttrEngine(live_open=False)
    contributions, integrity = replay_close_attribution(
        503,
        attr_engine=engine,
        close_price=4570.0,
        close_ts=1.0,
        real_pnl={"net": 0.0},
        recovery_meta={},  # no trade_attribution snapshot
    )
    assert (contributions, integrity) == ({}, "missing")
    assert engine.recorded == []


def test_engine_none_or_untrusted_price_stays_missing():
    for engine in (None, _FakeAttrEngine(live_open=True)):
        contributions, integrity = replay_close_attribution(
            504,
            attr_engine=engine,
            close_price=0.0,  # untrusted/absent close price
            close_ts=1.0,
            real_pnl=None,
            recovery_meta={"trade_attribution": {"open_price": 1.0}},
        )
        assert (contributions, integrity) == ({}, "missing")


def test_engine_error_degrades_to_conservative_missing():
    class _Broken:
        def has_open(self, pid):
            return True

        def record_close(self, *a, **k):
            raise RuntimeError("boom")

    contributions, integrity = replay_close_attribution(
        505,
        attr_engine=_Broken(),
        close_price=100.0,
        close_ts=1.0,
        real_pnl={"net": 1.0},
        recovery_meta={},
    )
    assert (contributions, integrity) == ({}, "missing")


def test_build_replayed_payloads_carries_attribution_into_review():
    engine = _FakeAttrEngine(live_open=True)
    payload = build_replayed_close_payloads(
        position_id=601,
        position_state={
            "symbol": "XAUUSD+",
            "context_integrity": "full",
            "recovery_meta": {"trade_attribution": {"open_price": 4583.0}},
        },
        real_pnl={
            "net": -12.41,
            "exec_price": 4597.29,
            "price_quality": "broker_reconciled",
        },
        strategy_name="factor_v4",
        now_ts=1787311200.0,
        context_integrity_default="partial",
        attr_engine=engine,
    )
    review = payload["review"]
    assert review["contributions"] == {"momentum": 0.4}
    assert review["attribution_integrity"] == "full"
    # Without SL-hit evidence the close reason stays conservative.
    assert review["close_reason"] == "restart_replay"


def test_build_replayed_payloads_without_engine_keeps_legacy_contract():
    payload = build_replayed_close_payloads(
        position_id=602,
        position_state={"symbol": "XAUUSD+"},
        real_pnl={"net": 1.0, "exec_price": 100.0},
        strategy_name="factor_v4",
        now_ts=1.0,
        context_integrity_default="partial",
    )
    assert payload["review"]["contributions"] == {}
    assert payload["review"]["attribution_integrity"] == "missing"
