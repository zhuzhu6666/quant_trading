from __future__ import annotations

import sqlite3

from backend.ledger.service import DecisionLedger


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
    try:
        row = conn.execute(
            """SELECT generation, artifact_hash, definition_fingerprint,
                      runtime_selection_fingerprint, config_hash, lineage_status
               FROM decision_factor_snapshot
               WHERE decision_id=? AND factor='candidate_alpha'""",
            (decision_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row == (7, "a" * 64, "d" * 64, "s" * 64, "c" * 64, "bound")


def test_missing_decision_factor_lineage_remains_explicitly_missing(tmp_path):
    db_path = tmp_path / "missing-lineage.sqlite"
    ledger = DecisionLedger(str(db_path))

    decision_id = ledger.log_decision(
        event_type="hold",
        factor_snapshots=[{"factor": "legacy_alpha", "direction": -1}],
    )

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            """SELECT generation, artifact_hash, definition_fingerprint,
                      runtime_selection_fingerprint, config_hash, lineage_status
               FROM decision_factor_snapshot
               WHERE decision_id=? AND factor='legacy_alpha'""",
            (decision_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row == (0, "", "", "", "", "lineage_missing")

