from __future__ import annotations

import json
import sqlite3

import pandas as pd
import pytest

from backend.core.db import STATE_DB_DDL
from backend.services import learning_backfill


def _init_db(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()


def test_learning_backfill_recovers_holding_seconds_from_ctrader_deals(monkeypatch, tmp_path):
    db_path = str(tmp_path / "state.db")
    _init_db(db_path)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO ctrader_deals
            (deal_id, position_id, order_id, exec_price, trade_side, exec_timestamp,
             commission, gross_profit, swap, close_commission, balance, is_close, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (1, 268046003, 101, 3330.0, "sell", 1_000_000.0, 0.0, 0.0, 0.0, 0.0, 10_000.0, 0, 1_000_100.0),
        )
        conn.execute(
            """
            INSERT INTO ctrader_deals
            (deal_id, position_id, order_id, exec_price, trade_side, exec_timestamp,
             commission, gross_profit, swap, close_commission, balance, is_close, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (2, 268046003, 102, 3310.0, "buy", 1_029_255.0, -0.09, 36.52, 0.0, -0.18, 10_036.52, 1, 1_029_256.0),
        )
        conn.commit()
    finally:
        conn.close()

    def _get_conn():
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        return db

    monkeypatch.setattr(learning_backfill, "get_state_conn", _get_conn)

    result = learning_backfill.run_learning_backfill(
        limit=10,
        allow_partial=True,
        rebuild_learning=False,
    )

    assert result["inserted_count"] == 1

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT review_json FROM trade_outcome_review").fetchone()
    finally:
        conn.close()

    assert row is not None
    review = json.loads(row["review_json"] or "{}")
    assert review["entry_ts_source"] == "ctrader_deals"
    assert review["holding_seconds"] == pytest.approx(29_255.0)
    assert review["context_integrity"] == "partial"
    assert review["close_reason"] == "broker_close"


def test_learning_backfill_enriches_path_metrics_from_bars(monkeypatch, tmp_path):
    db_path = str(tmp_path / "state.db")
    _init_db(db_path)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO ctrader_deals
            (deal_id, position_id, order_id, exec_price, trade_side, exec_timestamp,
             commission, gross_profit, swap, close_commission, balance, is_close, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (11, 3001, 201, 100.0, "buy", 1_000_000.0, 0.0, 0.0, 0.0, 0.0, 10_000.0, 0, 1_000_100.0),
        )
        conn.execute(
            """
            INSERT INTO ctrader_deals
            (deal_id, position_id, order_id, exec_price, trade_side, exec_timestamp,
             commission, gross_profit, swap, close_commission, balance, is_close, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (12, 3001, 202, 104.0, "sell", 1_001_200.0, -0.09, 4.0, 0.0, -0.18, 10_004.0, 1, 1_001_201.0),
        )
        conn.commit()
    finally:
        conn.close()

    bars = pd.DataFrame(
        [
            {"time": pd.Timestamp(1_000_200, unit="s"), "open": 100.0, "high": 102.0, "low": 99.5, "close": 101.5},
            {"time": pd.Timestamp(1_000_500, unit="s"), "open": 101.5, "high": 106.0, "low": 101.0, "close": 105.0},
            {"time": pd.Timestamp(1_000_800, unit="s"), "open": 105.0, "high": 105.2, "low": 103.5, "close": 104.5},
            {"time": pd.Timestamp(1_001_100, unit="s"), "open": 104.5, "high": 104.8, "low": 103.8, "close": 104.0},
        ]
    ).set_index("time")
    bars["time"] = bars.index

    class _FakeStore:
        def load_bars(self, symbol, timeframe, start=None, end=None, limit=None):
            return bars.copy()

    def _get_conn():
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        return db

    monkeypatch.setattr(learning_backfill, "get_state_conn", _get_conn)
    import data.store as data_store

    monkeypatch.setattr(data_store, "DataStore", lambda *args, **kwargs: _FakeStore())

    result = learning_backfill.run_learning_backfill(
        limit=10,
        allow_partial=True,
        rebuild_learning=False,
    )

    assert result["inserted_count"] == 1

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT mae, mfe, review_json FROM trade_outcome_review").fetchone()
    finally:
        conn.close()

    assert row is not None
    review = json.loads(row["review_json"] or "{}")
    assert float(row["mfe"]) > 4.0
    assert float(row["mae"]) >= 0.0
    assert review["path_source"] == "duckdb_bars"
    assert review["close_reason"] == "broker_close"
    assert review["profit_capture_ratio"] < 1.0
    assert review["giveback_ratio"] > 0.0
    assert review["time_in_profit_seconds"] > 0.0
    assert review["contract_version"] == "phase_d.v1"
    assert review["regime_fit"] == pytest.approx(review["regime_fit_score"])
    assert review["thesis_status_at_exit"] == review["thesis_status"]
    assert "primary_responsibility" in review
    assert "responsibility_labels" in review
    assert isinstance(review["responsibility_labels"], list)
    assert review["failure_taxonomy"]["context_integrity"] == "partial"
    assert review["thesis_status"] in {"intact", "weakening", "broken"}
    assert review["phase_c_diagnosis"]["primary_issue"] == "exit_capture"
    assert "profit_giveback" in review["phase_c_diagnosis"]["drivers"]


def test_rebuild_learning_state_preserves_non_factor_policy_suggestions(tmp_path):
    db_path = str(tmp_path / "state.db")
    _init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence, reason,
             evidence_json, status, created_at)
            VALUES ('psv_keep', 'position_supervisor_template',
                    'position_supervisor:conservative.v1', 'switch_template',
                    0.8, 'approved template switch', '{"source":"test"}',
                    'approved', 100.0)
            """
        )
        conn.execute(
            """
            INSERT INTO experience_pattern_stats
            (scope_type, scope_key, sample_count, updated_at)
            VALUES ('position_supervisor_template', 'position_supervisor:default.v1', 3, 100.0)
            """
        )
        conn.execute(
            """
            INSERT INTO experience_memory
            (experience_id, trade_id, source_table, source_id, append_source,
             regime_id, setup_hash, decision_context_json, outcome_label,
             reward_score, failure_tags_json, recommended_action,
             evidence_strength, artifact_version, created_at)
            VALUES ('exp_live_keep', 'live_trade', 'trade_outcome_review',
                    'review_live_keep', 'live_review', '', 'live_hash',
                    '{}', 'good_win', 0.4, '[]', 'watch', 0.7, 'v1', 100.0)
            """
        )
        learning_backfill.rebuild_learning_state(conn)
        kept = conn.execute(
            """
            SELECT scope_type, status
            FROM policy_suggestion
            WHERE suggestion_id='psv_keep'
            """
        ).fetchone()
        template_stat = conn.execute(
            """
            SELECT sample_count
            FROM experience_pattern_stats
            WHERE scope_type='position_supervisor_template'
              AND scope_key='position_supervisor:default.v1'
            """
        ).fetchone()
        live_exp = conn.execute(
            "SELECT append_source FROM experience_memory WHERE experience_id='exp_live_keep'"
        ).fetchone()
        duplicate_backfill = conn.execute(
            "SELECT 1 FROM experience_memory WHERE append_source='learning_backfill.v1'"
        ).fetchone()
    finally:
        conn.close()

    assert kept is not None
    assert kept["scope_type"] == "position_supervisor_template"
    assert kept["status"] == "approved"
    assert template_stat["sample_count"] == 3
    assert live_exp["append_source"] == "live_review"
    assert duplicate_backfill is None


def test_rebuild_learning_state_excludes_contaminated_review_lineage(tmp_path):
    db_path = str(tmp_path / "state.db")
    _init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        contaminated = {
            "worst_factor": "rsi_14",
            "system_issue_context": {
                "contaminates_learning": True,
                "labels": ["price_fact_invalid"],
            },
        }
        conn.execute(
            """
            INSERT INTO trade_outcome_review
            (review_id, trade_id, position_id, pnl, outcome_label,
             failure_tags_json, review_json, created_at)
            VALUES ('review_bad', 'trade_bad', 'position_bad', -1.0, 'bad_loss',
                    '[]', ?, 10.0),
                   ('review_clean', 'trade_clean', 'position_clean', 1.0, 'good_win',
                    '[]', '{"worst_factor":"adx"}', 20.0)
            """,
            (json.dumps(contaminated),),
        )

        learning_backfill.rebuild_learning_state(conn)

        bad = conn.execute(
            "SELECT 1 FROM experience_memory WHERE source_id='review_bad'"
        ).fetchall()
        clean = conn.execute(
            "SELECT 1 FROM experience_memory WHERE source_id='review_clean'"
        ).fetchall()
    finally:
        conn.close()

    assert bad == []
    assert clean


def test_learning_backfill_refreshes_trade_lesson_memory_without_new_reviews(monkeypatch, tmp_path):
    db_path = str(tmp_path / "state.db")
    _init_db(db_path)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO trade_outcome_review
            (review_id, trade_id, position_id, entry_decision_id, exit_decision_id,
             pnl, mae, mfe, outcome_label, failure_tags_json, summary_text,
             review_json, created_at)
            VALUES ('review_refresh', 'trade_refresh', 'pos_refresh', '', '',
                    -8.0, -10.0, 1.0, 'bad_loss', '["weak_entry_signal"]',
                    'refresh existing lesson', '{"primary_responsibility":"signal_quality"}', 100.0)
            """
        )
        conn.commit()
    finally:
        conn.close()

    def _get_conn():
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        return db

    monkeypatch.setattr(learning_backfill, "get_state_conn", _get_conn)

    result = learning_backfill.run_learning_backfill(limit=10, allow_partial=True, rebuild_learning=True)

    assert result["inserted_count"] == 0
    assert result["lesson_rebuild"]["status"] == "refreshed"
    assert result["lesson_rebuild"]["upserted"] == 1

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT decision_context_json FROM experience_memory WHERE source_id='review_refresh'"
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    context = json.loads(row[0] or "{}")
    assert context["agent_attribution"]["feedback_targets"] == ["autonomous_learning"]


def test_learning_backfill_restores_review_regime_from_entry_decision(monkeypatch, tmp_path):
    db_path = str(tmp_path / "state.db")
    _init_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO decision_ledger
            (decision_id, event_type, symbol, timeframe, decision_ts, regime_id, created_at)
            VALUES ('decision_regime', 'open', 'XAUUSD+', 'M5', 100.0, 'trend', 100.0)
            """
        )
        conn.execute(
            """
            INSERT INTO trade_outcome_review
            (review_id, trade_id, position_id, entry_decision_id, pnl, outcome_label,
             failure_tags_json, summary_text, review_json, created_at)
            VALUES ('review_regime', 'trade_regime', 'pos_regime', 'decision_regime', 1.0,
                    'good_win', '[]', 'regime restore', '{}', 200.0)
            """
        )
        conn.commit()
    finally:
        conn.close()

    def _get_conn():
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        return db

    monkeypatch.setattr(learning_backfill, "get_state_conn", _get_conn)
    result = learning_backfill.run_learning_backfill(limit=10, allow_partial=True, rebuild_learning=True)
    assert result["regime_backfill"]["updated"] == 1

    conn = sqlite3.connect(db_path)
    try:
        raw = conn.execute("SELECT review_json FROM trade_outcome_review WHERE review_id='review_regime'").fetchone()[0]
    finally:
        conn.close()
    review = json.loads(raw)
    assert review["regime_id"] == "trend"
    assert review["entry_regime"] == "trend"
    assert review["regime_source"] == "decision_ledger.entry_decision"
