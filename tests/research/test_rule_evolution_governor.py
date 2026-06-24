from __future__ import annotations

import sqlite3

from research.learning.governor import RuleEvolutionGovernor


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def test_governor_reviews_pending_and_rolls_back(tmp_path):
    db_path = str(tmp_path / "state.db")
    gov = RuleEvolutionGovernor(db_path)

    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO experience_pattern_stats
            (scope_type, scope_key, sample_count, win_count, bad_loss_count,
             avg_reward, last_outcome_label, recommended_action, updated_at)
            VALUES
            ('factor', 'fragile_factor', 4, 1, 3, -0.45, 'bad_loss', 'downweight', 1.0),
            ('factor', 'strong_factor', 5, 4, 0, 0.32, 'good_win', 'boost_small', 1.0)
            """
        )
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence, reason, status, created_at)
            VALUES
            ('p1', 'factor', 'fragile_factor', 'downweight', 0.8, 'test', 'proposed', 1.0),
            ('p2', 'factor', 'strong_factor', 'boost_small', 0.7, 'test', 'proposed', 1.0)
            """
        )
        conn.commit()
    finally:
        conn.close()

    reviewed = gov.review_pending()
    assert reviewed["approved"] == 2

    items = gov.list_suggestions(status="approved")
    assert {i["scope_key"] for i in items} == {"fragile_factor", "strong_factor"}

    conn = _connect(db_path)
    try:
        conn.execute(
            """
            UPDATE experience_pattern_stats
            SET sample_count=6, avg_reward=0.20
            WHERE scope_key='fragile_factor'
            """
        )
        conn.commit()
    finally:
        conn.close()

    reconciled = gov.reconcile_active()
    assert reconciled["rolled_back"] == 1

    rolled = gov.list_suggestions(status="rolled_back")
    assert rolled[0]["scope_key"] == "fragile_factor"


def test_governor_logs_learning_application(tmp_path):
    db_path = str(tmp_path / "state.db")
    gov = RuleEvolutionGovernor(db_path)

    app_id = gov.log_application(
        scope_type="factor",
        scope_key="foo",
        action="downweight",
        bias_multiplier=0.84,
        old_weight=0.5,
        new_weight=0.42,
        suggestion_ids=["s1", "s2"],
        cycle_ts=1234.0,
        details={"note": "demo"},
    )
    assert app_id

    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM learning_application_log WHERE application_id=?",
            (app_id,),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row["scope_key"] == "foo"
    assert float(row["new_weight"]) == 0.42
