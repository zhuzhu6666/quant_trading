from __future__ import annotations

import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from alpha.factor_identity import (
    canonical_factor_ast_json,
    canonical_factor_id,
    factor_definition_fingerprint,
)
from backend.services.governance_eligibility import evaluate_governance_eligibility
from backend.services.governance_mutation_coordinator import (
    GovernanceMutationCoordinator,
    GovernanceMutationPlan,
    _deep_slice,
    classify_governance_risk,
)
from backend.services._brain_helpers import connect, execute
from backend.services.v16_command_gate import V16CommandGate
from config import runtime_config


@pytest.fixture(autouse=True)
def _reset_runtime_config():
    runtime_config.reset_for_tests()
    yield
    runtime_config.reset_for_tests()


def _plan(**overrides):
    values = {
        "patch": {"position_supervisor_template_id": "position_supervisor:conservative.v2"},
        "source": "pytest_governance",
        "action": "switch_position_supervisor_template",
        "control_surface": "supervisor_template",
        "scope_type": "supervisor_template",
        "scope_key": "position_supervisor",
        "evidence_refs": {"review_id": "review-1"},
    }
    values.update(overrides)
    return GovernanceMutationPlan(**values)


def _row(db_path: Path, sql: str, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = conn.execute(sql, params).fetchone()
        return dict(result) if result else {}
    finally:
        conn.close()


def test_factor_identity_uses_normalized_full_sha256():
    left = "ts_mean(close, 20) + delta(volume, 5)"
    right = "delta(volume,5)+ts_mean(close,20.0)"

    assert canonical_factor_ast_json(left) == canonical_factor_ast_json(right)
    fingerprint = factor_definition_fingerprint(left)
    assert len(fingerprint) == 64
    assert canonical_factor_id(right) == f"dsl:{fingerprint}"


def test_governance_eligibility_is_fail_closed_and_caps_verified_recovery():
    full = {
        "label_status": "matured",
        "integrity": "full",
        "model_ready": True,
        "allowed_uses": ["executable_governance"],
        "lineage_complete": True,
        "lineage_unique": True,
    }
    assert evaluate_governance_eligibility(full).effective_weight == 1.0

    recovered = {**full, "integrity": "recovered", "verified_recovered": True}
    assert evaluate_governance_eligibility(recovered).effective_weight == 0.5

    contaminated = {
        **full,
        "integrity": "partial",
        "system_issue_context": {"contaminates_learning": True},
    }
    result = evaluate_governance_eligibility(contaminated)
    assert result.eligible is False
    assert result.effective_weight == 0.0
    assert "system_contaminated" in result.exclusion_reasons
    assert "integrity_partial" in result.exclusion_reasons


def test_risk_classification_ignores_caller_labels_and_fails_unknown_changes_closed():
    tightening = classify_governance_risk(
        {"factor_portfolio_weights": {"alpha": 0.4}},
        {"factor_portfolio_weights": {"alpha": 0.1}},
    )
    assert tightening.risk_class == "risk_tightening"
    assert tightening.v16_required is False

    template = classify_governance_risk(
        {"position_supervisor_template_id": "default"},
        {"position_supervisor_template_id": "claimed_rollback"},
    )
    assert template.risk_class == "risk_expanding"
    assert template.v16_required is True


def test_deep_slice_does_not_reintroduce_untouched_nested_siblings():
    payload = {
        "factor_signal_config": {
            "existing": {"enabled": True, "weight": 0.2},
            "untouched": {"enabled": True, "weight": 0.1},
        }
    }
    patch = {
        "factor_signal_config": {
            "new_factor": {"enabled": False, "lifecycle_status": "SHADOW"}
        }
    }

    assert _deep_slice(payload, patch) == {}
    assert _deep_slice(
        {
            **payload,
            "factor_signal_config": {
                **payload["factor_signal_config"],
                "new_factor": patch["factor_signal_config"]["new_factor"],
            },
        },
        patch,
    ) == {
        "factor_signal_config": {
            "new_factor": patch["factor_signal_config"]["new_factor"]
        }
    }


def test_factor_governance_audit_scope_keeps_non_factor_runtime_controls():
    from backend.runtime.factor_governance_orchestrator import FactorGovernanceOrchestrator

    payload = {
        "runtime_config": {
            "factor_signal_config": {
                "alpha": {"enabled": True, "weight": 0.2},
                "untouched": {"enabled": True, "weight": 0.1},
            },
            "factor_portfolio_weights": {"alpha": 0.2, "untouched": 0.1},
            "autonomy_mode": "demo_autonomous",
        },
        "evidence": {"review_id": "review-1"},
    }

    scoped = FactorGovernanceOrchestrator._scope_audit_config(payload, "alpha")

    assert scoped["runtime_config"]["factor_signal_config"] == {
        "alpha": {"enabled": True, "weight": 0.2}
    }
    assert scoped["runtime_config"]["factor_portfolio_weights"] == {"alpha": 0.2}
    assert scoped["runtime_config"]["autonomy_mode"] == "demo_autonomous"
    assert scoped["runtime_config"]["factor_signal_config"].get("untouched") is None


def test_model_influence_terminal_stage_is_tightening_but_reverse_is_expansion():
    before = {
        "model_influence_config": {
            "models": {"position_supervisor": {"stage": "demo_active"}}
        }
    }
    quarantined = {
        "model_influence_config": {
            "models": {"position_supervisor": {"stage": "quarantined"}}
        }
    }

    tightening = classify_governance_risk(before, quarantined)
    assert tightening.risk_class == "risk_tightening"
    assert tightening.v16_required is False

    reverse = classify_governance_risk(quarantined, before)
    assert reverse.risk_class == "risk_expanding"
    assert reverse.v16_required is True


def test_coordinator_commits_overlay_snapshot_and_intent_before_publish(tmp_path):
    db_path = tmp_path / "state.db"
    observed = []

    def publisher(config, transaction):
        durable = _row(
            db_path,
            "SELECT status, projection_status FROM governance_mutation_intent WHERE mutation_id=?",
            (transaction["mutation_id"],),
        )
        observed.append(durable)
        runtime_config.replace(config)

    result = GovernanceMutationCoordinator(db_path, publisher=publisher).execute(_plan())

    assert result["ok"] is True
    assert result["status"] == "committed"
    assert observed == [{"status": "committed", "projection_status": "pending"}]
    mutation_id = result["mutation_id"]
    intent = _row(db_path, "SELECT * FROM governance_mutation_intent WHERE mutation_id=?", (mutation_id,))
    overlay = _row(db_path, "SELECT mutation_id FROM runtime_config_overlay")
    snapshot = _row(db_path, "SELECT mutation_id, config_hash FROM runtime_config_snapshot")
    assert intent["status"] == "committed"
    assert intent["projection_status"] == "current"
    assert overlay["mutation_id"] == mutation_id
    assert snapshot["mutation_id"] == mutation_id
    assert snapshot["config_hash"] == intent["committed_config_hash"]


def test_coordinator_binds_existing_trade_lineage_ids_to_mutation_and_overlay(tmp_path):
    db_path = tmp_path / "state.db"
    lineage = {
        "bar_ts": "2026-08-02T08:00:00+00:00",
        "decision_id": "decision-1",
        "intent_id": "intent-1",
        "broker_order_id": "order-1",
        "deal_id": "deal-1",
        "position_id": "position-1",
        "review_id": "review-1",
        "sample_id": "sample-1",
    }

    result = GovernanceMutationCoordinator(db_path).execute(
        _plan(evidence_refs=lineage, mutation_id="mutation-lineage-1")
    )

    assert result["ok"] is True
    intent = _row(
        db_path,
        "SELECT mutation_id, evidence_refs_json, committed_config_hash, domain_hash "
        "FROM governance_mutation_intent WHERE mutation_id=?",
        (result["mutation_id"],),
    )
    overlay = _row(
        db_path,
        "SELECT mutation_id, overlay_hash FROM runtime_config_overlay"
    )
    snapshot = _row(
        db_path,
        "SELECT mutation_id, config_hash FROM runtime_config_snapshot"
    )

    assert intent["mutation_id"] == result["mutation_id"]
    assert json.loads(intent["evidence_refs_json"]) == lineage
    assert overlay["mutation_id"] == result["mutation_id"]
    assert overlay["overlay_hash"] == result["overlay_hash"]
    assert snapshot["mutation_id"] == result["mutation_id"]
    assert snapshot["config_hash"] == intent["committed_config_hash"]
    assert result["domain_hash"] == intent["domain_hash"]


def test_publish_failure_is_degraded_and_replayable_without_recommit(tmp_path):
    db_path = tmp_path / "state.db"
    attempts = []

    def publisher(config, transaction):
        attempts.append(transaction["mutation_id"])
        if len(attempts) == 1:
            raise RuntimeError("projection unavailable")
        runtime_config.replace(config)

    coordinator = GovernanceMutationCoordinator(db_path, publisher=publisher)
    first = coordinator.execute(_plan())
    assert first["status"] == "committed_projection_degraded"
    assert first["projection_status"] == "degraded"

    replay = coordinator.replay_projection(first["mutation_id"])
    assert replay["ok"] is True
    assert replay["projection_status"] == "current"
    assert len(attempts) == 2
    count = _row(db_path, "SELECT COUNT(*) AS n FROM runtime_config_snapshot")
    assert count["n"] == 1


def test_replay_uses_latest_durable_config_without_overwriting_later_commit(tmp_path):
    db_path = tmp_path / "state.db"
    published = []

    def publisher(config, transaction):
        published.append((transaction["mutation_id"], config.to_dict()))
        if len(published) == 1:
            raise RuntimeError("first projection unavailable")
        runtime_config.replace(config)

    coordinator = GovernanceMutationCoordinator(db_path, publisher=publisher)
    first = coordinator.execute(_plan(idempotency_key="older-degraded"))
    assert first["projection_status"] == "degraded"

    second = coordinator.execute(
        GovernanceMutationPlan(
            patch={"factor_portfolio_weights": {"later_alpha": 0.2}},
            source="pytest_later_commit",
            action="update_weight",
            control_surface="factor_weight",
            scope_type="factor_weight",
            scope_key="later_alpha",
            idempotency_key="later-commit",
        )
    )
    assert second["projection_status"] == "current"

    replay = coordinator.replay_projection(first["mutation_id"])

    assert replay["projection_status"] == "current"
    replayed_config = published[-1][1]
    assert replayed_config["position_supervisor_template_id"] == (
        "position_supervisor:conservative.v2"
    )
    assert replayed_config["factor_portfolio_weights"]["later_alpha"] == 0.2
    assert runtime_config.shared().factor_portfolio_weights["later_alpha"] == 0.2


def test_recovery_skips_older_mutation_when_same_scope_has_newer_commit(tmp_path):
    db_path = tmp_path / "state.db"
    attempts = []

    def publisher(config, transaction):
        attempts.append(transaction["mutation_id"])
        if len(attempts) == 1:
            raise RuntimeError("older projection unavailable")
        runtime_config.replace(config)

    coordinator = GovernanceMutationCoordinator(db_path, publisher=publisher)
    older = coordinator.execute(_plan(idempotency_key="same-scope-old"))
    newer = coordinator.execute(
        _plan(
            patch={"position_supervisor_template_id": "position_supervisor:restrictive.v3"},
            idempotency_key="same-scope-new",
        )
    )
    assert older["projection_status"] == "degraded"
    assert newer["projection_status"] == "current"

    replay = coordinator.replay_projection(older["mutation_id"])
    batch = coordinator.recover_committed_projections()

    assert replay["status"] == "projection_not_current_scope"
    assert batch["attempted_count"] == 0
    assert batch["skipped_noncurrent_count"] == 1
    assert attempts == [older["mutation_id"], newer["mutation_id"]]


def test_recovery_failure_stays_degraded_and_current_is_idempotent(tmp_path):
    db_path = tmp_path / "state.db"

    def unavailable(_config, _transaction):
        raise RuntimeError("projection still unavailable")

    coordinator = GovernanceMutationCoordinator(db_path, publisher=unavailable)
    first = coordinator.execute(_plan(idempotency_key="still-degraded"))

    recovery = coordinator.recover_committed_projections()

    assert recovery["ok"] is False
    assert recovery["degraded_count"] == 1
    intent = _row(
        db_path,
        "SELECT projection_status, projection_attempts FROM governance_mutation_intent",
    )
    assert intent["projection_status"] == "degraded"
    assert intent["projection_attempts"] == 2

    successful = GovernanceMutationCoordinator(db_path).replay_projection(first["mutation_id"])
    repeated = GovernanceMutationCoordinator(db_path).replay_projection(first["mutation_id"])
    assert successful["projection_status"] == "current"
    assert repeated["status"] == "projection_already_current"
    assert repeated["idempotent"] is True


def test_recovery_fails_closed_when_current_durable_overlay_is_corrupt(tmp_path):
    db_path = tmp_path / "state.db"

    def unavailable(_config, _transaction):
        raise RuntimeError("initial projection unavailable")

    coordinator = GovernanceMutationCoordinator(db_path, publisher=unavailable)
    committed = coordinator.execute(_plan(idempotency_key="corrupt-overlay"))
    assert committed["projection_status"] == "degraded"
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE runtime_config_overlay SET overlay_json='{not-json'")
    conn.commit()
    conn.close()

    recovery = GovernanceMutationCoordinator(db_path).recover_committed_projections()

    assert recovery["ok"] is False
    assert recovery["degraded_count"] == 1
    result = recovery["results"][0]
    assert result["projection_status"] == "degraded"
    assert result["error"]["type"] == "GovernanceMutationError"
    assert _row(
        db_path,
        "SELECT projection_status FROM governance_mutation_intent",
    )["projection_status"] == "degraded"


@pytest.mark.parametrize("stage", ["after_reserved", "after_prepared", "before_commit"])
def test_fault_before_commit_aborts_without_overlay_change(tmp_path, stage):
    db_path = tmp_path / f"{stage}.db"

    def fault(current):
        if current == stage:
            raise RuntimeError(f"fault:{stage}")

    result = GovernanceMutationCoordinator(db_path).execute(_plan(), fault_injector=fault)

    assert result["ok"] is False
    assert result["status"] == "aborted"
    intent = _row(db_path, "SELECT status, error_stage FROM governance_mutation_intent")
    assert intent["status"] == "aborted"
    overlay = _row(db_path, "SELECT COUNT(*) AS n FROM runtime_config_overlay")
    snapshot = _row(db_path, "SELECT COUNT(*) AS n FROM runtime_config_snapshot")
    assert overlay["n"] == 0
    assert snapshot["n"] == 0


def test_idempotency_and_scope_lock_prevent_duplicate_mutations(tmp_path):
    db_path = tmp_path / "state.db"
    coordinator = GovernanceMutationCoordinator(db_path)
    plan = _plan(idempotency_key="same-operation")
    first = coordinator.execute(plan)
    second = coordinator.execute(plan)
    assert first["mutation_id"] == second["mutation_id"]
    assert second["idempotent"] is True
    assert _row(db_path, "SELECT COUNT(*) AS n FROM runtime_config_snapshot")["n"] == 1

    busy_db = tmp_path / "busy.db"
    first_coordinator = GovernanceMutationCoordinator(busy_db)
    reserved = first_coordinator.reserve(_plan(idempotency_key="first"))
    blocked = GovernanceMutationCoordinator(busy_db).reserve(
        _plan(idempotency_key="second", mutation_id="second")
    )
    assert reserved["status"] == "reserved"
    assert blocked["status"] == "scope_busy"


def test_concurrent_scope_reservation_has_one_owner(tmp_path):
    db_path = tmp_path / "state.db"

    def reserve(index):
        return GovernanceMutationCoordinator(db_path).reserve(
            _plan(idempotency_key=f"concurrent-{index}", mutation_id=f"mutation-{index}")
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, (1, 2)))

    assert sum(result["status"] == "reserved" for result in results) == 1
    assert sum(result["status"] == "scope_busy" for result in results) == 1


def test_domain_write_rolls_back_with_intent_and_overlay(tmp_path):
    db_path = tmp_path / "state.db"
    coordinator = GovernanceMutationCoordinator(db_path)
    coordinator._prepare_storage()
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE domain_fact (mutation_id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

    def writer(conn, mutation_id, _config):
        conn.execute("INSERT INTO domain_fact(mutation_id) VALUES (?)", (mutation_id,))
        return {"written": True}

    def fault(stage):
        if stage == "before_commit":
            raise RuntimeError("kill before commit")

    result = coordinator.execute(_plan(), transaction_writer=writer, fault_injector=fault)
    assert result["status"] == "aborted"
    assert _row(db_path, "SELECT COUNT(*) AS n FROM domain_fact")["n"] == 0


def test_committed_terminal_transitions_are_explicit(tmp_path):
    db_path = tmp_path / "state.db"
    coordinator = GovernanceMutationCoordinator(db_path)
    first = coordinator.execute(_plan(idempotency_key="first"))
    rolled = coordinator.mark_rolled_back(first["mutation_id"], rollback_mutation_id="rollback-1")
    assert rolled["status"] == "rolled_back"

    second = coordinator.execute(
        _plan(idempotency_key="second", mutation_id="second", scope_key="other-supervisor")
    )
    superseded = coordinator.mark_superseded(
        second["mutation_id"], superseded_by_mutation_id="replacement-1"
    )
    assert superseded["status"] == "superseded"


def test_runtime_mutation_dual_record_flag_uses_coordinator(monkeypatch, tmp_path):
    from backend.core import static_feature_flags
    from backend.services.runtime_config_mutation import RuntimeConfigMutationService

    monkeypatch.setattr(
        static_feature_flags,
        "shared_static_feature_flags",
        lambda: SimpleNamespace(governance_mutation_coordinator_v2_mode="dual_record"),
    )
    db_path = tmp_path / "state.db"
    result = RuntimeConfigMutationService(db_path).apply_patch(
        {"position_supervisor_template_id": "position_supervisor:conservative.v2"},
        source="pytest_dual_record",
        action="switch_position_supervisor_template",
        governance_evidence_refs={"review_id": "review-1"},
        risk_reduction=True,
        audit=False,
    )

    assert result["ok"] is True
    assert result["coordinator_mode"] == "dual_record"
    assert result["caller_risk_reduction_ignored"] is True
    assert result["risk_classification"]["risk_class"] == "risk_expanding"
    assert result["mutation_id"]
    assert _row(db_path, "SELECT status FROM governance_mutation_intent")["status"] == "committed"


def test_coordinator_finalizes_v16_with_config_and_domain_hash_in_transaction(tmp_path):
    class ProductionLikeCoordinator(GovernanceMutationCoordinator):
        @property
        def production_state(self):
            return True

    db_path = tmp_path / "state.db"
    bootstrap = GovernanceMutationCoordinator(db_path)
    bootstrap._prepare_storage()
    V16CommandGate.ensure_finalize_schema(db_path)
    conn = connect(db_path)
    now = time.time()
    execute(
        conn,
        """INSERT INTO v16_brain_command
           (command_id, target_agent, scope_type, scope_key, action, decision,
            status, evidence_json, delegation_json, evidence_fingerprint,
            created_at, updated_at)
           VALUES ('command-1', 'factor_governance', 'factor_weight', 'new_alpha',
                   'update_weight', 'delegate', 'active', '{}', '{}',
                   'evidence-1', ?, ?)""",
        (now, now),
    )
    conn.commit()
    conn.close()

    def writer(conn, mutation_id, _config):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS committed_domain_fact "
            "(mutation_id TEXT PRIMARY KEY, fact_value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO committed_domain_fact(mutation_id, fact_value) VALUES (?, ?)",
            (mutation_id, "domain-fact-1"),
        )
        return {"fact_value": "domain-fact-1", "mutation_id": mutation_id}

    result = ProductionLikeCoordinator(db_path).execute(
        GovernanceMutationPlan(
            patch={"factor_portfolio_weights": {"new_alpha": 0.1}},
            source="pytest_v16_transaction",
            action="update_weight",
            control_surface="factor_weight",
            scope_type="factor_weight",
            scope_key="new_alpha",
            evidence_refs={"review_id": "review-1"},
            evidence_fingerprint="evidence-1",
            v16_target_agent="factor_governance",
        ),
        transaction_writer=writer,
    )

    assert result["ok"] is True
    assert result["risk_classification"]["v16_required"] is True
    command = _row(
        db_path,
        """SELECT claim_status, apply_count, finalized_mutation_id,
                  finalized_config_hash, finalized_domain_hash
           FROM v16_brain_command WHERE command_id='command-1'""",
    )
    intent = _row(
        db_path,
        """SELECT mutation_id, committed_config_hash, domain_hash
           FROM governance_mutation_intent WHERE mutation_id=?""",
        (result["mutation_id"],),
    )
    assert command["claim_status"] == "finalized"
    assert command["apply_count"] == 1
    assert command["finalized_mutation_id"] == intent["mutation_id"]
    assert command["finalized_config_hash"] == intent["committed_config_hash"]
    assert command["finalized_domain_hash"] == intent["domain_hash"]
    assert result["domain_hash"] == intent["domain_hash"]
    assert result["domain_result"] == {
        "fact_value": "domain-fact-1",
        "mutation_id": result["mutation_id"],
    }


def test_coordinator_enforces_operator_pause_for_direct_callers(tmp_path):
    db_path = tmp_path / "state.db"
    coordinator = GovernanceMutationCoordinator(db_path)

    paused = coordinator.execute(
        GovernanceMutationPlan(
            patch={"governance_expansion_paused": True},
            source="operator_pause",
            actor="operator:test",
            action="pause_governance_expansion",
            control_surface="governance_expansion_control",
            scope_type="governance_expansion_control",
            scope_key="governance_expansion_paused",
        )
    )
    blocked = coordinator.execute(
        GovernanceMutationPlan(
            patch={"factor_portfolio_weights": {"paused_alpha": 0.2}},
            source="direct_factor_lifecycle_like_caller",
            actor="system:factor_governance",
            action="update_weight",
            control_surface="factor_weight",
            scope_type="factor_weight",
            scope_key="paused_alpha",
        )
    )

    assert paused["status"] == "committed"
    assert blocked["ok"] is False
    assert blocked["status"] == "blocked_governance_expansion_paused"
    assert _row(
        db_path,
        "SELECT COUNT(*) AS n FROM governance_mutation_intent",
    )["n"] == 1


def test_only_operator_can_resume_governance_expansion(tmp_path):
    db_path = tmp_path / "state.db"
    coordinator = GovernanceMutationCoordinator(db_path)
    coordinator.execute(
        GovernanceMutationPlan(
            patch={"governance_expansion_paused": True},
            source="operator_pause",
            actor="operator:test",
            action="pause_governance_expansion",
            control_surface="governance_expansion_control",
            scope_type="governance_expansion_control",
            scope_key="governance_expansion_paused",
        )
    )

    system_resume = coordinator.execute(
        GovernanceMutationPlan(
            patch={"governance_expansion_paused": False},
            source="invalid_system_resume",
            actor="system:governance",
            action="resume_governance_expansion",
            control_surface="governance_expansion_control",
            scope_type="governance_expansion_control",
            scope_key="governance_expansion_paused",
        )
    )
    operator_resume = coordinator.execute(
        GovernanceMutationPlan(
            patch={"governance_expansion_paused": False},
            source="operator_resume",
            actor="operator:test",
            action="resume_governance_expansion",
            control_surface="governance_expansion_control",
            scope_type="governance_expansion_control",
            scope_key="governance_expansion_paused",
        )
    )

    assert system_resume["status"] == "operator_governance_pause_required"
    assert operator_resume["status"] == "committed"


def test_domain_only_entry_quality_mutation_commits_atomically(tmp_path):
    db_path = tmp_path / "state.db"

    def writer(conn, mutation_id, _config):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS entry_control "
            "(scope_key TEXT PRIMARY KEY, mutation_id TEXT, threshold REAL)"
        )
        conn.execute(
            "INSERT INTO entry_control VALUES ('weak_signal', ?, 0.5)",
            (mutation_id,),
        )
        return {"scope_key": "weak_signal", "threshold": 0.5}

    result = GovernanceMutationCoordinator(db_path).execute(
        GovernanceMutationPlan(
            patch={},
            source="pytest_entry_quality",
            actor="system:autonomous_learning",
            action="activate_entry_quality_control",
            control_surface="entry_quality",
            scope_type="entry_quality",
            scope_key="weak_signal",
            domain_only=True,
            domain_before={
                "min_abs_signal_score": 0.0,
                "strong_signal_override": 0.0,
            },
            domain_target={
                "min_abs_signal_score": 0.5,
                "strong_signal_override": 0.75,
            },
        ),
        transaction_writer=writer,
    )

    assert result["ok"] is True
    assert result["status"] == "committed"
    assert result["risk_classification"]["risk_class"] == "risk_tightening"
    assert _row(db_path, "SELECT threshold FROM entry_control")["threshold"] == 0.5
    assert _row(
        db_path,
        "SELECT status FROM governance_mutation_intent WHERE mutation_id=?",
        (result["mutation_id"],),
    )["status"] == "committed"
    assert _row(
        db_path,
        "SELECT mutation_id FROM runtime_config_overlay",
    ).get("mutation_id", "") != result["mutation_id"]
    assert _row(
        db_path,
        "SELECT COUNT(*) AS n FROM runtime_config_snapshot WHERE mutation_id=?",
        (result["mutation_id"],),
    )["n"] == 0


def test_domain_only_writer_failure_rolls_back_domain_fact(tmp_path):
    db_path = tmp_path / "state.db"

    def writer(conn, _mutation_id, _config):
        conn.execute("CREATE TABLE entry_control (scope_key TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO entry_control VALUES ('weak_signal')")
        raise RuntimeError("writer_failed")

    result = GovernanceMutationCoordinator(db_path).execute(
        GovernanceMutationPlan(
            patch={},
            source="pytest_entry_quality",
            action="activate_entry_quality_control",
            control_surface="entry_quality",
            scope_type="entry_quality",
            scope_key="weak_signal",
            domain_only=True,
            domain_before={"min_abs_signal_score": 0.0},
            domain_target={"min_abs_signal_score": 0.5},
        ),
        transaction_writer=writer,
    )

    assert result["ok"] is False
    assert _row(
        db_path,
        "SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table' AND name='entry_control'",
    )["n"] == 0


def test_builtin_demote_patch_with_activation_eligibility_stays_tightening():
    """A PREPARED->SHADOW demote that newly stamps autonomous_activation is
    still risk-tightening: eligibility is not authority (no weight, vote, or
    exposure), so stale-prepared builtin exits never wait on a V16 command."""
    before = {
        "factor_signal_config": {
            "harami": {
                "lifecycle_status": "PROMOTION_PREPARED",
                "enabled": True,
                "committed_mutation_id": "m0",
            }
        }
    }
    target = {
        "factor_signal_config": {
            "harami": {
                "lifecycle_status": "SHADOW",
                "enabled": True,
                "autonomous_activation": True,
                "committed_mutation_id": "m1",
            }
        }
    }

    classification = classify_governance_risk(before, target)

    assert classification.risk_class == "risk_tightening"
    assert classification.v16_required is False
