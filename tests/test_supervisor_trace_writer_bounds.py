import json
import sqlite3

from backend.ledger.service import DecisionLedger
from backend.services.state_payload_archive import restore_json_payload
from backend.services.supervisor_payload_contract import strip_recursive_supervisor_snapshots


def test_strip_recursive_supervisor_snapshots_keeps_an_explicit_marker():
    payload = {
        "action": "tighten",
        "evidence": {
            "score": 0.8,
            "supervisor_state": {
                "latest_supervisor": {
                    "action": "hold",
                    "evidence": {"supervisor_state": {}},
                }
            },
        },
    }

    sanitized = strip_recursive_supervisor_snapshots(payload)

    assert sanitized["action"] == "tighten"
    assert sanitized["evidence"]["score"] == 0.8
    assert sanitized["evidence"]["supervisor_state"]["latest_supervisor"] == {
        "omitted": True,
        "reason": "recursive_prior_supervisor_snapshot",
    }
    assert payload["evidence"]["supervisor_state"]["latest_supervisor"]["action"] == "hold"


def test_ledger_trace_is_bounded_without_archive_columns(tmp_path):
    ledger = DecisionLedger(str(tmp_path / "state.db"))
    previous = {
        "action": "hold",
        "summary_reason": "position_healthy",
        "evidence": {"supervisor_state": {}},
    }
    for _ in range(6):
        previous = {
            "action": "hold",
            "summary_reason": "position_healthy",
            "evidence": {"supervisor_state": {"latest_supervisor": previous}},
        }

    trace_id = ledger.log_position_supervisor_trace(
        position_id="bounded-1",
        action="tighten",
        summary_reason="profit_lock",
        verdict={
            "action": "tighten",
            "summary_reason": "profit_lock",
            "evidence": {
                "supervisor_posture": "range_capture",
                "supervisor_state": {"latest_supervisor": previous},
            },
        },
    )

    with ledger._conn() as conn:
        row = conn.execute(
            "SELECT verdict_json FROM position_supervisor_trace WHERE trace_id=?",
            (trace_id,),
        ).fetchone()

    stored = json.loads(row["verdict_json"])
    assert stored == {
        "action": "tighten",
        "summary_reason": "profit_lock",
        "evidence": {"supervisor_posture": "range_capture"},
    }
    assert len(row["verdict_json"].encode("utf-8")) < 1024


def test_ledger_trace_archive_keeps_sanitized_semantic_payload(tmp_path):
    db_path = tmp_path / "state.db"
    ledger = DecisionLedger(str(db_path))
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "ALTER TABLE position_supervisor_trace ADD COLUMN verdict_archive_hash TEXT NOT NULL DEFAULT ''"
        )
        conn.execute(
            "ALTER TABLE position_supervisor_trace ADD COLUMN verdict_raw_sha256 TEXT NOT NULL DEFAULT ''"
        )
        conn.execute(
            "ALTER TABLE position_supervisor_trace ADD COLUMN verdict_raw_bytes INTEGER NOT NULL DEFAULT 0"
        )
        conn.execute(
            """
            CREATE TABLE state_payload_archive (
                archive_hash TEXT PRIMARY KEY,
                source_table TEXT NOT NULL,
                source_id TEXT NOT NULL,
                payload_kind TEXT NOT NULL,
                codec TEXT NOT NULL,
                raw_sha256 TEXT NOT NULL,
                raw_bytes INTEGER NOT NULL,
                compressed_bytes INTEGER NOT NULL,
                payload_bytes BLOB NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        conn.commit()

    trace_id = ledger.log_position_supervisor_trace(
        position_id="archived-1",
        action="hold",
        verdict={
            "action": "hold",
            "evidence": {
                "supervisor_posture": "trend_hold",
                "supervisor_state": {
                    "latest_supervisor": {
                        "action": "hold",
                        "evidence": {"supervisor_state": {}},
                    }
                },
            },
        },
    )

    with ledger._conn() as conn:
        row = conn.execute(
            "SELECT verdict_json, verdict_archive_hash, verdict_raw_sha256, verdict_raw_bytes "
            "FROM position_supervisor_trace WHERE trace_id=?",
            (trace_id,),
        ).fetchone()
        restored = restore_json_payload(conn, row["verdict_archive_hash"])

    archive_payload = json.loads(restored)
    restored_verdict = json.loads(archive_payload["verdict_json"])
    assert json.loads(row["verdict_json"])["evidence"] == {"supervisor_posture": "trend_hold"}
    assert restored_verdict["evidence"]["supervisor_state"]["latest_supervisor"] == {
        "omitted": True,
        "reason": "recursive_prior_supervisor_snapshot",
    }
    assert row["verdict_raw_sha256"] == row["verdict_archive_hash"]
    assert row["verdict_raw_bytes"] == len(restored.encode("utf-8"))
