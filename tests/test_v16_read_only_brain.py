import json
import time

from backend.core.db import STATE_DB_DDL, connect_sqlite
from backend.services.backend_readiness import BackendReadinessService
from backend.services.brain_action_evaluator import BrainActionPlanEvaluatorService
from backend.services.brain_action_planner import BrainActionPlannerService
from backend.services.brain_low_impact_executor import BrainLowImpactExecutorService
from backend.services.brain_live_ready_guardrail import BrainLiveReadyGuardrailService
from backend.services.brain_medium_impact_governance import BrainMediumImpactGovernanceService
from backend.services.brain_governance_candidates import BrainGovernanceCandidateService
from backend.services.brain_governance_candidate_review import (
    BrainGovernanceCandidateReviewService,
    ensure_brain_governance_candidate_review_table,
)
from backend.services.brain_memory import BrainMemoryService
from backend.services.brain_state import BrainStateService
from backend.services.incident_controls import RuntimeIncidentControlService


def _readiness_fixture() -> dict:
    return {
        "schema_version": "backend_readiness.v1",
        "generated_at": time.time(),
        "ready_for_frontend": True,
        "market_session": {"status": "open"},
        "live": {
            "ctrader": {"status": "connected"},
            "loop": {"running": True},
            "readiness": {"ok": True},
        },
        "system_health": {
            "overall": "ok",
            "blocking_components": [],
        },
        "governance": {"status": "ok", "automatic_execution_enabled": True},
        "governance_freshness": {
            "tables": {
                "factor_catalog_snapshot": {"status": "fresh", "age_seconds": 10.0},
            }
        },
        "replay": {
            "schema_version": "replay_readiness.v1",
            "ok": False,
            "status": "missing_report",
            "latest_report": {},
        },
        "incident_control": {
            "schema_version": "runtime_incident_control.v1",
            "mode": "normal",
            "readiness_effect": {},
        },
        "release": {
            "schema_version": "release_readiness.v1",
            "ok": False,
            "latest_release": {},
        },
        "autonomy_health": {
            "schema_version": "autonomy_health.v1",
            "score": 0.72,
            "posture": "full",
            "blockers": [],
            "read_only": True,
        },
        "blockers": [],
        "known_observations": [],
    }


def test_brain_state_persists_read_only_world_model_snapshot(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()

    service = BrainStateService(db_path)
    snapshot = service.build(readiness=_readiness_fixture(), source="test")

    assert snapshot["schema_version"] == "brain_state_snapshot.v1"
    assert snapshot["read_only"] is True
    assert snapshot["affects_trading"] is False
    assert snapshot["world_model"]["strategy_posture"] == "defensive"
    assert snapshot["critic"]["max_allowed_action_scope"] == "observe_only"
    assert snapshot["boundary"]["does_not_mutate_runtime_overlay"] is True
    assert any(item["scope"] == "simulation" for item in snapshot["hypotheses"])
    assert snapshot["memory"]["schema_version"] == "brain_memory_retrieval.v1"
    assert snapshot["evidence_refs"]["memory"]["source_gaps"]

    latest = service.latest_snapshot()
    assert latest["snapshot_id"] == snapshot["snapshot_id"]
    assert latest["world_model"]["learning_posture"] == "warming_up"
    assert latest["memory"]["read_only"] is True

    status = service.status()
    assert status["schema_version"] == "brain_state_readiness.v1"
    assert status["ok"] is True
    assert status["affects_trading"] is False


def test_backend_readiness_exposes_v16_read_only_brain_contract(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(BackendReadinessService, "_live_status", staticmethod(lambda: _readiness_fixture()["live"] | {"market_session": {"status": "open"}}))
    monkeypatch.setattr(BackendReadinessService, "_system_health", staticmethod(lambda: _readiness_fixture()["system_health"]))
    monkeypatch.setattr(BackendReadinessService, "_model_status", staticmethod(lambda: {"permission_ok": True}))
    monkeypatch.setattr(BackendReadinessService, "_high_load_status", staticmethod(lambda market_session: {"status": "ok"}))
    monkeypatch.setattr(BackendReadinessService, "_governance_status", lambda self: _readiness_fixture()["governance"])
    monkeypatch.setattr(BackendReadinessService, "_factor_data_status", lambda self: {"status": "ok"})
    monkeypatch.setattr(BackendReadinessService, "_governance_freshness_status", lambda self: _readiness_fixture()["governance_freshness"])
    monkeypatch.setattr(BackendReadinessService, "_runtime_weight_integrity_status", staticmethod(lambda: {"ok": True}))
    monkeypatch.setattr(BackendReadinessService, "_execution_semantics_status", staticmethod(lambda: {"effective_send_orders": False, "blocking_components": []}))
    monkeypatch.setattr(BackendReadinessService, "_startup_status", lambda self: {"blocking_components": [], "known_observations": []})
    monkeypatch.setattr(BackendReadinessService, "_config_runtime_drift_status", staticmethod(lambda: {"known_observations": []}))
    monkeypatch.setattr(BackendReadinessService, "_audit_health_status", staticmethod(lambda: {"ok": True, "known_observations": []}))
    monkeypatch.setattr(BackendReadinessService, "_background_jobs_status", lambda self: {"ok": True})
    monkeypatch.setattr(BackendReadinessService, "_replay_status", lambda self: _readiness_fixture()["replay"])
    monkeypatch.setattr(BackendReadinessService, "_incident_control_status", lambda self: _readiness_fixture()["incident_control"])
    monkeypatch.setattr(BackendReadinessService, "_release_status", lambda self: _readiness_fixture()["release"])
    monkeypatch.setattr(BackendReadinessService, "_stability_status", lambda self, **kwargs: {"runtime_config_overlay": {}, "runtime_config_snapshot": {"ok": True, "config_hash": "cfg"}})
    monkeypatch.setattr(BackendReadinessService, "_autonomy_health_status", lambda self, **kwargs: _readiness_fixture()["autonomy_health"])

    result = BackendReadinessService(db_path=db_path).build()

    assert result["learning_effect_quality"]["boundary"]["read_only"] is True
    assert result["v16"]["learning_effect_quality"]["boundary"]["retry_requires_governor_decision"] is True
    assert result["v16"]["runtime_factor_budget"]["schema_version"] == "runtime_factor_budget.v1"
    assert result["frontend_contract"]["learning_effect_quality"] == "/api/learning/effect-quality"
    assert result["frontend_contract"]["v16_brain_state"] == "/api/ops/brain/state"
    assert result["frontend_contract"]["v16_brain_memory"] == "/api/ops/brain/memory"
    assert result["frontend_contract"]["v16_brain_action_plans"] == "/api/ops/brain/action-plans"
    assert result["frontend_contract"]["v16_brain_action_plan_evals"] == "/api/ops/brain/action-plan-evals"
    assert result["frontend_contract"]["v16_brain_low_impact_executions"] == "/api/ops/brain/low-impact-executions"
    assert result["frontend_contract"]["v16_brain_low_impact_execution_run"] == "/api/ops/brain/low-impact-executions/run"
    assert result["frontend_contract"]["v16_brain_medium_impact_governance"] == "/api/ops/brain/medium-impact-governance"
    assert result["frontend_contract"]["v16_brain_medium_impact_governance_materialize"] == "/api/ops/brain/medium-impact-governance/materialize"
    assert result["frontend_contract"]["v16_brain_governance_candidates"] == "/api/ops/brain/governance-candidates"
    assert result["frontend_contract"]["v16_brain_governance_candidate_submit"] == "/api/ops/brain/governance-candidates/{candidate_id}/submit"
    assert result["frontend_contract"]["v16_brain_governance_candidate_reviews"] == "/api/ops/brain/governance-candidate-reviews"
    assert result["frontend_contract"]["v16_brain_governance_candidate_review_run"] == "/api/ops/brain/governance-candidates/review"
    assert result["frontend_contract"]["v16_brain_live_ready_guardrails"] == "/api/ops/brain/live-ready-guardrails"
    assert result["frontend_contract"]["v16_brain_live_ready_guardrail_evaluate"] == "/api/ops/brain/live-ready-guardrails/evaluate"
    assert result["frontend_contract"]["v16_brain_live_ready_guardrail_tighten"] == "/api/ops/brain/live-ready-guardrails/tighten"
    assert result["frontend_contract"]["agent_authority"] == "/api/ops/agent-authority"
    assert result["frontend_contract"]["agent_scorecard"] == "/api/ops/agent-scorecard"
    assert result["frontend_contract"]["agent_briefing"] == "/api/ops/agent-briefing"
    assert result["frontend_contract"]["agent_trade_attribution"] == "/api/ops/agent-trade-attribution"
    assert result["frontend_contract"]["agent_chain_health"] == "/api/ops/agent-chain-health"
    assert result["v16"]["schema_version"] == "v16_readiness_contract.v1"
    assert result["v16"]["phase"] == "phase5_live_ready_guardrails"
    assert result["v16"]["control_plane_boundaries"]["read_only"] is True
    assert result["v16"]["control_plane_boundaries"]["shadow_action_plans_record_only"] is True
    assert result["v16"]["control_plane_boundaries"]["shadow_action_evals_record_only"] is True
    assert result["v16"]["control_plane_boundaries"]["low_impact_execution_requires_risk_policy"] is True
    assert result["v16"]["control_plane_boundaries"]["medium_impact_governance_candidates_only"] is True
    assert result["v16"]["control_plane_boundaries"]["medium_impact_governance_suggestions_only"] is False
    assert result["v16"]["control_plane_boundaries"]["medium_impact_policy_suggestion_bridge_manual_only"] is True
    assert result["v16"]["control_plane_boundaries"]["candidate_generation_context_required"] is True
    assert result["v16"]["control_plane_boundaries"]["candidate_review_bridge_preview_only"] is True
    assert result["v16"]["control_plane_boundaries"]["candidate_review_llm_advisory_only"] is True
    assert result["v16"]["control_plane_boundaries"]["candidate_bridge_requires_review"] is True
    assert result["v16"]["control_plane_boundaries"]["proposal_generation_context_required"] is True
    assert result["v16"]["control_plane_boundaries"]["live_ready_guardrails_only"] is True
    assert result["v16"]["control_plane_boundaries"]["live_ready_tightening_only"] is True
    assert result["v16"]["control_plane_boundaries"]["agent_authority_registry_is_source_of_truth"] is True
    assert result["v16"]["control_plane_boundaries"]["agent_scorecard_read_only"] is True
    assert result["v16"]["control_plane_boundaries"]["agent_briefing_read_only"] is True
    assert result["v16"]["control_plane_boundaries"]["agent_trade_feedback_read_only"] is True
    assert result["brain_state"]["ok"] is True
    assert result["brain_state"]["latest_snapshot"]["affects_trading"] is False
    assert result["brain_action_plans"]["schema_version"] == "brain_action_plan_readiness.v1"
    assert result["brain_action_plans"]["affects_trading"] is False
    assert result["brain_action_plan_evals"]["schema_version"] == "brain_action_plan_eval_readiness.v1"
    assert result["brain_action_plan_evals"]["affects_trading"] is False
    assert result["brain_low_impact_executions"]["schema_version"] == "brain_low_impact_execution_readiness.v1"
    assert result["brain_low_impact_executions"]["low_impact_only"] is True
    assert result["brain_medium_impact_governance"]["schema_version"] == "brain_medium_impact_governance_readiness.v1"
    assert result["brain_medium_impact_governance"]["medium_impact_governance"] is True
    assert result["brain_governance_candidates"]["schema_version"] == "brain_governance_candidate_readiness.v1"
    assert result["brain_governance_candidates"]["candidate_lane_isolated"] is True
    assert result["candidate_generation_context_coverage"]["schema_version"] == "candidate_generation_context_coverage.v1"
    assert result["candidate_generation_context_coverage"]["status"] == "ok"
    assert result["v16"]["candidate_generation_context_coverage"]["status"] == "ok"
    assert result["brain_governance_candidate_reviews"]["schema_version"] == "brain_governance_candidate_review_readiness.v1"
    assert result["brain_governance_candidate_reviews"]["bridge_preview_only"] is True
    assert result["candidate_bridge_review_coverage"]["schema_version"] == "candidate_bridge_review_coverage.v1"
    assert result["candidate_bridge_review_coverage"]["status"] == "ok"
    assert result["v16"]["candidate_bridge_review_coverage"]["status"] == "ok"
    assert result["proposal_generation_context_coverage"]["schema_version"] == "proposal_generation_context_coverage.v1"
    assert result["proposal_generation_context_coverage"]["status"] == "ok"
    assert result["v16"]["proposal_generation_context_coverage"]["status"] == "ok"
    assert result["brain_live_ready_guardrails"]["schema_version"] == "brain_live_ready_guardrail_readiness.v1"
    assert result["brain_live_ready_guardrails"]["live_ready_guardrails"] is True
    assert result["agent_authority"]["schema_version"] == "agent_authority_status.v1"
    assert result["agent_authority"]["registered_agents"] == 7
    assert result["v16"]["agent_authority"]["status"] == "ok"
    assert result["agent_scorecard"]["schema_version"] == "agent_scorecard_readiness.v1"
    assert result["agent_briefing"]["schema_version"] == "agent_briefing_readiness.v1"
    assert result["agent_chain_health"]["schema_version"] == "agent_chain_health.v1"
    assert result["autonomous_blueprint"]["schema_version"] == "autonomous_trading_blueprint_status.v1"
    assert result["autonomous_blueprint"]["deviation_guard"]["does_not_create_second_execution_path"] is True
    assert result["v16"]["autonomous_blueprint"]["schema_version"] == "autonomous_trading_blueprint_status.v1"


def test_brain_memory_retrieves_negative_memory_and_counter_evidence(tmp_path):
    db_path = tmp_path / "state.db"
    now = time.time()
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO experience_memory
            (experience_id, trade_id, source_table, source_id, append_source,
             regime_id, outcome_label, reward_score,
             failure_tags_json, recommended_action, evidence_strength, created_at)
            VALUES ('trade_lesson:review_loss_1', 'trade_1', 'trade_outcome_review', 'review_loss_1',
                    'trade_lesson_memory.v1', 'defensive', 'loss', -0.8,
                    '["simulation_gap"]', 'observe_only', 0.9, ?)
            """,
            (now - 30.0,),
        )
        conn.execute(
            """
            INSERT INTO trade_outcome_review
            (review_id, trade_id, position_id, pnl, outcome_label, summary_text, created_at)
            VALUES ('review_win_1', 'trade_2', 'pos_2', 120.0, 'win',
                    'defensive setup recovered after replay evidence improved', ?),
                   ('review_loss_1', 'trade_1', 'pos_1', -80.0, 'loss',
                    'defensive setup failed', ?)
            """,
            (now - 20.0, now - 30.0),
        )
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence, reason, status, created_at)
            VALUES ('sugg_1', 'factor', 'rsi_14', 'downweight', 0.7,
                    'factor posture unstable', 'blocked_by_risk', ?)
            """,
            (now - 10.0,),
        )
        conn.commit()
    finally:
        conn.close()

    readiness = _readiness_fixture()
    readiness["governance_freshness"]["tables"]["factor_health"] = {
        "status": "stale_or_empty",
        "age_seconds": 9999.0,
    }
    snapshot = BrainStateService(db_path).build(readiness=readiness, source="test")

    memory = snapshot["memory"]
    assert memory["ok"] is True
    assert memory["negative_matches"]
    assert memory["counter_evidence"]
    assert memory["evidence_balance"]["dominant"] == "mixed"
    narrow_memory = BrainMemoryService(db_path).retrieve(
        world_model={"market_regime": "defensive"},
        hypotheses=[],
        persist=False,
        limit=1,
    )
    assert len(narrow_memory["items"]) == 1
    assert narrow_memory["counter_evidence"]
    assert narrow_memory["evidence_balance"]["positive_count"] >= 1
    assert snapshot["critic"]["verdict"] == "shadow_only"
    assert "mixed_or_insufficient_memory_requires_observation" in snapshot["critic"]["objections"]
    assert any((item.get("counter_evidence_refs") or {}).get("memory") for item in snapshot["hypotheses"])
    assert any((item.get("evidence_refs") or {}).get("negative_memory") for item in snapshot["hypotheses"])

    indexed = BrainMemoryService(db_path).latest_indexed(limit=10)
    assert indexed["ok"] is True
    assert {item["source_table"] for item in indexed["items"]} >= {
        "experience_memory",
        "trade_outcome_review",
        "policy_suggestion",
    }


def test_brain_memory_uses_token_matching_not_generic_substrings():
    assert BrainMemoryService._similarity("factorization instability", {"factor"}) == 0.0
    assert BrainMemoryService._similarity("factor instability", {"factor"}) > 0.0


def test_brain_memory_excludes_system_contaminated_review_lineage(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    contaminated = {
        "summary": "contaminated review",
        "system_issue_context": {
            "contaminates_learning": True,
            "labels": ["market_data_stale"],
        },
    }
    clean = {"summary": "clean review"}
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            """INSERT INTO trade_outcome_review
               (review_id, trade_id, position_id, pnl, outcome_label, review_json, created_at)
               VALUES ('review_bad', 'trade_bad', 'position_bad', -1, 'loss', ?, 20),
                      ('review_clean', 'trade_clean', 'position_clean', 1, 'win', ?, 10)""",
            (json.dumps(contaminated), json.dumps(clean)),
        )
        conn.execute(
            """INSERT INTO experience_memory
               (experience_id, trade_id, source_table, source_id, decision_context_json,
                append_source, outcome_label, reward_score, evidence_strength, created_at)
               VALUES ('trade_lesson:review_bad', 'trade_bad', 'trade_outcome_review', 'review_bad', ?,
                       'trade_lesson_memory.v1',
                       'loss', -1, 1, 20),
                      ('trade_lesson:review_clean', 'trade_clean', 'trade_outcome_review', 'review_clean', ?,
                       'trade_lesson_memory.v1',
                       'win', 1, 1, 10)""",
            (
                json.dumps({"review_json": contaminated}),
                json.dumps({"review_json": clean}),
            ),
        )
        conn.execute(
            """INSERT INTO supervisor_counterfactual_review
               (counterfactual_id, review_id, trade_id, position_id, close_ts, label,
                confidence, horizons_json, evidence_json, created_at, updated_at)
               VALUES ('cf_bad', 'review_bad', 'trade_bad', 'position_bad', 20,
                       'premature_tighten', 0.9, '[{}]', '{}', 20, 20),
                      ('cf_clean', 'review_clean', 'trade_clean', 'position_clean', 10,
                       'premature_tighten', 0.9, '[{}]',
                       '{"maturity":{"governance_eligible":true}}', 10, 10),
                      ('cf_invalidated', 'review_clean', 'trade_clean', 'position_clean', 9,
                       'premature_tighten', 0.9, '[{}]',
                       '{"evidence_invalidated":true,"maturity":{"governance_eligible":false}}',
                       9, 9)"""
        )
        conn.execute(
            """INSERT INTO policy_suggestion
               (suggestion_id, scope_type, scope_key, action, confidence, reason,
                evidence_json, status, created_at)
               VALUES ('suggestion_invalidated', 'factor', 'rsi_14', 'downweight',
                       0.9, 'stale lineage', '{}', 'invalidated_evidence', 30)"""
        )
        conn.commit()
    finally:
        conn.close()

    result = BrainMemoryService(db_path).retrieve(persist=False, limit=50)
    source_ids = {item["source_id"] for item in result["items"]}

    # Trade review and experience memory share the same trade lineage and may
    # be deduplicated into one current memory item.
    assert {"trade_lesson:review_clean", "cf_clean"} <= source_ids
    assert not {"review_bad", "trade_lesson:review_bad", "cf_bad"} & source_ids
    assert "suggestion_invalidated" not in source_ids

    planning_evidence = BrainActionPlanEvaluatorService(db_path)._load_evidence(limit=50)
    assert {
        item["review_id"] for item in planning_evidence["trade_outcome_review"]
    } == {"review_clean"}
    assert {
        item["counterfactual_id"]
        for item in planning_evidence["supervisor_counterfactual_review"]
    } == {"cf_clean"}


def test_brain_action_planner_records_shadow_only_action_plans(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()

    snapshot = BrainStateService(db_path).build(readiness=_readiness_fixture(), source="test")
    run = BrainActionPlannerService(db_path).build_plans(
        brain_state=snapshot,
        persist=True,
        source="test",
    )

    assert run["schema_version"] == "brain_action_plan_run.v1"
    assert run["phase"] == "v16_phase2_shadow_brain"
    assert run["read_only"] is True
    assert run["affects_trading"] is False
    assert run["plan_count"] == 4

    plans = run["plans"]
    scope_types = {plan["scope"]["scope_type"] for plan in plans}
    assert scope_types == {
        "factor_weight",
        "parameter_template",
        "context_policy",
        "supervisor_template",
    }
    assert {plan["status"] for plan in plans} <= {"shadow_recorded", "critic_rejected"}
    for plan in plans:
        assert plan["boundary"]["does_not_execute_action_plan"] is True
        assert plan["boundary"]["does_not_mutate_runtime_overlay"] is True
        assert plan["boundary"]["does_not_write_learning_samples"] is True
        assert plan["shadow_eval"]["record_only"] is True
        assert plan["rollback_plan"]["required"] is False
        assert plan["max_impact"] == "none_shadow_only"
        assert "RiskPolicyService" in plan["required_services"]
        if plan["scope"]["scope_type"] == "factor_weight":
            assert "DecisionPolicy" in plan["required_services"]

    latest = BrainActionPlannerService(db_path).latest_plans(limit=10)
    assert latest["schema_version"] == "brain_action_plan_list.v1"
    assert latest["ok"] is True
    assert len(latest["plans"]) == 4
    assert latest["plans"][0]["read_only"] is True

    status = BrainActionPlannerService(db_path).status(limit=10)
    assert status["schema_version"] == "brain_action_plan_readiness.v1"
    assert status["ok"] is True
    assert status["plan_count"] == 4
    assert status["affects_trading"] is False


def test_brain_action_plan_evaluator_compares_shadow_plans_to_posterior_evidence(tmp_path):
    db_path = tmp_path / "state.db"
    now = time.time()
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO replay_report
            (replay_run_id, decision_count, matched_live_count, mismatch_count,
             metric_summary_json, evidence_grade, status, created_at)
            VALUES ('replay_1', 10, 9, 1, '{"coverage": 0.9}', 'B', 'completed', ?)
            """,
            (now - 40.0,),
        )
        conn.execute(
            """
            INSERT INTO trade_outcome_review
            (review_id, trade_id, position_id, pnl, outcome_label, summary_text, created_at)
            VALUES ('review_1', 'trade_1', 'pos_1', 12.5, 'win',
                    'posterior outcome supports shadow comparison', ?)
            """,
            (now - 30.0,),
        )
        conn.execute(
            """
            INSERT INTO learning_application_effect
            (application_id, scope_type, scope_key, action, status,
             observed_trade_count, baseline_trade_count, post_avg_reward,
             baseline_avg_reward, delta_avg_reward, post_win_rate,
             baseline_win_rate, updated_at, created_at)
            VALUES ('effect_1', 'factor', 'alpha_weight_policy', 'downweight',
                    'observed', 5, 5, 0.3, 0.2, 0.1, 0.6, 0.5, ?, ?)
            """,
            (now - 20.0, now - 20.0),
        )
        conn.execute(
            """
            INSERT INTO position_supervisor_trace
            (trace_id, decision_id, position_id, trade_id, event_ts, action,
             outcome, risk_allowed, execution_status, trace_integrity, created_at)
            VALUES ('trace_1', 'dec_1', 'pos_1', 'trade_1', ?, 'hold',
                    'observed', 1, 'observed', 'full', ?)
            """,
            (now - 10.0, now - 10.0),
        )
        conn.commit()
    finally:
        conn.close()

    snapshot = BrainStateService(db_path).build(readiness=_readiness_fixture(), source="test")
    BrainActionPlannerService(db_path).build_plans(brain_state=snapshot, persist=True, source="test")
    run = BrainActionPlanEvaluatorService(db_path).evaluate_latest_plans(limit=10, persist=True)

    assert run["schema_version"] == "brain_action_plan_eval_run.v1"
    assert run["ok"] is True
    assert run["read_only"] is True
    assert run["affects_trading"] is False
    assert len(run["evals"]) == 4
    assert {item["status"] for item in run["evals"]} == {"comparable"}
    assert any(item["comparison_verdict"] == "supportive" for item in run["evals"])
    for item in run["evals"]:
        assert item["boundary"]["does_not_execute_action_plan"] is True
        assert item["comparison"]["source_presence"]["replay_report"] is True
        assert item["comparison"]["source_presence"]["trade_outcome_review"] is True
        assert item["comparison"]["source_presence"]["position_supervisor_trace"] is True
        assert item["coverage_score"] >= 0.75

    latest = BrainActionPlanEvaluatorService(db_path).latest_evals(limit=10)
    assert latest["schema_version"] == "brain_action_plan_eval_list.v1"
    assert latest["ok"] is True
    assert len(latest["evals"]) == 4

    status = BrainActionPlanEvaluatorService(db_path).status(limit=10)
    assert status["schema_version"] == "brain_action_plan_eval_readiness.v1"
    assert status["ok"] is True
    assert status["eval_count"] == 4
    assert status["coverage_avg"] >= 0.75


def test_brain_low_impact_executor_runs_replay_job_through_risk_policy(tmp_path):
    db_path = tmp_path / "state.db"
    now = time.time()
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO replay_report
            (replay_run_id, decision_count, matched_live_count, mismatch_count,
             metric_summary_json, evidence_grade, status, created_at)
            VALUES ('replay_seed', 1, 1, 0, '{}', 'B', 'completed', ?)
            """,
            (now - 40.0,),
        )
        conn.execute(
            """
            INSERT INTO trade_outcome_review
            (review_id, trade_id, position_id, pnl, outcome_label, summary_text, created_at)
            VALUES ('review_seed', 'trade_1', 'pos_1', 1.0, 'win', 'ok', ?)
            """,
            (now - 30.0,),
        )
        conn.execute(
            """
            INSERT INTO position_supervisor_trace
            (trace_id, decision_id, position_id, trade_id, event_ts, action,
             outcome, risk_allowed, execution_status, trace_integrity, created_at)
            VALUES ('trace_seed', 'dec_1', 'pos_1', 'trade_1', ?, 'hold',
                    'observed', 1, 'observed', 'full', ?)
            """,
            (now - 10.0, now - 10.0),
        )
        conn.commit()
    finally:
        conn.close()

    snapshot = BrainStateService(db_path).build(readiness=_readiness_fixture(), source="test")
    BrainActionPlannerService(db_path).build_plans(brain_state=snapshot, persist=True, source="test")
    BrainActionPlanEvaluatorService(db_path).evaluate_latest_plans(limit=4, persist=True)

    run = BrainLowImpactExecutorService(
        db_path,
        replay_artifact_dir=tmp_path / "replay_artifacts",
    ).execute_latest(limit=1, allow_tighten=False, replay_lookback_days=1, replay_limit=10)

    assert run["schema_version"] == "brain_low_impact_execution_run.v1"
    assert run["ok"] is True
    execution = run["executions"][0]
    assert execution["schema_version"] == "brain_low_impact_execution.v1"
    assert execution["execution_action"] == "run_replay_job"
    assert execution["status"] == "executed"
    assert execution["risk_verdict"]["allowed"] is True
    assert execution["risk_verdict"]["reason"] == "low_impact_read_only_replay"
    assert execution["rollback_plan"]["runtime_mutation"] is False
    assert execution["result"]["replay_run_id"].startswith("brain_p3_replay_")
    assert execution["boundary"]["does_not_submit_orders"] is True
    assert execution["boundary"]["does_not_write_learning_samples"] is True

    latest = BrainLowImpactExecutorService(db_path).latest_executions(limit=5)
    assert latest["schema_version"] == "brain_low_impact_execution_list.v1"
    assert latest["ok"] is True
    assert latest["executions"][0]["execution_action"] == "run_replay_job"

    status = BrainLowImpactExecutorService(db_path).status(limit=5)
    assert status["schema_version"] == "brain_low_impact_execution_readiness.v1"
    assert status["ok"] is True
    assert status["low_impact_only"] is True


def test_brain_medium_impact_governance_materializes_governance_candidates_only(tmp_path):
    db_path = tmp_path / "state.db"
    now = time.time()
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO replay_report
            (replay_run_id, decision_count, matched_live_count, mismatch_count,
             metric_summary_json, evidence_grade, status, created_at)
            VALUES ('replay_p4', 10, 10, 0, '{}', 'A', 'completed', ?)
            """,
            (now - 40.0,),
        )
        conn.execute(
            """
            INSERT INTO trade_outcome_review
            (review_id, trade_id, position_id, pnl, outcome_label, summary_text, created_at)
            VALUES ('review_p4', 'trade_1', 'pos_1', 8.0, 'win', 'ok', ?)
            """,
            (now - 30.0,),
        )
        conn.execute(
            """
            INSERT INTO learning_application_effect
            (application_id, scope_type, scope_key, action, status,
             observed_trade_count, baseline_trade_count, post_avg_reward,
             baseline_avg_reward, delta_avg_reward, post_win_rate,
             baseline_win_rate, updated_at, created_at)
            VALUES ('effect_p4', 'factor', 'alpha_weight_policy', 'downweight',
                    'observed', 5, 5, 0.35, 0.2, 0.15, 0.7, 0.5, ?, ?)
            """,
            (now - 20.0, now - 20.0),
        )
        conn.execute(
            """
            INSERT INTO position_supervisor_trace
            (trace_id, decision_id, position_id, trade_id, event_ts, action,
             outcome, risk_allowed, execution_status, trace_integrity, created_at)
            VALUES ('trace_p4', 'dec_1', 'pos_1', 'trade_1', ?, 'hold',
                    'observed', 1, 'observed', 'full', ?)
            """,
            (now - 10.0, now - 10.0),
        )
        conn.commit()
    finally:
        conn.close()

    snapshot = BrainStateService(db_path).build(readiness=_readiness_fixture(), source="test")
    BrainActionPlannerService(db_path).build_plans(brain_state=snapshot, persist=True, source="test")
    BrainActionPlanEvaluatorService(db_path).evaluate_latest_plans(limit=4, persist=True)

    run = BrainMediumImpactGovernanceService(db_path).materialize_latest(limit=4)

    assert run["schema_version"] == "brain_medium_impact_governance_run.v1"
    assert run["ok"] is True
    assert any(item["status"] == "candidate_materialized" for item in run["items"])
    assert all(item["boundary"]["materializes_governance_candidates_only"] is True for item in run["items"])
    assert all(item["boundary"]["does_not_write_policy_suggestion_directly"] is True for item in run["items"])
    assert all(item["rollback_plan"]["runtime_mutation"] is False for item in run["items"])
    update_items = [item for item in run["items"] if item["governance_action"] == "update_weight"]
    assert update_items
    assert update_items[0]["decision_policy"]["required"] is True
    assert update_items[0]["decision_policy"]["applied"] is False
    assert update_items[0]["candidate_id"].startswith("brain_candidate_")

    conn = connect_sqlite(db_path, read_only=True)
    try:
        candidate_rows = conn.execute(
            "SELECT action, proposal_stage, status FROM brain_governance_candidate ORDER BY created_at"
        ).fetchall()
        suggestion_rows = conn.execute("SELECT action, status FROM policy_suggestion ORDER BY created_at").fetchall()
    finally:
        conn.close()
    assert candidate_rows
    assert all(row[1] == "governance_ready" for row in candidate_rows)
    assert all(row[2] == "active" for row in candidate_rows)
    assert "update_weight" in {row[0] for row in candidate_rows}
    assert suggestion_rows == []

    latest = BrainMediumImpactGovernanceService(db_path).latest_governance(limit=10)
    assert latest["schema_version"] == "brain_medium_impact_governance_list.v1"
    assert latest["ok"] is True
    assert latest["items"][0]["boundary"]["does_not_apply_factor_weights"] is True
    assert latest["items"][0]["candidate_id"]

    status = BrainMediumImpactGovernanceService(db_path).status(limit=10)
    assert status["schema_version"] == "brain_medium_impact_governance_readiness.v1"
    assert status["ok"] is True
    assert status["medium_impact_governance"] is True
    assert status["governance_candidates"]["candidate_lane_isolated"] is True


def test_brain_governance_candidate_manual_bridge_requires_compatible_payload(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()

    service = BrainGovernanceCandidateService(db_path)
    blocked = service.create_candidate(
        candidate_id="candidate_factor_update",
        source_agent="v16_brain",
        source_kind="brain_medium_impact_governance",
        source_ref_type="brain_action_plan_eval",
        source_ref_id="eval_factor",
        proposal_stage="governance_ready",
        capability_scope="medium_impact_governance",
        scope_type="factor",
        scope_key="alpha_weight_policy",
        action="update_weight",
        confidence=0.7,
        evidence_score=0.75,
        risk_class="medium",
        max_impact="medium_impact",
        risk_verdict={"allowed": True, "reason": "test"},
    )
    blocked_result = service.submit_candidate_to_policy_suggestion(blocked["candidate_id"], actor="test")
    assert blocked["lineage"]["agent_context"]["schema_version"] == "agent_generation_context.v1"
    assert blocked["lineage"]["agent_context"]["source_agent"] == "v16_brain"
    assert blocked_result["ok"] is False
    assert blocked_result["reason"] == "unsupported_legacy_governor_surface:factor/update_weight"

    ready = service.create_candidate(
        candidate_id="candidate_supervisor_ready",
        source_agent="v16_brain",
        source_kind="brain_medium_impact_governance",
        source_ref_type="brain_action_plan_eval",
        source_ref_id="eval_supervisor",
        proposal_stage="governance_ready",
        capability_scope="medium_impact_governance",
        scope_type="supervisor_template",
        scope_key="position_supervisor",
        action="switch_position_supervisor_template",
        confidence=0.8,
        evidence_score=0.75,
        risk_class="medium",
        max_impact="medium_impact",
        expected_effect={
            "replay": {"replay_run_id": "replay_ready", "status": "completed"},
            "supervisor": {"trace_count": 3, "risk_allowed_coverage": 1.0},
        },
        evidence_refs={"posterior": {"replay_report": "replay_ready", "position_supervisor_trace": ["trace_1"]}},
        risk_verdict={"allowed": True, "reason": "test"},
        lineage={"mapped_action": {"target_template_id": "position_supervisor:conservative.v1"}},
    )
    pre_review_submit = service.submit_candidate_to_policy_suggestion(ready["candidate_id"], actor="test")
    assert pre_review_submit["ok"] is False
    assert pre_review_submit["reason"] == "missing_bridge_ready_candidate_review"

    review = BrainGovernanceCandidateReviewService(db_path).review_candidate(ready["candidate_id"], persist=True)
    assert review["review"]["bridge_ready"] is True

    submit_result = service.submit_candidate_to_policy_suggestion(ready["candidate_id"], actor="test")

    assert submit_result["ok"] is True
    assert submit_result["status"] == "submitted_to_policy_suggestion"
    assert submit_result["suggestion_id"].startswith("brain_bridge_")

    conn = connect_sqlite(db_path, read_only=True)
    try:
        suggestion = conn.execute(
            "SELECT scope_type, scope_key, action, status, evidence_json FROM policy_suggestion WHERE suggestion_id=?",
            (submit_result["suggestion_id"],),
        ).fetchone()
        candidate = conn.execute(
            "SELECT proposal_stage, status, submitted_suggestion_id FROM brain_governance_candidate WHERE candidate_id='candidate_supervisor_ready'"
        ).fetchone()
    finally:
        conn.close()

    assert suggestion is not None
    assert suggestion[0] == "position_supervisor_template"
    assert suggestion[1] == "position_supervisor:conservative.v1"
    assert suggestion[2] == "switch_position_supervisor_template"
    assert suggestion[3] == "proposed"
    assert "candidate_supervisor_ready" in suggestion[4]
    evidence = json.loads(suggestion[4])
    assert evidence["bridge"]["candidate_review_required"] is True
    assert evidence["bridge"]["candidate_review_required_before_submit"] is True
    assert evidence["bridge"]["candidate_review"]["bridge_ready"] is True
    assert evidence["bridge"]["candidate_review"]["review_id"] == review["review"]["review_id"]
    assert evidence["lineage"]["agent_context"]["schema_version"] == "agent_generation_context.v1"
    assert evidence["lineage"]["agent_generation_context"]["schema_version"] == "agent_generation_context.v1"
    assert evidence["agent_generation_context"]["source_agent"] == "v16_brain"
    assert evidence["agent_context_required"] is True
    assert candidate[0] == "submitted_to_policy_suggestion"
    assert candidate[1] == "submitted"
    assert candidate[2] == submit_result["suggestion_id"]


def test_brain_governance_candidate_review_classifies_bridge_readiness(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()

    candidate_service = BrainGovernanceCandidateService(db_path)
    candidate_service.create_candidate(
        candidate_id="candidate_factor_update",
        source_agent="v16_brain",
        source_kind="brain_medium_impact_governance",
        source_ref_type="brain_action_plan_eval",
        source_ref_id="eval_factor",
        proposal_stage="governance_ready",
        capability_scope="medium_impact_governance",
        scope_type="factor",
        scope_key="alpha_weight_policy",
        action="update_weight",
        confidence=0.7,
        evidence_score=0.75,
        risk_class="medium",
        max_impact="medium_impact",
        risk_verdict={"allowed": True, "reason": "test"},
    )
    candidate_service.create_candidate(
        candidate_id="candidate_supervisor_ready",
        source_agent="v16_brain",
        source_kind="brain_medium_impact_governance",
        source_ref_type="brain_action_plan_eval",
        source_ref_id="eval_supervisor",
        proposal_stage="governance_ready",
        capability_scope="medium_impact_governance",
        scope_type="supervisor_template",
        scope_key="position_supervisor",
        action="switch_position_supervisor_template",
        confidence=0.8,
        evidence_score=0.75,
        risk_class="medium",
        max_impact="medium_impact",
        expected_effect={
            "source_presence": {
                "replay_report": True,
                "trade_outcome_review": True,
                "learning_application_effect": True,
                "position_supervisor_trace": True,
            },
            "replay": {"replay_run_id": "replay_ready", "status": "completed"},
            "supervisor": {"trace_count": 3, "risk_allowed_coverage": 1.0},
        },
        evidence_refs={"posterior": {"replay_report": "replay_ready", "position_supervisor_trace": ["trace_1"]}},
        risk_verdict={"allowed": True, "reason": "test"},
        lineage={"mapped_action": {"target_template_id": "position_supervisor:conservative.v1"}},
    )

    class FakeLLM:
        def __init__(self, db_path):
            self.db_path = db_path

        def run(self, **kwargs):
            return {
                "status": "dry_run",
                "audit": {
                    "audit_id": "llm:test",
                    "target_id": kwargs.get("target_id", ""),
                    "status": "dry_run",
                },
                "parsed": {"summary": "candidate reviewed"},
                "advisory_only": True,
            }

    monkeypatch.setattr("backend.services.brain_governance_candidate_review.LLMAdvisoryService", FakeLLM)
    run = BrainGovernanceCandidateReviewService(db_path).review_latest(limit=10, run_llm=True, llm_dry_run=True)

    assert run["schema_version"] == "brain_governance_candidate_review_run.v1"
    assert run["ok"] is True
    reviews = {item["candidate_id"]: item for item in run["items"]}
    assert reviews["candidate_supervisor_ready"]["review_status"] == "bridge_ready"
    assert reviews["candidate_supervisor_ready"]["bridge_ready"] is True
    assert reviews["candidate_supervisor_ready"]["bridge_preview"]["status"] == "bridge_ready"
    assert reviews["candidate_supervisor_ready"]["llm_advisory"]["audit"]["audit_id"] == "llm:test"
    assert reviews["candidate_factor_update"]["review_status"] == "not_bridge_compatible"
    assert reviews["candidate_factor_update"]["bridge_ready"] is False

    latest = BrainGovernanceCandidateReviewService(db_path).latest_reviews(limit=10)
    assert latest["schema_version"] == "brain_governance_candidate_review_list.v1"
    assert latest["ok"] is True
    status = BrainGovernanceCandidateReviewService(db_path).status(limit=10)
    assert status["schema_version"] == "brain_governance_candidate_review_readiness.v1"
    assert status["bridge_ready_count"] == 1


def test_brain_governance_candidate_review_uses_agent_reliability_gate(tmp_path):
    db_path = tmp_path / "state.db"
    now = time.time()
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO learning_application_log
            (application_id, cycle_ts, scope_type, scope_key, action,
             suggestion_ids_json, status, details_json, created_at)
            VALUES ('app_bad_v16', ?, 'factor', 'rsi_14', 'downweight',
                    '[]', 'applied', '{"source_agent":"v16_brain"}', ?)
            """,
            (now - 10, now - 10),
        )
        conn.execute(
            """
            INSERT INTO learning_application_effect
            (application_id, scope_type, scope_key, action, status,
             delta_avg_reward, updated_at, created_at)
            VALUES ('app_bad_v16', 'factor', 'rsi_14', 'downweight',
                    'ineffective', -0.2, ?, ?)
            """,
            (now - 5, now - 5),
        )
        conn.commit()
    finally:
        conn.close()

    BrainGovernanceCandidateService(db_path).create_candidate(
        candidate_id="candidate_supervisor_low_source",
        source_agent="v16_brain",
        source_kind="brain_medium_impact_governance",
        source_ref_type="brain_action_plan_eval",
        source_ref_id="eval_supervisor_low",
        proposal_stage="governance_ready",
        capability_scope="medium_impact_governance",
        scope_type="supervisor_template",
        scope_key="position_supervisor",
        action="switch_position_supervisor_template",
        confidence=0.8,
        evidence_score=0.9,
        risk_class="medium",
        max_impact="medium_impact",
        expected_effect={
            "source_presence": {
                "replay_report": True,
                "trade_outcome_review": True,
                "learning_application_effect": True,
                "position_supervisor_trace": True,
            },
            "replay": {"replay_run_id": "replay_ready", "status": "completed"},
            "supervisor": {"trace_count": 3, "risk_allowed_coverage": 1.0},
        },
        evidence_refs={"posterior": {"replay_report": "replay_ready", "position_supervisor_trace": ["trace_1"]}},
        risk_verdict={"allowed": True, "reason": "test"},
        lineage={"mapped_action": {"target_template_id": "position_supervisor:conservative.v1"}},
    )

    run = BrainGovernanceCandidateReviewService(db_path).review_latest(limit=5, run_llm=False)
    review = next(item for item in run["items"] if item["candidate_id"] == "candidate_supervisor_low_source")

    assert review["review_status"] == "needs_evidence"
    assert review["bridge_ready"] is False
    assert "agent_negative_effect_history_requires_counter_evidence" in review["evidence_gaps"]
    assert review["source_reliability"]["agent_scorecard"]["negative_effect_count"] == 1


def test_candidate_review_conflicts_ignore_legacy_ineligible_suggestions(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.executemany(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, status,
             governance_eligible, governance_eligibility_version,
             governance_eligibility_fingerprint, created_at)
            VALUES (?, 'factor', 'rsi_14', 'downweight', 'approved', ?, ?, ?, ?)
            """,
            [
                ("legacy", 0, "", "", 1.0),
                ("current", 1, "governance_eligibility.v1", "fp", 2.0),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    rows = BrainGovernanceCandidateReviewService(db_path)._active_policy_suggestions()

    assert [row["suggestion_id"] for row in rows] == ["current"]


def test_candidate_bridge_review_coverage_flags_missing_required_review(tmp_path):
    db_path = tmp_path / "state.db"
    now = time.time()
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence,
             reason, evidence_json, status, created_at)
            VALUES (?, 'factor', 'dsl_auto_bad', 'downweight', 0.8,
                    'required review missing', ?, 'proposed', ?)
            """,
            (
                "ps_required_missing",
                json.dumps(
                    {
                        "schema_version": "brain_governance_candidate_policy_suggestion_evidence.v1",
                        "candidate_id": "candidate_required_missing",
                        "source_agent": "factor_pruning_governance",
                        "bridge": {"candidate_review_required": True, "candidate_review_required_before_submit": True},
                    }
                ),
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence,
             reason, evidence_json, status, created_at)
            VALUES (?, 'factor', 'legacy_factor', 'downweight', 0.8,
                    'legacy bridge before review gate', ?, 'proposed', ?)
            """,
            (
                "ps_legacy_unreviewed",
                json.dumps(
                    {
                        "schema_version": "brain_governance_candidate_policy_suggestion_evidence.v1",
                        "candidate_id": "candidate_legacy",
                        "source_agent": "v16_brain",
                        "bridge": {"manual_only": True},
                    }
                ),
                now - 10,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    coverage = BrainGovernanceCandidateReviewService(db_path).bridge_review_coverage(limit=20)

    assert coverage["schema_version"] == "candidate_bridge_review_coverage.v1"
    assert coverage["status"] == "degraded"
    assert coverage["candidate_bridge_count"] == 2
    assert coverage["missing_required_review_count"] == 1
    assert coverage["legacy_unreviewed_count"] == 1
    statuses = {item["suggestion_id"]: item["coverage_status"] for item in coverage["items"]}
    assert statuses["ps_required_missing"] == "missing_required_review"
    assert statuses["ps_legacy_unreviewed"] == "legacy_unreviewed"


def test_candidate_bridge_review_coverage_accepts_bridge_ready_review(tmp_path):
    db_path = tmp_path / "state.db"
    now = time.time()
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()
    ensure_brain_governance_candidate_review_table(db_path)
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            """
            INSERT INTO brain_governance_candidate_review
            (review_id, candidate_id, review_status, bridge_ready,
             bridge_reason, evidence_gaps_json, conflict_json,
             bridge_preview_json, source_reliability_json,
             llm_advisory_json, boundary_json, created_at)
            VALUES ('review_ok', 'candidate_reviewed', 'bridge_ready', 1,
                    'bridge_ready', '[]', '{}', '{}', '{}', '{}', '{}', ?)
            """,
            (now - 5,),
        )
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence,
             reason, evidence_json, status, created_at)
            VALUES (?, 'factor', 'dsl_auto_reviewed', 'downweight', 0.8,
                    'reviewed bridge', ?, 'proposed', ?)
            """,
            (
                "ps_reviewed",
                json.dumps(
                    {
                        "schema_version": "brain_governance_candidate_policy_suggestion_evidence.v1",
                        "candidate_id": "candidate_reviewed",
                        "source_agent": "factor_pruning_governance",
                        "bridge": {"candidate_review_required": True, "candidate_review_required_before_submit": True},
                    }
                ),
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    coverage = BrainGovernanceCandidateReviewService(db_path).bridge_review_coverage(limit=20)

    assert coverage["status"] == "ok"
    assert coverage["candidate_bridge_count"] == 1
    assert coverage["covered_count"] == 1
    assert coverage["missing_required_review_count"] == 0
    assert coverage["items"][0]["coverage_status"] == "covered"


def test_candidate_generation_context_coverage_separates_new_and_legacy_candidates(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO brain_governance_candidate
            (candidate_id, source_agent, source_kind, source_ref_type, source_ref_id,
             proposal_stage, capability_scope, scope_type, scope_key, action,
             confidence, evidence_score, risk_class, max_impact,
             lineage_json, status, created_at, updated_at)
            VALUES ('legacy_candidate', 'v16_brain', 'brain_medium_impact_governance',
                    'brain_action_plan_eval', 'eval_legacy', 'governance_ready',
                    'medium_impact_governance', 'factor', 'rsi_14', 'downweight',
                    0.7, 0.8, 'medium', 'medium_impact',
                    '{}', 'active', 1.0, 1.0)
            """
        )
        conn.execute(
            """
            INSERT INTO brain_governance_candidate
            (candidate_id, source_agent, source_kind, source_ref_type, source_ref_id,
             proposal_stage, capability_scope, scope_type, scope_key, action,
             confidence, evidence_score, risk_class, max_impact,
             lineage_json, status, created_at, updated_at)
            VALUES ('bad_new_candidate', 'v16_brain', 'brain_medium_impact_governance',
                    'brain_action_plan_eval', 'eval_bad', 'governance_ready',
                    'medium_impact_governance', 'factor', 'adx_14', 'downweight',
                    0.7, 0.8, 'medium', 'medium_impact',
                    '{"agent_context_required": true}', 'active', 2.0, 2.0)
            """
        )
        conn.commit()
    finally:
        conn.close()

    coverage = BrainGovernanceCandidateService(db_path).generation_context_coverage(limit=20)

    assert coverage["status"] == "degraded"
    assert coverage["candidate_count"] == 2
    assert coverage["missing_required_context_count"] == 1
    assert coverage["legacy_missing_context_count"] == 1
    statuses = {item["candidate_id"]: item["coverage_status"] for item in coverage["items"]}
    assert statuses["bad_new_candidate"] == "missing_required_agent_context"
    assert statuses["legacy_candidate"] == "legacy_missing_agent_context"


def test_brain_live_ready_guardrail_locks_when_evidence_is_complete(tmp_path):
    db_path = tmp_path / "state.db"
    now = time.time()
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO position_lifecycle_event
            (event_id, position_id, trade_id, symbol, event_type, event_ts, net_volume)
            VALUES ('pos_open_p5', 'pos_1', 'trade_1', 'XAUUSD', 'opened', ?, 100.0)
            """,
            (now - 20.0,),
        )
        conn.execute(
            """
            INSERT INTO incident_playbook_event
            (event_id, playbook_id, event_type, actor, status, evidence_refs_json, notes, created_at)
            VALUES ('incident_memory_p5', 'playbook_p5', 'evidence_linked', 'test',
                    'recorded', '{"release_run_id":"release_p5"}', 'ready', ?)
            """,
            (now - 10.0,),
        )
        conn.commit()
    finally:
        conn.close()

    readiness = {
        **_readiness_fixture(),
        "execution_semantics": {"effective_send_orders": True},
        "live": {
            "ctrader": {"status": "connected"},
            "loop": {"running": True},
            "readiness": {"ok": True},
            "positions": {"broker_positions": [{"position_id": "pos_1"}]},
        },
        "replay": {"ok": True, "status": "available", "latest_report": {"replay_run_id": "replay_p5"}},
        "release": {
            "schema_version": "release_readiness.v1",
            "ok": True,
            "latest_release": {
                "run_id": "release_p5",
                "status": "completed",
                "runtime_config_hash": "cfg_p5",
                "rollback_ref": {"snapshot_hash": "cfg_p5"},
                "checklist": {"boundary": {"runtime_snapshot_required_for_rollback": True}},
            },
        },
        "v16": {
            "low_impact_executions": {"ok": True, "status": "available", "execution_count": 1},
            "medium_impact_governance": {"ok": True, "status": "available", "item_count": 2},
        },
    }

    guardrail = BrainLiveReadyGuardrailService(db_path).evaluate(readiness=readiness, source="test")

    assert guardrail["schema_version"] == "brain_live_ready_guardrail.v1"
    assert guardrail["status"] == "live_ready_locked"
    assert guardrail["live_capability_lock"]["locked"] is True
    assert guardrail["broker_local_divergence"]["status"] == "aligned"
    assert guardrail["release_rollback"]["rollback_ready"] is True
    assert guardrail["incident_memory"]["available"] is True
    assert guardrail["p3_p4_evidence"]["p3_available"] is True
    assert guardrail["p3_p4_evidence"]["p4_available"] is True
    assert guardrail["action_recommendation"]["action"] == "observe"
    assert guardrail["boundary"]["does_not_submit_orders"] is True

    latest = BrainLiveReadyGuardrailService(db_path).latest_guardrails(limit=5)
    assert latest["schema_version"] == "brain_live_ready_guardrail_list.v1"
    assert latest["ok"] is True
    assert latest["items"][0]["live_capability_lock"]["locked"] is True

    status = BrainLiveReadyGuardrailService(db_path).status(limit=5)
    assert status["schema_version"] == "brain_live_ready_guardrail_readiness.v1"
    assert status["ok"] is True
    assert status["live_ready_guardrails"] is True


def test_brain_live_ready_guardrail_tightens_only_through_incident_control(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()

    incident = RuntimeIncidentControlService(db_path)
    incident.set_mode("normal", reason="reset", confirm_thaw=True)
    try:
        service = BrainLiveReadyGuardrailService(db_path)
        tightened = service.tighten(
            target_mode="no_new_risk",
            reason="test p5 tighten",
            actor="test",
            readiness={**_readiness_fixture(), "execution_semantics": {"effective_send_orders": True}},
        )

        assert tightened["schema_version"] == "brain_live_ready_guardrail_tighten.v1"
        assert tightened["ok"] is True
        assert tightened["status"] == "tightened"
        assert tightened["incident_control_result"]["risk_verdict"]["allowed"] is True
        assert RuntimeIncidentControlService(db_path).status()["mode"] == "no_new_risk"

        RuntimeIncidentControlService(db_path).set_mode("frozen", reason="test freeze")
        refused = service.tighten(
            target_mode="no_new_risk",
            reason="must not relax",
            actor="test",
            readiness={**_readiness_fixture(), "incident_control": {"mode": "frozen"}},
        )

        assert refused["ok"] is False
        assert refused["status"] == "refused_to_relax_incident_mode"
        assert refused["current_mode"] == "frozen"
        assert RuntimeIncidentControlService(db_path).status()["mode"] == "frozen"
    finally:
        RuntimeIncidentControlService(db_path).set_mode("normal", reason="restore", confirm_thaw=True)


def test_candidate_review_fingerprint_ignores_volatile_audit_timestamps():
    candidate = {
        "candidate_id": "candidate-1",
        "source_agent": "factor_governance",
        "status": "active",
        "proposal_stage": "governance_ready",
        "scope_type": "factor",
        "scope_key": "rsi_14",
        "action": "downweight",
        "confidence": 0.7,
        "evidence_score": 0.8,
        "updated_at": 100.0,
        "expires_at": 200.0,
        "risk_verdict": {"allowed": True},
    }
    context = {"agent_scorecard": {}, "briefing": {}, "policy_suggestions": [], "candidates": [candidate]}
    first = BrainGovernanceCandidateReviewService._evidence_fingerprint(candidate, context)
    refreshed = {**candidate, "updated_at": 150.0, "expires_at": 250.0}
    second = BrainGovernanceCandidateReviewService._evidence_fingerprint(
        refreshed,
        {**context, "candidates": [refreshed]},
    )

    assert second == first
