from backend.core.db import STATE_DB_DDL, connect_sqlite
from backend.services.brain_governance_candidates import (
    BrainGovernanceCandidateService,
    ensure_brain_governance_candidate_table,
)
from backend.services.learning_application_store import LearningApplicationStore
from backend.services.v16_brain_planning import BrainActionPlanEvaluatorService


def test_supervisor_template_effect_scope_matches_position_supervisor_application():
    assert BrainActionPlanEvaluatorService._matches_scope(
        "supervisor_template",
        {"scope_type": "position_supervisor_template"},
    )


def test_candidate_materialization_refreshes_existing_candidate_evidence(tmp_path):
    db_path = tmp_path / "state.db"
    ensure_brain_governance_candidate_table(db_path)
    service = BrainGovernanceCandidateService(db_path)
    base = {
        "candidate_id": "brain_candidate_refresh",
        "source_agent": "v16_brain",
        "source_kind": "test",
        "source_ref_type": "test",
        "source_ref_id": "eval-1",
        "proposal_stage": "governance_ready",
        "capability_scope": "medium_impact_governance",
        "scope_type": "supervisor_template",
        "scope_key": "position_supervisor",
        "action": "switch_position_supervisor_template",
        "confidence": 0.75,
        "evidence_score": 0.75,
        "risk_class": "medium",
        "max_impact": "medium_impact",
        "expected_effect": {"source_presence": {"learning_application_effect": False}},
        "status": "active",
        "expires_at": 100.0,
        "created_at": 1.0,
        "updated_at": 1.0,
    }
    service._insert_candidate(base)
    refreshed = {
        **base,
        "expected_effect": {"source_presence": {"learning_application_effect": True}},
        "updated_at": 2.0,
    }
    service._insert_candidate(refreshed)

    loaded = service.load_candidate("brain_candidate_refresh")
    assert loaded["expected_effect"]["source_presence"]["learning_application_effect"] is True
    assert loaded["updated_at"] == 2.0


def test_learning_effect_evidence_is_not_globally_truncated_before_scope_filter(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
    finally:
        conn.close()
    store = LearningApplicationStore(db_path)
    for idx in range(20):
        store.write_effect(
            application_id=f"factor-effect-{idx}",
            scope_key=f"factor-{idx}",
            scope_type="factor_weight",
            action="factor_governance_cycle",
            updated_at=100.0 - idx,
        )
    store.write_effect(
        application_id="supervisor-effect-old",
        scope_key="position_supervisor",
        scope_type="supervisor_template",
        action="switch_position_supervisor_template",
        updated_at=1.0,
    )

    evidence = BrainActionPlanEvaluatorService(db_path)._load_evidence(limit=20)

    assert any(
        row["application_id"] == "supervisor-effect-old"
        for row in evidence["learning_application_effect"]
    )

def test_parameter_template_scope_is_routed_to_learning_chain(tmp_path):
    """Route ② (2026-09-05): the parameter_template surface is owned by the
    autonomous_learning chain.  The planner catalog must not build plans for
    it, and legacy evaluations must observe without materializing
    candidates (the planner never carried a target_template_id, so a
    candidate could never pass the bridge review)."""
    import backend.services.v16_brain_planning as planning_module

    assert not [
        action
        for action in planning_module.BrainActionPlannerService.ACTIONS
        if action.get("scope_type") == "parameter_template"
    ]

    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
    finally:
        conn.close()

    service = planning_module.BrainMediumImpactGovernanceService(db_path)
    item = service._materialize_eval(
        evaluation={
            "plan_id": "plan-route-2",
            "eval_id": "eval-route-2",
            "scope_type": "parameter_template",
            "coverage_score": 0.5,
            "comparison": {},
            "evidence_refs": {},
        },
        now=1000.0,
        autonomy_guard={},
        persist_candidate=False,
    )
    assert item["status"] == "routed_to_learning_chain"
    assert item["governance_action"] == "observe"
    assert item["candidate_id"] == ""
    assert item["suggestion_id"] == ""
    assert item["risk_verdict"]["reason"] == "parameter_template_owned_by_learning_chain"
