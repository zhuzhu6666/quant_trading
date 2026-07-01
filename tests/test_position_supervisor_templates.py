import sqlite3

from backend.core.db import STATE_DB_DDL
from backend.services.position_supervisor_templates import (
    CONSERVATIVE_TEMPLATE_ID,
    DEFAULT_TEMPLATE_ID,
    latest_applied_position_supervisor_template_id,
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
