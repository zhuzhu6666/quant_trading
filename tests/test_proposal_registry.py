from __future__ import annotations

import json
import time

from backend.services.proposal_registry import ProposalRegistryService, ensure_proposal_registry_table
from backend.services.brain_governance_candidates import ensure_brain_governance_candidate_table
from backend.services.brain_action_planner import ensure_brain_action_plan_table
from backend.core.db import connect_sqlite


def test_proposal_registry_normalizes_policy_candidate_and_action_plan(tmp_path):
    db_path = tmp_path / "state.db"
    now = time.time()
    ensure_proposal_registry_table(db_path)
    ensure_brain_governance_candidate_table(db_path)
    ensure_brain_action_plan_table(db_path)
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS policy_suggestion (
                suggestion_id TEXT PRIMARY KEY,
                scope_type TEXT NOT NULL,
                scope_key TEXT NOT NULL,
                action TEXT NOT NULL,
                confidence REAL DEFAULT 0.0,
                reason TEXT DEFAULT '',
                evidence_json TEXT DEFAULT '{}',
                status TEXT DEFAULT 'proposed',
                reviewed_at REAL DEFAULT 0.0,
                review_note TEXT DEFAULT '',
                created_at REAL NOT NULL DEFAULT 0.0
            )
            """
        )
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence, evidence_json, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "ps1",
                "factor",
                "rsi_14",
                "update_weight",
                0.8,
                json.dumps({"risk_verdict": {"allowed": True}, "expected_effect": {"reward_delta": 0.1}}),
                "proposed",
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO brain_governance_candidate
            (candidate_id, source_agent, source_kind, source_ref_type, source_ref_id,
             proposal_stage, capability_scope, scope_type, scope_key, action,
             confidence, evidence_score, risk_class, max_impact, expected_effect_json,
             evidence_refs_json, counter_evidence_refs_json, risk_verdict_json,
             decision_policy_json, rollback_plan_json, lineage_json, status,
             submitted_suggestion_id, submitted_at, expires_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 0, 0, ?, ?)
            """,
            (
                "bc1",
                "v16_brain",
                "brain_medium_impact_governance",
                "brain_action_plan_eval",
                "eval1",
                "governance_ready",
                "medium_impact_governance",
                "factor",
                "rsi_14",
                "update_weight",
                0.7,
                0.9,
                "medium",
                "medium_impact",
                "{}",
                "{}",
                "{}",
                json.dumps({"allowed": True}),
                "{}",
                "{}",
                "{}",
                "active",
                now,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO brain_action_plan
            (plan_id, action_type, status, scope_json, max_impact, required_services_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "plan1",
                "shadow_supervisor_template_review",
                "shadow_recorded",
                json.dumps({"scope_type": "supervisor_template", "scope_key": "position_supervisor"}),
                "none_shadow_only",
                json.dumps(["ReplayHarnessService", "RiskPolicyService"]),
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    refresh = ProposalRegistryService(db_path).refresh()
    assert refresh["ok"] is True
    listing = ProposalRegistryService(db_path).latest(limit=10)
    ids = {item["proposal_id"] for item in listing["items"]}
    assert "policy_suggestion:ps1" in ids
    assert "brain_governance_candidate:bc1" in ids
    assert "brain_action_plan:plan1" in ids
    policy = next(item for item in listing["items"] if item["proposal_id"] == "policy_suggestion:ps1")
    assert policy["control_surface"] == "factor_weight"
    assert "DecisionPolicy" in policy["required_gate"]
    assert policy["source_reliability"]["band"] in {"medium", "high"}
    assert policy["evidence_freshness"]["status"] == "fresh"


def test_proposal_registry_detects_control_surface_conflict(tmp_path):
    db_path = tmp_path / "state.db"
    ensure_proposal_registry_table(db_path)
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS policy_suggestion (
                suggestion_id TEXT PRIMARY KEY,
                scope_type TEXT NOT NULL,
                scope_key TEXT NOT NULL,
                action TEXT NOT NULL,
                confidence REAL DEFAULT 0.0,
                reason TEXT DEFAULT '',
                evidence_json TEXT DEFAULT '{}',
                status TEXT DEFAULT 'proposed',
                reviewed_at REAL DEFAULT 0.0,
                review_note TEXT DEFAULT '',
                created_at REAL NOT NULL DEFAULT 0.0
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence, evidence_json, status, created_at)
            VALUES (?, 'factor', 'rsi_14', ?, 0.7, '{}', 'proposed', ?)
            """,
            [("ps_down", "update_weight", 10.0), ("ps_promote", "promote_factor", 11.0)],
        )
        conn.commit()
    finally:
        conn.close()

    ProposalRegistryService(db_path).refresh()
    status = ProposalRegistryService(db_path).status()
    assert status["conflict_count"] >= 2


def test_proposal_review_refuses_authorization_and_keeps_llm_advisory_read_only(tmp_path):
    db_path = tmp_path / "state.db"
    ensure_proposal_registry_table(db_path)
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS llm_advisory_audit (
                audit_id TEXT PRIMARY KEY,
                provider TEXT DEFAULT '',
                model TEXT DEFAULT '',
                task_type TEXT DEFAULT '',
                target_type TEXT DEFAULT '',
                target_id TEXT DEFAULT '',
                status TEXT DEFAULT '',
                prompt_json TEXT DEFAULT '{}',
                response_json TEXT DEFAULT '{}',
                result_json TEXT DEFAULT '{}',
                error TEXT DEFAULT '',
                created_at REAL NOT NULL DEFAULT 0.0
            )
            """
        )
        conn.execute(
            """
            INSERT INTO llm_advisory_audit
            (audit_id, task_type, target_type, target_id, status, result_json, created_at)
            VALUES ('llm1', 'candidate_review', 'factor', 'rsi_14', 'recorded', '{}', 10.0)
            """
        )
        conn.commit()
    finally:
        conn.close()

    ProposalRegistryService(db_path).refresh()
    item = ProposalRegistryService(db_path).get("llm_advisory_audit:llm1")["proposal"]
    assert item["authority_state"] == "advisory_only"
    assert item["source_reliability"]["band"] == "low"
    assert item["source_reliability"]["advisory_only"] is True
    refused = ProposalRegistryService(db_path).review("llm_advisory_audit:llm1", decision="approved")
    assert refused["ok"] is False
    assert refused["status"] == "refused_authorizing_review"
