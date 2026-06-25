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
