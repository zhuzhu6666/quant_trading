import json
import time

from backend.core.db import STATE_DB_DDL, connect_sqlite
from backend.services.canonical_v2 import ensure_sqlite_schema, record_decision_event, record_review
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
        ensure_sqlite_schema(conn)
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
        ensure_sqlite_schema(conn)
        _insert_pruning_suggestion(conn)
        for idx in range(2):
            decision_id = f"entry_pre_{idx}"
            record_decision_event(
                conn,
                decision_id=decision_id,
                event_type="open",
                symbol="XAUUSD",
                timeframe="M1",
                decision_ts=now - 20 + idx,
                created_at=now - 20 + idx,
                factor_snapshots=[
                    {
                        "decision_id": decision_id,
                        "factor": "dsl_auto_effect",
                        "contribution_score": 0.5,
                    }
                ],
            )
            record_review(
                conn,
                review_id=f"pre_{idx}",
                trade_id=f"trade_pre_{idx}",
                entry_decision_id=decision_id,
                pnl=30.0,
                outcome_label="good_win",
                review={},
                created_at=now - 20 + idx,
            )
        for idx in range(3):
            decision_id = f"entry_post_{idx}"
            record_decision_event(
                conn,
                decision_id=decision_id,
                event_type="open",
                symbol="XAUUSD",
                timeframe="M1",
                decision_ts=now + 20 + idx,
                created_at=now + 20 + idx,
                factor_snapshots=[
                    {
                        "decision_id": decision_id,
                        "factor": "dsl_auto_effect",
                        "contribution_score": -0.5,
                    }
                ],
            )
            record_review(
                conn,
                review_id=f"post_{idx}",
                trade_id=f"trade_post_{idx}",
                entry_decision_id=decision_id,
                pnl=-40.0,
                outcome_label="bad_loss",
                review={},
                created_at=now + 20 + idx,
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


def _effect_row(db_path, *, factor, comparison_basis, delta, observed, app_suffix):
    store = LearningApplicationStore(db_path)
    app_id = store.prepare_application(
        scope_type="factor", scope_key=factor, action="downweight",
        bias_multiplier=0.82, old_weight=0.01, new_weight=0.0082,
        suggestion_ids=[f"sug-{app_suffix}"], status="applied",
        cycle_ts=time.time() - 3600,
    )
    store.write_effect(
        application_id=app_id, scope_key=factor, scope_type="factor",
        action="downweight", status="ineffective",
        observed_trade_count=observed, baseline_trade_count=observed,
        delta_avg_reward=delta,
        decision={
            "evidence_quality": {
                "comparison_basis": comparison_basis,
                "bounded_attribution_allowed": True,
            }
        },
    )
    return app_id


def test_posterior_evidence_eligibility_requires_comparable_window():
    """The posterior expansion brake may only consume attributable windows."""
    from research.learning.effect_reconciliation import (
        posterior_evidence_eligible,
        posterior_evidence_stamp,
    )

    assert posterior_evidence_eligible(
        {"comparison_basis": "exact_regime", "bounded_attribution_allowed": True}
    ) == (True, "comparable:exact_regime")
    assert posterior_evidence_eligible(
        {"comparison_basis": "unstratified_no_regime", "bounded_attribution_allowed": True}
    )[0] is True
    # Sample-count-only attribution is not attributable to the application.
    assert posterior_evidence_eligible(
        {"comparison_basis": "unstratified_bounded", "bounded_attribution_allowed": True}
    ) == (False, "comparison_basis_not_comparable:unstratified_bounded")
    assert posterior_evidence_eligible(
        {"comparison_basis": "exact_regime", "bounded_attribution_allowed": False}
    )[0] is False
    assert posterior_evidence_eligible(None)[0] is False
    # A stamped verdict is honoured even if the raw fields would derive another
    # answer, so later rule changes cannot reinterpret recorded evidence.
    stamped = {
        "comparison_basis": "unstratified_bounded",
        "bounded_attribution_allowed": True,
        "posterior_evidence_eligibility": posterior_evidence_stamp(
            {"comparison_basis": "exact_regime", "bounded_attribution_allowed": True}
        ),
    }
    assert posterior_evidence_eligible(stamped)[0] is True


def test_non_comparable_effect_does_not_drive_the_posterior_brake(tmp_path):
    """A regime-mismatched delta must not block or degrade an expansion."""
    import backend.runtime.factor_governance_orchestrator as governance_module
    from backend.services.runtime_config_overlay import RuntimeConfigOverlayService

    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        ensure_sqlite_schema(conn)
        conn.commit()
    finally:
        conn.close()

    _effect_row(
        db_path,
        factor="factor_incomparable",
        comparison_basis="unstratified_bounded",
        delta=-0.9,
        observed=50,
        app_suffix="incomparable",
    )
    _effect_row(
        db_path,
        factor="factor_comparable",
        comparison_basis="exact_regime",
        delta=-0.9,
        observed=50,
        app_suffix="comparable",
    )

    orch = governance_module.FactorGovernanceOrchestrator(
        risk_policy=type("_Risk", (), {"evaluate": lambda *_a, **_k: None})()
    )
    orch.overlay = RuntimeConfigOverlayService(db_path)

    assert orch._latest_posterior_effect("factor_incomparable") is None
    comparable = orch._latest_posterior_effect("factor_comparable")
    assert comparable is not None
    assert comparable["delta_avg_reward"] == -0.9
