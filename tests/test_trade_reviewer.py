from __future__ import annotations

import json
import sqlite3

from alpha.reflection.reviewer import TradeReviewer


def _row(db_path: str, sql: str, params: tuple = ()) -> sqlite3.Row:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(sql, params).fetchone()
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
        real_pnl={"deal_id": 323453066, "net": -0.84, "exec_timestamp": close_ts},
        close_reason="broker_close",
        close_reason_source="supervisor_tighten_stopout",
    )

    assert result["accepted"] is True
    row = _row(
        db_path,
        "SELECT created_at, review_json FROM trade_outcome_review WHERE review_id=?",
        (result["review_id"],),
    )
    payload = json.loads(row["review_json"])
    assert float(row["created_at"]) == close_ts
    assert payload["close_ts"] == close_ts
    assert payload["real_pnl"]["deal_id"] == 323453066


def test_trade_reviewer_deduplicates_same_broker_deal(tmp_path):
    db_path = str(tmp_path / "state.db")
    reviewer = TradeReviewer(db_path)
    close_ts = 1_782_750_916.0
    real_pnl = {"deal_id": 323453242, "net": -0.27, "exec_timestamp": close_ts}

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

    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM trade_outcome_review").fetchone()[0]
    finally:
        conn.close()
    assert count == 1
