from __future__ import annotations

import json
import sqlite3

from backend.core.db import STATE_DB_DDL
from scripts.phase_a_health_check import run_check


def test_phase_a_health_check_flags_temporal_anchor_drift(tmp_path):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO ctrader_deals
            (deal_id, position_id, exec_timestamp, gross_profit, swap, close_commission, closed_volume, is_close)
            VALUES (9001, 7001, 1000.0, -1.0, 0.0, -0.1, 100.0, 1)
            """
        )
        review_json = {
            "position_id": "7001",
            "close_ts": 1000.0,
            "real_pnl": {"deal_id": 9001, "net": -0.9},
            "close_reason_source": "external_broker_close",
            "attribution_integrity": "full",
        }
        conn.execute(
            """
            INSERT INTO trade_outcome_review
            (review_id, trade_id, position_id, outcome_label, failure_tags_json, review_json, created_at)
            VALUES ('review_drift', '7001', '7001', 'good_loss', '[]', ?, 1300.0)
            """,
            (json.dumps(review_json),),
        )
        conn.execute(
            """
            INSERT INTO experience_memory
            (experience_id, trade_id, source_table, source_id, append_source, regime_id, setup_hash,
             decision_context_json, outcome_label, reward_score, failure_tags_json, recommended_action,
             evidence_strength, artifact_version, created_at)
            VALUES ('exp_drift', '7001', 'trade_outcome_review', 'review_drift', 'live_review', '', 'h',
                    '{}', 'good_loss', -0.1, '[]', 'watch', 0.5, 'v1', 1600.0)
            """
        )
        conn.commit()
    finally:
        conn.close()

    result = run_check(db_path=db_path, hours=10_000_000, limit=10)
    codes = {item["code"] for item in result["issues"]}

    assert result["status"] == "blocked"
    assert "review_broker_time_mismatch" in codes
    assert "experience_event_time_mismatch" in codes
    assert result["counts"]["review_broker_time_mismatch"] == 1
    assert result["counts"]["experience_event_time_mismatch"] == 1
