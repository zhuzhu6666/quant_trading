from __future__ import annotations

import json
import sqlite3

from backend.core.db import connect_sqlite
from backend.ledger.service import DecisionLedger
from backend.services import state_dual_write


def _outbox_rows(db_path):
    conn = connect_sqlite(db_path, read_only=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM state_dual_write_outbox ORDER BY created_at").fetchall()]
    finally:
        conn.close()


def _log_sample_decision(ledger: DecisionLedger) -> str:
    return ledger.log_decision(
        event_type="signal",
        symbol="XAUUSD+",
        timeframe="M15",
        decision_ts=123.0,
        action_score=0.42,
        action_reason="unit_test",
        portfolio_state={"equity": 1000},
        risk_state={"allowed": True},
        action_json={"tick": 7},
        factor_snapshots=[
            {
                "factor": "adx",
                "source": "registry",
                "raw_value": 1.5,
                "normalized_value": 0.2,
                "direction": 1,
                "base_weight": 0.3,
                "policy_weight": 0.4,
                "shadow_score": 55,
                "health_score": 60,
                "gated": False,
                "gated_reason": "",
                "contribution_score": 0.08,
            },
            {
                "factor": "ema_slope",
                "raw_value": -0.1,
                "normalized_value": -0.3,
                "direction": -1,
                "base_weight": 0.2,
                "policy_weight": 0.2,
                "gated": True,
                "gated_reason": "test_gate",
                "contribution_score": -0.06,
            },
        ],
    )


def test_no_pg_dsn_only_writes_sqlite(monkeypatch, tmp_path):
    monkeypatch.setenv("QUANT_AUDIT_PG_DSN", "")
    monkeypatch.setenv("QUANT_AUDIT_PG_DUAL_WRITE", "true")
    db_path = tmp_path / "state.db"
    ledger = DecisionLedger(str(db_path))

    decision_id = _log_sample_decision(ledger)

    conn = connect_sqlite(db_path, read_only=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM decision_ledger WHERE decision_id=?", (decision_id,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM decision_factor_snapshot WHERE decision_id=?", (decision_id,)).fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM state_dual_write_outbox").fetchone()[0] == 0
    finally:
        conn.close()


def test_sqlite_success_generates_outbox_event(monkeypatch, tmp_path):
    monkeypatch.setenv("QUANT_AUDIT_PG_DSN", "postgresql://example")
    monkeypatch.setenv("QUANT_AUDIT_PG_DUAL_WRITE", "true")
    db_path = tmp_path / "state.db"
    ledger = DecisionLedger(str(db_path))

    decision_id = _log_sample_decision(ledger)
    rows = _outbox_rows(db_path)

    assert len(rows) == 1
    assert rows[0]["event_id"] == decision_id
    assert rows[0]["status"] == "pending"
    payload = json.loads(rows[0]["payload_json"])
    assert payload["decision"]["decision_id"] == decision_id
    assert [row["snapshot_seq"] for row in payload["factor_snapshots"]] == [1, 2]


class _FakeSink:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.events = []
        self.ensure_calls = 0

    def ensure_schema(self):
        self.ensure_calls += 1

    def write_event(self, payload):
        if self.fail:
            raise RuntimeError("pg down")
        self.events.append(payload)


def test_flush_success_marks_synced(monkeypatch, tmp_path):
    monkeypatch.setenv("QUANT_AUDIT_PG_DSN", "postgresql://example")
    monkeypatch.setenv("QUANT_AUDIT_PG_DUAL_WRITE", "true")
    db_path = tmp_path / "state.db"
    ledger = DecisionLedger(str(db_path))
    decision_id = _log_sample_decision(ledger)
    sink = _FakeSink()

    result = state_dual_write.flush_once(db_path=db_path, sink=sink)

    assert result == {"processed": 1, "synced": 1, "failed": 0}
    assert sink.ensure_calls == 1
    assert sink.events[0]["decision"]["decision_id"] == decision_id
    rows = _outbox_rows(db_path)
    assert rows[0]["status"] == "synced"
    assert rows[0]["synced_at"] > 0


def test_flush_failure_keeps_retry_without_raising(monkeypatch, tmp_path):
    monkeypatch.setenv("QUANT_AUDIT_PG_DSN", "postgresql://example")
    monkeypatch.setenv("QUANT_AUDIT_PG_DUAL_WRITE", "true")
    db_path = tmp_path / "state.db"
    ledger = DecisionLedger(str(db_path))
    _log_sample_decision(ledger)

    result = state_dual_write.flush_once(db_path=db_path, sink=_FakeSink(fail=True))

    assert result == {"processed": 1, "synced": 0, "failed": 1}
    rows = _outbox_rows(db_path)
    assert rows[0]["status"] == "retry"
    assert rows[0]["attempts"] == 1
    assert "pg down" in rows[0]["last_error"]


def test_duplicate_event_id_is_idempotent_in_outbox(monkeypatch, tmp_path):
    monkeypatch.setenv("QUANT_AUDIT_PG_DSN", "postgresql://example")
    monkeypatch.setenv("QUANT_AUDIT_PG_DUAL_WRITE", "true")
    db_path = tmp_path / "state.db"
    decision = {"decision_id": "dec_fixed", "event_type": "signal", "decision_ts": 1.0, "created_at": 1.0}

    for score in (0.1, 0.2):
        state_dual_write.enqueue_decision_ledger_event(
            db_path=db_path,
            decision={**decision, "action_score": score},
            factor_snapshots=[],
        )

    rows = _outbox_rows(db_path)
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload_json"])
    assert payload["decision"]["action_score"] == 0.2


def test_state_row_event_can_be_enqueued_inside_existing_transaction(monkeypatch, tmp_path):
    monkeypatch.setenv("QUANT_AUDIT_PG_DSN", "postgresql://example")
    monkeypatch.setenv("QUANT_AUDIT_PG_DUAL_WRITE", "true")
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        queued = state_dual_write.enqueue_state_row_event_on_conn(
            conn,
            db_path=db_path,
            table_name="runtime_kv",
            entity_key="loop_desired",
            row={"key": "loop_desired", "value_json": '{"enabled": true}', "updated_at": 42.0},
            operation="upsert",
            source_updated_at=42.0,
        )
        conn.commit()
    finally:
        conn.close()

    assert queued is True
    rows = _outbox_rows(db_path)
    assert len(rows) == 1
    assert rows[0]["event_type"] == state_dual_write.EVENT_STATE_ROW
    payload = json.loads(rows[0]["payload_json"])
    assert payload["table_name"] == "runtime_kv"
    assert payload["entity_key"] == "loop_desired"
    assert payload["row"]["key"] == "loop_desired"
    assert payload["source_updated_at"] == 42.0


def test_flush_state_row_event_marks_synced(monkeypatch, tmp_path):
    monkeypatch.setenv("QUANT_AUDIT_PG_DSN", "postgresql://example")
    monkeypatch.setenv("QUANT_AUDIT_PG_DUAL_WRITE", "true")
    db_path = tmp_path / "state.db"
    state_dual_write.enqueue_state_row_event(
        db_path=db_path,
        table_name="recovery_position_state",
        entity_key="123",
        row={"position_id": 123, "status": "open", "last_seen_at": 99.0},
        operation="upsert",
        source_updated_at=99.0,
    )
    sink = _FakeSink()

    result = state_dual_write.flush_once(db_path=db_path, sink=sink)

    assert result == {"processed": 1, "synced": 1, "failed": 0}
    assert sink.events[0]["event_type"] == state_dual_write.EVENT_STATE_ROW
    assert sink.events[0]["table_name"] == "recovery_position_state"
    rows = _outbox_rows(db_path)
    assert rows[0]["status"] == "synced"


def test_position_supervisor_trace_generates_state_row_outbox(monkeypatch, tmp_path):
    monkeypatch.setenv("QUANT_AUDIT_PG_DSN", "postgresql://example")
    monkeypatch.setenv("QUANT_AUDIT_PG_DUAL_WRITE", "true")
    db_path = tmp_path / "state.db"
    ledger = DecisionLedger(str(db_path))

    trace_id = ledger.log_position_supervisor_trace(
        position_id="pos-1",
        symbol="XAUUSD+",
        timeframe="M5",
        event_ts=101.0,
        action="tighten",
        summary_reason="unit_test",
        verdict={"action": "tighten"},
    )

    rows = _outbox_rows(db_path)
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload_json"])
    assert rows[0]["event_type"] == state_dual_write.EVENT_STATE_ROW
    assert payload["table_name"] == "position_supervisor_trace"
    assert payload["entity_key"] == trace_id
    assert payload["row"]["position_id"] == "pos-1"
