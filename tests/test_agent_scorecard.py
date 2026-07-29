from __future__ import annotations

import json
import time

from backend.core.db import STATE_DB_DDL, connect_sqlite
from backend.services.agent_authority_registry import AgentAuthorityRegistryService
from backend.services.agent_briefing import AgentBriefingContextService
from backend.services.agent_scorecard import AgentScorecardService
from backend.services.trade_lesson_memory import upsert_trade_lesson_memory


def test_agent_chain_health_treats_idle_v16_as_healthy(monkeypatch, tmp_path):
    service = AgentScorecardService(tmp_path / "state.db")
    service.registry = type("Registry", (), {
        "status": lambda *_args, **_kwargs: {"ok": True, "status": "healthy"}
    })()
    monkeypatch.setattr(
        "backend.services.agent_scorecard.ProposalRegistryService",
        lambda _db_path: type("Proposals", (), {
            "status": lambda *_args, **_kwargs: {"proposal_count": 1, "conflict_count": 0}
        })(),
    )
    monkeypatch.setattr(
        service,
        "scorecard",
        lambda **_kwargs: {"items": [{"source_agent": "test"}], "summary": {}},
    )
    monkeypatch.setattr(
        service,
        "latest_trade_attributions",
        lambda **_kwargs: {"ok": True, "status": "available", "summary": {"lesson_count": 1}},
    )
    monkeypatch.setattr(
        "backend.services.entry_quality_governance.EntryQualityGovernanceService",
        lambda _db_path: type("EntryQuality", (), {"status": lambda *_args: {"ok": True, "status": "healthy"}})(),
    )
    monkeypatch.setattr(
        "backend.services.v16_brain_orchestrator.V16BrainOrchestratorService",
        lambda _db_path: type("V16", (), {
            "status": lambda *_args, **_kwargs: {
                "ok": False,
                "status": "no_actionable_command",
                "actionable_command_count": 0,
                "cancelled_command_count": 3,
            }
        })(),
    )

    health = service.chain_health()
    v16 = next(item for item in health["checks"] if item["component"] == "v16_actionable_commands")

    assert health["ok"] is True
    assert v16["status"] == "idle"
    assert v16["ok"] is True


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
    assert factor_agent["terminal_effect_count"] == 1
    assert factor_agent["observing_effect_count"] == 0
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


def test_trade_attribution_counts_lesson_memory_participants_as_linked(tmp_path):
    db_path = tmp_path / "state.db"
    now = time.time()
    conn = _setup_state(db_path)
    try:
        conn.execute(
            """
            INSERT INTO trade_outcome_review
            (review_id, trade_id, position_id, entry_decision_id, exit_decision_id,
             pnl, mae, mfe, outcome_label, failure_tags_json, summary_text,
             review_json, created_at)
            VALUES ('review_lesson_only', 'trade_lesson_only', 'pos_lesson_only', '', '',
                    -10.0, -12.0, 1.0, 'bad_loss', '["weak_entry_signal"]',
                    'lesson only', '{"primary_responsibility":"signal_quality"}', ?)
            """,
            (now,),
        )
        row = conn.execute("SELECT * FROM trade_outcome_review WHERE review_id='review_lesson_only'").fetchone()
        upsert_trade_lesson_memory(conn, row)
        conn.commit()
    finally:
        conn.close()

    attribution = AgentScorecardService(db_path).latest_trade_attributions(
        limit=10,
        include_external_links=False,
    )
    item = attribution["items"][0]

    assert attribution["summary"]["linked_review_count"] == 1
    assert item["review_id"] == "review_lesson_only"
    assert {p["source_agent"] for p in item["participants"]} == {"autonomous_learning"}
    assert "autonomous_learning" in item["feedback_targets"]


def test_posterior_arbitration_filters_canonical_evidence_before_limit(tmp_path):
    db_path = tmp_path / "state.db"
    conn = _setup_state(db_path)
    try:
        conn.execute(
            """
            INSERT INTO trade_outcome_review
            (review_id, trade_id, position_id, pnl, outcome_label,
             failure_tags_json, summary_text, review_json, created_at)
            VALUES ('review_clean', 'trade_clean', 'position_shared', -1.0,
                    'good_loss', '[]', 'clean review',
                    '{"system_issue_context":{"contaminates_learning":false}}',
                    100.0)
            """
        )
        conn.execute(
            """
            INSERT INTO trade_outcome_review
            (review_id, trade_id, position_id, pnl, outcome_label,
             failure_tags_json, summary_text, review_json, created_at)
            VALUES ('review_dirty', 'trade_dirty', 'position_shared', -1.0,
                    'bad_loss', '[]', 'dirty review',
                    '{"system_issue_context":{"contaminates_learning":true}}',
                    99.0)
            """
        )
        conn.execute(
            """
            INSERT INTO supervisor_counterfactual_review
            (counterfactual_id, review_id, trade_id, position_id, close_ts,
             label, confidence, horizons_json, evidence_json, created_at, updated_at)
            VALUES ('cf_valid', 'review_clean', 'trade_clean', 'position_shared',
                    100.0, 'correct_stop', 0.8, '[{"horizon_minutes":30}]',
                    '{}', 100.0, 100.0)
            """
        )
        for index in range(10):
            conn.execute(
                """
                INSERT INTO supervisor_counterfactual_review
                (counterfactual_id, review_id, trade_id, position_id, close_ts,
                 label, confidence, horizons_json, evidence_json,
                 created_at, updated_at)
                VALUES (?, 'review_clean', 'trade_clean', 'position_shared',
                        ?, 'correct_stop', 0.9, '[]',
                        '{"evidence_invalidated":true}', ?, ?)
                """,
                (
                    f"cf_invalidated_{index}",
                    200.0 + index,
                    200.0 + index,
                    200.0 + index,
                ),
            )
        conn.execute(
            """
            INSERT INTO supervisor_counterfactual_review
            (counterfactual_id, review_id, trade_id, position_id, close_ts,
             label, confidence, horizons_json, evidence_json, created_at, updated_at)
            VALUES ('cf_dirty', 'review_dirty', 'trade_dirty', 'position_shared',
                    400.0, 'correct_stop', 0.9, '[]', '{}', 400.0, 400.0)
            """
        )
        conn.execute(
            """
            INSERT INTO supervisor_counterfactual_review
            (counterfactual_id, review_id, trade_id, position_id, close_ts,
             label, confidence, horizons_json, evidence_json, created_at, updated_at)
            VALUES ('cf_orphan', 'missing_review', 'trade_orphan', 'position_shared',
                    500.0, 'correct_stop', 0.9, '[]', '{}', 500.0, 500.0)
            """
        )
        conn.commit()
    finally:
        conn.close()

    attribution = AgentScorecardService(db_path).latest_trade_attributions(
        limit=1,
        include_external_links=False,
    )

    assert [
        item["counterfactual_id"]
        for item in attribution["items"][0]["counterfactuals"]
    ] == ["cf_valid"]


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


def test_candidate_only_quality_uses_candidate_lifecycle_denominator():
    score = AgentScorecardService._quality_score(
        {
            "proposal_count": 0,
            "candidate_count": 171,
            "policy_suggestion_count": 0,
            "application_count": 0,
            "status_counts": {"superseded": 91, "submitted": 61, "active": 19},
        }
    )

    assert score > 0.5


def test_candidate_only_quality_still_penalizes_failed_lifecycle():
    score = AgentScorecardService._quality_score(
        {
            "proposal_count": 0,
            "candidate_count": 20,
            "policy_suggestion_count": 0,
            "application_count": 0,
            "status_counts": {"superseded": 20},
        }
    )

    assert score < 0.5


def test_agent_generation_context_includes_scope_relevant_experience(tmp_path):
    db_path = tmp_path / "state.db"
    conn = _setup_state(db_path)
    try:
        for factor, reward in (("rsi_14", -0.6), ("macd", 0.4)):
            review_id = f"review_{factor}"
            conn.execute(
                """
                INSERT INTO trade_outcome_review
                (review_id, trade_id, position_id, review_json, created_at)
                VALUES (?, ?, ?, '{}', ?)
                """,
                (review_id, f"trade_{factor}", f"position_{factor}", time.time()),
            )
            conn.execute(
                """
                INSERT INTO experience_memory
                (experience_id, trade_id, source_table, source_id, append_source,
                 regime_id, setup_hash, decision_context_json,
                 outcome_label, reward_score, failure_tags_json, recommended_action,
                 evidence_strength, artifact_version, created_at)
                    VALUES (?, ?, 'trade_outcome_review', ?, 'trade_lesson_memory.v1',
                        'range', ?, ?, 'bad_loss', ?, '["weak_entry_signal"]',
                        'downweight', 0.9, 'v1', ?)
                """,
                (
                    f"exp_{factor}",
                    f"trade_{factor}",
                    review_id,
                    f"setup_{factor}",
                    json.dumps({"primary_factor": factor, "summary_text": f"lesson for {factor}"}),
                    reward,
                    time.time(),
                ),
            )
        conn.commit()
    finally:
        conn.close()

    context = AgentBriefingContextService(db_path).agent_context(
        "factor_governance",
        scope_type="factor",
        scope_key="rsi_14",
        action="update_weight",
        requested_writes=["policy_suggestion"],
    )

    assert [item["primary_factor"] for item in context["relevant_experience"]] == ["rsi_14"]
    assert context["relevant_experience"][0]["recommended_action"] == "downweight"
