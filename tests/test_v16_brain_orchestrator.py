import json
import time

from backend.core.db import STATE_DB_DDL, connect_sqlite
from backend.services.v16_brain_orchestrator import V16BrainOrchestratorService
from backend.services.v16_brain_snapshot import build_posterior_arbitration


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


def _seed_posterior_facts(db_path, now: float) -> None:
    conn = connect_sqlite(db_path)
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
        conn.execute(
            """
            INSERT INTO trade_outcome_review
            (review_id, trade_id, position_id, pnl, outcome_label,
             failure_tags_json, summary_text, review_json, created_at)
            VALUES ('review-v16', 'trade-v16', 'position-v16', -10.0, 'loss', ?,
                    'entry was weak', ?, ?)
            """,
            (
                json.dumps(["weak_entry"]),
                json.dumps({"primary_responsibility": "entry", "failure_taxonomy": {"primary_responsibility": "entry"}}),
                now - 20.0,
            ),
        )
        conn.execute(
            """
            INSERT INTO learning_application_effect
            (application_id, scope_type, scope_key, action, status,
             observed_trade_count, baseline_trade_count, post_avg_reward,
             baseline_avg_reward, delta_avg_reward, post_win_rate,
             baseline_win_rate, updated_at, created_at)
            VALUES ('effect-v16', 'supervisor_template', 'position_supervisor',
                    'switch_position_supervisor_template', 'observed', 5, 5,
                    0.30, 0.10, 0.20, 0.70, 0.50, ?, ?)
            """,
            (now - 10.0, now - 10.0),
        )
        conn.execute(
            """
            INSERT INTO position_supervisor_trace
            (trace_id, decision_id, position_id, trade_id, event_ts, action,
             outcome, risk_allowed, execution_status, trace_integrity, created_at)
            VALUES ('trace-v16', 'decision-v16', 'position-v16', 'trade-v16', ?,
                    'tighten', 'observed', 1, 'observed', 'full', ?)
            """,
            (now - 15.0, now - 15.0),
        )
        conn.execute(
            """
            INSERT INTO supervisor_counterfactual_review
            (counterfactual_id, review_id, trade_id, position_id, close_ts,
             close_reason, supervisor_event_type, supervisor_reason, label,
             confidence, horizons_json, evidence_json, created_at, updated_at)
            VALUES ('cf-v16', 'review-v16', 'trade-v16', 'position-v16', ?,
                    'stop', 'tighten', 'tighten happened too early',
                    'premature_tighten', 0.80, ?, ?, ?, ?)
            """,
            (
                now - 15.0,
                json.dumps([{"horizon_minutes": 30, "future_pnl": 9.7}]),
                json.dumps({"tags": ["future_bars_complete"]}),
                now - 5.0,
                now - 5.0,
            ),
        )
        conn.commit()
    finally:
        conn.close()


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


def test_v16_orchestrator_dispatches_without_direct_runtime_mutation(tmp_path):
    db_path = tmp_path / "state.db"
    _seed_posterior_facts(db_path, time.time())

    service = V16BrainOrchestratorService(db_path)
    result = service.run_once(readiness=_readiness(), limit=20, source="test", persist=True)

    assert result["status"] == "delegated"
    assert result["delegated_count"] == 1
    command = next(item for item in result["commands"] if item["decision"] == "delegate")
    assert command["target_agent"] == "position_supervisor_governance"
    assert command["candidate_id"]
    assert command["boundary"]["does_not_write"][0] == "policy_suggestion"
    assert command["delegation"]["execution_owner"] == "position_supervisor_governance"

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


def test_superseded_candidate_cancels_unclaimed_delegate(tmp_path):
    db_path = tmp_path / "state.db"
    _seed_posterior_facts(db_path, time.time())
    service = V16BrainOrchestratorService(db_path)
    service.run_once(readiness=_readiness(), limit=20, source="test", persist=True)
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
