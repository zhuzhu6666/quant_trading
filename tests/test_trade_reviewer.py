from __future__ import annotations

import json
import sqlite3

import pytest

from alpha.reflection.reviewer import TradeReviewer
from backend.core.db import connect_sqlite
from backend.services.canonical_v2 import record_decision_event, record_order_event
from backend.services.canonical_v2_reader import iter_review_rows, review_row


def _review(db_path: str, review_id: str) -> dict:
    conn = connect_sqlite(db_path, read_only=True)
    conn.row_factory = sqlite3.Row
    try:
        row = review_row(conn, review_id)
        assert row is not None
        return row
    finally:
        conn.close()


def test_trade_reviewer_uses_broker_close_ts_for_created_at(tmp_path):
    db_path = str(tmp_path / "state.db")
    reviewer = TradeReviewer(db_path)
    close_ts = 1_782_750_758.0

    result = reviewer.review_closed_trade(
        position_id="269479895",
        pnl=-0.84,
        close_price=3388.12,
        close_ts=close_ts,
        real_pnl={
            "deal_id": 323453066,
            "net": -0.84,
            "exec_price": 3388.12,
            "price_quality": "broker_reported",
            "exec_timestamp": close_ts,
        },
        close_reason="broker_close",
        close_reason_source="supervisor_tighten_stopout",
    )

    assert result["accepted"] is True
    row = _review(db_path, result["review_id"])
    payload = row["review_json"]
    assert row["created_at"] == close_ts
    assert payload["close_ts"] == close_ts
    assert payload["real_pnl"]["deal_id"] == 323453066
    assert payload["signal_score"] is None
    assert payload["attribution_integrity"] == "missing"
    assert "signal_score_missing" in payload["failure_taxonomy"]["evidence_gaps"]


def test_trade_reviewer_rejects_unknown_execution_price(tmp_path):
    db_path = str(tmp_path / "state.db")
    reviewer = TradeReviewer(db_path)

    result = reviewer.review_closed_trade(
        position_id="unknown_price",
        pnl=-2.5,
        close_price=0.0,
        close_ts=100.0,
        real_pnl={
            "deal_id": 55,
            "net": -2.5,
            "price_contract": "legacy_unknown",
            "price_quality": "unknown",
        },
        close_reason="restart_replay",
    )

    assert result["accepted"] is False
    assert result["skip_reason"] == "unknown_execution_price"


def test_trade_reviewer_deduplicates_same_broker_deal(tmp_path):
    db_path = str(tmp_path / "state.db")
    reviewer = TradeReviewer(db_path)
    close_ts = 1_782_750_916.0
    real_pnl = {
        "deal_id": 323453242,
        "net": -0.27,
        "exec_price": 3387.4,
        "price_quality": "broker_reported",
        "exec_timestamp": close_ts,
    }

    first = reviewer.review_closed_trade(
        position_id="269481301",
        pnl=-0.27,
        close_price=3387.4,
        close_ts=close_ts,
        real_pnl=real_pnl,
        close_reason="broker_close",
        close_reason_source="supervisor_tighten_stopout",
    )
    second = reviewer.review_closed_trade(
        position_id="269481301",
        pnl=-0.27,
        close_price=3387.4,
        close_ts=close_ts + 30.0,
        real_pnl=real_pnl,
        close_reason="broker_close",
        close_reason_source="supervisor_tighten_stopout",
    )

    assert first["accepted"] is True
    assert second["accepted"] is True
    assert second["deduplicated"] is True
    assert second["review_id"] == first["review_id"]

    conn = connect_sqlite(db_path, read_only=True)
    conn.row_factory = sqlite3.Row
    try:
        count = len(iter_review_rows(conn, limit=0))
    finally:
        conn.close()
    assert count == 1


def test_trade_reviewer_uses_path_quality_for_good_win(monkeypatch, tmp_path):
    db_path = str(tmp_path / "state.db")
    reviewer = TradeReviewer(db_path)
    monkeypatch.setattr(
        "alpha.reflection.reviewer.update_position_path_metrics",
        lambda **_kwargs: (
            {},
            {
                "mae": 0.0, "mfe": 6.78, "holding_efficiency": 0.95,
                "giveback_ratio": 0.09, "profit_capture_ratio": 0.91,
                "time_in_profit_seconds": 1000.0, "time_in_profit_ratio": 1.0,
                "time_decay_score": 0.8, "thesis_status": "intact",
                "regime_shift": "none",
            },
        ),
    )

    result = reviewer.review_closed_trade(
        position_id="clean_win",
        pnl=6.18,
        close_price=4098.58,
        close_ts=10_000.0,
        contributions={"trend": 1.0, "noise": -2.0},
        real_pnl={
            "deal_id": 99,
            "net": 6.18,
            "exec_price": 4098.58,
            "price_quality": "broker_reported",
            "exec_timestamp": 10_000.0,
        },
        close_reason="broker_close",
    )

    assert result["outcome_label"] == "good_win"
    assert "lucky_win" not in result["failure_tags"]
    assert result["review_json"]["entry_quality"] >= 0.62
    assert result["review_json"]["exit_quality"] == pytest.approx(0.887, abs=0.001)


def test_trade_reviewer_assigns_full_giveback_loss_to_exit(monkeypatch, tmp_path):
    db_path = str(tmp_path / "state.db")
    reviewer = TradeReviewer(db_path)
    monkeypatch.setattr(
        "alpha.reflection.reviewer.update_position_path_metrics",
        lambda **_kwargs: (
            {},
            {
                "mae": 1.0, "mfe": 3.2, "holding_efficiency": 0.2,
                "giveback_ratio": 1.0, "profit_capture_ratio": 0.0,
                "time_in_profit_seconds": 400.0, "time_in_profit_ratio": 0.55,
                "time_decay_score": 0.5, "thesis_status": "broken",
                "regime_shift": "none",
            },
        ),
    )

    result = reviewer.review_closed_trade(
        position_id="giveback_loss",
        pnl=-0.2,
        close_price=4104.0,
        close_ts=11_000.0,
        contributions={"trend": -0.1},
        real_pnl={
            "deal_id": 100,
            "net": -0.2,
            "exec_price": 4104.0,
            "price_quality": "broker_reported",
            "exec_timestamp": 11_000.0,
        },
        close_reason="broker_close",
    )

    assert "alpha_correct_but_capture_failed" in result["failure_tags"]
    assert result["review_json"]["primary_responsibility"] == "exit"
    assert result["review_json"]["factor_attribution"]["causal_level"] == "observational"
    assert result["review_json"]["factor_attribution"]["causal_claim"] is False


def test_trade_reviewer_separates_signal_and_fill_time_for_system_contamination(tmp_path):
    db_path = str(tmp_path / "state.db")
    reviewer = TradeReviewer(db_path)
    conn = sqlite3.connect(db_path)
    try:
        risk_verdict = {
            "allowed": True,
            "reason": "ok",
            "audit_payload": {
                "temporal_context": {
                    "evaluated_at": 1605.0,
                    "timeframe": "M5",
                    "timeframe_seconds": 300,
                },
                "state": {
                    "runtime_health_snapshot": {
                        "data_lag_seconds": 607.0,
                        "raw": {
                            "sync_health": {
                                "fresh": True,
                                "stale": False,
                                "degraded": False,
                                "last_bar_ts_by_tf": {"M5": 1000.0},
                            }
                        },
                    }
                },
            },
        }
        action = {
            "direction": -1,
            "score": -0.91,
            "risk_verdict": risk_verdict,
            "execution_context": {"actual_api_volume": 100.0},
            "sizing_trace": {"final_api_volume": 300.0},
            "event_context": {"event_near": True},
            "decision_quality_context": {"context_state": {"event_window_state": "none"}},
            "data_quality_context": {
                "schema_version": "entry_data_quality_context.v1",
                "quote_fresh": True,
                "quote_age_seconds": 0.2,
            },
            "market_session": {
                "market_data_age_seconds": 607.0,
            },
        }
        record_decision_event(
            conn,
            decision_id="dec_open_delay",
            trade_id="pos_delay",
            position_id="pos_delay",
            event_type="open",
            symbol="XAUUSD+",
            timeframe="M5",
            decision_ts=1000.0,
            regime_id="trend",
            action_score=-0.91,
            action_reason="executed",
            action=action,
            risk_state={"policy_verdict": risk_verdict},
            created_at=1605.0,
        )
        record_order_event(
            conn,
            event_id="ord_sub",
            decision_id="dec_open_delay",
            trade_id="pos_delay",
            order_id="pos_delay",
            broker_order_id="pos_delay",
            event_type="submitted",
            event_ts=1606.0,
            price=4127.2,
            volume=100.0,
            status="submitted",
            details={},
        )
        record_order_event(
            conn,
            event_id="ord_fill",
            decision_id="dec_open_delay",
            trade_id="pos_delay",
            order_id="pos_delay",
            broker_order_id="pos_delay",
            event_type="filled",
            event_ts=1607.0,
            price=4127.3,
            volume=100.0,
            status="filled",
            details={},
        )
        conn.commit()
    finally:
        conn.close()

    result = reviewer.review_closed_trade(
        position_id="pos_delay",
        pnl=-0.27,
        close_price=4127.0,
        close_ts=1907.0,
        contributions={"dsl_factor": -0.1},
        real_pnl={"deal_id": 1, "net": -0.27, "exec_timestamp": 1907.0},
        close_reason="thesis_broken",
        close_reason_source="supervisor_direct_close",
    )

    assert result["accepted"] is True
    review = result["review_json"]
    labels = review["responsibility_labels"]
    assert review["signal_bar_ts"] == 1000.0
    assert review["entry_ts"] == 1607.0
    assert review["holding_seconds"] == 300.0
    assert review["regime_id"] == "trend"
    assert review["entry_regime"] == "trend"
    assert review["entry_timing_context"]["signal_to_fill_delay_seconds"] == 607.0
    assert review["signal_score"] == -0.91
    assert review["action_score"] == -0.91
    assert review["summary_consistency"]["overall"] == "mismatch"
    assert review["summary_consistency"]["checks"]["sizing_trace_matches_execution"]["status"] == "mismatch"
    assert review["summary_consistency"]["checks"]["event_context_vs_factor_context"]["status"] == "different_scopes"
    assert review["primary_responsibility"] == "data_quality"
    assert "market_data_stale" in labels
    assert "signal_execution_delay" in labels
    assert "data_quality_issue" in result["failure_tags"]
    assert "primary_responsibility=data_quality" in result["summary_text"]

    conn = sqlite3.connect(db_path)
    try:
        factor_row = conn.execute(
            "SELECT confidence, notes FROM factor_contribution_review WHERE review_id=?",
            (result["review_id"],),
        ).fetchone()
    finally:
        conn.close()
    notes = json.loads(factor_row[1])
    assert factor_row[0] < 0.1
    assert notes["system_contaminated"] is True
    assert notes["factor_training_allowed"] is False
