from __future__ import annotations

import sqlite3

from backend.ledger.service import DecisionLedger
from backend.services.canonical_v2_reader import iter_decision_factor_snapshots


def test_new_decision_factor_snapshot_binds_full_runtime_lineage(tmp_path):
    db_path = tmp_path / "lineage.sqlite"
    ledger = DecisionLedger(str(db_path))
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """CREATE TABLE factor_lifecycle_state (
                   factor_name TEXT PRIMARY KEY,
                   generation INTEGER NOT NULL,
                   artifact_hash TEXT NOT NULL,
                   definition_fingerprint TEXT NOT NULL
               )"""
        )
        conn.execute(
            """INSERT INTO factor_lifecycle_state
               (factor_name, generation, artifact_hash, definition_fingerprint)
               VALUES ('candidate_alpha', 7, ?, ?)""",
            ("a" * 64, "d" * 64),
        )
        conn.commit()
    finally:
        conn.close()

    decision_id = ledger.log_decision(
        event_type="open",
        symbol="XAUUSD+",
        timeframe="M5",
        factor_snapshots=[{"factor": "candidate_alpha", "direction": 1}],
        runtime_selection_fingerprint="s" * 64,
        config_hash="c" * 64,
    )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = next(
            snapshot
            for snapshot in iter_decision_factor_snapshots(conn, decision_id)
            if snapshot["factor"] == "candidate_alpha"
        )
    finally:
        conn.close()
    assert row["generation"] == 7
    assert row["artifact_hash"] == "a" * 64
    assert row["definition_fingerprint"] == "d" * 64
    assert row["runtime_selection_fingerprint"] == "s" * 64
    assert row["config_hash"] == "c" * 64
    assert row["lineage_status"] == "bound"


def test_missing_decision_factor_lineage_remains_explicitly_missing(tmp_path):
    db_path = tmp_path / "missing-lineage.sqlite"
    ledger = DecisionLedger(str(db_path))

    decision_id = ledger.log_decision(
        event_type="hold",
        factor_snapshots=[{"factor": "legacy_alpha", "direction": -1}],
    )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = next(
            snapshot
            for snapshot in iter_decision_factor_snapshots(conn, decision_id)
            if snapshot["factor"] == "legacy_alpha"
        )
    finally:
        conn.close()
    assert row["generation"] == 0
    assert row["artifact_hash"] == ""
    assert row["definition_fingerprint"] == ""
    assert row["runtime_selection_fingerprint"] == ""
    assert row["config_hash"] == ""
    assert row["lineage_status"] == "lineage_missing"
