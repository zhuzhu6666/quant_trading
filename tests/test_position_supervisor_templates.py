import json
import sqlite3

import pytest

from backend.core.db import STATE_DB_DDL
from backend.services.position_supervisor_templates import (
    CONSERVATIVE_TEMPLATE_ID,
    DEFAULT_TEMPLATE_ID,
    PROFIT_PROTECTION_TEMPLATE_ID,
    get_position_supervisor_template,
    latest_applied_position_supervisor_template_id,
    list_position_supervisor_templates,
)


def _init_db(path):
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()


def test_latest_applied_position_supervisor_template_defaults_without_application(tmp_path):
    db_path = tmp_path / "state.db"
    _init_db(db_path)

    assert latest_applied_position_supervisor_template_id(db_path=db_path) == DEFAULT_TEMPLATE_ID


def test_latest_applied_position_supervisor_template_restores_recent_valid_application(tmp_path):
    db_path = tmp_path / "state.db"
    _init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO learning_application_log
            (application_id, cycle_ts, scope_type, scope_key, action, status, created_at)
            VALUES ('old_default', 10.0, 'position_supervisor_template', ?, 'switch_position_supervisor_template', 'applied', 10.0)
            """,
            (DEFAULT_TEMPLATE_ID,),
        )
        conn.execute(
            """
            INSERT INTO learning_application_log
            (application_id, cycle_ts, scope_type, scope_key, action, status, created_at)
            VALUES ('new_conservative', 20.0, 'position_supervisor_template', ?, 'switch_position_supervisor_template', 'applied', 20.0)
            """,
            (CONSERVATIVE_TEMPLATE_ID,),
        )
        conn.commit()
    finally:
        conn.close()

    assert latest_applied_position_supervisor_template_id(db_path=db_path) == CONSERVATIVE_TEMPLATE_ID


def test_latest_applied_position_supervisor_template_ignores_rolled_back_application(tmp_path):
    db_path = tmp_path / "state.db"
    _init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO learning_application_log
            (application_id, cycle_ts, scope_type, scope_key, action, status, created_at)
            VALUES ('rolled_back', 30.0, 'position_supervisor_template', ?, 'switch_position_supervisor_template', 'rolled_back', 30.0)
            """,
            (CONSERVATIVE_TEMPLATE_ID,),
        )
        conn.commit()
    finally:
        conn.close()

    assert latest_applied_position_supervisor_template_id(db_path=db_path) == DEFAULT_TEMPLATE_ID


def test_profit_protection_template_uses_longer_thesis_break_evidence_window():
    template = get_position_supervisor_template(PROFIT_PROTECTION_TEMPLATE_ID)

    assert template["thresholds"]["min_thesis_break_seconds"] == 300.0
    assert template["thresholds"]["broken_holding_efficiency_threshold"] == 0.18


def test_generated_template_remains_available_from_learning_application(tmp_path):
    db_path = tmp_path / "state.db"
    _init_db(db_path)
    template_id = "position_supervisor:auto_tpsl.pytest.v1"
    candidate_template = {
        "schema_version": "position_supervisor_template.v1",
        "template_id": template_id,
        "template_version": "auto_tpsl.pytest.v1",
        "template_role": "generated_dynamic_tpsl_capture_repair",
        "base_template_id": "position_supervisor:profit_protection.v1",
        "thresholds": {"min_thesis_break_seconds": 120.0},
        "sl_policy": {"profit_lock_multiplier": 0.82},
        "tp_policy": {"near_take_profit_action": "protect", "extension_enabled": True},
    }
    details = {
        "schema_version": "position_supervisor_template_switch.v1",
        "target_template_id": template_id,
        "evidence": {"candidate_template": candidate_template},
    }
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO learning_application_log
            (application_id, cycle_ts, scope_type, scope_key, action, status, details_json, created_at)
            VALUES ('generated_active', 40.0, 'position_supervisor_template', ?,
                    'switch_position_supervisor_template', 'mixed', ?, 40.0)
            """,
            (template_id, json.dumps(details, ensure_ascii=False)),
        )
        conn.commit()
    finally:
        conn.close()

    templates = {item["template_id"] for item in list_position_supervisor_templates(db_path=db_path)}
    restored = get_position_supervisor_template(template_id, db_path=db_path)

    assert template_id in templates
    assert restored["template_id"] == template_id
    assert restored["thresholds"]["min_thesis_break_seconds"] == 120.0
    assert latest_applied_position_supervisor_template_id(db_path=db_path) == template_id


def test_strict_startup_refuses_uncommitted_legacy_supervisor_application(tmp_path):
    db_path = tmp_path / "state.db"
    _init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO learning_application_log
            (application_id, cycle_ts, scope_type, scope_key, action, status,
             details_json, created_at)
            VALUES ('legacy_unverified', 50.0, 'position_supervisor_template', ?,
                    'switch_position_supervisor_template', 'applied', '{}', 50.0)
            """,
            (CONSERVATIVE_TEMPLATE_ID,),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(RuntimeError, match="legacy_position_supervisor_restore_unverified"):
        latest_applied_position_supervisor_template_id(
            db_path=db_path,
            require_authority=True,
        )


def test_strict_startup_allows_explicit_tightening_legacy_quarantine(tmp_path):
    db_path = tmp_path / "state.db"
    _init_db(db_path)
    details = {
        "governance_authority": "legacy_quarantined",
        "risk_class": "risk_tightening",
    }
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO learning_application_log
            (application_id, cycle_ts, scope_type, scope_key, action, status,
             details_json, created_at)
            VALUES ('legacy_reviewed', 60.0, 'position_supervisor_template', ?,
                    'switch_position_supervisor_template', 'applied', ?, 60.0)
            """,
            (CONSERVATIVE_TEMPLATE_ID, json.dumps(details)),
        )
        conn.commit()
    finally:
        conn.close()

    assert latest_applied_position_supervisor_template_id(
        db_path=db_path,
        require_authority=True,
    ) == CONSERVATIVE_TEMPLATE_ID


def test_strict_startup_accepts_hash_bound_committed_supervisor_application(tmp_path):
    db_path = tmp_path / "state.db"
    _init_db(db_path)
    mutation_id = "gmut_supervisor_committed"
    details = {
        "mutation_id": mutation_id,
        "commit_boundary": "governance_mutation_coordinator",
    }
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE governance_mutation_intent (
                mutation_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                projection_status TEXT NOT NULL,
                scope_type TEXT NOT NULL,
                committed_config_hash TEXT NOT NULL,
                domain_hash TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO governance_mutation_intent
            (mutation_id, status, projection_status, scope_type,
             committed_config_hash, domain_hash)
            VALUES (?, 'committed', 'current', 'supervisor_template',
                    'config-sha256', 'domain-sha256')
            """,
            (mutation_id,),
        )
        conn.execute(
            """
            INSERT INTO learning_application_log
            (application_id, cycle_ts, scope_type, scope_key, action, status,
             details_json, mutation_id, created_at)
            VALUES ('committed_supervisor', 70.0, 'position_supervisor_template', ?,
                    'switch_position_supervisor_template', 'applied', ?, ?, 70.0)
            """,
            (CONSERVATIVE_TEMPLATE_ID, json.dumps(details), mutation_id),
        )
        conn.commit()
    finally:
        conn.close()

    assert latest_applied_position_supervisor_template_id(
        db_path=db_path,
        require_authority=True,
    ) == CONSERVATIVE_TEMPLATE_ID


def test_applied_generated_snapshot_wins_over_same_id_uncommitted_suggestion(tmp_path):
    db_path = tmp_path / "state.db"
    _init_db(db_path)
    template_id = "position_supervisor:snapshot-priority.v1"
    applied_snapshot = {
        "template_id": template_id,
        "template_version": "snapshot-priority.v1",
        "base_template_id": PROFIT_PROTECTION_TEMPLATE_ID,
        "thresholds": {"min_thesis_break_seconds": 180.0},
    }
    suggestion_snapshot = {
        **applied_snapshot,
        "thresholds": {"min_thesis_break_seconds": 1.0},
    }
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO learning_application_log
            (application_id, cycle_ts, scope_type, scope_key, action, status,
             details_json, created_at)
            VALUES ('snapshot_applied', 80.0, 'position_supervisor_template', ?,
                    'switch_position_supervisor_template', 'applied', ?, 80.0)
            """,
            (
                template_id,
                json.dumps({"template_snapshot": applied_snapshot}),
            ),
        )
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence, reason,
             evidence_json, status, created_at)
            VALUES ('newer_uncommitted', 'position_supervisor_template', ?,
                    'switch_position_supervisor_template', 0.9, 'test', ?,
                    'approved', 90.0)
            """,
            (
                template_id,
                json.dumps({"template_snapshot": suggestion_snapshot}),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    restored = get_position_supervisor_template(template_id, db_path=db_path)

    assert restored["thresholds"]["min_thesis_break_seconds"] == 180.0
