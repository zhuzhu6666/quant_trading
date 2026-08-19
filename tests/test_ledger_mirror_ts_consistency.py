"""Same-transaction timestamp consistency between legacy writes and canonical mirrors.

The live mirrors (P0a) must record the identical event timestamp that the
legacy row carries.  A previous version evaluated ``event_ts or time.time()``
twice (once for the legacy INSERT, once for the mirror), which skewed the
canonical payload by the latency of the first insert (milliseconds).
"""
import sqlite3
import time

from backend.core.db import STATE_DB_DDL, connect_sqlite
from backend.ledger.service import DecisionLedger


def _capture_mirror(monkeypatch, module: str, name: str) -> dict:
    captured: dict = {}

    def fake(conn, **kwargs):
        captured.update(kwargs)
        return {"event_id": f"{name}_evt"}

    monkeypatch.setattr(f"backend.services.canonical_v2.{name}", fake)
    return captured


def _review_rows(conn: sqlite3.Connection, table: str) -> list[dict]:
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]


def test_log_order_event_mirror_uses_same_event_ts_as_legacy_row(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    ledger = DecisionLedger(str(db_path))
    ledger._ensure_schema()
    captured = _capture_mirror(monkeypatch, "backend.services.canonical_v2", "record_order_event")

    frozen = 1_784_937_600.123
    monkeypatch.setattr(time, "time", lambda: frozen)
    event_id = ledger.log_order_event(event_type="submitted", trade_id="t1", event_ts=None)

    assert captured["event_ts"] == frozen
    conn = connect_sqlite(db_path)
    try:
        rows = _review_rows(conn, "order_lifecycle_event")
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["event_id"] == event_id
    assert rows[0]["event_ts"] == frozen


def test_log_position_event_mirror_uses_same_event_ts_as_legacy_row(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    ledger = DecisionLedger(str(db_path))
    ledger._ensure_schema()
    captured = _capture_mirror(monkeypatch, "backend.services.canonical_v2", "record_position_event")

    frozen = 1_784_937_700.456
    monkeypatch.setattr(time, "time", lambda: frozen)
    event_id = ledger.log_position_event(position_id="p1", event_type="opened", event_ts=None)

    assert captured["event_ts"] == frozen
    conn = connect_sqlite(db_path)
    try:
        rows = _review_rows(conn, "position_lifecycle_event")
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["event_id"] == event_id
    assert rows[0]["event_ts"] == frozen


def test_log_order_event_passes_explicit_event_ts_to_mirror(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    ledger = DecisionLedger(str(db_path))
    ledger._ensure_schema()
    captured = _capture_mirror(monkeypatch, "backend.services.canonical_v2", "record_order_event")

    explicit = 1_784_937_800.999
    ledger.log_order_event(event_type="filled", trade_id="t2", event_ts=explicit)

    assert captured["event_ts"] == explicit
    conn = connect_sqlite(db_path)
    try:
        rows = _review_rows(conn, "order_lifecycle_event")
    finally:
        conn.close()
    assert rows[0]["event_ts"] == explicit
