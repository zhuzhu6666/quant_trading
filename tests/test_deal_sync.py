import sqlite3

import pytest

from backend.core.db import STATE_DB_DDL
from execution.deal_sync import sync_close_deals_batch


def _conn(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def test_close_deal_with_zero_gross_profit_still_recovers_real_pnl(tmp_path):
    db_path = tmp_path / "state.db"
    conn = _conn(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO ctrader_deals
            (deal_id, position_id, order_id, symbol_id, volume, filled_volume,
             exec_price, trade_side, deal_status, exec_timestamp, commission,
             entry_price, gross_profit, swap, close_commission, balance,
             closed_volume, is_close, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                9001,
                7001,
                8001,
                41,
                100,
                100,
                4050.0,
                "sell",
                2,
                1_782_756_091.236,
                -0.09,
                4050.0,
                0.0,
                0.0,
                -0.18,
                477.98,
                100,
                1,
                1_782_756_110.0,
            ),
        )
        conn.commit()

        class _Bridge:
            is_connected = False

        result = sync_close_deals_batch(_Bridge(), conn, {7001})
    finally:
        conn.close()

    assert result[7001]["source"] == "ctrader_deals"
    assert result[7001]["net"] == pytest.approx(-0.18)
    assert result[7001]["exec_timestamp"] == pytest.approx(1_782_756_091.236)
    assert result[7001]["closed_volume"] == 100


def test_close_deals_aggregate_partial_reduce_and_final_close(tmp_path):
    db_path = tmp_path / "state.db"
    conn = _conn(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        rows = [
            (9101, 7101, 8101, 41, 100, 100, 4060.0, "sell", 2, 1_000.0,
             -0.09, 4057.5, -2.40, 0.0, -0.16, 444.57, 100, 1, 1_001.0),
            (9102, 7101, 8102, 41, 100, 100, 4069.0, "sell", 2, 1_002.0,
             -0.09, 4057.5, -7.10, 0.0, -0.29, 434.62, 100, 1, 1_003.0),
        ]
        conn.executemany(
            """
            INSERT INTO ctrader_deals
            (deal_id, position_id, order_id, symbol_id, volume, filled_volume,
             exec_price, trade_side, deal_status, exec_timestamp, commission,
             entry_price, gross_profit, swap, close_commission, balance,
             closed_volume, is_close, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()

        class _Bridge:
            is_connected = False

        result = sync_close_deals_batch(_Bridge(), conn, {7101})
    finally:
        conn.close()

    assert result[7101]["gross"] == pytest.approx(-9.5)
    assert result[7101]["commission"] == pytest.approx(-0.45)
    assert result[7101]["net"] == pytest.approx(-9.95)
    assert result[7101]["closed_volume"] == pytest.approx(200.0)
    assert result[7101]["close_deals_count"] == 2
    assert result[7101]["deal_ids"] == [9101, 9102]
