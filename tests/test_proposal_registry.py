from __future__ import annotations

import json
import time

from backend.services.proposal_registry import ProposalRegistryService, ensure_proposal_registry_table
from backend.services.policy_suggestion_context import attach_policy_suggestion_agent_context
from backend.services.brain_governance_candidates import ensure_brain_governance_candidate_table
from backend.services.v16_brain_planning import ensure_brain_action_plan_table
from backend.services.autonomous_learning import ensure_autonomous_learning_tables
from backend.core.db import STATE_DB_DDL, connect_sqlite
from backend.services.learning_application_store import LearningApplicationStore


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
    assert policy["source_agent"] == "autonomous_learning"
    assert "DecisionPolicy" in policy["required_gate"]
    assert policy["authority_state"] == "requires_control_gate"
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


def test_proposal_registry_status_separates_actionable_from_historical_noise(tmp_path):
    db_path = tmp_path / "state.db"
    ensure_proposal_registry_table(db_path)
    conn = connect_sqlite(db_path)
    try:
        conn.executemany(
            """
            INSERT INTO proposal_registry
            (proposal_id, source_agent, source_ref_type, proposal_type,
             control_surface, target_scope, impact_level, source_reliability_json,
             evidence_freshness_json, status, route_recommendation, conflict_json,
             created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "policy_suggestion:actionable",
                    "autonomous_learning",
                    "policy_suggestion",
                    "factor_weight",
                    "factor_weight",
                    "factor:rsi_14",
                    "medium",
                    json.dumps({"band": "medium"}),
                    json.dumps({"stale": True, "status": "stale"}),
                    "proposed",
                    "request_review",
                    json.dumps({"conflict": True, "severity": "medium"}),
                    1.0,
                    1.0,
                ),
                (
                    "shadow_audit:old",
                    "lightgbm_shadow_models",
                    "factor_governance_shadow_audit",
                    "model_advisory",
                    "model_stage",
                    "factor_governance_shadow_audit:dsl_auto_old",
                    "shadow",
                    json.dumps({"band": "low", "advisory_only": True}),
                    json.dumps({"stale": True, "status": "stale"}),
                    "shadow",
                    "observe",
                    json.dumps({}),
                    1.0,
                    1.0,
                ),
                (
                    "policy_suggestion:needs_evidence",
                    "autonomous_learning",
                    "policy_suggestion",
                    "factor_weight",
                    "factor_weight",
                    "factor:macd_hist",
                    "medium",
                    json.dumps({"band": "low"}),
                    json.dumps({"stale": True, "status": "stale"}),
                    "needs_evidence",
                    "request_review",
                    json.dumps({}),
                    1.0,
                    1.0,
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    status = ProposalRegistryService(db_path).status()

    assert status["active_count"] == 3
    assert status["actionable_count"] == 1
    assert status["historical_noise_count"] == 2
    assert status["needs_evidence_count"] == 1
    assert status["conflict_count"] == 1
    assert status["stale_evidence_count"] == 1
    assert status["stale_review_required_count"] == 1
    assert status["stale_replay_required_count"] == 0
    assert status["hard_stale_evidence_count"] == 0
    assert status["low_reliability_count"] == 0
    assert status["raw_stale_evidence_count"] == 3
    assert status["raw_low_reliability_count"] == 2


def test_proposal_registry_status_reports_duplicate_and_conflict_groups(tmp_path):
    db_path = tmp_path / "state.db"
    ensure_proposal_registry_table(db_path)
    conn = connect_sqlite(db_path)
    try:
        conn.executemany(
            """
            INSERT INTO proposal_registry
            (proposal_id, source_agent, source_ref_type, proposal_type,
             control_surface, target_scope, impact_level, source_reliability_json,
             evidence_freshness_json, status, route_recommendation, conflict_json,
             created_at, updated_at)
            VALUES (?, 'factor_pruning_governance', 'brain_governance_candidate',
                    'factor_weight', 'factor_weight', 'factor:dsl_auto_x',
                    'medium', '{"band":"medium"}', '{"stale":false}',
                    'proposed', 'submit_governance', ?, ?, ?)
            """,
            [
                ("proposal:dup_1", json.dumps({"conflict": True, "control_surface": "factor_weight"}), 1.0, 1.0),
                ("proposal:dup_2", json.dumps({"conflict": False}), 2.0, 2.0),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    status = ProposalRegistryService(db_path).status()

    assert status["duplicate_group_count"] == 1
    assert status["top_duplicate_groups"][0]["source_agent"] == "factor_pruning_governance"
    assert status["top_duplicate_groups"][0]["count"] == 2
    assert status["conflict_group_count"] == 1
    assert status["conflict_groups"][0] == {"control_surface": "factor_weight", "count": 1}


def test_proposal_registry_repairs_required_generation_context_with_current_notice(tmp_path):
    db_path = tmp_path / "state.db"
    ensure_proposal_registry_table(db_path)
    ensure_autonomous_learning_tables(db_path)
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
            (suggestion_id, scope_type, scope_key, action, confidence,
             evidence_json, status, created_at)
            VALUES ('brain_bridge_missing_context', 'factor', 'dsl_auto_x',
                    'downweight', 0.8, ?, 'approved', ?)
            """,
            (
                json.dumps(
                    {
                        "schema_version": "brain_governance_candidate_policy_suggestion_evidence.v1",
                        "source_agent": "factor_pruning_governance",
                        "candidate_id": "factor_pruning:dsl_auto_x",
                        "bridge": {"candidate_review_required": True},
                        "lineage": {"schema_version": "factor_pruning_lineage.v1"},
                    }
                ),
                time.time(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    service = ProposalRegistryService(db_path)
    before = service.generation_context_coverage(limit=10)
    dry_run = service.repair_missing_generation_context(limit=10, dry_run=True, actor="test")
    repair = service.repair_missing_generation_context(limit=10, dry_run=False, actor="test")
    after = service.generation_context_coverage(limit=10)

    assert before["missing_required_context_count"] == 1
    assert dry_run["candidate_count"] == 1
    assert repair["repaired_count"] == 1
    assert after["status"] == "ok"
    conn = connect_sqlite(db_path, read_only=True)
    try:
        row = conn.execute(
            "SELECT evidence_json FROM policy_suggestion WHERE suggestion_id='brain_bridge_missing_context'"
        ).fetchone()
    finally:
        conn.close()
    evidence = json.loads(row[0])
    assert evidence["agent_generation_context"]["schema_version"] == "agent_generation_context.v1"
    assert evidence["agent_generation_context"]["context_status"] == "repair_current_context"
    assert evidence["agent_generation_context_repair"]["repair_context_is_current_not_original"] is True


def test_policy_suggestion_context_helper_attaches_agent_generation_context(tmp_path):
    payload = attach_policy_suggestion_agent_context(
        {"sample_count": 3},
        source_agent="autonomous_learning",
        scope_type="factor",
        action="downweight",
        requested_writes=["policy_suggestion"],
        status="proposed",
        impact_level="medium",
        db_path=tmp_path / "state.db",
    )

    assert payload["source_agent"] == "autonomous_learning"
    assert payload["agent_context_required"] is True
    assert payload["authority_verdict"]["registered"] is True
    assert payload["agent_context"]["schema_version"] == "agent_generation_context.v1"
    assert payload["agent_generation_context"]["schema_version"] == "agent_generation_context.v1"
    assert payload["agent_context"]["source_agent"] == "autonomous_learning"


def test_proposal_registry_infers_shadow_advisory_source_from_legacy_evidence(tmp_path):
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
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence,
             reason, evidence_json, status, created_at)
            VALUES ('ps_shadow', 'factor', 'rsi_14', 'review_factor_weight_or_template',
                    0.7, 'shadow advisory', ?, 'proposed', ?)
            """,
            (
                json.dumps(
                    {
                        "schema_version": "factor_governance_advisory.v1",
                        "model_type": "factor_governance_lightgbm",
                        "advisory_only": True,
                    }
                ),
                time.time(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    ProposalRegistryService(db_path).refresh()
    proposal = ProposalRegistryService(db_path).get("policy_suggestion:ps_shadow")["proposal"]

    assert proposal["source_agent"] == "lightgbm_shadow_models"
    assert proposal["required_gate"] == ["advisory_only"]
    assert proposal["authority_state"] == "advisory_only"
    assert proposal["source_reliability"]["advisory_only"] is True


def test_proposal_generation_context_coverage_separates_required_and_legacy(tmp_path):
    db_path = tmp_path / "state.db"
    now = time.time()
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
            (suggestion_id, scope_type, scope_key, action, confidence,
             evidence_json, status, created_at)
            VALUES (?, 'factor', ?, 'downweight', 0.8, ?, 'proposed', ?)
            """,
            [
                (
                    "ps_covered",
                    "dsl_auto_covered",
                    json.dumps(
                        {
                            "source_agent": "factor_pruning_governance",
                            "lineage": {
                                "agent_context_required": True,
                                "agent_context": {"schema_version": "agent_generation_context.v1"},
                            },
                        }
                    ),
                    now,
                ),
                (
                    "ps_required_missing",
                    "dsl_auto_required",
                    json.dumps(
                        {
                            "source_agent": "factor_pruning_governance",
                            "bridge": {"candidate_review_required": True},
                        }
                    ),
                    now - 1,
                ),
                (
                    "ps_legacy",
                    "legacy_factor",
                    json.dumps({"source_agent": "autonomous_learning"}),
                    now - 2,
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    coverage = ProposalRegistryService(db_path).generation_context_coverage(limit=10)

    assert coverage["schema_version"] == "proposal_generation_context_coverage.v1"
    assert coverage["status"] == "degraded"
    assert coverage["proposal_count"] == 3
    assert coverage["covered_count"] == 1
    assert coverage["missing_required_context_count"] == 1
    assert coverage["legacy_missing_context_count"] == 1
    statuses = {item["suggestion_id"]: item["coverage_status"] for item in coverage["items"]}
    assert statuses["ps_covered"] == "covered"
    assert statuses["ps_required_missing"] == "missing_required_agent_context"
    assert statuses["ps_legacy"] == "legacy_missing_agent_context"


def test_proposal_registry_reliability_gate_requires_evidence_for_negative_agent_history(tmp_path):
    db_path = tmp_path / "state.db"
    ensure_proposal_registry_table(db_path)
    ensure_autonomous_learning_tables(db_path)
    now = time.time()
    # Converged: lean learning tables come from STATE_DB_DDL; seed the
    # application/effect through the store so reads via the store work.
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence,
             evidence_json, status, created_at)
            VALUES ('ps_low_agent', 'factor', 'rsi_14', 'update_weight',
                    0.8, '{"source_agent":"autonomous_learning"}', 'proposed', ?)
            """,
            (now,),
        )
        conn.commit()
    finally:
        conn.close()
    store = LearningApplicationStore(db_path)
    app_id = store.prepare_application(
        scope_type="factor",
        scope_key="rsi_14",
        action="downweight",
        status="applied",
        suggestion_ids=["ps_low_agent"],
        details={"source_agent": "autonomous_learning"},
        cycle_ts=now - 10,
    )
    store.write_effect(
        application_id=app_id,
        scope_key="rsi_14",
        scope_type="factor",
        action="downweight",
        status="ineffective",
        delta_avg_reward=-0.2,
        decision={"source_agent": "autonomous_learning"},
        updated_at=now - 5,
    )

    ProposalRegistryService(db_path).refresh()
    item = ProposalRegistryService(db_path).get("policy_suggestion:ps_low_agent")["proposal"]

    gate = item["source_reliability"]["agent_reliability_gate"]
    assert gate["review_strictness"] == "high"
    assert "agent_negative_effect_history" in gate["reasons"]
    assert item["status"] == "needs_evidence"
    assert item["route_recommendation"] == "request_review"


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
    assert item["required_gate"] == ["advisory_only"]
    assert item["source_reliability"]["band"] == "low"
    assert item["source_reliability"]["advisory_only"] is True
    refused = ProposalRegistryService(db_path).review("llm_advisory_audit:llm1", decision="approved")
    assert refused["ok"] is False
    assert refused["status"] == "refused_authorizing_review"


def test_projection_compaction_preserves_authoritative_source_ledger(tmp_path):
    db_path = tmp_path / "state.db"
    ensure_proposal_registry_table(db_path)
    conn = connect_sqlite(db_path)
    try:
        conn.execute("CREATE TABLE source_fact (fact_id TEXT PRIMARY KEY, payload TEXT DEFAULT '')")
        conn.execute("INSERT INTO source_fact (fact_id, payload) VALUES ('fact_old', 'kept')")
        conn.execute(
            """
            INSERT INTO proposal_registry
            (proposal_id, status, created_at, updated_at)
            VALUES ('projection_old', 'completed', ?, ?)
            """,
            (time.time() - 40 * 86400.0, time.time() - 40 * 86400.0),
        )
        conn.commit()
    finally:
        conn.close()

    result = ProposalRegistryService(db_path).compact_projection()

    assert result["deleted_count"] == 1
    assert result["source_ledgers_preserved"] is True
    conn = connect_sqlite(db_path, read_only=True)
    try:
        assert conn.execute("SELECT payload FROM source_fact WHERE fact_id='fact_old'").fetchone()[0] == "kept"
        assert conn.execute("SELECT COUNT(*) AS n FROM proposal_registry").fetchone()[0] == 0
    finally:
        conn.close()
