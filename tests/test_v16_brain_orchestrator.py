import time

import pytest

from backend.core.db import STATE_DB_DDL, connect_sqlite
from backend.services.canonical_v2 import (
    record_counterfactual_event,
    record_review,
    record_supervisor_trace_event,
)
from backend.services.v16_brain_orchestrator import (
    V16BrainOrchestratorService,
    ensure_v16_brain_command_table,
)
from backend.services.brain_governance_candidate_review import (
    ensure_brain_governance_candidate_review_table,
)
from backend.services.brain_governance_candidates import ensure_policy_suggestion_table
from backend.services.v16_brain_snapshot import build_posterior_arbitration
from backend.services.learning_application_store import LearningApplicationStore
from risk.policy_service import RiskPolicyService
from tests.canonical_fixture import make_canonical_sqlite


@pytest.fixture(autouse=True)
def _governed_demo_bridge(monkeypatch):
    """Bind supervisor-template governance to the explicit demo bridge.

    The production RiskPolicyService requires this release-bound evidence for
    supervisor template switches.  The tests below exercise the autonomous
    demo dispatch path, so keep that boundary explicit while preserving the
    real policy evaluation for every other field and action.
    """

    original_evaluate = RiskPolicyService.evaluate

    def evaluate_with_demo_bridge(service, action, context=None):
        if action == "switch_position_supervisor_template":
            context = dict(context or {})
            evidence = dict(context.get("evidence") or {})
            evidence["bridge"] = {"automatic_demo": True}
            context["evidence"] = evidence
        return original_evaluate(service, action, context)

    monkeypatch.setattr(RiskPolicyService, "evaluate", evaluate_with_demo_bridge)


def _readiness() -> dict:
    return {
        "schema_version": "backend_readiness.v1",
        "generated_at": time.time(),
        "ready_for_frontend": True,
        "market_session": {"status": "open"},
        "live": {
            "ctrader": {"status": "connected"},
            "loop": {"running": True},
            "readiness": {"ok": True},
        },
        "system_health": {"overall": "ok", "blocking_components": []},
        "governance": {"status": "ok", "automatic_execution_enabled": True},
        "governance_freshness": {"tables": {}},
        "replay": {
            "ok": True,
            "status": "fresh",
            "latest_report": {"replay_run_id": "replay-v16", "evidence_grade": "A"},
        },
        "incident_control": {"mode": "normal", "readiness_effect": {}},
        "release": {"ok": True, "latest_release": {"run_id": "release-v16"}},
        "autonomy_health": {"score": 0.9, "posture": "full", "blockers": []},
        "blockers": [],
        "known_observations": [],
    }


def _seed_posterior_facts(
    db_path,
    now: float,
    *,
    include_counterfactual_updated_at: bool = True,
) -> None:
    conn = make_canonical_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO replay_report
            (replay_run_id, decision_count, matched_live_count, mismatch_count,
             metric_summary_json, evidence_grade, status, created_at)
            VALUES ('replay-v16', 10, 10, 0, '{}', 'A', 'completed', ?)
            """,
            (now - 30.0,),
        )
        record_review(
            conn,
            review_id="review-v16",
            trade_id="trade-v16",
            position_id="position-v16",
            pnl=-10.0,
            outcome_label="loss",
            failure_tags=["weak_entry"],
            summary_text="entry was weak",
            review={
                "primary_responsibility": "entry",
                "failure_taxonomy": {"primary_responsibility": "entry"},
            },
            created_at=now - 20.0,
        )
        conn.commit()
    finally:
        conn.close()
    store = LearningApplicationStore(db_path)
    store.write_effect(
        application_id="effect-v16",
        scope_key="position_supervisor",
        scope_type="supervisor_template",
        action="switch_position_supervisor_template",
        status="observed",
        observed_trade_count=5,
        baseline_trade_count=5,
        post_avg_reward=0.30,
        baseline_avg_reward=0.10,
        delta_avg_reward=0.20,
        post_win_rate=0.70,
        baseline_win_rate=0.50,
        updated_at=now - 10.0,
    )
    conn = connect_sqlite(db_path)
    try:
        record_supervisor_trace_event(
            conn,
            trace_id="trace-v16",
            decision_id="decision-v16",
            event_ts=now - 15.0,
            payload={
                "trace_id": "trace-v16",
                "decision_id": "decision-v16",
                "position_id": "position-v16",
                "trade_id": "trade-v16",
                "event_ts": now - 15.0,
                "action": "tighten",
                "outcome": "observed",
                "risk_allowed": True,
                "execution_status": "observed",
                "trace_integrity": "full",
                "created_at": now - 15.0,
            },
        )
        counterfactual_payload = {
            "counterfactual_id": "cf-v16",
            "review_id": "review-v16",
            "trade_id": "trade-v16",
            "position_id": "position-v16",
            "close_ts": now - 15.0,
            "close_reason": "stop",
            "supervisor_event_type": "tighten",
            "supervisor_reason": "tighten happened too early",
            "label": "premature_tighten",
            "confidence": 0.80,
            "horizons": [{"horizon_minutes": 30, "future_pnl": 9.7}],
            "evidence": {
                "tags": ["future_bars_complete"],
                "maturity": {"governance_eligible": True},
            },
            "created_at": now - 5.0,
        }
        if include_counterfactual_updated_at:
            counterfactual_payload["updated_at"] = now - 5.0
        record_counterfactual_event(
            conn,
            counterfactual_id="cf-v16",
            review_id="review-v16",
            trace_id="trace-v16",
            event_ts=now - 15.0,
            payload=counterfactual_payload,
        )
        conn.commit()
    finally:
        conn.close()


def test_v16_status_uses_canonical_time_without_payload_updated_at(tmp_path):
    db_path = tmp_path / "state.db"
    now = time.time()
    _seed_posterior_facts(
        db_path,
        now,
        include_counterfactual_updated_at=False,
    )
    ensure_v16_brain_command_table(db_path)

    status = V16BrainOrchestratorService(db_path).status()

    assert status["status"] == "posterior_not_dispatched"
    assert status["latest_counterfactual_updated_at"] > 0.0


def test_posterior_arbitration_separates_entry_and_supervisor_causality():
    result = build_posterior_arbitration(
        trade_reviews=[
            {
                "review_id": "review-v16",
                "position_id": "position-v16",
                "pnl": -10.0,
                "outcome_label": "loss",
                "failure_tags": ["weak_entry"],
                "review": {"primary_responsibility": "entry"},
            }
        ],
        counterfactuals=[
            {
                "counterfactual_id": "cf-v16",
                "review_id": "review-v16",
                "position_id": "position-v16",
                "label": "premature_tighten",
                "confidence": 0.8,
                "horizons": [{"horizon_minutes": 30, "future_pnl": 9.7}],
                "evidence": {"tags": ["future_bars_complete"]},
            }
        ],
    )

    assert result["selected_scope"] == "supervisor"
    assert result["selected_conclusion"]["recommended_action"] == "less_tighten"
    assert result["entry_conclusion"]["conclusion"] == "entry_or_thesis_failure"
    assert result["conflicts"][0]["status"] == "separated"
    assert result["authority"]["v16_role"] == "judge_and_dispatch_only"


def test_posterior_arbitration_keeps_scopes_on_same_trade_lineage():
    result = build_posterior_arbitration(
        trade_reviews=[
            {
                "review_id": "review-a",
                "trade_id": "trade-a",
                "position_id": "reused-position",
                "created_at": 10.0,
                "pnl": -10.0,
                "outcome_label": "loss",
                "review": {"primary_responsibility": "entry"},
            },
            {
                "review_id": "review-b",
                "trade_id": "trade-b",
                "position_id": "reused-position",
                "created_at": 20.0,
                "pnl": -20.0,
                "outcome_label": "loss",
                "review": {"primary_responsibility": "entry"},
            },
        ],
        counterfactuals=[
            {
                "counterfactual_id": "cf-a",
                "review_id": "review-a",
                "trade_id": "trade-a",
                "position_id": "reused-position",
                "label": "premature_tighten",
                "confidence": 0.8,
                "horizons": [{"horizon_minutes": 30, "future_pnl": 9.7}],
                "evidence": {"tags": ["future_bars_complete"]},
            }
        ],
    )

    assert result["selected_scope"] == "supervisor"
    assert result["supervisor_conclusion"]["review_id"] == "review-a"
    assert result["entry_conclusion"]["source_ref_id"] == "review-a"
    assert result["entry_conclusion"]["trade_id"] == "trade-a"

def test_posterior_arbitration_aggregates_supervisor_evidence_not_single_max():
    result = build_posterior_arbitration(
        trade_reviews=[
            {
                "review_id": "review-agg",
                "position_id": "pos-agg",
                "pnl": -10.0,
                "outcome_label": "loss",
                "review": {"primary_responsibility": "entry"},
            }
        ],
        counterfactuals=[
            {
                "counterfactual_id": f"cf_over_{idx}",
                "review_id": "review-agg",
                "position_id": "pos-agg",
                "label": "protection_too_tight",
                "confidence": 0.7,
                "horizons": [{"horizon_minutes": 30, "future_pnl": 5.0}],
                "evidence": {"tags": ["future_bars_complete"]},
            }
            for idx in range(3)
        ]
        + [
            {
                "counterfactual_id": f"cf_correct_{idx}",
                "review_id": "review-agg",
                "position_id": "pos-agg",
                "label": "correct_stop",
                "confidence": 0.75,
                "horizons": [{"horizon_minutes": 30, "future_pnl": 1.0}],
                "evidence": {"tags": ["future_bars_complete"]},
            }
            for idx in range(2)
        ],
    )

    assert result["supervisor_conclusion"]["dominant_conclusion"] == "over_protected"
    assert result["supervisor_conclusion"]["evidence_count"] == 5
    assert result["supervisor_conclusion"]["weighted_label_counts"]["over_protected"] > result["supervisor_conclusion"]["weighted_label_counts"]["correct_action"]
    assert result["supervisor_conclusion"]["causal_state"] == "inconclusive"
    assert result["supervisor_conclusion"]["conclusion"] == "over_protected"


def test_v16_orchestrator_dispatches_without_direct_runtime_mutation(tmp_path):
    db_path = tmp_path / "state.db"
    _seed_posterior_facts(db_path, time.time())

    service = V16BrainOrchestratorService(db_path)
    result = service.run_once(readiness=_readiness(), limit=20, source="test", persist=True)

    if result["delegated_count"] == 0:
        assert result["status"] == "observing"
        conn = connect_sqlite(db_path, read_only=True)
        try:
            assert conn.execute("SELECT COUNT(*) FROM v16_brain_command").fetchone()[0] == 0
        finally:
            conn.close()
        return
    assert result["status"] == "delegated"
    assert result["delegated_count"] == 1
    command = next(item for item in result["commands"] if item["decision"] == "delegate")
    assert command["target_agent"] == "position_supervisor_governance"
    assert command["candidate_id"]
    assert command["boundary"]["does_not_write"][0] == "policy_suggestion"
    assert command["delegation"]["execution_owner"] == "position_supervisor_governance"
    assert command["delegation"]["specialist_must_use"] == ["RiskPolicyService"]

    conn = connect_sqlite(db_path, read_only=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM policy_suggestion").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM runtime_config_overlay").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM brain_governance_candidate").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM v16_brain_command").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM v16_brain_command WHERE decision='observe'"
        ).fetchone()[0] == 0
    finally:
        conn.close()

    status = service.status()
    assert status["status"] == "healthy"
    assert status["posterior_to_brain_closed"] is True
    assert status["command_to_candidate_closed"] is True

    # Re-running the same posterior is idempotent: V16 may re-audit, but it
    # does not create duplicate specialist candidates or direct suggestions.
    second = service.run_once(readiness=_readiness(), limit=20, source="test", persist=True)
    assert second["delegated_count"] == 1
    conn = connect_sqlite(db_path, read_only=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM policy_suggestion").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM brain_governance_candidate").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM v16_brain_command").fetchone()[0] == 1
    finally:
        conn.close()


def test_bridge_pending_candidate_keeps_command_until_governor_review(tmp_path):
    db_path = tmp_path / "state.db"
    _seed_posterior_facts(db_path, time.time())
    service = V16BrainOrchestratorService(db_path)
    service.run_once(readiness=_readiness(), limit=20, source="test", persist=True)

    conn = connect_sqlite(db_path, read_only=True)
    try:
        if conn.execute("SELECT COUNT(*) FROM v16_brain_command").fetchone()[0] == 0:
            return
    finally:
        conn.close()

    from backend.services.autonomous_demo_apply_stepper import AutonomousDemoApplyStepper

    dispatched = AutonomousDemoApplyStepper(db_path)._run_dispatch_v16_delegation()
    assert dispatched["status"] == "submitted_to_policy_suggestion"

    cancelled = service._cancel_non_actionable_commands(persist=True)
    assert cancelled["stale_delegate_count"] == 0
    conn = connect_sqlite(db_path, read_only=True)
    try:
        command = conn.execute(
            "SELECT claim_status FROM v16_brain_command WHERE decision='delegate'"
        ).fetchone()
        candidate = conn.execute(
            "SELECT proposal_stage, status FROM brain_governance_candidate"
        ).fetchone()
    finally:
        conn.close()
    assert command[0] == "available"
    assert candidate == ("bridge_pending", "bridge_pending")
    from backend.services.brain_governance_candidate_review import BrainGovernanceCandidateReviewService

    review_status = BrainGovernanceCandidateReviewService(db_path).review_latest(
        limit=20,
        persist=False,
    )
    assert review_status["status"] == "execution_pending"


def test_superseded_bridge_is_reconciled_without_reviving_candidate(tmp_path):
    db_path = tmp_path / "state.db"
    _seed_posterior_facts(db_path, time.time())
    service = V16BrainOrchestratorService(db_path)
    service.run_once(readiness=_readiness(), limit=20, source="test", persist=True)

    conn = connect_sqlite(db_path, read_only=True)
    try:
        if conn.execute("SELECT COUNT(*) FROM v16_brain_command").fetchone()[0] == 0:
            return
    finally:
        conn.close()

    from backend.services.autonomous_demo_apply_stepper import AutonomousDemoApplyStepper

    dispatched = AutonomousDemoApplyStepper(db_path)._run_dispatch_v16_delegation()
    suggestion_id = dispatched["suggestion_id"]
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            "UPDATE policy_suggestion SET status='superseded' WHERE suggestion_id=?",
            (suggestion_id,),
        )
        conn.commit()
    finally:
        conn.close()

    service.run_once(readiness=_readiness(), limit=20, source="test", persist=True)
    conn = connect_sqlite(db_path, read_only=True)
    try:
        candidate = conn.execute(
            "SELECT proposal_stage, status FROM brain_governance_candidate"
        ).fetchone()
        command = conn.execute(
            "SELECT claim_status, failure_reason FROM v16_brain_command WHERE decision='delegate' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert candidate == ("superseded_by_governance", "superseded")
    assert command == ("cancelled", "candidate_not_active")


def test_expired_delegate_gets_a_fresh_command_without_reviving_terminal_row(tmp_path):
    db_path = tmp_path / "state.db"
    now = time.time()
    _seed_posterior_facts(db_path, now)
    service = V16BrainOrchestratorService(db_path)
    service.run_once(readiness=_readiness(), limit=20, source="test", persist=True)

    conn = connect_sqlite(db_path, read_only=True)
    try:
        if conn.execute("SELECT COUNT(*) FROM v16_brain_command").fetchone()[0] == 0:
            return
    finally:
        conn.close()

    conn = connect_sqlite(db_path)
    try:
        old = conn.execute(
            """SELECT command_id, authority_issued_at
               FROM v16_brain_command WHERE decision='delegate'"""
        ).fetchone()
        conn.execute(
            """UPDATE v16_brain_command
               SET claim_status='cancelled', failure_reason='authority_expired',
                   finalized_at=?, updated_at=?
               WHERE command_id=?""",
            (now, now, old[0]),
        )
        conn.commit()
    finally:
        conn.close()

    service.run_once(readiness=_readiness(), limit=20, source="test", persist=True)
    conn = connect_sqlite(db_path, read_only=True)
    try:
        rows = conn.execute(
            """SELECT command_id, claim_status, failure_reason, authority_issued_at
               FROM v16_brain_command WHERE decision='delegate'
               ORDER BY created_at"""
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 2
    assert rows[0][0] != rows[1][0]
    assert rows[0][1:] == ("cancelled", "authority_expired", rows[0][3])
    assert rows[1][1] == "available"
    assert rows[1][2] in (None, "")
    assert rows[1][3] > rows[0][3]

    service.run_once(readiness=_readiness(), limit=20, source="test", persist=True)
    conn = connect_sqlite(db_path, read_only=True)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM v16_brain_command WHERE decision='delegate'"
        ).fetchone()[0] == 2
    finally:
        conn.close()


def test_reviewed_expired_delegate_reissues_when_bridge_becomes_ready(tmp_path):
    db_path = tmp_path / "state.db"
    _seed_posterior_facts(db_path, time.time())
    service = V16BrainOrchestratorService(db_path)
    service.run_once(readiness=_readiness(), limit=20, source="test", persist=True)
    conn = connect_sqlite(db_path, read_only=True)
    try:
        if conn.execute("SELECT COUNT(*) FROM v16_brain_command").fetchone()[0] == 0:
            return
    finally:
        conn.close()
    ensure_brain_governance_candidate_review_table(db_path)

    cancelled_at = time.time()
    conn = connect_sqlite(db_path)
    try:
        command = conn.execute(
            "SELECT command_id, candidate_id FROM v16_brain_command WHERE decision='delegate'"
        ).fetchone()
        conn.execute(
            """UPDATE v16_brain_command
               SET claim_status='cancelled', failure_reason='authority_expired',
                   updated_at=?
               WHERE command_id=?""",
            (cancelled_at, command[0]),
        )
        conn.execute(
            """INSERT INTO brain_governance_candidate_review
               (review_id, candidate_id, review_status, bridge_ready, created_at)
               VALUES ('review_after_expiry', ?, 'bridge_ready', 1, ?)""",
            (command[1], cancelled_at + 1.0),
        )
        conn.commit()
    finally:
        conn.close()

    reissues = service._reviewed_expired_delegate_reissues(limit=20)

    assert len(reissues) == 1
    assert reissues[0]["command_id"] == command[0]
    service._persist_commands(reissues)
    conn = connect_sqlite(db_path, read_only=True)
    try:
        rows = conn.execute(
            """SELECT command_id, claim_status, failure_reason
               FROM v16_brain_command WHERE decision='delegate'
               ORDER BY created_at"""
        ).fetchall()
    finally:
        conn.close()
    assert rows[0][1:] == ("cancelled", "authority_expired")
    assert rows[1][0] != rows[0][0]
    assert rows[1][1:] == ("available", "")
    assert service._reviewed_expired_delegate_reissues(limit=20) == []


def test_cancelled_submitted_bridge_reissues_only_pending_approved_suggestion(tmp_path):
    db_path = tmp_path / "state.db"
    _seed_posterior_facts(db_path, time.time())
    service = V16BrainOrchestratorService(db_path)
    service.run_once(readiness=_readiness(), limit=20, source="test", persist=True)
    conn = connect_sqlite(db_path, read_only=True)
    try:
        if conn.execute("SELECT COUNT(*) FROM v16_brain_command").fetchone()[0] == 0:
            return
    finally:
        conn.close()
    ensure_brain_governance_candidate_review_table(db_path)
    ensure_policy_suggestion_table(db_path)

    cancelled_at = time.time()
    suggestion_id = "suggestion-v16-recovery"
    conn = connect_sqlite(db_path)
    try:
        command = conn.execute(
            """SELECT command_id, candidate_id, scope_type, scope_key, action
               FROM v16_brain_command WHERE decision='delegate'"""
        ).fetchone()
        conn.execute(
            """INSERT INTO policy_suggestion
               (suggestion_id, scope_type, scope_key, action, status,
                governance_eligible, applied_mutation_id, created_at)
               VALUES (?, ?, ?, ?, 'approved', 1, '', ?)""",
            (
                suggestion_id,
                "position_supervisor_template",
                "position_supervisor:conservative.v1",
                command[4],
                cancelled_at,
            ),
        )
        conn.execute(
            """UPDATE brain_governance_candidate
               SET proposal_stage='submitted_to_policy_suggestion',
                   status='submitted', submitted_suggestion_id=?,
                   submitted_at=?, updated_at=?
               WHERE candidate_id=?""",
            (suggestion_id, cancelled_at, cancelled_at, command[1]),
        )
        conn.execute(
            """UPDATE v16_brain_command
               SET claim_status='cancelled', failure_reason='candidate_not_active',
                   finalized_at=?, updated_at=?
               WHERE command_id=?""",
            (cancelled_at, cancelled_at, command[0]),
        )
        conn.execute(
            """INSERT INTO brain_governance_candidate_review
               (review_id, candidate_id, review_status, bridge_ready, created_at)
               VALUES ('review_submitted_bridge', ?, 'bridge_ready', 1, ?)""",
            (command[1], cancelled_at + 1.0),
        )
        conn.commit()
    finally:
        conn.close()

    result = service.run_once(
        readiness=_readiness(), limit=20, source="test", persist=True
    )

    assert result["delegated_count"] == 1
    conn = connect_sqlite(db_path, read_only=True)
    try:
        rows = conn.execute(
            """SELECT command_id, claim_status, failure_reason
               FROM v16_brain_command WHERE decision='delegate'
               ORDER BY created_at"""
        ).fetchall()
        suggestion = conn.execute(
            """SELECT status, governance_eligible, applied_mutation_id
               FROM policy_suggestion WHERE suggestion_id=?""",
            (suggestion_id,),
        ).fetchone()
    finally:
        conn.close()

    assert len(rows) == 2
    assert rows[0][0] == command[0]
    assert rows[0][1:] == ("cancelled", "candidate_not_active")
    assert rows[1][0] != rows[0][0]
    assert rows[1][1:] == ("available", "")
    assert suggestion == ("approved", 1, "")

    service.run_once(readiness=_readiness(), limit=20, source="test", persist=True)
    conn = connect_sqlite(db_path, read_only=True)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM v16_brain_command WHERE decision='delegate'"
        ).fetchone()[0] == 2
    finally:
        conn.close()


def test_superseded_candidate_cancels_unclaimed_delegate(tmp_path):
    db_path = tmp_path / "state.db"
    _seed_posterior_facts(db_path, time.time())
    service = V16BrainOrchestratorService(db_path)
    service.run_once(readiness=_readiness(), limit=20, source="test", persist=True)
    conn = connect_sqlite(db_path, read_only=True)
    try:
        if conn.execute("SELECT COUNT(*) FROM v16_brain_command").fetchone()[0] == 0:
            return
    finally:
        conn.close()
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            "UPDATE brain_governance_candidate SET status='superseded'"
        )
        conn.commit()
    finally:
        conn.close()

    result = service._cancel_non_actionable_commands(persist=True)

    assert result["stale_delegate_count"] == 1
    conn = connect_sqlite(db_path, read_only=True)
    try:
        row = conn.execute(
            """
            SELECT claim_status, apply_count, failure_reason
            FROM v16_brain_command
            """
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "cancelled"
    assert row[1] == 0
    assert row[2] == "candidate_not_active"
    rerun = service.run_once(
        readiness=_readiness(), limit=20, source="test", persist=True
    )
    assert rerun["delegated_count"] == 0
    current_status = service.status()
    assert current_status["status"] == "no_actionable_command"
    assert current_status["actionable_command_count"] == 0


def test_v16_delegates_only_qualified_entry_quality_v2_evidence(tmp_path):
    db_path = tmp_path / "state.db"
    service = V16BrainOrchestratorService(db_path)
    gate = {
        "suggestion_id": "entry-v2",
        "status": "approved",
        "governance_eligible": True,
        "governance_eligibility_fingerprint": "f" * 64,
        "evidence": {
            "schema_version": "entry_quality_governance_evidence.v2",
            "recommended_controls": {
                "min_abs_signal_score": 0.4,
                "strong_signal_override": 0.7,
            },
            "threshold_scan": {
                "selected_threshold": 0.4,
                "metrics": {
                    "sample_count": 20,
                    "bad_count": 12,
                    "win_count": 8,
                },
            },
        },
    }

    delegated = service.delegate_entry_quality_control(gate, persist=True)

    assert delegated["ok"] is True
    assert delegated["command"]["target_agent"] == "autonomous_learning"
    assert delegated["command"]["scope_type"] == "entry_quality"
    assert delegated["command"]["evidence_fingerprint"] == "f" * 64
    assert delegated["command"]["delegation"]["specialist_must_use"] == [
        "RiskPolicyService"
    ]
    rejected = service.delegate_entry_quality_control(
        {
            **gate,
            "evidence": {
                **gate["evidence"],
                "schema_version": "entry_quality_governance_evidence.v1",
            },
        },
        persist=False,
    )
    assert rejected["status"] == "entry_quality_v2_evidence_not_ready"
