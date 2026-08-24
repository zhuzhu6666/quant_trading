"""Shared canonical_v2 SQLite fixtures for the R5 canonicalisation.

``backend.services.canonical_v2._sql`` rewrites the ``canonical_v2.`` schema
prefix to bare table names on SQLite (PG keeps the prefix), so the canonical
tables below are created directly in the main database — no ``ATTACH`` needed,
and every fresh business connection re-resolves them unchanged.

The full fixture intentionally reuses the production SQLite DDL.  The
historical ``legacy_mapping`` table (dropped in S5, read/write paths removed in
R1) is not part of that contract.  ``TRAINING_SAMPLE_ROW_DDL`` is the deliberate
single-table subset for fixtures that retain legacy decision/review tables.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from backend.services.canonical_v2 import CANONICAL_SQLITE_DDL


# Compatibility name retained for existing test imports.  There is one source
# of truth for the full canonical SQLite schema: backend.services.canonical_v2.
CANONICAL_V2_BARE_DDL = CANONICAL_SQLITE_DDL


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
