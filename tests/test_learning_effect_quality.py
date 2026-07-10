import json
import time

from backend.core.db import STATE_DB_DDL, connect_sqlite
from backend.services.learning_effect_quality import LearningEffectQualityService


def test_learning_effect_quality_requires_new_evidence_for_retry(tmp_path):
    db_path = tmp_path / "state.db"
    now = time.time()
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO learning_application_log
            (application_id, cycle_ts, scope_type, scope_key, action, status, created_at)
            VALUES ('app1', ?, 'factor', 'rsi_14', 'downweight', 'inconclusive', ?)
            """,
            (now - 100, now - 100),
        )
        conn.execute(
            """
            INSERT INTO learning_application_effect
            (application_id, scope_type, scope_key, action, status, decision_json, updated_at, created_at)
            VALUES ('app1', 'factor', 'rsi_14', 'downweight', 'inconclusive', ?, ?, ?)
            """,
            (json.dumps({"evidence_quality": {"causal_status": "bounded_window_insufficient_samples", "retry_via_new_application": True}}), now, now - 100),
        )
        conn.execute("INSERT INTO decision_factor_snapshot (decision_id, factor) VALUES ('d1', 'rsi_14')")
        conn.execute(
            "INSERT INTO trade_outcome_review (review_id, entry_decision_id, created_at) VALUES ('r1', 'd1', ?)",
            (now + 10,),
        )
        conn.commit()
    finally:
        conn.close()

    status = LearningEffectQualityService(db_path).status()
    assert status["status_counts"]["inconclusive"] == 1
    assert status["reason_counts"]["bounded_window_insufficient_samples"] == 1
    assert status["retry_review_count"] == 1
    assert status["retry_candidate_count"] == 1
    assert status["retry_candidates"][0]["retry_eligible"] is True
    assert status["boundary"]["does_not_create_application"] is True

    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            """
            INSERT INTO learning_application_log
            (application_id, cycle_ts, scope_type, scope_key, action, status, created_at)
            VALUES ('app1_newer', ?, 'factor', 'rsi_14', 'downweight', 'applied', ?)
            """,
            (now + 1, now + 1),
        )
        conn.commit()
    finally:
        conn.close()

    blocked = LearningEffectQualityService(db_path).status()
    assert blocked["retry_candidate_count"] == 0
    assert blocked["retry_reviews"][0]["eligibility_reason"] == "newer_application_already_exists"


def test_learning_effect_quality_reports_concurrent_backlog_as_degraded(tmp_path):
    db_path = tmp_path / "state.db"
    now = time.time()
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO learning_application_effect
            (application_id, scope_type, scope_key, action, status, decision_json, updated_at, created_at)
            VALUES ('app2', 'factor', 'ema_slope', 'downweight', 'observing', ?, ?, ?)
            """,
            (json.dumps({"evidence_quality": {"causal_status": "confounded_by_concurrent_application"}}), now, now),
        )
        conn.commit()
    finally:
        conn.close()

    status = LearningEffectQualityService(db_path).status()
    assert status["status"] == "degraded"
    assert status["slo"]["checks"]["no_concurrent_attribution_backlog"] is False


def test_learning_effect_quality_rejects_bounded_reason_left_nonterminal(tmp_path):
    db_path = tmp_path / "state.db"
    now = time.time()
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO learning_application_effect
            (application_id, scope_type, scope_key, action, status, decision_json, updated_at, created_at)
            VALUES ('app3', 'factor', 'macd_hist', 'downweight', 'observing', ?, ?, ?)
            """,
            (json.dumps({"evidence_quality": {"causal_status": "bounded_window_insufficient_samples"}}), now, now),
        )
        conn.commit()
    finally:
        conn.close()

    status = LearningEffectQualityService(db_path).status()
    assert status["bounded_nonterminal_count"] == 1
    assert status["slo"]["checks"]["bounded_windows_terminalize"] is False
    assert status["status"] == "degraded"
