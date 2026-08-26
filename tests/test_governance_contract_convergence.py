from __future__ import annotations

import time

from backend.core.db import STATE_DB_DDL, connect_sqlite
from backend.services.brain_governance_candidates import BrainGovernanceCandidateService
from backend.services.v16_brain_planning import BrainMediumImpactGovernanceService
from backend.services.brain_governance_candidate_review import BrainGovernanceCandidateReviewService
from backend.services.v16_brain_orchestrator import (
    V16BrainOrchestratorService,
    ensure_v16_brain_command_table,
)
from backend.services.v16_command_gate import V16CommandGate


def _db(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    conn.executescript(STATE_DB_DDL)
    conn.commit()
    conn.close()
    ensure_v16_brain_command_table(db_path)
    return db_path


def test_candidate_creation_fails_closed_for_advisory_source(tmp_path):
    db_path = _db(tmp_path)
    service = BrainGovernanceCandidateService(db_path)

    result = service.create_candidate(
        candidate_id="llm_candidate_denied",
        source_agent="llm_advisory",
        source_kind="test_advisory",
        source_ref_type="test",
        source_ref_id="audit-1",
        proposal_stage="governance_ready",
        capability_scope="factor_weight",
        scope_type="factor",
        scope_key="rsi_14",
        action="downweight",
        confidence=0.9,
        evidence_score=0.9,
        risk_class="risk_tightening",
        max_impact="shadow",
        risk_verdict={"allowed": True},
    )

    assert result["ok"] is False
    assert result["status"] == "authority_denied"
    assert result["authority_verdict"]["allowed"] is False

    conn = connect_sqlite(db_path, read_only=True)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM brain_governance_candidate WHERE candidate_id=?",
            ("llm_candidate_denied",),
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 0


def test_v16_claim_requires_bridge_ready_review_for_linked_candidate(tmp_path):
    db_path = _db(tmp_path)
    candidate = BrainGovernanceCandidateService(db_path).create_candidate(
        candidate_id="candidate_without_review",
        source_agent="v16_brain",
        source_kind="brain_medium_impact_governance",
        source_ref_type="test",
        source_ref_id="eval-1",
        proposal_stage="governance_ready",
        capability_scope="medium_impact_governance",
        scope_type="parameter_template",
        scope_key="online_light:default",
        action="switch_parameter_template",
        confidence=0.8,
        evidence_score=0.8,
        risk_class="risk_expanding",
        max_impact="medium_impact",
        risk_verdict={"allowed": True},
        persist=True,
    )
    now = time.time()
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            """
            INSERT INTO v16_brain_command
            (command_id, candidate_id, target_agent, scope_type, scope_key,
             action, decision, status, evidence_json, delegation_json,
             claim_status, posterior_fingerprint, evidence_fingerprint,
             authority_issued_at, created_at, updated_at)
            VALUES (?, ?, 'autonomous_learning', 'parameter_template',
                    'online_light', 'switch_parameter_template', 'delegate',
                    'delegated_to_specialist', '{}', '{}', 'available',
                    'posterior-1', 'evidence-1', ?, ?, ?)
            """,
            (
                "command_without_review",
                candidate["candidate_id"],
                now,
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    result = V16CommandGate.claim(
        db_path,
        target_agent="autonomous_learning",
        scope_type="parameter_template",
        scope_key="online_light",
        action="switch_parameter_template",
        command_id="command_without_review",
        candidate_id=candidate["candidate_id"],
        posterior_fingerprint="posterior-1",
        evidence_fingerprint="evidence-1",
    )

    assert result["allowed"] is False
    assert result["status"] in {"candidate_review_required", "v16_command_unavailable"}


def test_v16_does_not_reissue_same_fingerprint_after_specialist_no_action(tmp_path):
    db_path = _db(tmp_path)
    service = V16BrainOrchestratorService(db_path)
    first = {
        "command_id": "cmd-first",
        "candidate_id": "factor-a",
        "target_agent": "factor_governance",
        "scope_type": "factor_weight",
        "scope_key": "factor-a",
        "action": "factor_governance_cycle",
        "decision": "delegate",
        "status": "delegated_to_specialist",
        "evidence": {"candidate_id": "factor-a"},
        "delegation": {},
        "posterior_fingerprint": "p" * 64,
        "evidence_fingerprint": "e" * 64,
        "max_apply_count": 1,
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    service._persist_commands([dict(first)])
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            """UPDATE v16_brain_command
               SET claim_status='cancelled', status='specialist_no_action',
                   failure_reason='factor_governance_cycle_no_committed_action'
               WHERE command_id='cmd-first'"""
        )
        conn.commit()
    finally:
        conn.close()

    second = {**first, "command_id": "cmd-second", "created_at": time.time(), "updated_at": time.time()}
    service._persist_commands([second])
    conn = connect_sqlite(db_path, read_only=True)
    try:
        assert conn.execute(
            """SELECT COUNT(*) FROM v16_brain_command
               WHERE candidate_id='factor-a'
                 AND posterior_fingerprint=?
                 AND evidence_fingerprint=?""",
            ("p" * 64, "e" * 64),
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_parameter_scope_alias_is_canonicalized_without_widening_other_scopes():
    assert V16CommandGate._scope_matches(
        {"scope_type": "parameter_template", "scope_key": "online_light:default"},
        scope_type="parameter_template",
        scope_key="online_light",
    )
    assert not V16CommandGate._scope_matches(
        {"scope_type": "parameter_template", "scope_key": "offline_deep"},
        scope_type="parameter_template",
        scope_key="online_light",
    )


def test_context_policy_without_runtime_writer_stays_observation_only(tmp_path):
    db_path = _db(tmp_path)
    result = BrainMediumImpactGovernanceService(db_path)._materialize_eval(
        evaluation={
            "eval_id": "context-eval",
            "plan_id": "context-plan",
            "scope_type": "context_policy",
            "coverage_score": 0.9,
            "comparison_verdict": "supported",
            "comparison": {
                "posterior_arbitration": {"selected_scope": "entry"},
            },
        },
        now=time.time(),
        autonomy_guard={},
        persist_candidate=True,
    )
    assert result["status"] == "unsupported_governance_surface"
    assert result["candidate_id"] == ""
    conn = connect_sqlite(db_path, read_only=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM brain_governance_candidate").fetchone()[0] == 0
    finally:
        conn.close()


def test_factor_apply_rejects_advisory_source_before_weight_writer(monkeypatch, tmp_path):
    import backend.services.autonomous_learning as autonomous_learning
    import config.runtime_config as runtime_config
    from backend.services.governance_eligibility import GOVERNANCE_ELIGIBILITY_VERSION
    from config.runtime_config import RuntimeConfig

    db_path = _db(tmp_path)
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            """INSERT INTO policy_suggestion
               (suggestion_id, scope_type, scope_key, action, evidence_json,
                status, governance_eligible, governance_eligibility_version,
                governance_eligibility_fingerprint, created_at)
               VALUES (?, 'factor', ?, 'downweight', ?, 'approved', 1, ?, ?, ?)""",
            (
                "llm-approved-factor",
                "factor-a",
                '{"source_agent":"llm_advisory","expected_effect":{"suggested_target_weight":0.05}}',
                GOVERNANCE_ELIGIBILITY_VERSION,
                "f" * 64,
                time.time(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    cfg = RuntimeConfig(
        autonomy_mode="demo_nursery",
        factor_portfolio_weights={"factor-a": 0.10},
        factor_signal_config={"factor-a": {"enabled": True}},
    )
    monkeypatch.setattr(autonomous_learning, "STATE_DB", db_path)
    monkeypatch.setattr(runtime_config, "shared", lambda: cfg)

    result = autonomous_learning._apply_approved_factor_suggestions_for_demo(
        experiment_id="source-provenance-test",
        limit=1,
    )
    assert result["applied"] is False
    assert result["items"][0]["status"] == "skipped_source_authority"
    assert result["items"][0]["source_agent"] == "llm_advisory"


def test_parameter_apply_forwards_v16_identity_without_copying_evidence(monkeypatch, tmp_path):
    import backend.services.parameter_templates as parameter_templates
    import backend.services.v16_command_gate as v16_command_gate
    import backend.services.autonomous_learning as autonomous_learning
    from backend.services.governance_eligibility import GOVERNANCE_ELIGIBILITY_VERSION

    db_path = _db(tmp_path)
    evidence = (
        '{"candidate_id":"parameter-candidate","factor_id":"factor-a",'
        '"target_template_id":"template-new","regime_key":"default",'
        '"boundary":{"recommended_scope":"online_light"}}'
    )
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            """INSERT INTO policy_suggestion
               (suggestion_id, scope_type, scope_key, action, evidence_json,
                status, governance_eligible, governance_eligibility_version,
                governance_eligibility_fingerprint, created_at)
               VALUES (?, 'parameter_template', 'online_light',
                       'switch_parameter_template', ?, 'approved', 1, ?, ?, ?)""",
            (
                "parameter-suggestion",
                evidence,
                GOVERNANCE_ELIGIBILITY_VERSION,
                "f" * 64,
                time.time(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    calls: list[dict] = []

    class FakeParameterTemplates:
        def __init__(self, _db_path):
            pass

        def get_active_template(self, *, factor_id, regime_key):
            return {"template_id": "template-old", "factor_id": factor_id, "regime_key": regime_key}

        def activate_template(self, **kwargs):
            calls.append(dict(kwargs))
            return {"ok": True, "blocked": False, "status": "applied"}

    monkeypatch.setattr(parameter_templates, "ParameterTemplateService", FakeParameterTemplates)
    monkeypatch.setattr(
        v16_command_gate.V16CommandGate,
        "authorize",
        staticmethod(
            lambda *_args, **_kwargs: {
                "allowed": True,
                "status": "v16_command_authorized",
                "command_id": "v16-parameter-command",
                "candidate_id": "parameter-candidate",
                "posterior_fingerprint": "p" * 64,
                "evidence_fingerprint": "e" * 64,
            }
        ),
    )

    result = autonomous_learning._auto_apply_parameter_template_suggestions(
        db_path=db_path,
        experiment_id="parameter-handoff-test",
        limit=1,
    )
    assert result["applied"]
    assert calls[0]["v16_command_id"] == "v16-parameter-command"
    assert calls[0]["v16_candidate_id"] == "parameter-candidate"
    assert calls[0]["v16_posterior_fingerprint"] == "p" * 64
    assert calls[0]["v16_evidence_fingerprint"] == "e" * 64


def test_supervisor_bootstrap_does_not_require_post_mutation_effect(tmp_path):
    db_path = _db(tmp_path)
    candidate = {
        "candidate_id": "supervisor-bootstrap",
        "source_agent": "v16_brain",
        "scope_type": "supervisor_template",
        "scope_key": "position_supervisor",
        "action": "switch_position_supervisor_template",
        "proposal_stage": "governance_ready",
        "status": "active",
        "evidence_score": 0.9,
        "risk_verdict": {"allowed": True},
        "expected_effect": {
            "source_presence": {
                "replay_report": True,
                "canonical_v2.trade_review": True,
                "canonical_v2.supervisor_trace": True,
                "learning_application_effect": False,
            },
            "replay": {"replay_run_id": "replay-1"},
            "supervisor": {"trace_count": 3},
        },
        "lineage": {"mapped_action": {"target_template_id": "position_supervisor:new.v1"}},
        "expires_at": 0.0,
    }
    gaps = BrainGovernanceCandidateReviewService(db_path)._evidence_gaps(
        candidate,
        now=time.time(),
    )
    assert "missing_learning_application_effect" not in gaps
