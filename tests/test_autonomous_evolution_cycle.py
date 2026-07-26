from __future__ import annotations

import time

from backend.core.db import STATE_DB_DDL, connect_sqlite
from backend.services.autonomous_demo_apply_stepper import AutonomousDemoApplyStepper
from backend.services.autonomous_evolution_cycle import AutonomousEvolutionCycleService
from backend.services.autonomous_evolution_runner import AutonomousEvolutionNurseryRunner
from backend.services.brain_governance_candidate_review import ensure_brain_governance_candidate_review_table
from backend.services.brain_governance_candidates import ensure_brain_governance_candidate_table
from backend.services.proposal_registry import ensure_proposal_registry_table
from backend.services.replay_harness import ReplayHarnessService
from backend.services.v16_brain_orchestrator import V16BrainOrchestratorService
from backend.services.v16_command_gate import V16CommandGate


def _readiness(*, replay_status: str = "fresh", release_status: str = "completed", posture: str = "full") -> dict:
    return {
        "governance": {"autonomy_mode": "demo_nursery"},
        "autonomy_health": {"posture": posture},
        "replay": {"status": replay_status},
        "release": {"status": release_status},
        "live": {"loop": {"status": "running"}},
        "v16": {
            "control_plane_boundaries": {
                "risk_policy_service_required_for_future_actions": True,
                "decision_policy_required_for_future_weight_writes": True,
                "runtime_overlay_snapshot_required_for_future_mutations": True,
                "proposal_registry_review_only": True,
            }
        },
    }


def _create_core_tables(db_path, *, include_replay: bool, include_effect: bool) -> None:
    now = time.time()
    conn = connect_sqlite(db_path)
    try:
        for table in [
            "decision_ledger",
            "trade_outcome_review",
            "autonomous_learning_sample",
            "experience_memory",
            "learning_application_log",
            "learning_application_effect",
            "replay_report",
        ]:
            conn.execute(f"CREATE TABLE IF NOT EXISTS {table} (id TEXT PRIMARY KEY, created_at REAL)")
        for table in [
            "decision_ledger",
            "trade_outcome_review",
            "autonomous_learning_sample",
            "experience_memory",
            "learning_application_log",
        ]:
            conn.execute(f"INSERT INTO {table} (id, created_at) VALUES (?, ?)", (f"{table}_1", now))
        if include_replay:
            conn.execute("INSERT INTO replay_report (id, created_at) VALUES (?, ?)", ("replay_1", now))
        if include_effect:
            conn.execute("INSERT INTO learning_application_effect (id, created_at) VALUES (?, ?)", ("effect_1", now))
        conn.commit()
    finally:
        conn.close()


def _create_candidate_review(db_path) -> None:
    now = time.time()
    ensure_brain_governance_candidate_table(db_path)
    ensure_brain_governance_candidate_review_table(db_path)
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            """
            INSERT INTO brain_governance_candidate
            (candidate_id, source_agent, source_kind, source_ref_type, source_ref_id,
             proposal_stage, capability_scope, scope_type, scope_key, action,
             confidence, evidence_score, risk_class, max_impact, expected_effect_json,
             evidence_refs_json, counter_evidence_refs_json, risk_verdict_json,
             decision_policy_json, rollback_plan_json, lineage_json, status,
             submitted_suggestion_id, submitted_at, expires_at, created_at, updated_at)
            VALUES (?, 'v16_brain', 'test', 'test', 'ref',
                    'governance_ready', 'medium_impact_governance', 'factor', 'rsi_14', 'update_weight',
                    0.8, 0.9, 'medium', 'medium_impact', '{}',
                    '{}', '{}', '{"allowed": true}',
                    '{}', '{}', '{}', 'active',
                    '', 0, 0, ?, ?)
            """,
            ("candidate_1", now, now),
        )
        conn.execute(
            """
            INSERT INTO brain_governance_candidate_review
            (review_id, candidate_id, review_status, bridge_ready, bridge_reason,
             evidence_gaps_json, conflict_json, bridge_preview_json,
             source_reliability_json, llm_advisory_json, boundary_json, created_at)
            VALUES (?, ?, 'bridge_ready', 1, 'ok', '[]', '{}', '{}', '{}', '{}', '{}', ?)
            """,
            ("review_1", "candidate_1", now),
        )
        conn.commit()
    finally:
        conn.close()


def test_autonomous_evolution_cycle_blocks_stale_replay_and_missing_effect(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    ensure_proposal_registry_table(db_path)
    _create_core_tables(db_path, include_replay=False, include_effect=False)
    _create_candidate_review(db_path)
    monkeypatch.setattr(
        AutonomousEvolutionCycleService,
        "_chain_health",
        lambda self: {"ok": True, "status": "ok", "schema_version": "agent_chain_health.v1"},
    )

    cycle = AutonomousEvolutionCycleService(db_path).status(readiness=_readiness(replay_status="stale"))

    assert cycle["status"] == "needs_attention"
    components = {item["component"] for item in cycle["blockers"]}
    assert "evidence" in components
    assert "replay" in components
    assert "effect_monitor" in components
    assert any(item["action"] == "run_replay" for item in cycle["next_actions"])
    assert cycle["boundary"]["does_not_apply_proposals"] is True


def test_autonomous_evolution_cycle_ready_for_guarded_demo_apply(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    ensure_proposal_registry_table(db_path)
    _create_core_tables(db_path, include_replay=True, include_effect=True)
    _create_candidate_review(db_path)
    monkeypatch.setattr(
        AutonomousEvolutionCycleService,
        "_chain_health",
        lambda self: {"ok": True, "status": "ok", "schema_version": "agent_chain_health.v1"},
    )

    cycle = AutonomousEvolutionCycleService(db_path).status(readiness=_readiness())

    assert cycle["status"] == "ready_for_guarded_demo_apply"
    assert cycle["stable_demo_nursery_ready"] is True
    assert cycle["blockers"] == []
    assert cycle["next_actions"][0]["action"] == "auto_bridge_reviewed_candidates"
    assert cycle["next_actions"][0]["executor"] == "autonomous_evolution_nursery"
    assert cycle["human_intervention_required"] is False


def test_autonomous_evolution_cycle_treats_routed_stale_proposals_as_work_queue(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    ensure_proposal_registry_table(db_path)
    _create_core_tables(db_path, include_replay=True, include_effect=True)
    _create_candidate_review(db_path)
    monkeypatch.setattr(
        AutonomousEvolutionCycleService,
        "_chain_health",
        lambda self: {"ok": True, "status": "ok", "schema_version": "agent_chain_health.v1"},
    )
    monkeypatch.setattr(
        AutonomousEvolutionCycleService,
        "_proposal_status",
        lambda self, *, refresh: {
            "ok": True,
            "schema_version": "proposal_registry_status.v1",
            "proposal_count": 2,
            "active_count": 2,
            "actionable_count": 2,
            "high_unresolved_conflict_count": 0,
            "stale_evidence_count": 2,
            "stale_replay_required_count": 1,
            "stale_review_required_count": 1,
            "hard_stale_evidence_count": 0,
        },
    )

    cycle = AutonomousEvolutionCycleService(db_path).status(readiness=_readiness())

    assert "proposal_registry" not in {item["component"] for item in cycle["blockers"]}
    actions = {item["action"] for item in cycle["next_actions"]}
    assert "auto_run_proposal_replay_refresh" in actions
    assert "auto_review_stale_proposals" in actions


def test_autonomous_evolution_runner_repairs_then_uses_existing_learning_cycle(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    ensure_proposal_registry_table(db_path)
    _create_core_tables(db_path, include_replay=True, include_effect=True)
    _create_candidate_review(db_path)
    monkeypatch.setattr(
        AutonomousEvolutionCycleService,
        "_chain_health",
        lambda self: {"ok": True, "status": "ok", "schema_version": "agent_chain_health.v1"},
    )
    readiness_after_repair = iter([
        _readiness(replay_status="fresh", release_status="missing"),
        _readiness(replay_status="fresh", release_status="completed"),
        _readiness(replay_status="fresh", release_status="completed"),
    ])
    monkeypatch.setattr(AutonomousEvolutionNurseryRunner, "_build_readiness", lambda self: next(readiness_after_repair))
    monkeypatch.setattr(
        AutonomousEvolutionNurseryRunner,
        "_run_bar_replay",
        lambda self, *, lookback_days, limit: {"ok": True, "schema_version": "test_replay.v1", "status": "completed"},
    )
    monkeypatch.setattr(
        AutonomousEvolutionNurseryRunner,
        "_reconcile_effects",
        lambda self, *, limit: {"ok": True, "schema_version": "test_effects.v1", "status": "reconciled"},
    )
    monkeypatch.setattr(
        AutonomousEvolutionNurseryRunner,
        "_refresh_proposals",
        lambda self: {"ok": True, "schema_version": "test_proposals.v1", "status": "available"},
    )
    monkeypatch.setattr(
        AutonomousEvolutionNurseryRunner,
        "_create_release_evidence",
        lambda self, *, run_id, readiness, cycle, actions: {"ok": True, "run_id": f"release_{run_id}", "status": "completed"},
    )
    monkeypatch.setattr(
        AutonomousEvolutionNurseryRunner,
        "_run_learning_cycle",
        lambda self, *, sample_limit, recommendation_limit: {"ok": True, "schema_version": "test_learning.v1", "status": "completed"},
    )

    result = AutonomousEvolutionNurseryRunner(db_path).run_once(
        readiness=_readiness(replay_status="stale", release_status="missing"),
        apply_when_ready=True,
        full_learning_cycle=True,
    )

    assert result["status"] == "completed"
    actions = [item["action"] for item in result["actions"]]
    assert actions == [
        "run_bar_replay_evidence",
        "refresh_proposal_registry",
        "record_release_evidence",
        "run_autonomous_learning_cycle",
    ]
    assert result["boundary"]["demo_apply_uses_existing_autonomous_learning_cycle"] is True


def test_autonomous_evolution_runner_defaults_to_small_demo_apply(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    ensure_proposal_registry_table(db_path)
    _create_core_tables(db_path, include_replay=True, include_effect=True)
    _create_candidate_review(db_path)
    monkeypatch.setattr(
        AutonomousEvolutionCycleService,
        "_chain_health",
        lambda self: {"ok": True, "status": "ok", "schema_version": "agent_chain_health.v1"},
    )
    monkeypatch.setattr(AutonomousEvolutionNurseryRunner, "_build_readiness", lambda self: _readiness())
    monkeypatch.setattr(
        AutonomousEvolutionNurseryRunner,
        "_refresh_proposals",
        lambda self: {"ok": True, "schema_version": "test_proposals.v1", "status": "available"},
    )
    monkeypatch.setattr(
        AutonomousEvolutionNurseryRunner,
        "_run_demo_apply",
        lambda self, *, suggestion_limit: {
            "ok": True,
            "schema_version": "demo_autonomy_apply.v1",
            "status": "completed",
            "suggestion_limit": suggestion_limit,
        },
    )

    result = AutonomousEvolutionNurseryRunner(db_path).run_once(apply_when_ready=True, suggestion_limit=7)

    assert result["status"] == "completed"
    actions = result["actions"]
    demo_action = next(item for item in actions if item["action"] == "run_demo_autonomy_apply")
    assert demo_action["result"]["suggestion_limit"] == 7


def test_autonomous_evolution_runner_demo_owns_review_bridge_and_apply(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    ensure_proposal_registry_table(db_path)
    _create_core_tables(db_path, include_replay=True, include_effect=True)
    _create_candidate_review(db_path)
    monkeypatch.setattr(
        AutonomousEvolutionCycleService,
        "_chain_health",
        lambda self: {"ok": True, "status": "ok", "schema_version": "agent_chain_health.v1"},
    )
    monkeypatch.setattr(AutonomousEvolutionNurseryRunner, "_build_readiness", lambda self: _readiness())
    monkeypatch.setattr(
        AutonomousEvolutionNurseryRunner,
        "_review_candidates",
        lambda self, *, limit: {"ok": True, "status": "reviewed", "limit": limit},
    )
    monkeypatch.setattr(
        AutonomousEvolutionNurseryRunner,
        "_bridge_demo_candidates",
        lambda self, *, limit: {"ok": True, "status": "bridged", "submitted_count": 1, "limit": limit},
    )
    monkeypatch.setattr(
        AutonomousEvolutionNurseryRunner,
        "_run_learning_cycle",
        lambda self, *, sample_limit, recommendation_limit: {
            "ok": True,
            "schema_version": "test_learning.v1",
            "status": "completed",
        },
    )

    result = AutonomousEvolutionNurseryRunner(db_path).run_once(
        refresh_proposals=False,
        automatic_demo=True,
    )

    assert result["status"] == "completed"
    actions = [item["action"] for item in result["actions"]]
    assert actions == [
        "auto_review_governance_candidates",
        "auto_bridge_governance_candidates",
        "run_autonomous_learning_cycle",
    ]


def test_autonomous_evolution_runner_consumes_one_recommended_step(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    ensure_proposal_registry_table(db_path)
    _create_core_tables(db_path, include_replay=True, include_effect=True)
    _create_candidate_review(db_path)
    calls: list[dict] = []

    monkeypatch.setattr(
        AutonomousEvolutionCycleService,
        "_chain_health",
        lambda self: {"ok": True, "status": "ok", "schema_version": "agent_chain_health.v1"},
    )
    monkeypatch.setattr(AutonomousEvolutionNurseryRunner, "_build_readiness", lambda self: _readiness())

    class FakeStepper:
        def __init__(self, db_path):
            self.db_path = db_path

        def plan(self):
            return {
                "ok": True,
                "schema_version": "autonomous_demo_apply_plan.v1",
                "steps": [
                    {
                        "step": "governor_review",
                        "pending_count": 1,
                        "recommended": True,
                        "execution_profile": "bounded_existing_apply_step",
                    },
                    {
                        "step": "factor_pruning_bridge",
                        "pending_count": 3,
                        "recommended": True,
                        "execution_profile": "bounded_candidate_review_bridge",
                    },
                    {
                        "step": "sync_factor_weights",
                        "pending_count": 10,
                        "recommended": True,
                        "execution_profile": "bounded_existing_apply_step",
                    },
                ],
            }

        def run_step(self, step: str, *, limit: int, confirm_step: bool, actor: str):
            calls.append({"step": step, "limit": limit, "confirm_step": confirm_step, "actor": actor})
            return {
                "ok": True,
                "schema_version": "autonomous_demo_apply_step.v1",
                "status": "completed",
                "step": step,
                "limit": limit,
            }

    monkeypatch.setattr("backend.services.autonomous_demo_apply_stepper.AutonomousDemoApplyStepper", FakeStepper)

    result = AutonomousEvolutionNurseryRunner(db_path).run_once(
        refresh_proposals=False,
        consume_recommended_step=True,
        recommended_step_limit=1,
    )

    assert result["status"] == "completed"
    actions = result["actions"]
    assert [item["action"] for item in actions] == ["consume_recommended_demo_apply_step"]
    assert actions[0]["result"]["selected_step"]["step"] == "governor_review"
    assert calls == [
        {
            "step": "governor_review",
            "limit": 1,
            "confirm_step": True,
            "actor": "system:autonomous_evolution_nursery_runner.recommended_step",
        }
    ]


def test_autonomous_evolution_runner_consumes_step_before_automatic_apply(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    ensure_proposal_registry_table(db_path)
    _create_core_tables(db_path, include_replay=True, include_effect=True)
    _create_candidate_review(db_path)
    calls: list[str] = []
    monkeypatch.setattr(
        AutonomousEvolutionCycleService,
        "_chain_health",
        lambda self: {"ok": True, "status": "ok", "schema_version": "agent_chain_health.v1"},
    )
    monkeypatch.setattr(AutonomousEvolutionNurseryRunner, "_build_readiness", lambda self: _readiness())
    monkeypatch.setattr(
        AutonomousEvolutionNurseryRunner,
        "_consume_recommended_step",
        lambda self, *, limit, allowlist: calls.append("step") or {"ok": True, "status": "completed"},
    )
    monkeypatch.setattr(
        AutonomousEvolutionNurseryRunner,
        "_run_learning_cycle",
        lambda self, *, sample_limit, recommendation_limit: calls.append("learning") or {"ok": True, "status": "completed"},
    )

    result = AutonomousEvolutionNurseryRunner(db_path).run_once(
        refresh_proposals=False,
        apply_when_ready=True,
        full_learning_cycle=True,
        consume_recommended_step=True,
    )

    assert result["status"] == "completed"
    assert calls == ["step", "learning"]
    assert [item["action"] for item in result["actions"]] == [
        "consume_recommended_demo_apply_step",
        "run_autonomous_learning_cycle",
    ]


def test_recommended_step_closes_approved_weight_before_bridging_more_candidates():
    selected = AutonomousEvolutionNurseryRunner._select_recommended_step(
        {
            "steps": [
                {"step": "factor_pruning_bridge", "pending_count": 7, "recommended": True},
                {"step": "sync_factor_weights", "pending_count": 1, "recommended": True},
            ]
        }
    )

    assert selected["step"] == "sync_factor_weights"


def test_replay_freshness_records_lightweight_report(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    service = ReplayHarnessService(db_path)
    monkeypatch.setattr(
        ReplayHarnessService,
        "_load_decisions",
        lambda self, *, since_ts, limit: [
            {
                "decision_id": "decision_1",
                "decision_ts": time.time(),
                "symbol": "XAUUSD+",
                "timeframe": "M1",
                "action_json": '{"risk_policy_verdict": {"allowed": true}, "gate": {"allowed": true}}',
                "risk_state_json": "{}",
                "portfolio_state_json": "{}",
                "factor_snapshot_count": 1,
                "event_type": "open_decision",
            }
        ],
    )
    monkeypatch.setattr(
        ReplayHarnessService,
        "_load_bar_window",
        lambda self, *, symbol, timeframe, decision_ts, warmup_bars, post_bars: [
            {"time": decision_ts, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0}
        ],
    )

    report = service.run_bar_replay_freshness(limit=5, warmup_bars=2, window_sample_limit=1)

    assert report["scope"]["kind"] == "bar_replay_freshness"
    assert report["status"] == "completed"
    assert report["evidence_grade"] in {"B", "C"}
    freshness = report["metric_summary"]["nursery_freshness"]
    assert freshness["full_recompute"] is False
    assert freshness["bar_loaded_window_count"] == 1


def test_autonomous_demo_apply_stepper_requires_confirmation_and_runs_one_step(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    calls: list[dict] = []
    monkeypatch.setattr("backend.services.autonomous_demo_apply_stepper._demo_autonomous_enabled", lambda: True)
    monkeypatch.setattr("backend.services.autonomous_demo_apply_stepper._new_experiment_id", lambda: "step_test_run")
    monkeypatch.setattr(
        AutonomousDemoApplyStepper,
        "_run_factor_pruning_governance",
        lambda self, *, limit: calls.append({"db_path": self.db_path, "limit": limit})
        or {"ok": True, "limit": limit},
    )

    service = AutonomousDemoApplyStepper(db_path)
    rejected = service.run_step("factor_pruning_governance", confirm_step=False)

    assert rejected["status"] == "confirmation_required"
    assert calls == []

    result = service.run_step("factor_pruning_governance", limit=3, confirm_step=True, actor="test")

    assert result["ok"] is True
    assert result["status"] == "completed"
    assert result["step"] == "factor_pruning_governance"
    assert result["result"]["limit"] == 3
    assert result["execution_context"]["schema_version"] == "autonomous_demo_apply_step_execution_context.v1"
    assert result["execution_context"]["posterior_monitor"]["primary_reader"] == "RuleEvolutionGovernor.reconcile_application_effects"
    assert result["execution_context"]["rollback_refs"]["run_id"] == result["run_id"]
    assert calls == [{"db_path": db_path, "limit": 3}]


def test_autonomous_demo_apply_stepper_plan_reports_known_steps(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.services.autonomous_demo_apply_stepper._demo_autonomous_enabled", lambda: True)
    plan = AutonomousDemoApplyStepper(tmp_path / "state.db").plan()

    steps = [item["step"] for item in plan["steps"]]
    assert plan["schema_version"] == "autonomous_demo_apply_plan.v1"
    assert "governor_review" in steps
    assert "apply_supervisor_templates" in steps
    assert all(item["requires_confirm_step"] for item in plan["steps"])


def test_autonomous_demo_apply_plan_excludes_legacy_factor_approvals(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("backend.services.autonomous_demo_apply_stepper._demo_autonomous_enabled", lambda: True)
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.executemany(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, status,
             governance_eligible, governance_eligibility_version,
             governance_eligibility_fingerprint, created_at)
            VALUES (?, 'factor', ?, 'downweight', 'approved', ?, ?, ?, ?)
            """,
            [
                ("legacy", "rsi_14", 0, "", "", 1.0),
                ("current", "macd_hist", 1, "governance_eligibility.v1", "fp", 2.0),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    plan = AutonomousDemoApplyStepper(db_path).plan()

    assert plan["pending"]["sync_factor_weights"] == 1


def test_v16_actionable_predicate_skips_stale_head_and_claimed_command(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "state.db"
    now = time.time()
    monkeypatch.setenv("QUANT_V16_COMMAND_MAX_AGE_SECONDS", "60")
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.executemany(
            """
            INSERT INTO brain_governance_candidate
            (candidate_id, status, created_at, updated_at)
            VALUES (?, 'active', ?, ?)
            """,
            [
                ("candidate-stale", now - 120.0, now - 120.0),
                ("candidate-fresh", now - 10.0, now - 10.0),
            ],
        )
        conn.executemany(
            """
            INSERT INTO v16_brain_command
            (command_id, candidate_id, target_agent, scope_type, scope_key,
             action, decision, status, claim_status, authority_issued_at,
             created_at, updated_at)
            VALUES (?, ?, 'position_supervisor_governance',
                    'supervisor_template', 'position_supervisor',
                    'switch_position_supervisor_template', 'delegate',
                    'delegated_to_specialist', 'available', ?, ?, ?)
            """,
            [
                ("command-stale", "candidate-stale", now - 120.0, now - 120.0, now - 120.0),
                ("command-fresh", "candidate-fresh", now - 10.0, now - 10.0, now - 10.0),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    stepper = AutonomousDemoApplyStepper(db_path)
    assert stepper.plan()["pending"]["dispatch_v16_delegation"] == 1
    assert V16BrainOrchestratorService(db_path).status()["actionable_command_count"] == 1
    assert V16CommandGate.authorize(
        db_path,
        command_id="command-stale",
        target_agent="position_supervisor_governance",
        scope_type="supervisor_template",
        scope_key="position_supervisor",
        action="switch_position_supervisor_template",
    )["allowed"] is False
    cancelled = V16BrainOrchestratorService(
        db_path
    )._cancel_non_actionable_commands(persist=True)
    assert cancelled["expired_delegate_count"] == 1
    assert stepper.plan()["pending"]["dispatch_v16_delegation"] == 1

    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            """
            UPDATE v16_brain_command
            SET claim_status='claimed', claim_token='other-worker',
                claimed_at=?, claim_expires_at=?
            WHERE command_id='command-fresh'
            """,
            (now, now + 30.0),
        )
        conn.commit()
    finally:
        conn.close()

    assert stepper.plan()["pending"]["dispatch_v16_delegation"] == 0
    assert V16BrainOrchestratorService(db_path).status()["actionable_command_count"] == 0
    assert V16CommandGate.authorize(
        db_path,
        command_id="command-fresh",
        target_agent="position_supervisor_governance",
        scope_type="supervisor_template",
        scope_key="position_supervisor",
        action="switch_position_supervisor_template",
    )["allowed"] is False


def test_autonomous_demo_apply_stepper_limits_conflict_resolution(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("backend.services.autonomous_demo_apply_stepper._demo_autonomous_enabled", lambda: True)
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE policy_suggestion (
                suggestion_id TEXT PRIMARY KEY,
                scope_type TEXT,
                scope_key TEXT,
                action TEXT,
                confidence REAL,
                evidence_json TEXT,
                status TEXT,
                review_note TEXT,
                reviewed_at REAL,
                governance_eligible INTEGER NOT NULL DEFAULT 0,
                governance_eligibility_version TEXT NOT NULL DEFAULT '',
                governance_eligibility_fingerprint TEXT NOT NULL DEFAULT '',
                created_at REAL
            )
            """
        )
        rows = [
            ("s1", "factor", "rsi_14", "boost_small", 0.2, "{}", "approved", "", 0.0, 1.0),
            ("s2", "factor", "rsi_14", "boost_small", 0.3, "{}", "approved", "", 0.0, 2.0),
            ("s3", "factor", "rsi_14", "boost_small", 0.4, "{}", "approved", "", 0.0, 3.0),
        ]
        conn.executemany(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence, evidence_json,
             status, review_note, reviewed_at, governance_eligible,
             governance_eligibility_version, governance_eligibility_fingerprint, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'governance_eligibility.v1', 'fp', ?)
            """,
            rows,
        )
        conn.commit()
    finally:
        conn.close()

    result = AutonomousDemoApplyStepper(db_path).run_step("resolve_conflicts", limit=1, confirm_step=True)

    assert result["ok"] is True
    assert result["result"]["superseded"] == 1
    assert result["result"]["remaining_superseded"] >= 1
    assert result["execution_context"]["result_refs"]["superseded"] == 1
    conn = connect_sqlite(db_path, read_only=True)
    try:
        superseded_count = conn.execute("SELECT COUNT(*) FROM policy_suggestion WHERE status='superseded'").fetchone()[0]
    finally:
        conn.close()
    assert superseded_count == 1
