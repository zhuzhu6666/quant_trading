import json
import sqlite3

from backend.core.db import STATE_DB_DDL
from backend.services.position_supervisor_templates import (
    CONSERVATIVE_TEMPLATE_ID,
    DEFAULT_TEMPLATE_ID,
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
