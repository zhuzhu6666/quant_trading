from __future__ import annotations

import json
import sqlite3

from backend.core.db import STATE_DB_DDL
from scripts import backfill_controlled_close_learning as backfill


def _init_db(path: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()


def _scalar(path: str, sql: str) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute(sql).fetchone()[0])
    finally:
        conn.close()


def test_controlled_close_learning_backfill_dry_run_and_apply(monkeypatch, tmp_path):
    db_path = str(tmp_path / "state.db")
    _init_db(db_path)
    monkeypatch.setattr(
        backfill.learning_backfill,
        "_infer_path_metrics_from_bars",
        lambda **kwargs: {
            "mfe": 0.0,
            "mae": 1.2,
            "giveback_ratio": 0.0,
            "profit_capture_ratio": 0.0,
            "time_in_profit_seconds": 0.0,
            "time_in_profit_ratio": 0.0,
            "holding_efficiency": 0.2,
            "time_decay_score": 0.0,
            "thesis_status": "broken",
            "regime_shift": "none",
            "position_path_state": {},
            "path_source": "test",
        },
    )

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO decision_ledger
            (decision_id, trade_id, position_id, event_type, symbol, timeframe,
             decision_ts, action_score, action_reason, action_json, created_at)
            VALUES ('dec_open_1', '123', '123', 'open', 'XAUUSD+', 'M5',
                    1000.0, -0.8, 'executed', '{"direction":-1}', 1000.0)
            """
        )
        conn.execute(
            """
            INSERT INTO decision_ledger
            (decision_id, trade_id, position_id, event_type, symbol, timeframe,
             decision_ts, action_score, action_reason, action_json, created_at)
            VALUES ('dec_sup_1', '123', '123', 'supervisor_close', 'XAUUSD+', 'M5',
                    1100.0, 0.97, 'thesis_broken',
                    '{"supervisor_verdict":{"action":"close","thesis_status":"broken","summary_reason":"thesis_broken"}}',
                    1100.0)
            """
        )
        conn.execute(
            """
            INSERT INTO position_lifecycle_event
            (event_id, position_id, trade_id, symbol, event_type, event_ts, net_volume, avg_price)
            VALUES ('posevt_open_1', '123', '123', 'XAUUSD+', 'opened', 1000.0, 100.0, 4160.0)
            """
        )
        conn.execute(
            """
            INSERT INTO ctrader_deals
            (deal_id, position_id, order_id, exec_price, trade_side, exec_timestamp,
             commission, entry_price, gross_profit, swap, close_commission, balance, is_close, fetched_at)
            VALUES (1, 123, 10, 41.60, 'sell', 1001.0,
                    0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0, 1001.0)
            """
        )
        conn.execute(
            """
            INSERT INTO ctrader_deals
            (deal_id, position_id, order_id, exec_price, trade_side, exec_timestamp,
             commission, entry_price, gross_profit, swap, close_commission, balance, is_close, fetched_at)
            VALUES (2, 123, 11, 41.65, 'buy', 1105.0,
                    -0.09, 4160.0, -1.0, 0.0, -0.18, 998.82, 1, 1106.0)
            """
        )
        conn.commit()
    finally:
        conn.close()

    dry_run = backfill.run_backfill(position_ids=[123], apply=False, db_path=db_path)
    assert dry_run["planned"][0]["will_insert_close_ledger"] is True
    assert dry_run["planned"][0]["close_reason_source"] == "supervisor_direct_close"
    assert _scalar(db_path, "SELECT COUNT(*) FROM decision_ledger WHERE event_type='close'") == 0

    applied = backfill.run_backfill(position_ids=[123], apply=True, db_path=db_path)
    assert applied["applied"][0]["exit_decision_id"].startswith("dec_backfill_")
    assert applied["applied"][0]["review_id"].startswith("review_")
    assert applied["applied"][0]["experience_id"].startswith("trade_lesson:")
    assert _scalar(db_path, "SELECT COUNT(*) FROM decision_ledger WHERE event_type='close'") == 1
    assert _scalar(db_path, "SELECT COUNT(*) FROM position_lifecycle_event WHERE event_type='closed'") == 1
    assert _scalar(db_path, "SELECT COUNT(*) FROM trade_outcome_review") == 1
    assert _scalar(db_path, "SELECT COUNT(*) FROM experience_memory WHERE append_source='trade_lesson_memory.v1'") == 1

    conn = sqlite3.connect(db_path)
    try:
        review_json = json.loads(conn.execute("SELECT review_json FROM trade_outcome_review").fetchone()[0])
        event_details = json.loads(
            conn.execute("SELECT details_json FROM position_lifecycle_event WHERE event_type='closed'").fetchone()[0]
        )
    finally:
        conn.close()
    assert review_json["close_price"] == 4165.0
    assert review_json["close_reason"] == "thesis_broken"
    assert review_json["close_reason_source"] == "supervisor_direct_close"
    assert review_json["attribution_integrity"] == "missing"
    assert event_details["real_pnl"]["net"] == -1.18

    second = backfill.run_backfill(position_ids=[123], apply=True, db_path=db_path)
    assert second["planned"][0]["will_insert_close_ledger"] is False
    assert second["planned"][0]["will_insert_review"] is False
    assert _scalar(db_path, "SELECT COUNT(*) FROM decision_ledger WHERE event_type='close'") == 1
    assert _scalar(db_path, "SELECT COUNT(*) FROM trade_outcome_review") == 1
