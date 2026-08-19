from __future__ import annotations

import json
import time

from alpha.decision_policy import WeightDecision
from backend.core.db import STATE_DB_DDL, connect_sqlite
from backend.runtime.factor_governance_orchestrator import _dumps
from backend.services.brain_governance_candidate_review import BrainGovernanceCandidateReviewService
from backend.services.brain_governance_candidates import BrainGovernanceCandidateService
from backend.services.factor_weight_change import FactorWeightChangeService
from backend.services.mutation_audit import record_api_mutation
from backend.services.proposal_registry import ProposalRegistryService, ensure_proposal_registry_table
from backend.services.v16_command_gate import V16CommandGate


def _db(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    conn.executescript(STATE_DB_DDL)
    conn.commit()
    conn.close()
    return db_path


def test_weight_decision_is_json_safe_for_governance_ledgers():
    payload = json.loads(_dumps({"decisions": {"rsi_14": WeightDecision("rsi_14", 0.1, 0.08, "test")}}))
    assert payload["decisions"]["rsi_14"]["old_weight"] == 0.1
    assert payload["decisions"]["rsi_14"]["new_weight"] == 0.08


def test_v16_command_gate_is_fail_closed_and_accepts_recent_delegate(tmp_path):
    db_path = _db(tmp_path)
    now = time.time()
    blocked = V16CommandGate.authorize(
        db_path,
        target_agent="factor_governance",
        scope_type="factor_weight",
        scope_key="alpha_weight_policy",
        action="factor_governance_cycle",
    )
    assert blocked["allowed"] is False
    assert blocked["status"] == "v16_command_required"

    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            """INSERT INTO v16_brain_command
               (command_id, candidate_id, target_agent, scope_type, scope_key,
                action, decision, status, created_at, updated_at)
               VALUES ('cmd-factor', 'candidate-factor', 'factor_governance',
                       'factor_weight', 'alpha_weight_policy', 'update_weight',
                       'delegate', 'delegated_to_specialist', ?, ?)""",
            (now, now),
        )
        conn.commit()
    finally:
        conn.close()

    allowed = V16CommandGate.authorize(
        db_path,
        target_agent="factor_governance",
        scope_type="factor_weight",
        scope_key="alpha_weight_policy",
        action="factor_governance_cycle",
    )
    assert allowed["allowed"] is True
    assert allowed["command_id"] == "cmd-factor"


def test_v16_command_claim_is_single_use_and_evidence_bound(tmp_path):
    db_path = _db(tmp_path)
    now = time.time()
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            """INSERT INTO v16_brain_command
               (command_id, candidate_id, target_agent, scope_type, scope_key,
                action, decision, status, posterior_fingerprint, evidence_fingerprint,
                created_at, updated_at)
               VALUES ('cmd-claim', 'candidate-claim', 'autonomous_learning',
                       'parameter_template', 'online_light', 'switch_parameter_template',
                       'delegate', 'delegated_to_specialist', 'posterior-a', 'evidence-a', ?, ?)""",
            (now, now),
        )
        conn.commit()
    finally:
        conn.close()

    claim = V16CommandGate.claim(
        db_path,
        target_agent="autonomous_learning",
        scope_type="parameter_template",
        scope_key="online_light",
        action="switch_parameter_template",
        posterior_fingerprint="posterior-a",
        evidence_fingerprint="evidence-a",
    )
    assert claim["allowed"] is True
    assert claim["claim_token"]

    second = V16CommandGate.claim(
        db_path,
        target_agent="autonomous_learning",
        scope_type="parameter_template",
        scope_key="online_light",
        action="switch_parameter_template",
        evidence_fingerprint="evidence-a",
    )
    assert second["allowed"] is False

    consumed = V16CommandGate.finalize(
        db_path,
        command_id=claim["command_id"],
        claim_token=claim["claim_token"],
        mutation_id="mutation-claim",
        config_hash="",
        domain_hash="",
    )
    assert consumed["allowed"] is True
    third = V16CommandGate.claim(
        db_path,
        target_agent="autonomous_learning",
        scope_type="parameter_template",
        scope_key="online_light",
        action="switch_parameter_template",
        evidence_fingerprint="evidence-a",
    )
    assert third["allowed"] is False


def test_v16_gate_scope_match_is_not_blocked_by_other_fresh_commands(tmp_path):
    db_path = _db(tmp_path)
    now = time.time()
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            """INSERT INTO v16_brain_command
               (command_id, candidate_id, target_agent, scope_type, scope_key,
                action, decision, status, created_at, updated_at)
               VALUES ('cmd-target', 'candidate-target', 'factor_governance',
                       'factor_weight', 'target_factor', 'update_weight',
                       'delegate', 'delegated_to_specialist', ?, ?)""",
            (now - 10.0, now - 10.0),
        )
        conn.executemany(
            """INSERT INTO v16_brain_command
               (command_id, candidate_id, target_agent, scope_type, scope_key,
                action, decision, status, created_at, updated_at)
               VALUES (?, ?, 'factor_governance', 'factor_weight', ?,
                       'update_weight', 'delegate', 'delegated_to_specialist', ?, ?)""",
            [
                (
                    f"cmd-noise-{index}",
                    f"candidate-noise-{index}",
                    f"other_factor_{index}",
                    now - index / 1000.0,
                    now - index / 1000.0,
                )
                for index in range(205)
            ],
        )
        conn.commit()
    finally:
        conn.close()

    authorized = V16CommandGate.authorize(
        db_path,
        target_agent="factor_governance",
        scope_type="factor_weight",
        scope_key="target_factor",
        action="update_weight",
    )
    assert authorized["allowed"] is True
    assert authorized["command_id"] == "cmd-target"

    claimed = V16CommandGate.claim(
        db_path,
        target_agent="factor_governance",
        scope_type="factor_weight",
        scope_key="target_factor",
        action="update_weight",
    )
    assert claimed["allowed"] is True
    assert claimed["command_id"] == "cmd-target"


def test_factor_weight_batch_reservation_respects_global_budget(tmp_path, monkeypatch):
    db_path = _db(tmp_path)
    service = FactorWeightChangeService(db_path)
    decisions = {
        f"factor_{index}": WeightDecision(f"factor_{index}", 0.1, 0.08, "batch-test")
        for index in range(30)
    }
    plan = {
        "ok": True,
        "schema_version": "factor_weight_change_plan.v1",
        "status": "planned",
        "decisions": decisions,
        "admitted_decisions": decisions,
        "admissions": {name: {"allowed": True, "status": "admitted"} for name in decisions},
        "proposed_weights": {name: decision.new_weight for name, decision in decisions.items()},
    }
    monkeypatch.setattr(service, "plan", lambda **_kwargs: dict(plan))

    result = service.execute(
        source="test_batch_weight_change",
        producer="test",
        run_id="test_batch_run",
        actor="test",
        reason="batch reservation regression",
        factor_configs={},
        current_weights={name: decision.old_weight for name, decision in decisions.items()},
        risk_check=lambda _plan: {"allowed": True, "reason": "test"},
    )

    assert result["status"] == "applied"
    assert len(result["applications"]) == 24
    assert result["batch_admission"]["global_active_budget"] == 24
    assert result["batch_admission"]["reserved_count"] == 24
    assert service.admission.global_active_count() == 24


def test_candidate_review_skips_unchanged_evidence_and_expires_legacy_rows(tmp_path):
    db_path = _db(tmp_path)
    candidates = BrainGovernanceCandidateService(db_path)
    now = time.time()
    candidates.create_candidate(
        candidate_id="candidate-stable",
        source_agent="v16_brain",
        source_kind="brain_medium_impact_governance",
        source_ref_type="test",
        source_ref_id="test",
        proposal_stage="brain_candidate",
        capability_scope="medium_impact_governance",
        scope_type="factor_weight",
        scope_key="rsi_14",
        action="update_weight",
        confidence=0.8,
        evidence_score=0.8,
        risk_class="medium",
        max_impact="medium",
        evidence_refs={"posterior": "stable"},
        now=now,
    )
    first = BrainGovernanceCandidateReviewService(db_path).review_latest(limit=1, persist=True)
    second = BrainGovernanceCandidateReviewService(db_path).review_latest(limit=1, persist=True)
    assert first["item_count"] == 1
    assert second["status"] == "no_new_evidence"
    assert second["skipped_unchanged_count"] == 1

    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            "UPDATE brain_governance_candidate SET expires_at=?, created_at=? WHERE candidate_id=?",
            (now - 1.0, now - 90000.0, "candidate-stable"),
        )
        conn.commit()
    finally:
        conn.close()
    lifecycle = candidates.reconcile_expired_candidates(now=now)
    assert lifecycle["expired_count"] == 1
    assert candidates.latest_candidates(limit=10)["items"] == []


def test_proposal_registry_compacts_repeated_source_events_without_deleting_ledger(tmp_path):
    db_path = _db(tmp_path)
    ensure_proposal_registry_table(db_path)
    now = time.time()
    conn = connect_sqlite(db_path)
    try:
        conn.executemany(
            """INSERT INTO policy_suggestion
               (suggestion_id, scope_type, scope_key, action, confidence, status, created_at)
               VALUES (?, 'factor', 'rsi_14', 'update_weight', 0.8, 'proposed', ?)""",
            [("old", now - 10.0), ("new", now)],
        )
        conn.commit()
    finally:
        conn.close()

    result = ProposalRegistryService(db_path).refresh()
    assert result["summary"]["proposal_count"] == 1
    assert result["summary"]["active_count"] == 1
    conn = connect_sqlite(db_path, read_only=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM policy_suggestion").fetchone()[0] == 2
    finally:
        conn.close()


def test_system_mutation_audit_preserves_agent_lineage(monkeypatch):
    captured = []

    def fake_record(**kwargs):
        captured.append(kwargs)
        return f"decision-{len(captured)}"

    import backend.services.evolution_ledger as evolution_ledger

    monkeypatch.setattr(evolution_ledger, "record_evolution_decision", fake_record)
    record_api_mutation(
        user="system:factor_governance",
        endpoint="test",
        action="update_weight",
        status="applied",
    )
    record_api_mutation(
        user="operator:test",
        endpoint="test",
        action="patch_runtime_config",
        status="applied",
    )
    assert captured[0]["decision_type"] == "autonomous_mutation"
    assert captured[0]["evidence"]["source_agent"] == "factor_governance"
    assert captured[1]["decision_type"] == "manual_api_mutation"
    assert captured[1]["evidence"]["source_agent"] == "operator"
