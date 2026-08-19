"""Shared canonical_v2 SQLite fixtures for the R5 canonicalisation.

``backend.services.canonical_v2._sql`` rewrites the ``canonical_v2.`` schema
prefix to bare table names on SQLite (PG keeps the prefix), so the canonical
tables below are created directly in the main database — no ``ATTACH`` needed,
and every fresh business connection re-resolves them unchanged.

Mirrors ``migrations/state_pg/0016`` + ``0017`` with SQLite-appropriate types.
The historical ``legacy_mapping`` table (dropped in S5, read/write paths removed
in R1) is intentionally not created here.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

CANONICAL_V2_BARE_DDL = """
CREATE TABLE IF NOT EXISTS payload_blob (
    payload_hash TEXT PRIMARY KEY,
    payload_kind TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    canonical_bytes BLOB NOT NULL,
    codec TEXT NOT NULL DEFAULT 'gzip',
    raw_sha256 TEXT NOT NULL,
    raw_bytes INTEGER NOT NULL,
    compressed_bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    producer TEXT NOT NULL,
    producer_version TEXT NOT NULL DEFAULT '',
    schema_version TEXT NOT NULL,
    correlation_id TEXT NOT NULL DEFAULT '',
    causation_id TEXT NOT NULL DEFAULT '',
    parent_event_id TEXT,
    idempotency_key TEXT NOT NULL DEFAULT '',
    payload_hash TEXT NOT NULL REFERENCES payload_blob(payload_hash),
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS event_idempotency
    ON event(producer, idempotency_key) WHERE idempotency_key <> '';
CREATE TABLE IF NOT EXISTS event_relation (
    from_event_id TEXT NOT NULL REFERENCES event(event_id),
    to_event_id TEXT NOT NULL REFERENCES event(event_id),
    relation_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (from_event_id, to_event_id, relation_type)
);
CREATE TABLE IF NOT EXISTS state_version (
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
CREATE TABLE IF NOT EXISTS training_sample (
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
CREATE TABLE IF NOT EXISTS dataset_manifest (
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
CREATE TABLE IF NOT EXISTS dataset_manifest_member (
    dataset_id TEXT NOT NULL REFERENCES dataset_manifest(dataset_id),
    sample_id TEXT NOT NULL REFERENCES training_sample(sample_id),
    sample_order INTEGER NOT NULL,
    sample_digest TEXT NOT NULL,
    PRIMARY KEY (dataset_id, sample_id),
    UNIQUE (dataset_id, sample_order)
);
CREATE TABLE IF NOT EXISTS projection_run (
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
CREATE UNIQUE INDEX IF NOT EXISTS projection_run_identity
    ON projection_run(run_kind, projection_name, source_watermark, code_version, input_digest);
CREATE TABLE IF NOT EXISTS training_sample_row (
    sample_id TEXT PRIMARY KEY,
    sample_type TEXT NOT NULL DEFAULT '',
    source_table TEXT NOT NULL DEFAULT '',
    source_id TEXT NOT NULL DEFAULT '',
    decision_id TEXT NOT NULL DEFAULT '',
    trade_id TEXT NOT NULL DEFAULT '',
    position_id TEXT NOT NULL DEFAULT '',
    symbol TEXT NOT NULL DEFAULT '',
    timeframe TEXT NOT NULL DEFAULT '',
    event_ts REAL,
    label_status TEXT NOT NULL DEFAULT '',
    integrity TEXT NOT NULL DEFAULT '',
    train_weight REAL NOT NULL DEFAULT 1.0,
    features_json TEXT NOT NULL DEFAULT '{}',
    verdict_json TEXT NOT NULL DEFAULT '{}',
    label_json TEXT NOT NULL DEFAULT '{}',
    trace_json TEXT NOT NULL DEFAULT '{}',
    evidence_contract_json TEXT NOT NULL DEFAULT '{}',
    config_version INTEGER NOT NULL DEFAULT 0,
    config_hash TEXT NOT NULL DEFAULT '',
    evolution_run_id TEXT NOT NULL DEFAULT '',
    system_contaminated INTEGER NOT NULL DEFAULT 0,
    governance_eligible INTEGER NOT NULL DEFAULT 0,
    governance_effective_weight REAL NOT NULL DEFAULT 1.0,
    governance_eligibility_version TEXT NOT NULL DEFAULT '',
    governance_ineligible_reason TEXT NOT NULL DEFAULT '',
    governance_eligibility_fingerprint TEXT NOT NULL DEFAULT '',
    content_fingerprint TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tsr_sample_type_status
    ON training_sample_row(sample_type, label_status, governance_eligible);
CREATE INDEX IF NOT EXISTS idx_tsr_decision ON training_sample_row(decision_id);
CREATE INDEX IF NOT EXISTS idx_tsr_fingerprint
    ON training_sample_row(content_fingerprint, updated_at);
CREATE INDEX IF NOT EXISTS idx_tsr_event_ts ON training_sample_row(event_ts);
"""

# Only the training-sample-row table (bare). Used by fixtures that keep legacy
# decision/review tables while the sample domain is canonical-only.
TRAINING_SAMPLE_ROW_DDL = """
CREATE TABLE IF NOT EXISTS training_sample_row (
    sample_id TEXT PRIMARY KEY,
    sample_type TEXT NOT NULL DEFAULT '',
    source_table TEXT NOT NULL DEFAULT '',
    source_id TEXT NOT NULL DEFAULT '',
    decision_id TEXT NOT NULL DEFAULT '',
    trade_id TEXT NOT NULL DEFAULT '',
    position_id TEXT NOT NULL DEFAULT '',
    symbol TEXT NOT NULL DEFAULT '',
    timeframe TEXT NOT NULL DEFAULT '',
    event_ts REAL,
    label_status TEXT NOT NULL DEFAULT '',
    integrity TEXT NOT NULL DEFAULT '',
    train_weight REAL NOT NULL DEFAULT 1.0,
    features_json TEXT NOT NULL DEFAULT '{}',
    verdict_json TEXT NOT NULL DEFAULT '{}',
    label_json TEXT NOT NULL DEFAULT '{}',
    trace_json TEXT NOT NULL DEFAULT '{}',
    evidence_contract_json TEXT NOT NULL DEFAULT '{}',
    config_version INTEGER NOT NULL DEFAULT 0,
    config_hash TEXT NOT NULL DEFAULT '',
    evolution_run_id TEXT NOT NULL DEFAULT '',
    system_contaminated INTEGER NOT NULL DEFAULT 0,
    governance_eligible INTEGER NOT NULL DEFAULT 0,
    governance_effective_weight REAL NOT NULL DEFAULT 1.0,
    governance_eligibility_version TEXT NOT NULL DEFAULT '',
    governance_ineligible_reason TEXT NOT NULL DEFAULT '',
    governance_eligibility_fingerprint TEXT NOT NULL DEFAULT '',
    content_fingerprint TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tsr_sample_type_status
    ON training_sample_row(sample_type, label_status, governance_eligible);
CREATE INDEX IF NOT EXISTS idx_tsr_decision ON training_sample_row(decision_id);
CREATE INDEX IF NOT EXISTS idx_tsr_fingerprint
    ON training_sample_row(content_fingerprint, updated_at);
CREATE INDEX IF NOT EXISTS idx_tsr_event_ts ON training_sample_row(event_ts);
"""


def make_canonical_sqlite(path: str | Path | None = None) -> sqlite3.Connection:
    """Open an SQLite connection whose main database also holds the canonical
    tables (bare names). Pass ``path`` to persist to a file."""
    conn = sqlite3.connect(str(path) if path is not None else ":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(CANONICAL_V2_BARE_DDL)
    return conn


def seed_canonical_sqlite_file(path: str | Path) -> None:
    """Create bare canonical tables inside an existing SQLite state DB file."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(CANONICAL_V2_BARE_DDL)
    conn.commit()
    conn.close()


def create_training_sample_row_tables(conn: sqlite3.Connection) -> None:
    """Create the bare canonical training_sample_row tables on a live connection."""
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(TRAINING_SAMPLE_ROW_DDL)


def ensure_training_sample_row_sqlite(path: str | Path) -> None:
    """Create ONLY the bare canonical training_sample_row table (sample domain).

    Used by fixtures that keep legacy decision/review tables (so
    ``_canonical_ready`` stays False) while the sample domain is already
    canonical-only. Use ``create_training_sample_row_tables(conn)`` when a
    connection is already open and holds an uncommitted transaction.
    """
    conn = sqlite3.connect(str(path))
    try:
        create_training_sample_row_tables(conn)
        conn.commit()
    finally:
        conn.close()
