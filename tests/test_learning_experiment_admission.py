import json
import time

from backend.core.db import STATE_DB_DDL, connect_sqlite
from backend.services.experience_prior import ExperiencePriorService
from backend.services.learning_experiment_admission import LearningExperimentAdmissionService


def _db(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    conn.executescript(STATE_DB_DDL)
    conn.commit()
    conn.close()
    return db_path


def test_admission_blocks_active_scope_and_immaterial_weight_delta(tmp_path):
    db_path = _db(tmp_path)
    now = time.time()
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            """
            INSERT INTO learning_application_log
            (application_id, cycle_ts, scope_type, scope_key, action, status, created_at)
            VALUES ('app_active', ?, 'factor', 'rsi_14', 'update_weight', 'applied', ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO learning_application_effect
            (application_id, scope_type, scope_key, action, status, updated_at, created_at)
            VALUES ('app_active', 'factor', 'rsi_14', 'update_weight', 'observing', ?, ?)
            """,
            (now, now),
        )
        conn.commit()
    finally:
        conn.close()

    service = LearningExperimentAdmissionService(db_path)
    blocked = service.evaluate(
        scope_type="factor", scope_key="rsi_14", action="update_weight", old_weight=0.10, new_weight=0.08
    )
    assert blocked["allowed"] is False
    assert blocked["status"] == "blocked_active_experiment"

    conn = connect_sqlite(db_path)
    try:
        conn.execute("UPDATE learning_application_log SET status='reinforced' WHERE application_id='app_active'")
        conn.execute("UPDATE learning_application_effect SET status='reinforced' WHERE application_id='app_active'")
        conn.commit()
    finally:
        conn.close()

    immaterial = service.evaluate(
        scope_type="factor", scope_key="rsi_14", action="update_weight", old_weight=0.10, new_weight=0.099
    )
    admitted = service.evaluate(
        scope_type="factor", scope_key="rsi_14", action="update_weight", old_weight=0.10, new_weight=0.09
    )
    structural = service.evaluate(scope_type="factor", scope_key="redundancy", action="update_redundancy_groups")
    assert immaterial["status"] == "blocked_immaterial_delta"
    assert admitted["allowed"] is True
    assert structural["effect_tracking"] == "not_trade_attributed"


def test_experience_prior_uses_only_terminal_bounded_effects(tmp_path):
    db_path = _db(tmp_path)
    now = time.time()
    bounded = {
        "evidence_quality": {
            "bounded_attribution_allowed": True,
            "target_regime": "trend",
        }
    }
    unbounded = {"evidence_quality": {"bounded_attribution_allowed": False}}
    conn = connect_sqlite(db_path)
    try:
        conn.executemany(
            """
            INSERT INTO learning_application_effect
            (application_id, scope_type, scope_key, action, status, observed_trade_count,
             delta_avg_reward, decision_json, updated_at, created_at)
            VALUES (?, 'factor', ?, 'update_weight', ?, ?, ?, ?, ?, ?)
            """,
            [
                ("bounded", "rsi_14", "reinforced", 5, 0.2, json.dumps(bounded), now, now),
                ("unbounded", "macd_hist", "ineffective", 10, -0.2, json.dumps(unbounded), now, now),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    result = ExperiencePriorService(db_path).build(cache_seconds=0.0)
    assert result["eligible_count"] == 1
    assert result["rejected_unbounded_count"] == 1
    assert result["priors"]["rsi_14"]["multiplier"] == 1.1
    assert result["priors"]["rsi_14"]["confidence"] >= 0.6
    assert result["boundary"]["decision_policy_remains_authority"] is True


def test_admission_enforces_global_active_experiment_budget(tmp_path):
    db_path = _db(tmp_path)
    now = time.time()
    conn = connect_sqlite(db_path)
    try:
        conn.executemany(
            """
            INSERT INTO learning_application_log
            (application_id, cycle_ts, scope_type, scope_key, action, status, created_at)
            VALUES (?, ?, 'factor', ?, 'update_weight', 'applied', ?)
            """,
            [("active_1", now, "factor_a", now), ("active_2", now, "factor_b", now)],
        )
        conn.commit()
    finally:
        conn.close()

    service = LearningExperimentAdmissionService(db_path)
    result = service.evaluate(
        scope_type="factor",
        scope_key="factor_c",
        action="update_weight",
        old_weight=0.1,
        new_weight=0.08,
        max_global_active_experiments=2,
    )

    assert result["allowed"] is False
    assert result["status"] == "blocked_global_experiment_budget"
    assert result["global_active_count"] == 2
