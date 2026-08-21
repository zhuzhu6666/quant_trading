"""Canonical lifecycle writers preserve the caller's event timestamp."""

import time

from backend.ledger.service import DecisionLedger
from backend.services.canonical_v2_reader import iter_order_rows, iter_position_rows


def test_log_order_event_writes_the_same_event_ts_to_canonical(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    ledger = DecisionLedger(str(db_path))

    frozen = 1_784_937_600.123
    monkeypatch.setattr(time, "time", lambda: frozen)
    event_id = ledger.log_order_event(event_type="submitted", trade_id="t1", event_ts=None)

    with ledger._conn() as conn:
        rows = iter_order_rows(conn, limit=0)

    assert len(rows) == 1
    assert rows[0]["event_id"] == event_id
    assert rows[0]["event_ts"] == frozen
    assert rows[0]["trade_id"] == "t1"


def test_log_position_event_writes_the_same_event_ts_to_canonical(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    ledger = DecisionLedger(str(db_path))

    frozen = 1_784_937_700.456
    monkeypatch.setattr(time, "time", lambda: frozen)
    event_id = ledger.log_position_event(position_id="p1", event_type="opened", event_ts=None)

    with ledger._conn() as conn:
        rows = iter_position_rows(conn, limit=0)

    assert len(rows) == 1
    assert rows[0]["event_id"] == event_id
    assert rows[0]["event_ts"] == frozen
    assert rows[0]["position_id"] == "p1"


def test_log_order_event_preserves_explicit_event_ts(tmp_path):
    db_path = tmp_path / "state.db"
    ledger = DecisionLedger(str(db_path))
    explicit = 1_784_937_800.999

    ledger.log_order_event(event_type="filled", trade_id="t2", event_ts=explicit)

    with ledger._conn() as conn:
        rows = iter_order_rows(conn, limit=0)

    assert rows[0]["event_ts"] == explicit
