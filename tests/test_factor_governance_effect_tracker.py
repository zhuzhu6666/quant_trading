import json
import time

from backend.core.db import STATE_DB_DDL, connect_sqlite
from backend.services.factor_governance_effect_tracker import FactorGovernanceEffectTrackerService
from backend.services.learning_application_store import LearningApplicationStore


def _insert_pruning_suggestion(conn, *, suggestion_id="brain_bridge_effect", factor="dsl_auto_effect", status="approved"):
    now = time.time()
    evidence = {
        "schema_version": "brain_governance_candidate_policy_suggestion_evidence.v1",
        "source_agent": "factor_pruning_governance",
        "source_kind": "factor_pruning_candidate_materializer",
        "risk_verdict": {"allowed": True},
        "decision_policy_preview": {"required": True, "decision": {"old_weight": 0.01, "new_weight": 0.0}},
        "rollback_plan": {"restore_weight": 0.01},
    }
    conn.execute(
        """
        INSERT INTO policy_suggestion
        (suggestion_id, scope_type, scope_key, action, confidence, reason,
         evidence_json, status, reviewed_at, review_note, created_at)
        VALUES (?, 'factor', ?, 'downweight', 0.9, 'pruning evidence',
                ?, ?, ?, 'approved by test', ?)
        """,
        (suggestion_id, factor, json.dumps(evidence), status, now, now),
    )
    return now


def _seed_observing_application(db_path, suggestion_id, factor, status, cycle_ts):
    store = LearningApplicationStore(db_path)
    app_id = store.prepare_application(
        scope_type="factor", scope_key=factor, action="downweight",
        bias_multiplier=0.82, old_weight=0.01, new_weight=0.0082,
        suggestion_ids=[suggestion_id], status=status, cycle_ts=cycle_ts,
    )
    store.write_effect(
        application_id=app_id, scope_key=factor, scope_type="factor",
        action="downweight", status="observing",
        observed_trade_count=0, baseline_trade_count=0, decision={},
        updated_at=cycle_ts,
    )
    return app_id


def test_factor_governance_effect_tracker_reports_observing_application(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        now = _insert_pruning_suggestion(conn)
        conn.commit()
    finally:
        conn.close()
    _seed_observing_application(db_path, "brain_bridge_effect", "dsl_auto_effect", "observing", now + 1)

    result = FactorGovernanceEffectTrackerService(db_path).status()

    assert result["schema_version"] == "factor_governance_effect_tracker.v1"
    assert result["item_count"] == 1
    item = result["items"][0]
    assert item["stage"] == "observing"
    assert item["recommended_action"] == "collect_more_trades"
    assert item["application"]["application_id"]
    assert item["effect"]["status"] == "observing"
    assert item["evidence_contract"]["has_risk_verdict"] is True


def test_factor_governance_effect_tracker_reconcile_marks_ineffective(tmp_path):
    db_path = tmp_path / "state.db"
    now = time.time()
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        _insert_pruning_suggestion(conn)
        for idx in range(2):
            conn.execute(
                """
                INSERT INTO trade_outcome_review
                (review_id, trade_id, entry_decision_id, pnl, outcome_label, review_json, created_at)
                VALUES (?, ?, ?, 30.0, 'good_win', '{}', ?)
                """,
                (f"pre_{idx}", f"trade_pre_{idx}", f"entry_pre_{idx}", now - 20 + idx),
            )
            conn.execute(
                """
                INSERT INTO decision_factor_snapshot
                (decision_id, factor, contribution_score)
                VALUES (?, 'dsl_auto_effect', 0.5)
                """,
                (f"entry_pre_{idx}",),
            )
        for idx in range(3):
            conn.execute(
                """
                INSERT INTO trade_outcome_review
                (review_id, trade_id, entry_decision_id, pnl, outcome_label, review_json, created_at)
                VALUES (?, ?, ?, -40.0, 'bad_loss', '{}', ?)
                """,
                (f"post_{idx}", f"trade_post_{idx}", f"entry_post_{idx}", now + 20 + idx),
            )
            conn.execute(
                """
                INSERT INTO decision_factor_snapshot
                (decision_id, factor, contribution_score)
                VALUES (?, 'dsl_auto_effect', -0.5)
                """,
                (f"entry_post_{idx}",),
            )
        conn.commit()
    finally:
        conn.close()
    _seed_observing_application(db_path, "brain_bridge_effect", "dsl_auto_effect", "observing", now)

    result = FactorGovernanceEffectTrackerService(db_path).reconcile(limit=10)

    assert result["governor_result"]["rolled_back"] == 1
    item = result["effect_status"]["items"][0]
    assert item["stage"] == "rolled_back"
    assert item["recommended_action"] == "watch_after_rollback"
    conn = connect_sqlite(db_path)
    try:
        suggestion = conn.execute(
            "SELECT status FROM policy_suggestion WHERE suggestion_id='brain_bridge_effect'"
        ).fetchone()
    finally:
        conn.close()
    assert suggestion[0] == "rolled_back"
