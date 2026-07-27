from __future__ import annotations

import sqlite3

from backend.services.live_committed_policy import load_live_policy_controls


def _connection(tmp_path):
    conn = sqlite3.connect(tmp_path / "state.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE policy_suggestion (
            suggestion_id TEXT PRIMARY KEY,
            scope_type TEXT,
            scope_key TEXT,
            action TEXT,
            confidence REAL,
            reason TEXT,
            evidence_json TEXT,
            status TEXT,
            reviewed_at REAL,
            created_at REAL,
            applied_mutation_id TEXT DEFAULT ''
        );
        CREATE TABLE governance_mutation_intent (
            mutation_id TEXT PRIMARY KEY,
            status TEXT
        );
        """
    )
    rows = [
        ("approved_only", "approved", "", 40.0),
        ("legacy_applied", "applied", "", 30.0),
        ("committed_applied", "applied", "mut_committed", 20.0),
        ("dangling_applied", "applied", "mut_prepared", 10.0),
    ]
    conn.executemany(
        """
        INSERT INTO policy_suggestion
        (suggestion_id, scope_type, scope_key, action, confidence, reason,
         evidence_json, status, reviewed_at, created_at, applied_mutation_id)
        VALUES (?, 'entry_quality', 'weak_signal', 'raise_weak_signal_threshold',
                0.8, 'test', '{}', ?, ?, ?, ?)
        """,
        [(sid, status, created, created, mutation_id) for sid, status, mutation_id, created in rows],
    )
    conn.executemany(
        "INSERT INTO governance_mutation_intent (mutation_id, status) VALUES (?, ?)",
        [("mut_committed", "committed"), ("mut_prepared", "prepared")],
    )
    conn.commit()
    return conn


def test_live_never_consumes_approved_and_dual_mode_quarantines_legacy(tmp_path) -> None:
    conn = _connection(tmp_path)
    try:
        controls = load_live_policy_controls(
            conn,
            scope_type="entry_quality",
            allowed_actions={"raise_weak_signal_threshold"},
            limit=20,
            coordinator_mode="dual_record",
        )
    finally:
        conn.close()

    by_id = {item["suggestion_id"]: item for item in controls}
    assert set(by_id) == {"legacy_applied", "committed_applied"}
    assert "approved_only" not in by_id
    assert "dangling_applied" not in by_id
    assert by_id["legacy_applied"]["governance_authority"] == "legacy_quarantined"
    assert by_id["committed_applied"]["governance_authority"] == "committed_mutation"


def test_enforce_mode_consumes_only_applied_committed_mutation(tmp_path) -> None:
    conn = _connection(tmp_path)
    try:
        controls = load_live_policy_controls(
            conn,
            scope_type="entry_quality",
            allowed_actions={"raise_weak_signal_threshold"},
            limit=20,
            coordinator_mode="enforce",
        )
    finally:
        conn.close()

    assert [item["suggestion_id"] for item in controls] == ["committed_applied"]
    assert controls[0]["committed_mutation_id"] == "mut_committed"


def test_missing_mutation_columns_fail_closed_in_enforce_mode(tmp_path) -> None:
    conn = sqlite3.connect(tmp_path / "legacy.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE policy_suggestion (
            suggestion_id TEXT PRIMARY KEY,
            scope_type TEXT,
            scope_key TEXT,
            action TEXT,
            confidence REAL,
            reason TEXT,
            evidence_json TEXT,
            status TEXT,
            reviewed_at REAL,
            created_at REAL
        );
        INSERT INTO policy_suggestion VALUES
        ('legacy', 'entry_cluster', 'same_direction_ge_1',
         'increase_same_direction_cooldown', 0.8, 'legacy', '{}', 'applied', 2, 1);
        """
    )
    try:
        controls = load_live_policy_controls(
            conn,
            scope_type="entry_cluster",
            allowed_actions={"increase_same_direction_cooldown"},
            limit=20,
            coordinator_mode="enforce",
        )
    finally:
        conn.close()
    assert controls == []


def test_dual_mode_legacy_compatibility_accepts_only_declared_tightening(tmp_path) -> None:
    conn = sqlite3.connect(tmp_path / "mixed-actions.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE policy_suggestion (
            suggestion_id TEXT PRIMARY KEY,
            scope_type TEXT,
            scope_key TEXT,
            action TEXT,
            confidence REAL,
            reason TEXT,
            evidence_json TEXT,
            status TEXT,
            reviewed_at REAL,
            created_at REAL,
            applied_mutation_id TEXT DEFAULT ''
        );
        INSERT INTO policy_suggestion VALUES
        ('legacy_down', 'factor', 'foo', 'downweight', 0.8, '', '{}', 'applied', 2, 2, ''),
        ('legacy_boost', 'factor', 'bar', 'boost_small', 0.8, '', '{}', 'applied', 3, 3, '');
        """
    )
    try:
        controls = load_live_policy_controls(
            conn,
            scope_type="factor",
            allowed_actions={"downweight", "boost_small"},
            legacy_tightening_actions={"downweight"},
            limit=20,
            coordinator_mode="dual_record",
        )
    finally:
        conn.close()

    assert [item["suggestion_id"] for item in controls] == ["legacy_down"]
    assert controls[0]["governance_authority"] == "legacy_quarantined"


def test_terminal_application_cannot_leave_applied_control_live(tmp_path) -> None:
    conn = _connection(tmp_path)
    conn.executescript(
        """
        CREATE TABLE learning_application_log (
            application_id TEXT PRIMARY KEY,
            scope_type TEXT,
            suggestion_ids_json TEXT,
            status TEXT
        );
        INSERT INTO learning_application_log VALUES
        ('app_old', 'entry_quality', '["committed_applied"]', 'superseded');
        """
    )
    try:
        controls = load_live_policy_controls(
            conn,
            scope_type="entry_quality",
            allowed_actions={"raise_weak_signal_threshold"},
            limit=20,
            coordinator_mode="enforce",
        )
    finally:
        conn.close()

    assert controls == []
