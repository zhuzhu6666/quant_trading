from __future__ import annotations

import json
import sqlite3

from backend.services.parameter_templates import ParameterTemplateService
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


def test_governor_reuses_active_application_for_same_suggestion(tmp_path):
    db_path = str(tmp_path / "state.db")
    gov = RuleEvolutionGovernor(db_path)

    first_id = gov.log_application(
        scope_type="factor",
        scope_key="foo",
        action="boost_small",
        bias_multiplier=1.04,
        old_weight=0.2,
        new_weight=0.208,
        suggestion_ids=["s1"],
        cycle_ts=100.0,
        details={"note": "first"},
    )
    second_id = gov.log_application(
        scope_type="factor",
        scope_key="foo",
        action="boost_small",
        bias_multiplier=1.05,
        old_weight=0.21,
        new_weight=0.2205,
        suggestion_ids=["s1"],
        cycle_ts=200.0,
        details={"note": "refresh"},
    )

    assert second_id == first_id

    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT application_id, cycle_ts, bias_multiplier, old_weight, new_weight, details_json FROM learning_application_log"
        ).fetchall()
        effect = conn.execute(
            "SELECT decision_json FROM learning_application_effect WHERE application_id=?",
            (first_id,),
        ).fetchone()
    finally:
        conn.close()

    assert len(rows) == 1
    assert float(rows[0]["cycle_ts"]) == 200.0
    assert float(rows[0]["new_weight"]) == 0.2205
    assert json.loads(rows[0]["details_json"])["note"] == "refresh"
    assert json.loads(effect["decision_json"])["new_weight"] == 0.2205


def test_governor_supersedes_older_duplicate_active_applications(tmp_path):
    db_path = str(tmp_path / "state.db")
    gov = RuleEvolutionGovernor(db_path)

    first_id = gov.log_application(
        scope_type="factor",
        scope_key="foo",
        action="boost_small",
        bias_multiplier=1.04,
        old_weight=0.2,
        new_weight=0.208,
        suggestion_ids=["s1"],
        cycle_ts=100.0,
        details={"note": "first"},
    )

    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO learning_application_log
            (application_id, cycle_ts, scope_type, scope_key, action, bias_multiplier,
             old_weight, new_weight, suggestion_ids_json, status, details_json, created_at)
            VALUES ('dup_old', 90.0, 'factor', 'foo', 'boost_small', 1.03, 0.19, 0.1957, '["s1"]', 'observing', '{}', 90.0)
            """
        )
        conn.execute(
            """
            INSERT INTO learning_application_effect
            (application_id, scope_type, scope_key, action, status, decision_json, updated_at, created_at)
            VALUES ('dup_old', 'factor', 'foo', 'boost_small', 'observing', '{}', 90.0, 90.0)
            """
        )
        conn.commit()
    finally:
        conn.close()

    second_id = gov.log_application(
        scope_type="factor",
        scope_key="foo",
        action="boost_small",
        bias_multiplier=1.05,
        old_weight=0.21,
        new_weight=0.2205,
        suggestion_ids=["s1"],
        cycle_ts=200.0,
        details={"note": "refresh"},
    )

    assert second_id == first_id

    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT application_id, status FROM learning_application_log ORDER BY application_id"
        ).fetchall()
        effects = conn.execute(
            "SELECT application_id, status FROM learning_application_effect ORDER BY application_id"
        ).fetchall()
    finally:
        conn.close()

    assert [dict(r) for r in rows] == [
        {"application_id": "dup_old", "status": "superseded"},
        {"application_id": first_id, "status": "applied"},
    ]
    assert [dict(r) for r in effects] == [
        {"application_id": "dup_old", "status": "superseded"},
        {"application_id": first_id, "status": "observing"},
    ]


def test_reconcile_application_effects_marks_ineffective_and_rolls_back(tmp_path):
    db_path = str(tmp_path / "state.db")
    gov = RuleEvolutionGovernor(db_path)

    app_id = gov.log_application(
        scope_type="factor",
        scope_key="fragile_factor",
        action="boost_small",
        bias_multiplier=1.05,
        old_weight=0.5,
        new_weight=0.525,
        suggestion_ids=["s1"],
        cycle_ts=200.0,
        details={"note": "demo"},
    )

    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence, reason, status, created_at)
            VALUES ('s1', 'factor', 'fragile_factor', 'boost_small', 0.8, 'test', 'approved', 100.0)
            """
        )
        for idx, ts in enumerate((100.0, 120.0, 140.0, 220.0, 240.0, 260.0), start=1):
            decision_id = f"dec_{idx}"
            pnl = 60.0 if ts < 200.0 else -90.0
            review_payload = {"real_pnl": {"net": pnl}, "close_reason": "broker_close", "context_integrity": "full"}
            conn.execute(
                """
                INSERT INTO decision_factor_snapshot
                (decision_id, factor, contribution_score)
                VALUES (?, 'fragile_factor', ?)
                """,
                (decision_id, pnl / 100.0),
            )
            conn.execute(
                """
                INSERT INTO trade_outcome_review
                (review_id, trade_id, position_id, entry_decision_id, pnl, outcome_label,
                 failure_tags_json, summary_text, review_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, '[]', '', ?, ?)
                """,
                (
                    f"r_{idx}",
                    f"t_{idx}",
                    f"p_{idx}",
                    decision_id,
                    pnl,
                    "bad_loss" if pnl < 0 else "lucky_win",
                    json.dumps(review_payload),
                    ts,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    result = gov.reconcile_application_effects(min_trades=3, observe_trades=3, baseline_min_trades=2)
    assert result["observed"] == 1
    assert result["rolled_back"] == 1

    conn = _connect(db_path)
    try:
        app_row = conn.execute(
            "SELECT status, details_json FROM learning_application_log WHERE application_id=?",
            (app_id,),
        ).fetchone()
        suggestion_row = conn.execute(
            "SELECT status, review_note FROM policy_suggestion WHERE suggestion_id='s1'"
        ).fetchone()
        effect_row = conn.execute(
            "SELECT status, observed_trade_count, baseline_trade_count, delta_avg_reward FROM learning_application_effect WHERE application_id=?",
            (app_id,),
        ).fetchone()
    finally:
        conn.close()

    assert app_row["status"] == "ineffective"
    assert suggestion_row["status"] == "rolled_back"
    assert effect_row["status"] == "ineffective"
    assert int(effect_row["observed_trade_count"]) == 3
    assert int(effect_row["baseline_trade_count"]) == 3
    assert float(effect_row["delta_avg_reward"]) < 0


def test_reconcile_application_effects_reinforces_positive_application(tmp_path):
    db_path = str(tmp_path / "state.db")
    gov = RuleEvolutionGovernor(db_path)

    app_id = gov.log_application(
        scope_type="factor",
        scope_key="strong_factor",
        action="boost_small",
        bias_multiplier=1.05,
        old_weight=0.3,
        new_weight=0.315,
        suggestion_ids=["s2"],
        cycle_ts=200.0,
        details={"note": "demo"},
    )

    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence, reason, status, created_at)
            VALUES ('s2', 'factor', 'strong_factor', 'boost_small', 0.7, 'test', 'approved', 100.0)
            """
        )
        samples = (
            (100.0, 20.0),
            (120.0, 25.0),
            (140.0, 18.0),
            (220.0, 95.0),
            (240.0, 85.0),
            (260.0, 90.0),
        )
        for idx, (ts, pnl) in enumerate(samples, start=1):
            decision_id = f"strong_dec_{idx}"
            review_payload = {"real_pnl": {"net": pnl}, "close_reason": "broker_close", "context_integrity": "full"}
            conn.execute(
                """
                INSERT INTO decision_factor_snapshot
                (decision_id, factor, contribution_score)
                VALUES (?, 'strong_factor', ?)
                """,
                (decision_id, pnl / 100.0),
            )
            conn.execute(
                """
                INSERT INTO trade_outcome_review
                (review_id, trade_id, position_id, entry_decision_id, pnl, outcome_label,
                 failure_tags_json, summary_text, review_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, '[]', '', ?, ?)
                """,
                (
                    f"sr_{idx}",
                    f"st_{idx}",
                    f"sp_{idx}",
                    decision_id,
                    pnl,
                    "lucky_win",
                    json.dumps(review_payload),
                    ts,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    result = gov.reconcile_application_effects(min_trades=3, observe_trades=3, baseline_min_trades=2)
    assert result["observed"] == 1
    assert result["reinforced"] == 1

    conn = _connect(db_path)
    try:
        app_row = conn.execute(
            "SELECT status FROM learning_application_log WHERE application_id=?",
            (app_id,),
        ).fetchone()
        effect_row = conn.execute(
            "SELECT status, observed_trade_count, baseline_trade_count, delta_avg_reward FROM learning_application_effect WHERE application_id=?",
            (app_id,),
        ).fetchone()
        reinforced_rows = conn.execute(
            """
            SELECT suggestion_id, status, action, review_note
            FROM policy_suggestion
            WHERE scope_key='strong_factor'
            ORDER BY created_at DESC
            """
        ).fetchall()
    finally:
        conn.close()

    assert app_row["status"] == "reinforced"
    assert effect_row["status"] == "reinforced"
    assert int(effect_row["observed_trade_count"]) == 3
    assert int(effect_row["baseline_trade_count"]) == 3
    assert float(effect_row["delta_avg_reward"]) > 0
    assert len(reinforced_rows) == 2
    assert reinforced_rows[0]["status"] == "approved"
    assert reinforced_rows[0]["action"] == "boost_small"


def test_reconcile_application_effects_observes_position_supervisor_template(tmp_path):
    db_path = str(tmp_path / "state.db")
    gov = RuleEvolutionGovernor(db_path)

    app_id = gov.log_application(
        scope_type="position_supervisor_template",
        scope_key="position_supervisor:profit_protection.v1",
        action="switch_position_supervisor_template",
        bias_multiplier=1.0,
        old_weight=0.0,
        new_weight=0.0,
        suggestion_ids=["psv1"],
        cycle_ts=200.0,
        details={
            "previous_template_id": "position_supervisor:default.v1",
            "target_template_id": "position_supervisor:profit_protection.v1",
        },
    )

    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence, reason, status, created_at)
            VALUES ('psv1', 'position_supervisor_template', 'position_supervisor:profit_protection.v1',
                    'switch_position_supervisor_template', 0.7, 'test', 'approved', 100.0)
            """
        )
        samples = (
            (100.0, "pre_1", 4.0, 4.2, 0.10, 0.70),
            (120.0, "pre_2", 3.0, 3.5, 0.12, 0.65),
            (140.0, "pre_3", 2.5, 3.0, 0.15, 0.60),
            (220.0, "post_1", -2.0, 3.0, 0.96, 0.02),
            (240.0, "post_2", -2.5, 2.8, 0.94, 0.03),
            (260.0, "post_3", -1.8, 2.6, 0.92, 0.04),
        )
        for ts, review_id, pnl, mfe, giveback, capture in samples:
            review_payload = {
                "real_pnl": {"net": pnl},
                "close_reason": "broker_close",
                "close_reason_source": "supervisor_tighten_stopout",
                "context_integrity": "full",
                "mfe": mfe,
                "giveback_ratio": giveback,
                "profit_capture_ratio": capture,
                "inferred_close_supervisor": {
                    "event_type": "supervisor_tighten",
                    "action": "tighten",
                    "action_reason": "profit_giveback_after_mfe",
                },
            }
            conn.execute(
                """
                INSERT INTO trade_outcome_review
                (review_id, trade_id, position_id, entry_decision_id, pnl, outcome_label,
                 failure_tags_json, summary_text, review_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, '[]', '', ?, ?)
                """,
                (
                    review_id,
                    f"trade_{review_id}",
                    f"pos_{review_id}",
                    f"dec_{review_id}",
                    pnl,
                    "bad_loss" if pnl < 0 else "good_win",
                    json.dumps(review_payload),
                    ts,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    result = gov.reconcile_application_effects(min_trades=3, observe_trades=3, baseline_min_trades=2)
    assert result["observed"] == 1
    assert result["rolled_back"] == 1

    conn = _connect(db_path)
    try:
        effect_row = conn.execute(
            """
            SELECT status, observed_trade_count, baseline_trade_count, delta_avg_reward
            FROM learning_application_effect
            WHERE application_id=?
            """,
            (app_id,),
        ).fetchone()
        suggestion_row = conn.execute(
            "SELECT status FROM policy_suggestion WHERE suggestion_id='psv1'"
        ).fetchone()
    finally:
        conn.close()

    assert effect_row["status"] == "ineffective"
    assert int(effect_row["observed_trade_count"]) == 3
    assert int(effect_row["baseline_trade_count"]) == 3
    assert float(effect_row["delta_avg_reward"]) < 0
    assert suggestion_row["status"] == "rolled_back"


def test_reconcile_application_effects_waits_when_baseline_too_thin(tmp_path):
    db_path = str(tmp_path / "state.db")
    gov = RuleEvolutionGovernor(db_path)

    app_id = gov.log_application(
        scope_type="factor",
        scope_key="new_factor",
        action="boost_small",
        bias_multiplier=1.04,
        old_weight=0.2,
        new_weight=0.208,
        suggestion_ids=[],
        cycle_ts=200.0,
        details={},
    )

    conn = _connect(db_path)
    try:
        samples = (
            (120.0, 15.0),
            (220.0, 60.0),
            (240.0, 70.0),
            (260.0, 55.0),
        )
        for idx, (ts, pnl) in enumerate(samples, start=1):
            decision_id = f"new_dec_{idx}"
            review_payload = {"real_pnl": {"net": pnl}, "close_reason": "broker_close", "context_integrity": "full"}
            conn.execute(
                """
                INSERT INTO decision_factor_snapshot
                (decision_id, factor, contribution_score)
                VALUES (?, 'new_factor', ?)
                """,
                (decision_id, pnl / 100.0),
            )
            conn.execute(
                """
                INSERT INTO trade_outcome_review
                (review_id, trade_id, position_id, entry_decision_id, pnl, outcome_label,
                 failure_tags_json, summary_text, review_json, created_at)
                VALUES (?, ?, ?, ?, ?, 'lucky_win', '[]', '', ?, ?)
                """,
                (
                    f"nr_{idx}",
                    f"nt_{idx}",
                    f"np_{idx}",
                    decision_id,
                    pnl,
                    json.dumps(review_payload),
                    ts,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    result = gov.reconcile_application_effects(min_trades=3, observe_trades=3, baseline_min_trades=2)
    assert result["observed"] == 1
    assert result["waiting"] == 1
    assert result["reinforced"] == 0
    assert result["rolled_back"] == 0

    conn = _connect(db_path)
    try:
        app_row = conn.execute(
            "SELECT status, details_json FROM learning_application_log WHERE application_id=?",
            (app_id,),
        ).fetchone()
        effect_row = conn.execute(
            "SELECT status, observed_trade_count, baseline_trade_count FROM learning_application_effect WHERE application_id=?",
            (app_id,),
        ).fetchone()
    finally:
        conn.close()

    assert app_row["status"] == "observing"
    assert effect_row["status"] == "observing"
    assert int(effect_row["observed_trade_count"]) == 3
    assert int(effect_row["baseline_trade_count"]) == 1


def test_reconcile_application_effects_waits_when_no_post_reviews(tmp_path):
    db_path = str(tmp_path / "state.db")
    gov = RuleEvolutionGovernor(db_path)

    app_id = gov.log_application(
        scope_type="factor",
        scope_key="sleeping_factor",
        action="downweight",
        bias_multiplier=0.88,
        old_weight=0.4,
        new_weight=0.352,
        suggestion_ids=[],
        cycle_ts=200.0,
        details={},
    )

    conn = _connect(db_path)
    try:
        for idx, ts in enumerate((100.0, 120.0, 140.0), start=1):
            decision_id = f"sleep_dec_{idx}"
            pnl = -30.0
            review_payload = {"real_pnl": {"net": pnl}, "close_reason": "broker_close", "context_integrity": "full"}
            conn.execute(
                """
                INSERT INTO decision_factor_snapshot
                (decision_id, factor, contribution_score)
                VALUES (?, 'sleeping_factor', ?)
                """,
                (decision_id, pnl / 100.0),
            )
            conn.execute(
                """
                INSERT INTO trade_outcome_review
                (review_id, trade_id, position_id, entry_decision_id, pnl, outcome_label,
                 failure_tags_json, summary_text, review_json, created_at)
                VALUES (?, ?, ?, ?, ?, 'good_loss', '[]', '', ?, ?)
                """,
                (
                    f"sleep_r_{idx}",
                    f"sleep_t_{idx}",
                    f"sleep_p_{idx}",
                    decision_id,
                    pnl,
                    json.dumps(review_payload),
                    ts,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    result = gov.reconcile_application_effects(min_trades=3, observe_trades=3, baseline_min_trades=2)
    assert result["observed"] == 1
    assert result["waiting"] == 1

    conn = _connect(db_path)
    try:
        app_row = conn.execute(
            "SELECT status FROM learning_application_log WHERE application_id=?",
            (app_id,),
        ).fetchone()
        effect_row = conn.execute(
            "SELECT status, observed_trade_count, baseline_trade_count, last_review_at FROM learning_application_effect WHERE application_id=?",
            (app_id,),
        ).fetchone()
    finally:
        conn.close()

    assert app_row["status"] == "observing"
    assert effect_row["status"] == "observing"
    assert int(effect_row["observed_trade_count"]) == 0
    assert int(effect_row["baseline_trade_count"]) == 3
    assert float(effect_row["last_review_at"]) == 0.0


def test_reconcile_parameter_template_effects_rolls_back_active_template(tmp_path):
    db_path = str(tmp_path / "state.db")
    gov = RuleEvolutionGovernor(db_path)
    svc = ParameterTemplateService(db_path)

    old_template = svc.upsert_template(
        {
            "factor_id": "rsi_14",
            "regime_key": "range",
            "factor_family": "momentum_oscillator",
            "template_version": "range_safe.v1",
            "template_role": "default",
            "formula_version": "registry_builtin.v1",
            "base_parameter_version": "default.v1",
            "parameters": {"length": 18, "upper_band": 72, "lower_band": 28},
            "applicable_regimes": ["range"],
            "avoid_regimes": ["strong_trend"],
            "holding_profile_hint": {"style": "short_swing", "min_bars": 2, "max_bars": 10},
            "evidence": {"note": "old"},
        },
        source="manual",
    )
    new_template = svc.upsert_template(
        {
            "factor_id": "rsi_14",
            "regime_key": "range",
            "factor_family": "momentum_oscillator",
            "template_version": "range_fast.v1",
            "template_role": "aggressive",
            "formula_version": "registry_builtin.v1",
            "base_parameter_version": "default.v1",
            "parameters": {"length": 14, "upper_band": 70, "lower_band": 30},
            "applicable_regimes": ["range"],
            "avoid_regimes": ["low_vol"],
            "holding_profile_hint": {"style": "fast_reversion", "min_bars": 1, "max_bars": 6},
            "evidence": {"note": "new"},
        },
        source="manual",
    )
    svc.activate_template(
        factor_id="rsi_14",
        regime_key="range",
        template_id=old_template["template_id"],
        note="baseline active",
    )
    switch = svc.create_switch_suggestion(
        factor_id="rsi_14",
        regime_key="range",
        template_id=new_template["template_id"],
        note="proposed switch",
    )
    gov.set_status(switch["suggestion_id"], "approved", "approved for test")
    applied = svc.activate_template(
        factor_id="rsi_14",
        regime_key="range",
        template_id=new_template["template_id"],
        suggestion_id=switch["suggestion_id"],
        note="apply new template",
    )
    assert applied["ok"] is True

    conn = _connect(db_path)
    try:
        apps = conn.execute(
            """
            SELECT application_id
            FROM learning_application_log
            WHERE scope_type='parameter_template'
            ORDER BY created_at DESC
            """
        ).fetchall()
        assert len(apps) >= 2
        conn.execute(
            "UPDATE learning_application_log SET status='superseded', cycle_ts=80.0 WHERE application_id=?",
            (str(apps[-1]["application_id"]),),
        )
        conn.execute(
            "UPDATE learning_application_log SET cycle_ts=200.0 WHERE application_id=?",
            (str(apps[0]["application_id"]),),
        )
        conn.commit()
    finally:
        conn.close()

    conn = _connect(db_path)
    try:
        baseline_samples = (
            (120.0, 55.0),
            (140.0, 45.0),
        )
        for idx, (ts, pnl) in enumerate(baseline_samples, start=1):
            decision_id = f"base_dec_{idx}"
            review_payload = {"real_pnl": {"net": pnl}, "close_reason": "broker_close", "context_integrity": "full"}
            conn.execute(
                """
                INSERT INTO decision_factor_snapshot
                (decision_id, factor, contribution_score)
                VALUES (?, 'rsi_14', ?)
                """,
                (decision_id, pnl / 100.0),
            )
            conn.execute(
                """
                INSERT INTO trade_outcome_review
                (review_id, trade_id, position_id, entry_decision_id, pnl, outcome_label,
                 failure_tags_json, summary_text, review_json, created_at)
                VALUES (?, ?, ?, ?, ?, 'good_win', '[]', '', ?, ?)
                """,
                (
                    f"base_r_{idx}",
                    f"base_t_{idx}",
                    f"base_p_{idx}",
                    decision_id,
                    pnl,
                    json.dumps(review_payload),
                    ts,
                ),
            )

        post_samples = (
            (300.0, -60.0),
            (320.0, -45.0),
            (340.0, -50.0),
        )
        for idx, (ts, pnl) in enumerate(post_samples, start=1):
            decision_id = f"post_dec_{idx}"
            review_payload = {"real_pnl": {"net": pnl}, "close_reason": "broker_close", "context_integrity": "full"}
            conn.execute(
                """
                INSERT INTO decision_factor_snapshot
                (decision_id, factor, contribution_score)
                VALUES (?, 'rsi_14', ?)
                """,
                (decision_id, pnl / 100.0),
            )
            conn.execute(
                """
                INSERT INTO trade_outcome_review
                (review_id, trade_id, position_id, entry_decision_id, pnl, outcome_label,
                 failure_tags_json, summary_text, review_json, created_at)
                VALUES (?, ?, ?, ?, ?, 'bad_loss', '[]', '', ?, ?)
                """,
                (
                    f"post_r_{idx}",
                    f"post_t_{idx}",
                    f"post_p_{idx}",
                    decision_id,
                    pnl,
                    json.dumps(review_payload),
                    ts,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    result = gov.reconcile_application_effects(min_trades=3, observe_trades=3, baseline_min_trades=2)

    assert result["rolled_back"] == 1

    active = svc.get_active_template(factor_id="rsi_14", regime_key="range")
    logs = svc.list_switch_logs(factor_id="rsi_14", limit=10)

    assert active["template_id"] == old_template["template_id"]
    assert logs[0]["status"] == "rolled_back"
    assert logs[0]["new_template_id"] == old_template["template_id"]


def test_conflict_resolver_supersedes_factor_boost_when_entry_quality_suppresses_same_factor(tmp_path):
    db_path = str(tmp_path / "state.db")
    gov = RuleEvolutionGovernor(db_path)
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence, reason, evidence_json, status, created_at)
            VALUES
            ('boost1', 'factor', 'ema_slope', 'boost_small', 0.9, 'test', '{}', 'approved', 100.0),
            ('suppress1', 'entry_quality', 'ema_slope', 'suppress_recent_worst_factor', 0.7, 'test',
             '{"suppressed_factor":"ema_slope"}', 'approved', 90.0)
            """
        )
        conn.commit()
    finally:
        conn.close()

    result = gov.resolve_conflicts()

    assert result["superseded"] == 1
    conn = _connect(db_path)
    try:
        rows = {
            row["suggestion_id"]: row["status"]
            for row in conn.execute("SELECT suggestion_id, status FROM policy_suggestion").fetchall()
        }
    finally:
        conn.close()
    assert rows["boost1"] == "superseded"
    assert rows["suppress1"] == "approved"


def test_conflict_resolver_template_switch_blocks_same_factor_weight_change(tmp_path):
    db_path = str(tmp_path / "state.db")
    gov = RuleEvolutionGovernor(db_path)
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence, reason, evidence_json, status, created_at)
            VALUES
            ('weight1', 'factor', 'rsi_14', 'downweight', 0.9, 'test', '{}', 'approved', 100.0),
            ('tpl1', 'parameter_template', 'rsi_14:range', 'switch_parameter_template', 0.6, 'test',
             '{"factor_id":"rsi_14","regime_key":"range","target_template_id":"tpl_new"}', 'approved', 80.0)
            """
        )
        conn.commit()
    finally:
        conn.close()

    gov.resolve_conflicts()

    conn = _connect(db_path)
    try:
        rows = {
            row["suggestion_id"]: row["status"]
            for row in conn.execute("SELECT suggestion_id, status FROM policy_suggestion").fetchall()
        }
    finally:
        conn.close()
    assert rows["weight1"] == "superseded"
    assert rows["tpl1"] == "approved"


def test_governor_approves_online_light_parameter_template_switch(tmp_path):
    db_path = str(tmp_path / "state.db")
    gov = RuleEvolutionGovernor(db_path)
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence, reason, evidence_json, status, created_at)
            VALUES
            ('tpl_switch', 'parameter_template', 'rsi_14:range', 'switch_parameter_template', 0.7, 'test',
             '{"factor_id":"rsi_14","regime_key":"range","target_template_id":"tpl_new","boundary":{"recommended_scope":"online_light"}}',
             'proposed', 100.0)
            """
        )
        conn.commit()
    finally:
        conn.close()

    result = gov.review_pending()

    assert result["approved"] == 1
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT status, review_note FROM policy_suggestion WHERE suggestion_id='tpl_switch'"
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "approved"
    assert "online_light" in row["review_note"]


def test_conflict_resolver_keeps_auto_tpsl_over_stale_supervisor_template(tmp_path):
    db_path = str(tmp_path / "state.db")
    gov = RuleEvolutionGovernor(db_path)
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence, reason, evidence_json, status, created_at)
            VALUES
            ('old_profit', 'position_supervisor_template', 'position_supervisor:profit_protection.v1',
             'switch_position_supervisor_template', 0.95, 'test', '{}', 'approved', 200.0),
            ('auto_tpsl', 'position_supervisor_template', 'position_supervisor:auto_tpsl.v2',
             'switch_position_supervisor_template', 0.7, 'test',
             '{"target_template_id":"position_supervisor:auto_tpsl.v2"}', 'approved', 100.0)
            """
        )
        conn.commit()
    finally:
        conn.close()

    gov.resolve_conflicts()

    conn = _connect(db_path)
    try:
        rows = {
            row["suggestion_id"]: row["status"]
            for row in conn.execute("SELECT suggestion_id, status FROM policy_suggestion").fetchall()
        }
    finally:
        conn.close()
    assert rows["old_profit"] == "superseded"
    assert rows["auto_tpsl"] == "approved"
