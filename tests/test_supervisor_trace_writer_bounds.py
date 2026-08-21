import json

from backend.ledger.service import DecisionLedger
from backend.services.canonical_v2 import read_payload
from backend.services.canonical_v2_reader import iter_supervisor_trace_rows
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


def test_ledger_trace_is_bounded_in_the_canonical_projection(tmp_path):
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
        rows = iter_supervisor_trace_rows(conn, limit=0, trace_id=trace_id)

    assert len(rows) == 1
    stored = json.loads(rows[0]["verdict_json"])
    assert stored == {
        "action": "tighten",
        "summary_reason": "profit_lock",
        "evidence": {"supervisor_posture": "range_capture"},
    }
    assert len(rows[0]["verdict_json"].encode("utf-8")) < 1024


def test_trace_uses_canonical_payload_and_has_no_archive_path(tmp_path):
    db_path = tmp_path / "state.db"
    ledger = DecisionLedger(str(db_path))
    trace_id = ledger.log_position_supervisor_trace(
        position_id="canonical-1",
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
        event = conn.execute(
            "SELECT payload_hash FROM event WHERE event_id=?",
            (f"live_supervisor_trace_{trace_id}",),
        ).fetchone()
        payload = read_payload(conn, event["payload_hash"])
        archive_tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('state_payload_archive', 'position_supervisor_trace')"
        ).fetchall()

    assert archive_tables == []
    assert payload["verdict"]["evidence"] == {"supervisor_posture": "trend_hold"}
    assert payload["raw_verdict"]["evidence"]["supervisor_state"]["latest_supervisor"] == {
        "omitted": True,
        "reason": "recursive_prior_supervisor_snapshot",
    }
