from __future__ import annotations

import json
import time

from backend.core.db import STATE_DB_DDL, connect_sqlite
from backend.services.agent_authority import AgentAuthorityRegistryService
from backend.services.agent_briefing import AgentBriefingContextService
from backend.services.agent_scorecard import AgentScorecardService
from backend.services.brain_governance_candidates import ensure_brain_governance_candidate_table
from backend.services.canonical_v2 import (
    ensure_sqlite_schema,
    record_counterfactual_event,
    record_review,
)
from backend.services.canonical_v2_reader import review_row
from backend.services.learning_application_store import LearningApplicationStore
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
    ensure_sqlite_schema(conn)
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
        conn.commit()
    finally:
        conn.close()

    store = LearningApplicationStore(db_path)
    app_id = store.prepare_application(
        scope_type="factor", scope_key="rsi_14", action="downweight",
        bias_multiplier=0.8, old_weight=0.3, new_weight=0.2,
        suggestion_ids=["s1"], status="applied", cycle_ts=now,
        details={"source_agent": "factor_governance", "authority_verdict": verdict},
    )
    store.write_effect(
        application_id=app_id, scope_key="rsi_14", scope_type="factor",
        action="downweight", status="effective", delta_avg_reward=0.12,
        updated_at=now,
    )

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
        record_review(
            conn,
            review_id="review_1",
            trade_id="trade_1",
            position_id="pos_1",
            entry_decision_id="entry_1",
            exit_decision_id="exit_1",
            pnl=-20.0,
            mae=-25.0,
            mfe=2.0,
            outcome_label="bad_loss",
            failure_tags=["weak_entry_signal"],
            summary_text="weak entry",
            review={"primary_responsibility": "signal_quality"},
            created_at=now,
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
        row = review_row(conn, "review_1")
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
        record_review(
            conn,
            review_id="review_lesson_only",
            trade_id="trade_lesson_only",
            position_id="pos_lesson_only",
            pnl=-10.0,
            mae=-12.0,
            mfe=1.0,
            outcome_label="bad_loss",
            failure_tags=["weak_entry_signal"],
            summary_text="lesson only",
            review={"primary_responsibility": "signal_quality"},
            created_at=now,
        )
        row = review_row(conn, "review_lesson_only")
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


def test_posterior_arbitration_filters_invalid_canonical_evidence(tmp_path):
    db_path = tmp_path / "state.db"
    conn = _setup_state(db_path)
    try:
        record_review(
            conn,
            review_id="review_clean",
            trade_id="trade_clean",
            position_id="position_shared",
            pnl=-1.0,
            outcome_label="good_loss",
            failure_tags=[],
            summary_text="clean review",
            review={"system_issue_context": {"contaminates_learning": False}},
            created_at=100.0,
        )
        record_review(
            conn,
            review_id="review_dirty",
            trade_id="trade_dirty",
            position_id="position_shared",
            pnl=-1.0,
            outcome_label="bad_loss",
            failure_tags=[],
            summary_text="dirty review",
            review={"system_issue_context": {"contaminates_learning": True}},
            created_at=99.0,
        )
        record_counterfactual_event(
            conn,
            counterfactual_id="cf_valid",
            review_id="review_clean",
            event_ts=100.0,
            payload={
                "counterfactual_id": "cf_valid",
                "review_id": "review_clean",
                "trade_id": "trade_clean",
                "position_id": "position_shared",
                "close_ts": 100.0,
                "label": "correct_stop",
                "confidence": 0.8,
                "horizons": [{"horizon_minutes": 30}],
                "evidence": {},
            },
        )
        # The canonical reader applies its limit after logical-id deduplication
        # and before scorecard-level evidence filtering. Keep the valid item
        # inside that bounded read while retaining invalid and orphaned rows.
        for index in range(9):
            counterfactual_id = f"cf_invalidated_{index}"
            event_ts = 200.0 + index
            record_counterfactual_event(
                conn,
                counterfactual_id=counterfactual_id,
                review_id="review_clean",
                event_ts=event_ts,
                payload={
                    "counterfactual_id": counterfactual_id,
                    "review_id": "review_clean",
                    "trade_id": "trade_clean",
                    "position_id": "position_shared",
                    "close_ts": event_ts,
                    "label": "correct_stop",
                    "confidence": 0.9,
                    "horizons": [],
                    "evidence": {"evidence_invalidated": True},
                },
            )
        record_counterfactual_event(
            conn,
            counterfactual_id="cf_dirty",
            review_id="review_dirty",
            event_ts=400.0,
            payload={
                "counterfactual_id": "cf_dirty",
                "review_id": "review_dirty",
                "trade_id": "trade_dirty",
                "position_id": "position_shared",
                "close_ts": 400.0,
                "label": "correct_stop",
                "confidence": 0.9,
                "horizons": [],
                "evidence": {},
            },
        )
        record_counterfactual_event(
            conn,
            counterfactual_id="cf_orphan",
            review_id="missing_review",
            event_ts=500.0,
            payload={
                "counterfactual_id": "cf_orphan",
                "review_id": "missing_review",
                "trade_id": "trade_orphan",
                "position_id": "position_shared",
                "close_ts": 500.0,
                "label": "correct_stop",
                "confidence": 0.9,
                "horizons": [],
                "evidence": {},
            },
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


def test_candidate_only_quality_ignores_posterior_not_selected_rotation():
    score = AgentScorecardService._quality_score(
        {
            "proposal_count": 0,
            "candidate_count": 20,
            "policy_suggestion_count": 0,
            "application_count": 0,
            "status_counts": {"superseded": 20},
            "_posterior_not_selected_count": 20,
        }
    )

    assert score == 0.55


def test_candidate_only_quality_penalizes_only_non_rotation_superseded():
    rotation_only = AgentScorecardService._quality_score(
        {
            "proposal_count": 0,
            "candidate_count": 20,
            "policy_suggestion_count": 0,
            "application_count": 0,
            "status_counts": {"superseded": 20},
            "_posterior_not_selected_count": 20,
        }
    )
    mixed = AgentScorecardService._quality_score(
        {
            "proposal_count": 0,
            "candidate_count": 20,
            "policy_suggestion_count": 0,
            "application_count": 0,
            "status_counts": {"superseded": 20},
            "_posterior_not_selected_count": 19,
        }
    )
    failed = AgentScorecardService._quality_score(
        {
            "proposal_count": 0,
            "candidate_count": 20,
            "policy_suggestion_count": 0,
            "application_count": 0,
            "status_counts": {"superseded": 20},
        }
    )

    assert rotation_only > mixed > failed


def test_agent_scorecard_keeps_rotation_detail_private_and_source_eligible(tmp_path):
    db_path = tmp_path / "state.db"
    conn = _setup_state(db_path)
    try:
        conn.commit()
    finally:
        conn.close()

    ensure_brain_governance_candidate_table(db_path)
    now = time.time()
    conn = connect_sqlite(db_path)
    try:
        conn.executemany(
            """
            INSERT INTO brain_governance_candidate
            (candidate_id, source_agent, proposal_stage, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (f"v16_rotation_{index}", "v16_brain", "posterior_not_selected", "superseded", now, now)
                for index in range(20)
            ],
        )
        conn.commit()
    finally:
        conn.close()

    scorecard = AgentScorecardService(db_path).scorecard(limit=50)
    v16 = next(item for item in scorecard["items"] if item["source_agent"] == "v16_brain")

    assert v16["candidate_count"] == 20
    assert v16["quality_score"] == 0.55
    assert "_posterior_not_selected_count" not in v16


def test_agent_generation_context_includes_scope_relevant_experience(tmp_path):
    db_path = tmp_path / "state.db"
    conn = _setup_state(db_path)
    try:
        for factor, reward in (("rsi_14", -0.6), ("macd", 0.4)):
            review_id = f"review_{factor}"
            record_review(
                conn,
                review_id=review_id,
                trade_id=f"trade_{factor}",
                position_id=f"position_{factor}",
                failure_tags=[],
                review={},
                created_at=time.time(),
            )
            conn.execute(
                """
                INSERT INTO experience_memory
                (experience_id, trade_id, source_table, source_id, append_source,
                 regime_id, setup_hash, decision_context_json,
                 outcome_label, reward_score, failure_tags_json, recommended_action,
                 evidence_strength, artifact_version, created_at)
                    VALUES (?, ?, 'canonical_v2.trade_review', ?, 'trade_lesson_memory.v1',
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


def test_agent_generation_context_does_not_promote_raw_entry_action_after_supervisor_posterior(tmp_path):
    db_path = tmp_path / "state.db"
    conn = _setup_state(db_path)
    try:
        now = time.time()
        record_review(
            conn,
            review_id="review_posterior",
            trade_id="trade_posterior",
            position_id="position_posterior",
            pnl=-1.0,
            outcome_label="bad_loss",
            failure_tags=["weak_entry_signal"],
            review={"primary_responsibility": "signal_quality"},
            created_at=now,
        )
        conn.execute(
            """
            INSERT INTO experience_memory
            (experience_id, trade_id, source_table, source_id, append_source,
             decision_context_json, outcome_label, reward_score,
             failure_tags_json, recommended_action, evidence_strength,
             artifact_version, created_at)
            VALUES ('exp_posterior', 'trade_posterior', 'canonical_v2.trade_review',
                    'review_posterior', 'trade_lesson_memory.v1',
                    '{"primary_factor": "rsi_14"}', 'bad_loss', -0.6,
                    '["weak_entry_signal"]', 'downweight', 0.9, 'v1', ?)
            """,
            (now,),
        )
        record_counterfactual_event(
            conn,
            counterfactual_id="cf_posterior",
            review_id="review_posterior",
            event_ts=now,
            payload={
                "counterfactual_id": "cf_posterior",
                "review_id": "review_posterior",
                "trade_id": "trade_posterior",
                "position_id": "position_posterior",
                "close_ts": now,
                "label": "premature_tighten",
                "confidence": 0.8,
                "horizons": [{"horizon_minutes": 30, "future_pnl": 4.0}],
                "evidence": {"tags": ["future_recovered", "original_tp_first"]},
            },
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

    item = context["relevant_experience"][0]
    assert item["source_recommended_action"] == "downweight"
    assert item["recommended_action"] == "observe_and_compare"
    assert item["evidence_eligible"] is False
    assert item["posterior_action"] == "less_tighten"
    assert item["posterior_reconciliation"]["status"] == "entry_conclusion_retained"
