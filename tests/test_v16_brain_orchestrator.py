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
from backend.services.brain_governance_candidates import (
    BrainGovernanceCandidateService,
    ensure_policy_suggestion_table,
)
from backend.services.v16_posterior_arbitration import build_posterior_arbitration
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


def test_v16_run_once_reconciles_commands_without_materialising_cognition(tmp_path):
    """Run once no longer derives candidates, plans, evals or commands.

    Cognition materialisation was stopped on 2026-09-11 and the arbitration
    carrier no longer needs the snapshot table: commands are only issued by
    their owning specialists through ``delegate_*`` and merely reconciled
    here, while this cycle's posterior arbitration is derived from canonical
    evidence and published as ``posterior_fingerprint``.
    """
    db_path = tmp_path / "state.db"
    _seed_posterior_facts(db_path, time.time())

    service = V16BrainOrchestratorService(db_path)
    result = service.run_once(limit=20, persist=True)
    assert result["status"] == "observing"
    assert result["delegated_count"] == 0
    assert result["commands"] == []
    assert result["posterior_fingerprint"]
    assert "snapshot_id" not in result
    assert "plan_count" not in result
    assert "eval_count" not in result
    assert "governance_count" not in result

    conn = connect_sqlite(db_path, read_only=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM v16_brain_command").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM brain_governance_candidate").fetchone()[0] == 0
        # The cognition ledgers were retired with their producers on
        # 2026-09-11; the cycle must not recreate them.
        for retired in (
            "brain_state_snapshot",
            "brain_action_plan",
            "brain_action_plan_eval",
            "brain_medium_impact_governance",
        ):
            assert conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
                (retired,),
            ).fetchone()[0] == 0
    finally:
        conn.close()

    # Idempotent: a second run over unchanged facts still writes nothing.
    second = service.run_once(limit=20, persist=True)
    assert second["delegated_count"] == 0
    conn = connect_sqlite(db_path, read_only=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM v16_brain_command").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM brain_governance_candidate").fetchone()[0] == 0
    finally:
        conn.close()


def test_reviewed_expired_delegate_is_reissued_once(tmp_path):
    """A cancelled delegate with a fresh reviewed bridge is reissued exactly once."""
    db_path = tmp_path / "state.db"
    conn = make_canonical_sqlite(db_path)
    conn.executescript(STATE_DB_DDL)
    conn.commit()
    conn.close()
    ensure_v16_brain_command_table(db_path)
    ensure_policy_suggestion_table(db_path)
    ensure_brain_governance_candidate_review_table(db_path)

    candidate_id = "candidate_reissue"
    BrainGovernanceCandidateService(db_path).create_candidate(
        candidate_id=candidate_id,
        source_agent="v16_brain",
        source_kind="brain_medium_impact_governance",
        source_ref_type="test",
        source_ref_id="eval-reissue",
        proposal_stage="governance_ready",
        capability_scope="medium_impact_governance",
        scope_type="factor",
        scope_key="alpha_weight_policy",
        action="downweight",
        confidence=0.8,
        evidence_score=0.8,
        risk_class="medium",
        max_impact="medium_impact",
        risk_verdict={"allowed": True},
        status="active",
        persist=True,
    )
    now = time.time()
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            "UPDATE brain_governance_candidate SET submitted_suggestion_id='sug-reissue' "
            "WHERE candidate_id=?",
            (candidate_id,),
        )
        conn.execute(
            """INSERT INTO brain_governance_candidate_review
               (review_id, candidate_id, review_status, bridge_ready,
                bridge_reason, evidence_gaps_json, conflict_json,
                bridge_preview_json, source_reliability_json,
                llm_advisory_json, boundary_json, evidence_fingerprint, created_at)
               VALUES (?, ?, 'bridge_ready', 1, '', '[]', '{}', '{}', '{}', '{}', '{}', ?, ?)""",
            ("review-reissue", candidate_id, "e" * 64, now + 1.0),
        )
        conn.commit()
    finally:
        conn.close()

    service = V16BrainOrchestratorService(db_path)
    service._persist_commands(
        [
            {
                "command_id": "cmd-expired",
                "candidate_id": candidate_id,
                "target_agent": "autonomous_learning",
                "scope_type": "factor",
                "scope_key": "alpha_weight_policy",
                "action": "downweight",
                "decision": "delegate",
                "status": "delegated_to_specialist",
                "evidence": {"candidate_id": candidate_id},
                "delegation": {},
                "posterior_fingerprint": "p" * 64,
                "evidence_fingerprint": "e" * 64,
                "max_apply_count": 1,
                "created_at": now - 60.0,
                "updated_at": now - 60.0,
            }
        ]
    )
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            """UPDATE v16_brain_command
               SET claim_status='cancelled', failure_reason='authority_expired',
                   finalized_at=?, updated_at=?
               WHERE command_id='cmd-expired'""",
            (now - 30.0, now - 30.0),
        )
        conn.commit()
    finally:
        conn.close()

    reissues = service._reviewed_expired_delegate_reissues(limit=20)
    assert [item["candidate_id"] for item in reissues] == [candidate_id]
    service._persist_commands(reissues)
    # The fresh command is claimable, so the same candidate is not reissued again.
    assert service._reviewed_expired_delegate_reissues(limit=20) == []



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
