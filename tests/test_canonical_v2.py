from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from backend.services.canonical_v2 import (
    CanonicalV2ConflictError,
    CanonicalV2Error,
    append_event,
    append_relation,
    finish_projection_run,
    put_dataset_manifest,
    put_dataset_members,
    put_legacy_mapping,
    put_payload,
    put_state_version,
    put_training_sample,
    read_payload,
    start_projection_run,
)


def _canonical_sqlite() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("ATTACH DATABASE ':memory:' AS canonical_v2")
    conn.executescript(
        """
        CREATE TABLE canonical_v2.payload_blob (
            payload_hash TEXT PRIMARY KEY,
            payload_kind TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            canonical_bytes BLOB NOT NULL,
            codec TEXT NOT NULL,
            raw_sha256 TEXT NOT NULL,
            raw_bytes INTEGER NOT NULL,
            compressed_bytes INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE canonical_v2.event (
            event_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            producer TEXT NOT NULL,
            producer_version TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            causation_id TEXT NOT NULL,
            parent_event_id TEXT,
            idempotency_key TEXT NOT NULL,
            payload_hash TEXT NOT NULL REFERENCES payload_blob(payload_hash),
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX canonical_v2.event_idempotency
            ON event(producer, idempotency_key)
            WHERE idempotency_key <> '';
        CREATE TABLE canonical_v2.event_relation (
            from_event_id TEXT NOT NULL REFERENCES event(event_id),
            to_event_id TEXT NOT NULL REFERENCES event(event_id),
            relation_type TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (from_event_id, to_event_id, relation_type)
        );
        CREATE TABLE canonical_v2.state_version (
            state_version_id TEXT PRIMARY KEY,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            valid_from TEXT NOT NULL,
            valid_to TEXT,
            source_event_id TEXT NOT NULL REFERENCES event(event_id),
            payload_hash TEXT NOT NULL REFERENCES payload_blob(payload_hash),
            created_at TEXT NOT NULL,
            UNIQUE (entity_type, entity_id, version)
        );
        CREATE TABLE canonical_v2.training_sample (
            sample_id TEXT PRIMARY KEY,
            sample_type TEXT NOT NULL,
            source_event_ids TEXT NOT NULL,
            feature_hash TEXT NOT NULL,
            feature_schema_hash TEXT NOT NULL,
            label_hash TEXT NOT NULL,
            trace_hash TEXT NOT NULL,
            evidence_contract TEXT NOT NULL,
            config_version INTEGER NOT NULL,
            config_hash TEXT NOT NULL,
            horizon_minutes INTEGER NOT NULL,
            target_source TEXT NOT NULL,
            sample_status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE canonical_v2.dataset_manifest (
            dataset_id TEXT PRIMARY KEY,
            purpose TEXT NOT NULL,
            training_window TEXT NOT NULL,
            horizon_minutes INTEGER NOT NULL,
            query_contract_hash TEXT NOT NULL,
            sample_digest TEXT NOT NULL,
            feature_schema_hash TEXT NOT NULL,
            label_contract_hash TEXT NOT NULL,
            target_source TEXT NOT NULL,
            config_hash TEXT NOT NULL,
            source_watermark TEXT NOT NULL,
            code_commit TEXT NOT NULL,
            artifact_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE canonical_v2.dataset_manifest_member (
            dataset_id TEXT NOT NULL REFERENCES dataset_manifest(dataset_id),
            sample_id TEXT NOT NULL REFERENCES training_sample(sample_id),
            sample_order INTEGER NOT NULL,
            sample_digest TEXT NOT NULL,
            PRIMARY KEY (dataset_id, sample_id),
            UNIQUE (dataset_id, sample_order)
        );
        CREATE TABLE canonical_v2.projection_run (
            projection_run_id TEXT PRIMARY KEY,
            run_kind TEXT NOT NULL,
            projection_name TEXT NOT NULL,
            source_watermark TEXT NOT NULL,
            code_version TEXT NOT NULL,
            input_digest TEXT NOT NULL,
            output_digest TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            error_code TEXT NOT NULL DEFAULT ''
        );
        CREATE UNIQUE INDEX canonical_v2.projection_run_identity
            ON projection_run(run_kind, projection_name, source_watermark, code_version, input_digest);
        CREATE TABLE canonical_v2.legacy_mapping (
            legacy_table TEXT NOT NULL,
            legacy_primary_key TEXT NOT NULL,
            canonical_event_id TEXT REFERENCES event(event_id),
            canonical_payload_hash TEXT REFERENCES payload_blob(payload_hash),
            classification TEXT NOT NULL,
            mapping_confidence TEXT NOT NULL,
            unresolved_reason TEXT NOT NULL,
            migration_run_id TEXT NOT NULL REFERENCES projection_run(projection_run_id),
            PRIMARY KEY (legacy_table, legacy_primary_key, migration_run_id)
        );
        """
    )
    return conn


def test_payload_is_content_addressed_and_restorable() -> None:
    conn = _canonical_sqlite()
    value = {"after": {"weight": 0.25}, "before": {"weight": 0.1}}

    first = put_payload(conn, value, payload_kind="factor_state", schema_version="v1")
    second = put_payload(conn, value, payload_kind="factor_state", schema_version="v1")

    assert first == second
    assert conn.execute("SELECT COUNT(*) FROM canonical_v2.payload_blob").fetchone()[0] == 1
    assert read_payload(conn, first.payload_hash) == value

    other_kind = put_payload(conn, value, payload_kind="governance_target", schema_version="v1")
    assert other_kind.payload_hash != first.payload_hash


def test_event_idempotency_does_not_merge_real_occurrences() -> None:
    conn = _canonical_sqlite()
    first_payload = put_payload(conn, {"value": 1}, payload_kind="decision", schema_version="v1")
    second_payload = put_payload(conn, {"value": 2}, payload_kind="decision", schema_version="v1")

    first = append_event(
        conn,
        event_type="risk_decision",
        entity_type="decision",
        entity_id="decision-1",
        payload_hash=first_payload.payload_hash,
        producer="test-writer",
        idempotency_key="request-1",
    )
    retry = append_event(
        conn,
        event_type="risk_decision",
        entity_type="decision",
        entity_id="decision-1",
        payload_hash=first_payload.payload_hash,
        producer="test-writer",
        idempotency_key="request-1",
    )
    assert retry["event_id"] == first["event_id"]
    assert retry["created"] is False

    with pytest.raises(CanonicalV2ConflictError):
        append_event(
            conn,
            event_type="risk_decision",
            entity_type="decision",
            entity_id="decision-1",
            payload_hash=second_payload.payload_hash,
            producer="test-writer",
            idempotency_key="request-1",
        )

    occurrence = append_event(
        conn,
        event_type="risk_decision",
        entity_type="decision",
        entity_id="decision-1",
        payload_hash=first_payload.payload_hash,
        producer="test-writer",
    )
    assert occurrence["event_id"] != first["event_id"]
    assert conn.execute("SELECT COUNT(*) FROM canonical_v2.event").fetchone()[0] == 2
    assert append_relation(
        conn,
        from_event_id=occurrence["event_id"],
        to_event_id=first["event_id"],
        relation_type="caused_by",
    )
    assert not append_relation(
        conn,
        from_event_id=occurrence["event_id"],
        to_event_id=first["event_id"],
        relation_type="caused_by",
    )
    before = put_state_version(
        conn,
        state_version_id="state-before",
        entity_type="governance_target",
        entity_id="target-1",
        version=1,
        valid_from=datetime.now(timezone.utc),
        source_event_id=first["event_id"],
        payload_hash=first_payload.payload_hash,
    )
    rollback = put_state_version(
        conn,
        state_version_id="state-rollback",
        entity_type="governance_target",
        entity_id="target-1",
        version=2,
        valid_from=datetime.now(timezone.utc),
        source_event_id=occurrence["event_id"],
        payload_hash=before["payload_hash"],
    )
    assert rollback["payload_hash"] == before["payload_hash"]


def test_training_manifest_and_projection_run_are_reference_only() -> None:
    conn = _canonical_sqlite()
    payload = put_payload(conn, {"decision": "open"}, payload_kind="decision", schema_version="v1")
    event = append_event(
        conn,
        event_type="risk_decision",
        entity_type="decision",
        entity_id="decision-2",
        payload_hash=payload.payload_hash,
        producer="test-writer",
        idempotency_key="request-2",
    )
    sample = put_training_sample(
        conn,
        sample_id="sample-1",
        sample_type="open_outcome",
        source_event_ids=[event["event_id"]],
        feature_hash="feature-hash",
        feature_schema_hash="feature-schema",
        label_hash="label-hash",
        trace_hash="trace-hash",
        evidence_contract={"schema_version": "learning_evidence_contract.v1"},
        config_version=15,
        config_hash="config-hash",
        horizon_minutes=30,
        target_source="trade_review",
        sample_status="ready",
    )
    assert sample["created"] is True
    retry = put_training_sample(
        conn,
        sample_id="sample-1",
        sample_type="open_outcome",
        source_event_ids=[event["event_id"]],
        feature_hash="feature-hash",
        feature_schema_hash="feature-schema",
        label_hash="label-hash",
        trace_hash="trace-hash",
        evidence_contract={"schema_version": "learning_evidence_contract.v1"},
        config_version=15,
        config_hash="config-hash",
        horizon_minutes=30,
        target_source="trade_review",
        sample_status="ready",
    )
    assert retry["created"] is False

    manifest = put_dataset_manifest(
        conn,
        dataset_id="dataset-1",
        purpose="position-quality",
        training_window="window-1",
        horizon_minutes=30,
        query_contract_hash="query-hash",
        sample_digest="sample-digest",
        feature_schema_hash="feature-schema",
        label_contract_hash="label-contract",
        target_source="trade_review",
        config_hash="config-hash",
        source_watermark="event-1",
        code_commit="commit-1",
    )
    assert manifest["created"] is True
    assert put_dataset_members(
        conn,
        dataset_id="dataset-1",
        members=[("sample-1", 0, "sample-digest")],
    ) == 1
    assert put_dataset_members(
        conn,
        dataset_id="dataset-1",
        members=[("sample-1", 0, "sample-digest")],
    ) == 0
    with pytest.raises(CanonicalV2ConflictError):
        put_dataset_members(
            conn,
            dataset_id="dataset-1",
            members=[("sample-1", 0, "different-digest")],
        )

    started = start_projection_run(
        conn,
        projection_run_id="run-1",
        run_kind="projection",
        projection_name="brain_memory",
        source_watermark="event-1",
        code_version="code-1",
        input_digest="input-1",
        started_at=datetime.now(timezone.utc),
    )
    assert started["status"] == "running"
    retry_run = start_projection_run(
        conn,
        projection_run_id="run-2",
        run_kind="projection",
        projection_name="brain_memory",
        source_watermark="event-1",
        code_version="code-1",
        input_digest="input-1",
    )
    assert retry_run["projection_run_id"] == "run-1"
    assert retry_run["created"] is False
    with pytest.raises(CanonicalV2ConflictError):
        start_projection_run(
            conn,
            projection_run_id="run-1",
            run_kind="projection",
            projection_name="brain_memory",
            source_watermark="event-2",
            code_version="code-1",
            input_digest="input-1",
        )
    mapping = put_legacy_mapping(
        conn,
        legacy_table="legacy_decision",
        legacy_primary_key="decision-2",
        canonical_event_id=event["event_id"],
        canonical_payload_hash=payload.payload_hash,
        classification="fact",
        mapping_confidence="exact",
        migration_run_id="run-1",
    )
    assert mapping["created"] is True
    assert put_legacy_mapping(
        conn,
        legacy_table="legacy_decision",
        legacy_primary_key="decision-2",
        canonical_event_id=event["event_id"],
        canonical_payload_hash=payload.payload_hash,
        classification="fact",
        mapping_confidence="exact",
        migration_run_id="run-1",
    )["created"] is False
    with pytest.raises(CanonicalV2Error):
        put_legacy_mapping(
            conn,
            legacy_table="legacy_unknown",
            legacy_primary_key="row-1",
            classification="quarantine",
            mapping_confidence="unresolved",
            migration_run_id="run-1",
        )
    finish_projection_run(
        conn,
        projection_run_id="run-1",
        status="completed",
        output_digest="output-1",
    )
    finish_projection_run(
        conn,
        projection_run_id="run-1",
        status="completed",
        output_digest="output-1",
    )
    with pytest.raises(CanonicalV2ConflictError):
        finish_projection_run(
            conn,
            projection_run_id="run-1",
            status="completed",
            output_digest="different-output",
        )
    assert conn.execute("SELECT COUNT(*) FROM canonical_v2.training_sample").fetchone()[0] == 1


def test_vertical_decision_to_dataset_lineage_has_one_payload_authority() -> None:
    conn = _canonical_sqlite()
    payloads = {
        kind: put_payload(
            conn,
            {"kind": kind, "value": 1},
            payload_kind=kind,
            schema_version="v1",
        )
        for kind in ("decision", "execution", "position", "review", "label", "trace")
    }
    events = []
    for event_type, entity_type, entity_id, kind in (
        ("risk_decision", "decision", "decision-vertical", "decision"),
        ("broker_execution", "execution", "execution-vertical", "execution"),
        ("position_transition", "position", "position-vertical", "position"),
        ("trade_review", "review", "review-vertical", "review"),
        ("label_observation", "label", "label-vertical", "label"),
        ("training_run", "training", "run-vertical", "trace"),
    ):
        events.append(
            append_event(
                conn,
                event_type=event_type,
                entity_type=entity_type,
                entity_id=entity_id,
                payload_hash=payloads[kind].payload_hash,
                producer="vertical-test",
                idempotency_key=f"{kind}-vertical",
            )
        )

    for from_event, to_event, relation_type in (
        (events[1], events[0], "caused_by"),
        (events[2], events[1], "caused_by"),
        (events[3], events[2], "reviews"),
        (events[4], events[3], "labels"),
        (events[5], events[4], "derived_from"),
    ):
        assert append_relation(
            conn,
            from_event_id=from_event["event_id"],
            to_event_id=to_event["event_id"],
            relation_type=relation_type,
        )

    sample = put_training_sample(
        conn,
        sample_id="sample-vertical",
        sample_type="position-quality",
        source_event_ids=[event["event_id"] for event in events[:5]],
        feature_hash=payloads["trace"].payload_hash,
        feature_schema_hash="feature-schema-vertical",
        label_hash=payloads["label"].payload_hash,
        trace_hash=payloads["trace"].payload_hash,
        evidence_contract={"schema_version": "learning_evidence_contract.v1"},
        config_version=15,
        config_hash="config-vertical",
        horizon_minutes=30,
        target_source="trade_review",
        sample_status="ready",
    )
    manifest = put_dataset_manifest(
        conn,
        dataset_id="dataset-vertical",
        purpose="position-quality",
        training_window="vertical-window",
        horizon_minutes=30,
        query_contract_hash="query-vertical",
        sample_digest="sample-vertical-digest",
        feature_schema_hash="feature-schema-vertical",
        label_contract_hash="label-vertical",
        target_source="trade_review",
        config_hash="config-vertical",
        source_watermark="watermark-vertical",
        code_commit="commit-vertical",
    )
    assert put_dataset_members(
        conn,
        dataset_id=manifest["dataset_id"],
        members=[(sample["sample_id"], 0, "sample-vertical-digest")],
    ) == 1
    assert conn.execute("SELECT COUNT(*) FROM canonical_v2.payload_blob").fetchone()[0] == 6
    assert conn.execute("SELECT COUNT(*) FROM canonical_v2.event").fetchone()[0] == 6
    assert conn.execute("SELECT COUNT(*) FROM canonical_v2.event_relation").fetchone()[0] == 5
    assert conn.execute("SELECT COUNT(*) FROM canonical_v2.training_sample").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM canonical_v2.dataset_manifest_member").fetchone()[0] == 1
