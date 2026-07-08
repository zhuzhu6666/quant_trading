from __future__ import annotations

import json
import time

from backend.core.db import STATE_DB_DDL, connect_sqlite
from backend.services.agent_authority_registry import AgentAuthorityRegistryService
from backend.services.agent_briefing import AgentBriefingContextService
from backend.services.agent_scorecard import AgentScorecardService
from backend.services.trade_lesson_memory import upsert_trade_lesson_memory


def _setup_state(db_path):
    conn = connect_sqlite(db_path)
    conn.row_factory = __import__("sqlite3").Row
    conn.executescript(STATE_DB_DDL)
    return conn


def test_agent_scorecard_counts_proposals_applications_and_effects(tmp_path):
    db_path = tmp_path / "state.db"
    now = time.time()
    conn = _setup_state(db_path)
    try:
        verdict = AgentAuthorityRegistryService().evaluate_scope_write(
            "factor_governance",
            "factor",
            "downweight",
            requested_writes=["policy_suggestion"],
            status="proposed",
            impact_level="medium",
        )
        conn.execute(
            """
            INSERT INTO proposal_registry
            (proposal_id, source_agent, source_ref_type, source_ref_id, proposal_type,
             control_surface, target_scope, impact_level, confidence, evidence_refs_json,
             counter_evidence_refs_json, required_gate_json, risk_verdict_json,
             decision_policy_preview_json, expected_effect_json, rollback_plan_json,
             source_reliability_json, evidence_freshness_json, status, authority_state,
             route_recommendation, conflict_json, review_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "proposal_factor_1",
                "factor_governance",
                "policy_suggestion",
                "s1",
                "factor_weight",
                "factor_weight",
                "factor:rsi_14",
                "medium",
                0.8,
                "{}",
                "{}",
                json.dumps(["DecisionPolicy", "RiskPolicyService"]),
                "{}",
                "{}",
                "{}",
                "{}",
                json.dumps({"band": "high"}),
                json.dumps({"stale": False}),
                "proposed",
                "requires_control_gate",
                "submit_governance",
                "{}",
                "{}",
                now,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence, reason,
             evidence_json, status, reviewed_at, review_note, created_at)
            VALUES ('s1', 'factor', 'rsi_14', 'downweight', 0.8, 'test',
                    ?, 'approved', ?, 'ok', ?)
            """,
            (json.dumps({"source_agent": "factor_governance", "authority_verdict": verdict}), now, now),
        )
        conn.execute(
            """
            INSERT INTO learning_application_log
            (application_id, cycle_ts, scope_type, scope_key, action, bias_multiplier,
             old_weight, new_weight, suggestion_ids_json, status, details_json, created_at)
            VALUES ('app1', ?, 'factor', 'rsi_14', 'downweight', 0.8, 0.3, 0.2,
                    '["s1"]', 'applied', ?, ?)
            """,
            (
                now,
                json.dumps({"source_agent": "factor_governance", "authority_verdict": verdict}),
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO learning_application_effect
            (application_id, scope_type, scope_key, action, status, delta_avg_reward,
             updated_at, created_at)
            VALUES ('app1', 'factor', 'rsi_14', 'downweight', 'effective', 0.12, ?, ?)
            """,
            (now, now),
        )
        conn.commit()
    finally:
        conn.close()

    scorecard = AgentScorecardService(db_path).scorecard(limit=50)
    factor_agent = next(item for item in scorecard["items"] if item["source_agent"] == "factor_governance")

    assert factor_agent["proposal_count"] == 1
    assert factor_agent["policy_suggestion_count"] == 1
    assert factor_agent["application_count"] == 1
    assert factor_agent["positive_effect_count"] == 1
    assert factor_agent["contract_violation_count"] == 0
    assert scorecard["summary"]["application_count"] == 1


def test_trade_attribution_links_review_to_agents_and_lesson_memory(tmp_path):
    db_path = tmp_path / "state.db"
    now = time.time()
    conn = _setup_state(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS open_quality_shadow_audit (
                inference_id TEXT PRIMARY KEY,
                trade_id TEXT DEFAULT '',
                position_id TEXT DEFAULT '',
                created_at REAL NOT NULL DEFAULT 0.0
            )
            """
        )
        conn.execute(
            """
            INSERT INTO trade_outcome_review
            (review_id, trade_id, position_id, entry_decision_id, exit_decision_id,
             pnl, mae, mfe, outcome_label, failure_tags_json, summary_text,
             review_json, created_at)
            VALUES ('review_1', 'trade_1', 'pos_1', 'entry_1', 'exit_1',
                    -20.0, -25.0, 2.0, 'bad_loss', '["weak_entry_signal"]',
                    'weak entry', '{"primary_responsibility":"signal_quality"}', ?)
            """,
            (now,),
        )
        conn.execute(
            "INSERT INTO open_quality_shadow_audit (inference_id, trade_id, position_id, created_at) VALUES ('inf1', 'trade_1', 'pos_1', ?)",
            (now,),
        )
        conn.execute(
            """
            INSERT INTO proposal_registry
            (proposal_id, source_agent, source_ref_type, source_ref_id, proposal_type,
             control_surface, target_scope, impact_level, confidence, evidence_refs_json,
             counter_evidence_refs_json, required_gate_json, risk_verdict_json,
             decision_policy_preview_json, expected_effect_json, rollback_plan_json,
             source_reliability_json, evidence_freshness_json, status, authority_state,
             route_recommendation, conflict_json, review_json, created_at, updated_at)
            VALUES ('proposal_1', 'factor_governance', 'policy_suggestion', 's1',
                    'factor_weight', 'factor_weight', 'factor:rsi_14', 'medium', 0.7,
                    '{"trade_id":"trade_1"}', '{}', '["DecisionPolicy","RiskPolicyService"]',
                    '{}', '{}', '{}', '{}', '{}', '{}', 'proposed',
                    'requires_control_gate', 'submit_governance', '{}', '{}', ?, ?)
            """,
            (now, now),
        )
        row = conn.execute("SELECT * FROM trade_outcome_review WHERE review_id='review_1'").fetchone()
        upsert_trade_lesson_memory(conn, row)
        conn.commit()
    finally:
        conn.close()

    attribution = AgentScorecardService(db_path).latest_trade_attributions(limit=10)
    item = attribution["items"][0]

    assert item["review_id"] == "review_1"
    assert {p["source_agent"] for p in item["participants"]} >= {"lightgbm_shadow_models", "factor_governance"}
    assert item["lesson"]["recommended_action"] == "tighten_entry_review"
    assert "factor_governance" in item["feedback_targets"]


def test_agent_briefing_includes_governance_coverage(tmp_path):
    db_path = tmp_path / "state.db"
    conn = _setup_state(db_path)
    try:
        conn.commit()
    finally:
        conn.close()

    briefing = AgentBriefingContextService(db_path).build(limit=5)

    assert briefing["schema_version"] == "agent_briefing_context.v1"
    assert briefing["governance_coverage"]["schema_version"] == "agent_briefing_governance_coverage.v1"
    assert briefing["governance_coverage"]["status"] == "ok"
    assert briefing["governance_coverage"]["proposal_generation_context_coverage"]["status"] == "ok"
    assert briefing["review_rules"]["candidate_context_required"] is True
    assert briefing["review_rules"]["candidate_review_required_before_bridge"] is True
