from __future__ import annotations

import sqlite3

import pytest

from backend.services.canonical_v2 import (
    append_event,
    ensure_sqlite_schema,
    put_payload,
    record_decision_event,
)
from scripts.canonical_v2_trade_lineage_audit import audit_lineage, apply_repairs, main
import scripts.canonical_v2_trade_lineage_audit as lineage_audit


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_sqlite_schema(conn)
    return conn


def _review(
    conn: sqlite3.Connection,
    *,
    review_id: str,
    producer: str = "live_closed_position",
    entry_decision_id: str = "entry-1",
    exit_decision_id: str = "exit-1",
) -> None:
    payload = put_payload(
        conn,
        {
            "review_id": review_id,
            "entry_decision_id": entry_decision_id,
            "exit_decision_id": exit_decision_id,
        },
        payload_kind="trade_review",
        schema_version="canonical_payload.v1",
    )
    append_event(
        conn,
        event_id=f"live_review_{review_id}",
        event_type="trade_review",
        entity_type="review",
        entity_id=review_id,
        payload_hash=payload.payload_hash,
        producer=producer,
        idempotency_key=review_id,
    )


def _decision(conn: sqlite3.Connection, decision_id: str) -> None:
    record_decision_event(
        conn,
        decision_id=decision_id,
        event_type="open",
        symbol="XAUUSD+",
        timeframe="M1",
        decision_ts=100.0,
    )


def test_audit_is_bounded_and_does_not_guess_missing_parent() -> None:
    conn = _db()
    try:
        _decision(conn, "entry-1")
        _decision(conn, "exit-1")
        _review(conn, review_id="r1")
        _review(conn, review_id="r2", entry_decision_id="not-present")
        _review(
            conn,
            review_id="revision-r3",
            producer="autonomous_learning",
        )

        report = audit_lineage(conn, limit=1)
        assert report["reviews_scanned"] == 1
        assert report["writes_performed"] is False
        assert conn.execute("SELECT COUNT(*) FROM event_relation").fetchone()[0] == 0

        full = audit_lineage(conn, limit=10)
        statuses = {(item["review_id"], item["role"]): item["status"] for item in full["findings"]}
        assert statuses[("r1", "entry")] == "missing_relation"
        assert statuses[("r1", "exit")] == "missing_relation"
        assert statuses[("r2", "entry")] == "parent_missing"
        assert statuses[("revision-r3", "entry")] == "unlinked_non_live_producer"
    finally:
        conn.close()


def test_apply_is_explicit_transactional_idempotent_and_revalidates() -> None:
    conn = _db()
    try:
        _decision(conn, "entry-1")
        _decision(conn, "exit-1")
        _review(conn, review_id="r1")
        report = audit_lineage(conn, limit=10)

        applied = apply_repairs(conn, report)
        assert applied["applied_count"] == 2
        assert applied["already_linked_count"] == 0
        assert conn.execute("SELECT COUNT(*) FROM event_relation").fetchone()[0] == 2

        after = audit_lineage(conn, limit=10)
        assert after["counts"] == {"linked": 2}
        retry = apply_repairs(conn, after)
        assert retry["applied_count"] == 0
        assert retry["already_linked_count"] == 0
        assert conn.execute("SELECT COUNT(*) FROM event_relation").fetchone()[0] == 2
    finally:
        conn.close()


def test_ambiguous_parent_is_reported_and_never_repaired() -> None:
    conn = _db()
    try:
        first = put_payload(conn, {"kind": "decision"}, payload_kind="risk_decision", schema_version="v1")
        second = put_payload(conn, {"kind": "decision"}, payload_kind="risk_decision", schema_version="v1")
        for event_id, ref in (("decision-a", first), ("decision-b", second)):
            append_event(
                conn,
                event_id=event_id,
                event_type="risk_decision",
                entity_type="decision",
                entity_id="same-id",
                payload_hash=ref.payload_hash,
                producer=event_id,
            )
        _review(conn, review_id="ambiguous", entry_decision_id="same-id", exit_decision_id="")
        report = audit_lineage(conn, limit=10)
        finding = next(item for item in report["findings"] if item["role"] == "entry")
        assert finding["status"] == "parent_ambiguous"
        result = apply_repairs(conn, report)
        assert result["applied_count"] == 0
        assert conn.execute("SELECT COUNT(*) FROM event_relation").fetchone()[0] == 0
    finally:
        conn.close()


def test_cli_defaults_to_read_only_dry_run(tmp_path, capsys) -> None:
    db_path = tmp_path / "canonical.sqlite"
    conn = sqlite3.connect(db_path)
    ensure_sqlite_schema(conn)
    conn.commit()
    conn.close()

    assert main(["--sqlite", str(db_path), "--limit", "1"]) == 0
    output = capsys.readouterr().out
    assert '"mode": "dry_run"' in output
    assert '"writes_performed": false' in output


def test_apply_rolls_back_all_relations_on_failure(monkeypatch) -> None:
    conn = _db()
    try:
        _decision(conn, "entry-1")
        _decision(conn, "exit-1")
        _review(conn, review_id="r1")
        report = audit_lineage(conn, limit=10)
        original = lineage_audit.append_relation
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("synthetic relation failure")
            return original(*args, **kwargs)

        monkeypatch.setattr(lineage_audit, "append_relation", fail_second)
        with pytest.raises(RuntimeError, match="synthetic relation failure"):
            apply_repairs(conn, report)
        assert conn.execute("SELECT COUNT(*) FROM event_relation").fetchone()[0] == 0
    finally:
        conn.close()


def test_apply_rejects_tampered_parent_from_report() -> None:
    conn = _db()
    try:
        _decision(conn, "entry-1")
        _decision(conn, "exit-1")
        _review(conn, review_id="r1")
        report = audit_lineage(conn, limit=10)
        entry = next(item for item in report["findings"] if item["role"] == "entry")
        entry["parent_event_id"] = "live_decision_exit-1"

        result = apply_repairs(conn, report)
        assert result["applied_count"] == 1  # exit finding remains genuine
        assert any(
            item["apply_status"] == "stale_plan_parent_mismatch"
            for item in result["skipped"]
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM event_relation WHERE relation_type='derived_from'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_apply_rejects_forged_live_producer_from_report() -> None:
    conn = _db()
    try:
        _decision(conn, "entry-1")
        _review(conn, review_id="revision", producer="autonomous_learning", exit_decision_id="")
        report = audit_lineage(conn, limit=10)
        entry = next(item for item in report["findings"] if item["role"] == "entry")
        # A caller cannot promote a revision by mutating the report metadata.
        entry["status"] = "missing_relation"
        entry["producer"] = "live_closed_position"

        result = apply_repairs(conn, report)
        assert result["applied_count"] == 0
        assert any(
            item["apply_status"] == "child_not_live_producer"
            for item in result["skipped"]
        )
        assert conn.execute("SELECT COUNT(*) FROM event_relation").fetchone()[0] == 0
    finally:
        conn.close()


def test_apply_rechecks_stale_report_as_already_linked() -> None:
    conn = _db()
    try:
        _decision(conn, "entry-1")
        _decision(conn, "exit-1")
        _review(conn, review_id="r1")
        report = audit_lineage(conn, limit=10)
        entry = next(item for item in report["findings"] if item["role"] == "entry")
        append_event_relation = (
            entry["review_event_id"],
            entry["parent_event_id"],
            entry["relation_type"],
        )
        from backend.services.canonical_v2 import append_relation

        append_relation(
            conn,
            from_event_id=append_event_relation[0],
            to_event_id=append_event_relation[1],
            relation_type=append_event_relation[2],
        )
        result = apply_repairs(conn, report)
        assert result["already_linked_count"] == 1
        assert result["applied_count"] == 1
        assert conn.execute("SELECT COUNT(*) FROM event_relation").fetchone()[0] == 2
    finally:
        conn.close()
