import json
import time
from pathlib import Path

import pandas as pd

from backend.core.db import STATE_DB_DDL, connect_sqlite
from backend.services.autonomy_health import AutonomyHealthService
from backend.services.incident_controls import RuntimeIncidentControlService
from backend.services import replay_harness as replay_harness_module
from tests.canonical_fixture import ensure_training_sample_row_sqlite
from backend.services.release_control import ReleaseControlService
from backend.services.replay_harness import ReplayHarnessService
from backend.services.v15_phase0 import V15Phase0CompletionService
from config import runtime_config as rc


def test_replay_recovery_lookup_keeps_numeric_position_id_as_text(monkeypatch):
    captured = {}

    class _Result:
        def fetchone(self):
            return {"position_id": "284214987"}

    def fake_execute(_conn, _sql, params=None):
        captured["params"] = params
        return _Result()

    monkeypatch.setattr(replay_harness_module, "_execute", fake_execute)
    service = object.__new__(ReplayHarnessService)

    row = service._find_recovery_position_state(
        object(),
        position_id="284214987",
    )

    assert row["position_id"] == "284214987"
    assert captured["params"] == ("284214987",)


def test_replay_harness_persists_factor_gate_risk_report(tmp_path):
    db_path = tmp_path / "state.db"
    now = time.time()
    verdict = {"allowed": True, "reason": "ok", "audit_payload": {"action": "open_trade"}}
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        ensure_training_sample_row_sqlite(db_path)
        conn.execute(
            """
            INSERT INTO runtime_config_snapshot
            (config_hash, source, config_json, run_id, created_at)
            VALUES ('cfg_hash', 'test', '{}', '', ?)
            """,
            (now,),
        )
        conn.execute(
            """
            INSERT INTO decision_ledger
            (decision_id, event_type, symbol, timeframe, decision_ts, action_score,
             action_reason, portfolio_state_json, risk_state_json, action_json, created_at)
            VALUES ('dec_1', 'open', 'XAUUSD+', 'M5', ?, 0.7, 'executed',
                    '{}', ?, ?, ?)
            """,
            (
                now - 30.0,
                json.dumps({"policy_verdict": verdict}),
                json.dumps({"gate_passed": True, "gate_reason": "pass", "risk_verdict": verdict}),
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO decision_factor_snapshot
            (decision_id, factor, normalized_value, policy_weight, contribution_score)
            VALUES ('dec_1', 'rsi_14', 0.6, 0.2, 0.12)
            """
        )
        conn.commit()
    finally:
        conn.close()

    service = ReplayHarnessService(db_path, artifact_dir=tmp_path / "replay_artifacts")
    report = service.run_factor_gate_risk_replay(lookback_days=1, limit=10)

    assert report["decision_count"] == 1
    assert report["matched_live_count"] == 1
    assert report["mismatch_count"] == 0
    assert report["runtime_config_hash"] == "cfg_hash"
    assert report["metric_summary"]["risk_verdict_coverage"] == 1.0
    assert report["evidence_grade"] == "A"
    assert report["artifact_path"]
    assert report["artifact_hash"]
    assert Path(report["artifact_path"]).exists()

    latest = service.latest_report()
    assert latest["replay_run_id"] == report["replay_run_id"]
    assert latest["artifact_hash"] == report["artifact_hash"]
    assert latest["artifact_path"] == report["artifact_path"]


def test_bar_replay_evidence_records_decision_bar_alignment(tmp_path):
    db_path = tmp_path / "state.db"
    now = time.time()
    verdict = {
        "allowed": True,
        "reason": "ok",
        "audit_payload": {
            "action": "open_trade",
            "temporal_context": {
                "timeframe_seconds": 300,
                "seconds_since_last_trade": 999999.0,
                "bars_since_last_trade": 999999.0,
            },
            "state": {
                "open_position_count": 0,
                "total_api_volume": 0.0,
                "requested_api_volume": 1.0,
            },
        },
    }
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        ensure_training_sample_row_sqlite(db_path)
        conn.execute(
            """
            INSERT INTO runtime_config_snapshot
            (config_hash, source, config_json, run_id, created_at)
            VALUES ('cfg_hash', 'test', '{}', '', ?)
            """,
            (now,),
        )
        conn.execute(
            """
            INSERT INTO decision_ledger
            (decision_id, trade_id, position_id, event_type, symbol, timeframe, decision_ts, action_score,
             action_reason, portfolio_state_json, risk_state_json, action_json, created_at)
            VALUES ('dec_bar_1', 'trade_1', '1001', 'open', 'XAUUSD+', 'M5', ?, 0.8, 'executed',
                    '{}', ?, ?, ?)
            """,
            (
                now - 30.0,
                json.dumps({"policy_verdict": verdict}),
                json.dumps(
                    {
                        "direction": 1,
                        "score": 0.8,
                        "gate_passed": True,
                        "gate_reason": "passed",
                        "requested_volume": 1.0,
                        "risk_verdict": verdict,
                    }
                ),
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO decision_factor_snapshot
            (decision_id, factor, normalized_value, policy_weight, contribution_score)
            VALUES ('dec_bar_1', 'rsi_14', 0.6, 0.2, 0.12)
            """
        )
        conn.execute(
            """
            INSERT INTO order_lifecycle_event
            (event_id, decision_id, trade_id, order_id, broker_order_id,
             event_type, event_ts, price, volume, status, details_json)
            VALUES ('ord_submitted_1', 'dec_bar_1', 'trade_1', 'order_1', 'broker_1',
                    'submitted', ?, 1.3, 1.0, 'submitted', '{}')
            """,
            (now - 29.0,),
        )
        conn.execute(
            """
            INSERT INTO order_lifecycle_event
            (event_id, decision_id, trade_id, order_id, broker_order_id,
             event_type, event_ts, price, volume, status, details_json)
            VALUES ('ord_filled_1', 'dec_bar_1', 'trade_1', 'order_1', 'broker_1',
                    'filled', ?, 1.31, 1.0, 'filled', '{}')
            """,
            (now - 28.0,),
        )
        conn.execute(
            """
            INSERT INTO position_lifecycle_event
            (event_id, position_id, trade_id, symbol, event_type, event_ts,
             net_volume, avg_price, unrealized_pnl, realized_pnl, details_json)
            VALUES ('pos_opened_1', '1001', 'trade_1', 'XAUUSD+', 'opened', ?,
                    1.0, 1.31, 0.0, 0.0, '{}')
            """,
            (now - 27.0,),
        )
        conn.execute(
            """
            INSERT INTO position_lifecycle_event
            (event_id, position_id, trade_id, symbol, event_type, event_ts,
             net_volume, avg_price, unrealized_pnl, realized_pnl, details_json)
            VALUES ('pos_closed_1', '1001', 'trade_1', 'XAUUSD+', 'closed', ?,
                    0.0, 1.31, 0.0, -1.25, ?)
            """,
            (
                now - 9.0,
                json.dumps({"close_reason": "thesis_broken"}),
            ),
        )
        conn.execute(
            """
            INSERT INTO trade_outcome_review
            (review_id, trade_id, position_id, entry_decision_id, exit_decision_id,
             pnl, outcome_label, summary_text, review_json, created_at)
            VALUES ('review_1', 'trade_1', '1001', 'dec_bar_1', 'dec_exit_1',
                    -1.25, 'bad_loss',
                    'trade trade_1 closed pnl=-1.25; outcome=bad_loss; primary_factor=rsi_14; worst_factor=adx',
                    ?, ?)
            """,
            (
                json.dumps({"close_ts": now - 9.0, "close_reason": "thesis_broken"}),
                now - 8.0,
            ),
        )
        conn.execute(
            """
            INSERT INTO training_sample_row
            (sample_id, sample_type, source_table, source_id, decision_id,
             trade_id, position_id, symbol, timeframe, event_ts,
             label_status, integrity, train_weight, system_contaminated,
             governance_eligible, governance_effective_weight,
             created_at, updated_at)
            VALUES ('als_trade_1', 'trade_review_outcome', 'trade_outcome_review',
                    'review_1', 'dec_bar_1', 'trade_1', '1001', 'XAUUSD+', 'M5',
                    ?, 'matured', 'full', 1.0, 0, 1, 1.0, ?, ?)
            """,
            (now - 8.0, now - 8.0, now - 8.0),
        )
        conn.execute(
            """
            INSERT INTO ctrader_deals
            (deal_id, position_id, order_id, symbol_id, volume, filled_volume,
             exec_price, trade_side, deal_status, exec_timestamp, commission,
             entry_price, gross_profit, swap, close_commission, balance,
             closed_volume, is_close, fetched_at)
            VALUES (5001, 1001, 1, 41, 1, 1, 1.31, 'BUY', 2, ?,
                    0.0, 1.31, 0.0, 0.0, 0.0, 10000.0, 0, 0, ?)
            """,
            (now - 27.5, now),
        )
        conn.execute(
            """
            INSERT INTO position_supervisor_trace
            (trace_id, decision_id, position_id, trade_id, symbol, timeframe,
             tick, event_ts, action, summary_reason, confidence, template_id,
             template_version, stage, outcome, risk_action, risk_allowed,
             risk_reason, execution_status, execution_reason, context_json,
             verdict_json, risk_verdict_json, execution_json, trace_integrity,
             config_version, config_hash, evolution_run_id, created_at)
            VALUES ('sup_trace_1', 'dec_bar_1', '1001', 'trade_1', 'XAUUSD+', 'M5',
                    1, ?, 'hold', 'ok', 0.7, 'default', 'v1',
                    'manage', 'observed', 'close_position', 1, 'ok',
                    'observed', 'no_action', ?, '{}', ?, ?,
                    'full', 1, 'cfg_hash', '', ?)
            """,
            (
                now - 20.0,
                json.dumps(
                    {
                        "position_id": "1001",
                        "close_reason": "supervisor",
                        "loop_running": True,
                        "bridge_connected": True,
                        "temporal_context": {"timeframe_seconds": 300},
                    }
                ),
                json.dumps({"allowed": True, "reason": "risk_reducing_action"}),
                json.dumps({"status": "observed"}),
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO supervisor_counterfactual_review
            (counterfactual_id, review_id, trade_id, position_id, close_ts,
             close_reason, supervisor_event_type, supervisor_reason, label,
             confidence, horizons_json, evidence_json, created_at, updated_at)
            VALUES ('cf_1', 'review_1', 'trade_1', '1001', ?, 'broker_close',
                    'supervisor_close', 'ok', 'correct_stop', 0.76, ?,
                    ?, ?, ?)
            """,
            (
                now - 10.0,
                json.dumps([{"horizon_minutes": 30, "future_pnl_delta": -0.2}]),
                json.dumps({"schema_version": "supervisor_counterfactual.v1", "advisory_only": True}),
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    service = ReplayHarnessService(db_path, artifact_dir=tmp_path / "replay_artifacts")

    def _fake_bar_window(**kwargs):
        decision_ts = kwargs["decision_ts"]
        return [
            {"time": decision_ts - 600.0, "open": 1.0, "high": 1.2, "low": 0.9, "close": 1.1, "volume": 10.0},
            {"time": decision_ts - 300.0, "open": 1.1, "high": 1.3, "low": 1.0, "close": 1.2, "volume": 12.0},
            {"time": decision_ts, "open": 1.2, "high": 1.4, "low": 1.1, "close": 1.3, "volume": 13.0},
        ]

    def _fake_enrich_bar_window(bars, **kwargs):
        frame = pd.DataFrame(bars)
        frame["rsi_14"] = [45.0, 55.0, 60.0]
        return frame

    service._load_bar_window = _fake_bar_window
    service._enrich_bar_window = _fake_enrich_bar_window
    report = service.run_bar_replay_evidence(lookback_days=1, limit=10, warmup_bars=2, post_bars=1)

    bar_metrics = report["metric_summary"]["bar_replay"]
    frame_metrics = report["metric_summary"]["factor_frame_replay"]
    gate_metrics = report["metric_summary"]["execution_gate_recompute"]
    risk_metrics = report["metric_summary"]["risk_policy_recompute"]
    order_metrics = report["metric_summary"]["order_lifecycle_replay"]
    causality_metrics = report["metric_summary"]["order_outcome_causality_replay"]
    slippage_metrics = report["metric_summary"]["broker_fill_slippage_replay"]
    position_metrics = report["metric_summary"]["position_lifecycle_replay"]
    supervisor_metrics = report["metric_summary"]["supervisor_action_replay"]
    counterfactual_metrics = report["metric_summary"]["supervisor_counterfactual_replay"]
    subaction_metrics = report["metric_summary"]["risk_policy_subaction_replay"]
    outcome_learning = report["metric_summary"]["trade_outcome_learning_preview"]
    assert report["scope"]["kind"] == "bar_replay_evidence"
    assert report["decision_count"] == 1
    assert report["matched_live_count"] == 1
    assert report["mismatch_count"] == 0
    assert report["evidence_grade"] == "A"
    assert bar_metrics["schema_version"] == "bar_replay_metrics.v1"
    assert bar_metrics["aligned_decision_count"] == 1
    assert bar_metrics["bar_window_coverage"] == 1.0
    assert bar_metrics["bar_window_hash"]
    assert frame_metrics["schema_version"] == "factor_frame_replay_metrics.v1"
    assert frame_metrics["factor_frame_ok_count"] == 1
    assert frame_metrics["factor_frame_coverage"] == 1.0
    assert frame_metrics["factor_frame_hash"]
    assert gate_metrics["schema_version"] == "execution_gate_recompute_metrics.v1"
    assert gate_metrics["attempted_count"] == 1
    assert gate_metrics["agreement_count"] == 1
    assert gate_metrics["disagreement_count"] == 0
    assert risk_metrics["schema_version"] == "risk_policy_recompute_metrics.v1"
    assert risk_metrics["attempted_count"] == 0
    assert risk_metrics["agreement_count"] == 0
    assert risk_metrics["disagreement_count"] == 0
    assert risk_metrics["input_gap_count"] == 1
    assert "missing_recorded_risk_metrics_snapshot" in (
        risk_metrics["input_gap_examples"][0]["issues"]
    )
    assert order_metrics["schema_version"] == "order_lifecycle_replay_metrics.v1"
    assert order_metrics["expected_order_decision_count"] == 1
    assert order_metrics["covered_order_decision_count"] == 1
    assert order_metrics["filled_event_count"] == 1
    assert causality_metrics["schema_version"] == "order_outcome_causality_metrics.v1"
    assert causality_metrics["expected_open_count"] == 1
    assert causality_metrics["complete_chain_count"] == 1
    assert causality_metrics["causality_issue_count"] == 0
    assert causality_metrics["broker_deal_link_count"] == 1
    assert slippage_metrics["schema_version"] == "broker_fill_slippage_metrics.v1"
    assert slippage_metrics["filled_event_count"] == 1
    assert slippage_metrics["measured_fill_count"] == 1
    assert slippage_metrics["avg_abs_slippage_points"] == 0.01
    assert slippage_metrics["broker_deal_price_match_count"] == 1
    assert position_metrics["schema_version"] == "position_lifecycle_replay_metrics.v1"
    assert position_metrics["expected_position_decision_count"] == 1
    assert position_metrics["covered_position_decision_count"] == 1
    assert position_metrics["opened_event_count"] == 1
    assert supervisor_metrics["schema_version"] == "supervisor_action_replay_metrics.v1"
    assert supervisor_metrics["trace_count"] == 1
    assert supervisor_metrics["risk_verdict_count"] == 1
    assert supervisor_metrics["execution_status_count"] == 1
    assert supervisor_metrics["trace_integrity_issue_count"] == 0
    assert counterfactual_metrics["schema_version"] == "supervisor_counterfactual_replay_metrics.v1"
    assert counterfactual_metrics["positions_with_counterfactual"] == 1
    assert counterfactual_metrics["counterfactual_count"] == 1
    assert counterfactual_metrics["labels"] == {"correct_stop": 1}
    assert subaction_metrics["schema_version"] == "risk_policy_subaction_replay_metrics.v1"
    assert subaction_metrics["candidate_count"] == 1
    assert subaction_metrics["attempted_count"] == 1
    assert subaction_metrics["agreement_count"] == 1
    assert subaction_metrics["actions"] == {"close_position": 1}
    assert outcome_learning["closed_count"] == 1
    assert outcome_learning["trainable_count"] == 1
    assert outcome_learning["items"][0]["outcome"]["result"] == "loss"
    assert outcome_learning["items"][0]["outcome"]["pnl"] == -1.25
    assert outcome_learning["items"][0]["outcome"]["primary_factor"] == "rsi_14"
    assert outcome_learning["items"][0]["direction"] == 1
    assert outcome_learning["items"][0]["direction_label"] == "direction_long"
    assert outcome_learning["items"][0]["learning"]["status"] == "learning_sample_ready"

    choices = service.list_bar_preview_decisions(lookback_days=1, limit=10)
    assert choices["items"][0]["decision_id"] == "dec_bar_1"
    assert choices["items"][0]["direction"] == 1
    assert choices["items"][0]["direction_label"] == "direction_long"
    assert choices["items"][0]["outcome_result"] == "loss"
    assert choices["items"][0]["learning_status"] == "learning_sample_ready"
    assert choices["items"][0]["entry_ts"] == round(now - 30.0, 3)
    assert choices["items"][0]["exit_ts"] == round(now - 9.0, 3)
    assert choices["items"][0]["exit_decision_id"] == "dec_exit_1"
    assert choices["items"][0]["holding_seconds"] == 21.0
    assert choices["items"][0]["close_reason"] == "thesis_broken"
    assert choices["items"][0]["system_view"]["direction"] == 1
    assert choices["items"][0]["system_view"]["direction_label"] == "direction_long"
    assert choices["items"][0]["system_view"]["score"] == 0.8
    assert choices["items"][0]["system_view"]["pnl"] == -1.25
    assert choices["items"][0]["system_view"]["close_reason"] == "thesis_broken"

    selected_preview = service.run_bar_window_preview(
        lookback_days=1,
        limit=1,
        warmup_bars=2,
        post_bars=1,
        decision_id="dec_bar_1",
    )
    selected_outcome = selected_preview["metric_summary"]["trade_outcome_learning_preview"]
    assert selected_preview["decision_count"] == 1
    assert selected_preview["scope"]["decision_id"] == "dec_bar_1"
    assert selected_outcome["items"][0]["direction_label"] == "direction_long"
    assert selected_outcome["items"][0]["outcome"]["pnl"] == -1.25
    assert selected_outcome["items"][0]["learning"]["status"] == "learning_sample_ready"

    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            """
            UPDATE training_sample_row
            SET system_contaminated=1, governance_eligible=0,
                governance_effective_weight=0.0
            WHERE sample_id='als_trade_1'
            """
        )
        conn.commit()
    finally:
        conn.close()
    contaminated_preview = service.run_bar_window_preview(
        lookback_days=1,
        limit=1,
        warmup_bars=2,
        post_bars=1,
        decision_id="dec_bar_1",
    )
    contaminated_learning = contaminated_preview["metric_summary"][
        "trade_outcome_learning_preview"
    ]["items"][0]["learning"]
    assert contaminated_learning["status"] == "learning_sample_observe"
    assert contaminated_learning["matured_sample_count"] == 0
    assert Path(report["artifact_path"]).exists()


def test_autonomy_health_v1_is_machine_readable_and_read_only(tmp_path):
    db_path = tmp_path / "state.db"
    now = time.time()
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        ensure_training_sample_row_sqlite(db_path)
        # PG-only payload pool consulted by AutonomyHealthService._action_stats
        # (evolution_decision LEFT JOIN mutation_payload); not part of the
        # SQLite STATE_DB_DDL fixture.
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS mutation_payload (
                payload_hash TEXT PRIMARY KEY,
                evidence_json TEXT,
                risk_verdict_json TEXT,
                before_json TEXT,
                after_json TEXT,
                result_json TEXT,
                rollback_json TEXT,
                byte_length INTEGER,
                created_at REAL
            );
            """
        )
        conn.execute(
            """
            INSERT INTO training_sample_row
            (sample_id, sample_type, source_table, source_id, event_ts, label_status,
             integrity, train_weight, created_at, updated_at)
            VALUES ('s1', 'shadow_open_decision', 'decision_ledger', 'dec_1',
                    ?, 'matured', 'full', 0.9, ?, ?)
            """,
            (now, now, now),
        )
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence, reason,
             evidence_json, status, reviewed_at, review_note, created_at)
            VALUES ('ps1', 'factor', 'rsi_14', 'demo_auto_approve', 0.8,
                    'test', '{}', 'approved', ?, 'auto-approved by demo_autonomous', ?)
            """,
            (now, now),
        )
        conn.commit()
    finally:
        conn.close()

    service = AutonomyHealthService(db_path)
    health = service.build(
        live_status={"loop": {"running": True}, "readiness": {"ok": True}},
        system_health={"score": 1.0, "blocking_components": []},
        governance={
            "factor_governance_runtime": {
                "enabled": True,
                "stale_after_seconds": 7200.0,
                "latest_catalog_snapshot": {"age_seconds": 10.0},
            }
        },
        stability={
            "runtime_config_snapshot": {"ok": True},
            "runtime_config_overlay": {"ok": True, "suspicious": False},
        },
        replay_status={
            "status": "fresh",
            "age_seconds": 10.0,
            "stale_after_seconds": 86400.0,
            "latest_report": {"evidence_grade": "A"},
        },
        governance_freshness={
            "tables": {
                "position_quality_shadow_audit": {"status": "fresh"},
                "factor_governance_shadow_audit": {"status": "fresh"},
            }
        },
        model_status={},
        persist_min_interval_sec=0.0,
    )
    health_2 = service.build(
        live_status={"loop": {"running": True}, "readiness": {"ok": True}},
        system_health={"score": 0.8, "blocking_components": []},
        governance={
            "factor_governance_runtime": {
                "enabled": True,
                "stale_after_seconds": 7200.0,
                "latest_catalog_snapshot": {"age_seconds": 60.0},
            }
        },
        stability={
            "runtime_config_snapshot": {"ok": True},
            "runtime_config_overlay": {"ok": True, "suspicious": False},
        },
        replay_status={
            "status": "fresh",
            "age_seconds": 60.0,
            "stale_after_seconds": 86400.0,
            "latest_report": {"evidence_grade": "A"},
        },
        governance_freshness={
            "tables": {
                "position_quality_shadow_audit": {"status": "fresh"},
                "factor_governance_shadow_audit": {"status": "fresh"},
            }
        },
        model_status={},
        persist_min_interval_sec=0.0,
    )

    assert health["schema_version"] == "autonomy_health.v1"
    assert health["read_only"] is True
    assert health["posture"] in {"full", "constrained"}
    assert health["score"] > 0.75
    assert health["replay_freshness"] > 0.9
    assert health["config_restore_success"] == 1.0
    assert health["persistence"]["schema_version"] == "autonomy_health_persistence.v1"
    assert health["persistence"]["persisted"] is True
    assert health["scope_recommendation"]["applied"] is False
    assert health["scope_recommendation"]["requires_risk_policy_for_actions"] is True
    assert health_2["trend"]["schema_version"] == "autonomy_health_trend.v1"
    assert health_2["trend"]["sample_count"] >= 2
    assert health_2["trend"]["latest_snapshot_id"]

    approval = service.record_scope_approval(
        health=health_2,
        actor="test",
        decision="approved_for_tightening",
        reason="scope recommendation reviewed",
        event_id="scope_approval_test",
    )
    latest_approval = service.latest_scope_approval()

    assert approval["schema_version"] == "autonomy_scope_approval_event.v1"
    assert approval["event_id"] == "scope_approval_test"
    assert approval["snapshot_id"]
    assert approval["recommendation"]["schema_version"] == "autonomy_scope_recommendation.v1"
    assert approval["boundary"]["audit_only"] is True
    assert approval["boundary"]["can_tighten_only"] is True
    assert approval["boundary"]["applied"] is False
    assert approval["boundary"]["risk_policy_service_required_for_actions"] is True
    assert approval["boundary"]["decision_policy_required_for_weight_writes"] is True
    assert approval["boundary"]["runtime_overlay_snapshot_required_for_config_changes"] is True
    assert latest_approval["event_id"] == "scope_approval_test"


def test_autonomy_health_filters_before_limit_and_uses_governance_weight(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        ensure_training_sample_row_sqlite(db_path)
        conn.execute(
            """
            INSERT INTO training_sample_row
            (sample_id, sample_type, source_table, source_id, event_ts,
             label_status, integrity, train_weight, system_contaminated,
             governance_eligible, governance_effective_weight,
             created_at, updated_at)
            VALUES ('clean_sample', 'trade_review_outcome',
                    'trade_outcome_review', 'clean_review', 1.0,
                    'matured', 'full', 0.0, 0, 1, 0.8, 1.0, 1.0)
            """
        )
        conn.executemany(
            """
            INSERT INTO training_sample_row
            (sample_id, sample_type, source_table, source_id, event_ts,
             label_status, integrity, train_weight, system_contaminated,
             governance_eligible, governance_effective_weight,
             created_at, updated_at)
            VALUES (?, 'trade_review_outcome', 'trade_outcome_review', ?,
                    ?, 'matured', 'full', 1.0, 1, 1, 1.0, ?, ?)
            """,
            [
                (
                    f"contaminated_{index}",
                    f"contaminated_review_{index}",
                    1000.0 + index,
                    1000.0 + index,
                    1000.0 + index,
                )
                for index in range(500)
            ],
        )
        conn.commit()
    finally:
        conn.close()

    stats = AutonomyHealthService(db_path)._evidence_integrity_stats()

    assert stats["sample_count"] == 1
    assert stats["ready_sample_count"] == 1
    assert round(stats["evidence_integrity"], 6) == 0.93


def test_autonomy_health_enforcement_tightens_scope_through_incident_control(tmp_path):
    rc.reset_for_tests()
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()

    service = AutonomyHealthService(db_path)
    health = {
        "schema_version": "autonomy_health.v1",
        "posture": "shadow_only",
        "scope_recommendation": {
            "schema_version": "autonomy_scope_recommendation.v1",
            "mode": "shadow_only",
            "posture": "shadow_only",
            "can_tighten_only": True,
            "requires_risk_policy_for_actions": True,
            "applied": False,
        },
    }

    event = service.enforce_scope_recommendation(
        health=health,
        actor="test",
        reason="health degraded",
        event_id="scope_enforcement_test",
    )
    latest = service.latest_scope_enforcement()
    overlay = RuntimeIncidentControlService(db_path).status()
    conn = connect_sqlite(db_path)
    try:
        row = conn.execute(
            """
            SELECT overlay_json
            FROM runtime_config_overlay
            WHERE overlay_id = 'autonomous_factor_governance'
            """
        ).fetchone()
    finally:
        conn.close()

    assert event["schema_version"] == "autonomy_scope_enforcement_event.v1"
    assert event["event_id"] == "scope_enforcement_test"
    assert event["status"] == "applied"
    assert event["applied"] is True
    assert event["current_mode"] == "normal"
    assert event["target_mode"] == "shadow_only"
    assert event["risk_verdict"]["allowed"] is True
    assert event["mutation"]["updated_keys"] == ["runtime_incident_mode"]
    assert event["boundary"]["uses_incident_control_service"] is True
    assert event["boundary"]["risk_policy_service_required"] is True
    assert event["boundary"]["runtime_overlay_snapshot_required_for_applied_changes"] is True
    assert latest["event_id"] == "scope_enforcement_test"
    assert overlay["mode"] == "shadow_only"
    assert json.loads(row[0])["runtime_incident_mode"] == "shadow_only"
    rc.reset_for_tests()


def test_autonomy_health_reads_incident_mode_from_its_own_state_store(
    monkeypatch, tmp_path
):
    isolated_db = tmp_path / "isolated-state.db"
    observed_paths = []

    def status(service):
        observed_paths.append(Path(service.db_path))
        return {"mode": "normal"}

    monkeypatch.setattr(RuntimeIncidentControlService, "status", status)

    assert AutonomyHealthService(isolated_db)._current_incident_mode() == "normal"
    assert observed_paths == [isolated_db]


def test_autonomy_health_enforcement_never_relaxes_incident_mode(tmp_path):
    rc.reset_for_tests()
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()

    incident = RuntimeIncidentControlService(db_path)
    frozen = incident.set_mode("frozen", reason="fixture", actor="test")
    assert frozen["ok"] is True

    service = AutonomyHealthService(db_path)
    event = service.enforce_scope_recommendation(
        health={
            "schema_version": "autonomy_health.v1",
            "posture": "constrained",
            "scope_recommendation": {
                "schema_version": "autonomy_scope_recommendation.v1",
                "mode": "constrain_high_impact_actions",
                "can_tighten_only": True,
            },
        },
        actor="test",
        reason="should not relax frozen",
        event_id="scope_enforcement_no_relax",
    )

    assert event["ok"] is True
    assert event["status"] == "already_at_or_stricter"
    assert event["applied"] is False
    assert event["current_mode"] == "frozen"
    assert event["target_mode"] == "no_new_risk"
    assert event["mutation"] == {}
    assert event["boundary"]["does_not_relax_incident_mode"] is True
    assert incident.status()["mode"] == "frozen"
    rc.reset_for_tests()


def test_release_control_records_start_and_finish_evidence(tmp_path):
    db_path = tmp_path / "state.db"
    now = time.time()
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO runtime_config_snapshot
            (config_hash, source, config_json, run_id, created_at)
            VALUES ('cfg_release', 'test', '{}', 'release_fixture', ?)
            """,
            (now,),
        )
        conn.execute(
            """
            INSERT INTO replay_report
            (replay_run_id, scope_json, input_dataset_hash, runtime_config_hash,
             code_version, decision_count, matched_live_count, mismatch_count,
             metric_summary_json, replay_error, evidence_grade, artifact_path,
             artifact_hash, status, created_at)
            VALUES ('replay_release', '{}', 'dataset_hash', 'cfg_release',
                    'test_sha', 1, 1, 0, '{}', '', 'A', '/tmp/replay.json',
                    'artifact_hash', 'completed', ?)
            """,
            (now,),
        )
        conn.commit()
    finally:
        conn.close()

    service = ReleaseControlService(db_path)
    started = service.start_release(
        release_class="daily_autonomous_mutation",
        summary={"scope": "v15_phase0"},
        tests=[{"name": "pytest", "status": "passed"}],
        rollback_ref={"snapshot_hash": "cfg_release"},
        created_by="test",
        readiness={
            "ready_for_frontend": True,
            "blockers": [],
            "autonomy_health": {"posture": "full"},
        },
        run_id="release_test",
    )

    assert started["run_id"] == "release_test"
    assert started["status"] == "started"
    assert started["runtime_config_hash"] == "cfg_release"
    assert started["replay_run_id"] == "replay_release"
    assert started["replay_artifact_hash"] == "artifact_hash"
    assert started["incident_mode"] == "normal"
    assert started["checklist"]["ok"] is True
    assert started["checklist"]["control_plane_boundaries"]["runtime_overlay_is_source_of_truth"] is True

    finished = service.finish_release(
        "release_test",
        status="completed",
        tests=[{"name": "py_compile", "status": "passed"}],
        readiness={
            "ready_for_frontend": True,
            "blockers": [],
            "autonomy_health": {"posture": "full"},
        },
    )

    assert finished["status"] == "completed"
    assert finished["tests"] == [{"name": "py_compile", "status": "passed"}]
    latest = service.latest_release()
    assert latest["run_id"] == "release_test"
    assert latest["status"] == "completed"
    assert latest["runtime_config_hash"] == "cfg_release"


def test_release_approval_trail_is_audit_only(tmp_path):
    db_path = tmp_path / "state.db"
    now = time.time()
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO runtime_config_snapshot
            (config_hash, source, config_json, run_id, created_at)
            VALUES ('cfg_approval', 'test', '{}', 'approval_fixture', ?)
            """,
            (now,),
        )
        conn.commit()
    finally:
        conn.close()

    service = ReleaseControlService(db_path)
    started = service.start_release(
        release_class="operator_override",
        summary={"scope": "approval_trail"},
        rollback_ref={"snapshot_hash": "cfg_approval"},
        created_by="test",
        readiness={
            "ready_for_frontend": True,
            "blockers": [],
            "autonomy_health": {"posture": "constrained"},
        },
        run_id="release_approval_test",
    )
    event = service.record_approval_event(
        "release_approval_test",
        action="operator_approval",
        actor="alice",
        decision="approved",
        reason="replay and rollback evidence reviewed",
        evidence_refs={
            "release_run_id": "release_approval_test",
            "runtime_config_hash": "cfg_approval",
            "checklist_ok": started["checklist"]["ok"],
        },
        event_id="approval_test_event",
        created_at=now + 1,
    )
    trail = service.approval_trail("release_approval_test")
    after = service.get_release("release_approval_test")

    assert event["schema_version"] == "release_approval_event.v1"
    assert event["event_id"] == "approval_test_event"
    assert event["decision"] == "approved"
    assert event["boundary"]["audit_only"] is True
    assert event["boundary"]["risk_policy_service_required_for_risk_mutations"] is True
    assert event["boundary"]["decision_policy_required_for_weight_writes"] is True
    assert event["boundary"]["runtime_overlay_snapshot_required_for_config_changes"] is True
    assert trail["schema_version"] == "release_approval_trail.v1"
    assert trail["event_count"] == 1
    assert trail["events"][0]["event_id"] == "approval_test_event"
    assert after["status"] == "started"
    assert after["runtime_config_hash"] == "cfg_approval"


def test_release_watchdog_cancels_abandoned_started_release(tmp_path):
    db_path = tmp_path / "state.db"
    service = ReleaseControlService(db_path)
    service.start_release(
        release_class="autonomous_evolution",
        summary={"scope": "watchdog_test"},
        run_id="release_stale_test",
    )
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            "UPDATE release_run SET created_at=?, updated_at=? WHERE run_id=?",
            (time.time() - 7200.0, time.time() - 7200.0, "release_stale_test"),
        )
        conn.commit()
    finally:
        conn.close()

    result = service.close_stale_started_release(max_age_seconds=3600.0, actor="test:watchdog")

    assert result["status"] == "cancelled"
    assert service.get_release("release_stale_test")["status"] == "cancelled"
    trail = service.approval_trail("release_stale_test")
    assert trail["events"][-1]["action"] == "stale_release_watchdog"


def test_incident_playbook_persists_risk_prechecked_plan_without_runtime_mutation(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()

    service = RuntimeIncidentControlService(db_path)
    playbook = service.build_playbook(
        scenario="broker_disconnect",
        severity="high",
        release_run_id="release_incident_test",
        created_by="test",
        playbook_id="playbook_test",
    )
    latest = service.latest_playbook()
    conn = connect_sqlite(db_path)
    try:
        overlay_count = conn.execute("SELECT COUNT(*) FROM runtime_config_overlay").fetchone()[0]
    finally:
        conn.close()

    assert playbook["schema_version"] == "incident_playbook_run.v1"
    assert playbook["playbook_id"] == "playbook_test"
    assert playbook["target_mode"] == "only_close"
    assert playbook["risk_precheck"]["allowed"] is True
    assert playbook["boundary"]["audit_and_plan_only"] is True
    assert playbook["boundary"]["does_not_apply_incident_mode"] is True
    assert playbook["boundary"]["incident_mode_change_requires_risk_policy"] is True
    assert playbook["boundary"]["incident_mode_change_requires_runtime_overlay_snapshot"] is True
    set_mode_step = [step for step in playbook["steps"] if step["step"] == "set_incident_control"][0]
    assert set_mode_step["requires_risk_policy"] is True
    assert set_mode_step["applies_runtime_change"] is False
    assert latest["playbook_id"] == "playbook_test"
    assert latest["release_ref"]["release_run_id"] == "release_incident_test"
    assert overlay_count == 0


def test_incident_playbook_event_trail_binds_evidence_without_runtime_mutation(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()

    service = RuntimeIncidentControlService(db_path)
    service.build_playbook(
        scenario="data_gap",
        severity="high",
        release_run_id="release_event_test",
        created_by="test",
        playbook_id="playbook_event_test",
    )
    missing = service.record_playbook_event(
        "missing_playbook",
        event_type="readiness_captured",
        event_id="missing_event",
    )
    first = service.record_playbook_event(
        "playbook_event_test",
        event_type="readiness_captured",
        actor="alice",
        status="captured",
        evidence_refs={"readiness_status": "degraded"},
        notes="captured before applying incident mode",
        event_id="playbook_event_1",
        created_at=1000.0,
    )
    second = service.record_playbook_event(
        "playbook_event_test",
        event_type="replay_evidence_linked",
        actor="alice",
        status="linked",
        evidence_refs={"replay_run_id": "replay_1", "artifact_hash": "hash_1"},
        event_id="playbook_event_2",
        created_at=1001.0,
    )
    trail = service.playbook_events("playbook_event_test")
    conn = connect_sqlite(db_path)
    try:
        overlay_count = conn.execute("SELECT COUNT(*) FROM runtime_config_overlay").fetchone()[0]
    finally:
        conn.close()

    assert missing["ok"] is False
    assert missing["status"] == "missing_playbook"
    assert first["schema_version"] == "incident_playbook_event.v1"
    assert first["event_id"] == "playbook_event_1"
    assert first["boundary"]["audit_only"] is True
    assert first["boundary"]["does_not_apply_incident_mode"] is True
    assert first["boundary"]["does_not_change_runtime_overlay"] is True
    assert first["boundary"]["incident_mode_change_requires_risk_policy"] is True
    assert second["evidence_refs"]["replay_run_id"] == "replay_1"
    assert trail["schema_version"] == "incident_playbook_event_trail.v1"
    assert trail["event_count"] == 2
    assert [event["event_id"] for event in trail["events"]] == ["playbook_event_1", "playbook_event_2"]
    assert trail["events"][0]["evidence_refs"]["readiness_status"] == "degraded"
    assert overlay_count == 0


def test_v15_phase0_completion_gate_separates_code_and_operational_evidence():
    readiness = {
        "schema_version": "backend_readiness.v1",
        "replay": {
            "schema_version": "replay_readiness.v1",
            "ok": False,
            "status": "missing_report",
            "latest_report": {},
        },
        "incident_control": {
            "schema_version": "runtime_incident_control.v1",
            "mode": "normal",
            "valid_modes": ["normal", "shadow_only", "no_new_risk", "only_close", "frozen"],
        },
        "release": {
            "schema_version": "release_readiness.v1",
            "ok": False,
            "latest_release": {"ok": False, "status": "missing_release_run"},
        },
        "autonomy_health": {
            "schema_version": "autonomy_health.v1",
            "score": 0.82,
            "posture": "full",
            "read_only": True,
        },
        "v15": {
            "schema_version": "v15_readiness_contract.v1",
            "snapshot": {"ok": True, "config_hash": "cfg_hash", "status": "available"},
            "control_plane_boundaries": {
                "runtime_overlay_is_source_of_truth": True,
                "runtime_snapshot_required_for_rollback": True,
                "risk_policy_service_required": True,
                "decision_policy_required_for_weight_writes": True,
                "models_shadow_or_advisory_only": True,
            },
        },
    }

    phase0 = V15Phase0CompletionService().build(readiness=readiness)

    assert phase0["schema_version"] == "v15_phase0_completion.v1"
    assert phase0["implementation_complete"] is True
    assert phase0["operationally_ready"] is False
    assert phase0["status"] == "complete"
    assert phase0["operational_status"] == "needs_evidence"
    assert "replay_harness_v1" in phase0["evidence_gaps"]
    assert "release_run_ledger_v1" in phase0["evidence_gaps"]
    assert phase0["read_only"] is True


def test_bar_decision_choices_fall_back_to_closed_recovery_state(tmp_path):
    db_path = tmp_path / "state.db"
    now = time.time()
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO decision_ledger
            (decision_id, trade_id, position_id, event_type, symbol, timeframe,
             decision_ts, action_score, action_reason, portfolio_state_json,
             risk_state_json, action_json, created_at)
            VALUES ('dec_recovery_1', 'trade_recovery_1', '2002', 'open',
                    'XAUUSD+', 'M15', ?, 0.8, 'executed', '{}', '{}', '{}', ?)
            """,
            (now - 900.0, now - 100.0),
        )
        conn.execute(
            """
            INSERT INTO recovery_position_state
            (position_id, symbol, status, entry_decision_id, closed_at,
             close_reason, close_pnl)
            VALUES (2002, 'XAUUSD+', 'closed', 'dec_recovery_1', ?,
                    'broker_close', 3.5)
            """,
            (now - 300.0,),
        )
        conn.commit()
    finally:
        conn.close()

    choices = ReplayHarnessService(db_path).list_bar_preview_decisions(lookback_days=1, limit=10)
    item = choices["items"][0]
    assert item["decision_id"] == "dec_recovery_1"
    assert item["outcome_status"] == "closed"
    assert item["entry_ts"] == round(now - 900.0, 3)
    assert item["exit_ts"] == round(now - 300.0, 3)
    assert item["holding_seconds"] == 600.0
    assert item["close_reason"] == "broker_close"
    assert item["pnl"] == 3.5
