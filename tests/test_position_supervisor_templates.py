import json
import sqlite3

import pytest

from backend.core.db import STATE_DB_DDL
from backend.services.learning_application_store import LearningApplicationStore
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


def _prepare_app(
    path,
    *,
    scope_key,
    status,
    cycle_ts,
    details=None,
    mutation_id="",
):
    return LearningApplicationStore(str(path)).prepare_application(
        scope_type="position_supervisor_template",
        scope_key=scope_key,
        action="switch_position_supervisor_template",
        status=status,
        cycle_ts=cycle_ts,
        mutation_id=mutation_id,
        details=details,
    )


def test_latest_applied_position_supervisor_template_defaults_without_application(tmp_path):
    db_path = tmp_path / "state.db"
    _init_db(db_path)

    assert latest_applied_position_supervisor_template_id(db_path=db_path) == DEFAULT_TEMPLATE_ID


def test_latest_applied_position_supervisor_template_restores_recent_valid_application(tmp_path):
    db_path = tmp_path / "state.db"
    _init_db(db_path)
    _prepare_app(
        db_path, scope_key=DEFAULT_TEMPLATE_ID, status="applied", cycle_ts=10.0
    )
    _prepare_app(
        db_path, scope_key=CONSERVATIVE_TEMPLATE_ID, status="applied", cycle_ts=20.0
    )

    assert latest_applied_position_supervisor_template_id(db_path=db_path) == CONSERVATIVE_TEMPLATE_ID


def test_latest_applied_position_supervisor_template_ignores_rolled_back_application(tmp_path):
    db_path = tmp_path / "state.db"
    _init_db(db_path)
    _prepare_app(
        db_path, scope_key=CONSERVATIVE_TEMPLATE_ID, status="rolled_back", cycle_ts=30.0
    )

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
    _prepare_app(
        db_path,
        scope_key=template_id,
        status="mixed",
        cycle_ts=40.0,
        details=details,
    )

    templates = {item["template_id"] for item in list_position_supervisor_templates(db_path=db_path)}
    restored = get_position_supervisor_template(template_id, db_path=db_path)

    assert template_id in templates
    assert restored["template_id"] == template_id
    assert restored["thresholds"]["min_thesis_break_seconds"] == 120.0
    assert latest_applied_position_supervisor_template_id(db_path=db_path) == template_id


def test_strict_startup_refuses_uncommitted_legacy_supervisor_application(tmp_path):
    db_path = tmp_path / "state.db"
    _init_db(db_path)
    _prepare_app(
        db_path,
        scope_key=CONSERVATIVE_TEMPLATE_ID,
        status="applied",
        cycle_ts=50.0,
        details={},
    )

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
    _prepare_app(
        db_path,
        scope_key=CONSERVATIVE_TEMPLATE_ID,
        status="applied",
        cycle_ts=60.0,
        details=details,
    )

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
        conn.commit()
    finally:
        conn.close()
    _prepare_app(
        db_path,
        scope_key=CONSERVATIVE_TEMPLATE_ID,
        status="applied",
        cycle_ts=70.0,
        details=details,
        mutation_id=mutation_id,
    )

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
        _prepare_app(
            db_path,
            scope_key=template_id,
            status="applied",
            cycle_ts=80.0,
            details={"template_snapshot": applied_snapshot},
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
