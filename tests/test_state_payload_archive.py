import hashlib
import json
import sqlite3

import pytest

from backend.services.review_contract import normalize_trade_review_contract
from backend.services.state_payload_archive import (
    archive_json_payload,
    load_supervisor_trace_archive,
    restore_json_payload,
    supervisor_trace_archive_text,
)
from backend.services.supervisor_payload_contract import compact_supervisor_mapping


def _db(path):
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
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
    return conn


def test_archive_roundtrip_preserves_exact_sha256(tmp_path):
    conn = _db(tmp_path / "archive.db")
    try:
        raw = json.dumps({"number": 0.12345678901234567, "nested": [1, 2, 3]}, ensure_ascii=False)
        metadata = archive_json_payload(
            conn,
            source_table="trade_outcome_review",
            source_id="review-1",
            payload_kind="review_json",
            raw_json=raw,
        )
        conn.commit()
        assert metadata["raw_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
        assert restore_json_payload(conn, metadata["archive_hash"]) == raw
    finally:
        conn.close()


def test_archive_restore_rejects_compressed_byte_metadata_drift(tmp_path):
    conn = _db(tmp_path / "archive-metadata.db")
    try:
        metadata = archive_json_payload(
            conn,
            source_table="trade_outcome_review",
            source_id="review-1",
            payload_kind="review_json",
            raw_json='{"value":1}',
        )
        conn.commit()
        conn.execute(
            "UPDATE state_payload_archive SET compressed_bytes=compressed_bytes+1 WHERE archive_hash=?",
            (metadata["archive_hash"],),
        )
        conn.commit()
        with pytest.raises(ValueError, match="compressed length mismatch"):
            restore_json_payload(conn, metadata["archive_hash"])
    finally:
        conn.close()


def test_trace_archive_roundtrip_preserves_each_json_field_text(tmp_path):
    conn = _db(tmp_path / "trace-archive.db")
    try:
        fields = {
            "context_json": '{"decimal":0.123456789012345678901234567890}',
            "verdict_json": '{"evidence":{"current_pnl":1.0000000000000000001}}',
            "risk_verdict_json": '{"allowed":true}',
            "execution_json": '{"candidate":{"nested":true}}',
        }
        metadata = archive_json_payload(
            conn,
            source_table="position_supervisor_trace",
            source_id="trace-1",
            payload_kind="supervisor_trace",
            raw_json=supervisor_trace_archive_text(**fields),
        )
        conn.commit()
        assert load_supervisor_trace_archive(conn, metadata["archive_hash"]) == fields
    finally:
        conn.close()


def test_supervisor_projection_removes_recursive_candidate_without_touching_scalars():
    payload = {
        "decision_id": "decision-1",
        "confidence": 0.987654321,
        "execution": {"candidate": {"previous": {"candidate": {"x": 1}}}, "status": "hold"},
        "evidence": {"current_pnl": 1.23456789},
    }
    projected = compact_supervisor_mapping(payload, nested_keys=frozenset({"evidence", "execution"}))
    assert projected["decision_id"] == "decision-1"
    assert projected["confidence"] == payload["confidence"]
    assert projected["evidence"] == {"current_pnl": payload["evidence"]["current_pnl"]}
    assert "candidate" not in projected["execution"]


def test_review_normalization_attaches_recursive_payload_digest_and_bounds_candidate():
    review = {
        "inferred_close_supervisor": {
            "decision_id": "d1",
            "execution": {"candidate": {"prior": {"candidate": {"x": 1}}}},
        },
        "responsibility_domains": {
            "position_management": {
                "supervisor": {"action": "close", "execution": {"candidate": {"x": 1}}}
            }
        },
    }
    normalized = normalize_trade_review_contract(review)
    assert normalized["supervisor_payload_sha256"]
    assert normalized["inferred_close_supervisor"]["decision_id"] == "d1"
    assert "candidate" not in normalized["inferred_close_supervisor"].get("execution", {})
