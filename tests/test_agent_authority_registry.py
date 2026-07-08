from __future__ import annotations

import json

from backend.core.db import connect_sqlite
from backend.services.agent_authority_registry import AgentAuthorityRegistryService


def test_agent_authority_registry_lists_current_source_agents():
    registry = AgentAuthorityRegistryService()

    listing = registry.list_agents()

    assert listing["schema_version"] == "agent_authority_registry.v1"
    assert listing["registered_agents"] == 6
    assert {item["source_agent"] for item in listing["sources"]} == {
        "v16_brain",
        "autonomous_learning",
        "factor_governance",
        "factor_pruning_governance",
        "llm_advisory",
        "lightgbm_shadow_models",
    }


def test_agent_authority_unknown_source_is_review_only_blocked():
    verdict = AgentAuthorityRegistryService().evaluate(
        "mystery_agent",
        "factor_weight",
        "update_weight",
        requested_writes=["policy_suggestion"],
        status="proposed",
        impact_level="medium",
    )

    assert verdict["registered"] is False
    assert verdict["allowed"] is False
    assert verdict["authority_state"] == "blocked_by_agent_authority"
    assert "unknown_source_agent" in verdict["violations"]


def test_llm_advisory_cannot_gain_executable_authority():
    verdict = AgentAuthorityRegistryService().evaluate(
        "llm_advisory",
        "factor_weight",
        "update_weight",
        requested_writes=["policy_suggestion"],
        status="proposed",
        impact_level="medium",
    )

    assert verdict["required_gate"] == ["advisory_only"]
    assert verdict["authority_state"] == "advisory_only"
    assert verdict["advisory_only"] is True
    assert verdict["allowed"] is False
    assert "write_not_allowed:policy_suggestion" in verdict["violations"]


def test_lightgbm_shadow_models_remain_advisory_only():
    verdict = AgentAuthorityRegistryService().evaluate(
        "lightgbm_shadow_models",
        "factor_weight",
        "review_factor_weight_or_template",
        requested_writes=[],
        status="proposed",
        impact_level="shadow",
    )

    assert verdict["required_gate"] == ["advisory_only"]
    assert verdict["authority_state"] == "advisory_only"
    assert verdict["advisory_only"] is True
    assert verdict["allowed"] is False


def test_factor_governance_can_propose_but_not_bypass_control_gates():
    verdict = AgentAuthorityRegistryService().evaluate(
        "factor_governance",
        "factor_weight",
        "downweight",
        requested_writes=["policy_suggestion"],
        status="proposed",
        impact_level="medium",
    )

    assert verdict["allowed"] is True
    assert verdict["authority_state"] == "requires_control_gate"
    assert verdict["required_gate"] == ["DecisionPolicy", "RiskPolicyService"]


def test_agent_authority_status_reports_unknown_sources(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE proposal_registry (
                proposal_id TEXT PRIMARY KEY,
                source_agent TEXT DEFAULT '',
                control_surface TEXT DEFAULT '',
                proposal_type TEXT DEFAULT '',
                required_gate_json TEXT NOT NULL DEFAULT '[]',
                authority_state TEXT DEFAULT '',
                status TEXT DEFAULT '',
                updated_at REAL NOT NULL DEFAULT 0.0
            )
            """
        )
        conn.execute(
            """
            INSERT INTO proposal_registry
            (proposal_id, source_agent, control_surface, proposal_type,
             required_gate_json, authority_state, status, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "proposal:unknown",
                "mystery_agent",
                "factor_weight",
                "update_weight",
                json.dumps(["DecisionPolicy", "RiskPolicyService"]),
                "requires_control_gate",
                "proposed",
                10.0,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    status = AgentAuthorityRegistryService().status(db_path=db_path)

    assert status["status"] == "degraded"
    assert status["registered_agents"] == 6
    assert status["unknown_sources"][0]["source_agent"] == "mystery_agent"


def test_agent_authority_status_flags_shadow_model_executable_gate(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE proposal_registry (
                proposal_id TEXT PRIMARY KEY,
                source_agent TEXT DEFAULT '',
                control_surface TEXT DEFAULT '',
                proposal_type TEXT DEFAULT '',
                required_gate_json TEXT NOT NULL DEFAULT '[]',
                authority_state TEXT DEFAULT '',
                status TEXT DEFAULT '',
                updated_at REAL NOT NULL DEFAULT 0.0
            )
            """
        )
        conn.execute(
            """
            INSERT INTO proposal_registry
            (proposal_id, source_agent, control_surface, proposal_type,
             required_gate_json, authority_state, status, updated_at)
            VALUES ('proposal:shadow_bad_gate', 'lightgbm_shadow_models', 'factor_weight',
                    'review_factor_weight_or_template', ?, 'requires_control_gate',
                    'proposed', 10.0)
            """,
            (json.dumps(["DecisionPolicy", "RiskPolicyService"]),),
        )
        conn.commit()
    finally:
        conn.close()

    status = AgentAuthorityRegistryService().status(db_path=db_path)

    assert status["status"] == "degraded"
    assert status["contract_violations"][0]["reason"] == "advisory_source_gate_must_be_advisory_only"


def test_manual_bridge_policy_suggestion_is_not_direct_write_violation(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE policy_suggestion (
                suggestion_id TEXT PRIMARY KEY,
                scope_type TEXT NOT NULL,
                scope_key TEXT NOT NULL,
                action TEXT NOT NULL,
                confidence REAL DEFAULT 0.0,
                reason TEXT DEFAULT '',
                evidence_json TEXT DEFAULT '{}',
                status TEXT DEFAULT 'proposed',
                created_at REAL NOT NULL DEFAULT 0.0
            )
            """
        )
        evidence = {
            "source_agent": "factor_pruning_governance",
            "bridge": {"manual_only": True, "actor": "test"},
        }
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence,
             reason, evidence_json, status, created_at)
            VALUES ('brain_bridge_1', 'factor', 'rsi_14', 'downweight',
                    0.7, 'manual bridge', ?, 'proposed', 10.0)
            """,
            (json.dumps(evidence),),
        )
        conn.commit()
    finally:
        conn.close()

    status = AgentAuthorityRegistryService().status(db_path=db_path)

    assert status["status"] == "ok"
    assert status["contract_violations"] == []
