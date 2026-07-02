import sqlite3

import pytest

from backend.core.db import STATE_DB_DDL
from backend.services.realized_pnl import get_realized_pnl_series


def _conn_factory(db_path):
    def _conn():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn

    return _conn


def _init_db(db_path):
    conn = _conn_factory(db_path)()
    try:
        conn.executescript(STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()


def test_realized_pnl_series_uses_ctrader_close_deals(tmp_path):
    db_path = tmp_path / "state.db"
    _init_db(db_path)
    conn = _conn_factory(db_path)()
    try:
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
                101,
                2001,
                3001,
                41,
                100,
                100,
                4061.0,
                "sell",
                2,
                1000.0,
                -0.09,
                4060.0,
                1.25,
                0.0,
                -0.18,
                11001.25,
                100,
                1,
                1001.0,
            ),
        )
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
                102,
                2002,
                3002,
                41,
                100,
                100,
                4058.0,
                "sell",
                2,
                1010.0,
                -0.09,
                4060.0,
                -2.0,
                0.0,
                -0.18,
                10999.43,
                100,
                1,
                1011.0,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    result = get_realized_pnl_series(
        from_ts=900.0,
        to_ts=1100.0,
        conn_factory=_conn_factory(db_path),
    )

    assert result["ok"] is True
    assert result["summary"]["trades"] == 2
    assert result["summary"]["wins"] == 1
    assert result["summary"]["losses"] == 1
    assert result["points"][0]["pnl"] == pytest.approx(1.07)
    assert result["points"][0]["cumulative"] == pytest.approx(1.07)
    assert result["points"][1]["pnl"] == pytest.approx(-2.18)
    assert result["points"][1]["cumulative"] == pytest.approx(-1.11)


def test_realized_pnl_series_falls_back_without_double_counting(tmp_path):
    db_path = tmp_path / "state.db"
    _init_db(db_path)
    conn = _conn_factory(db_path)()
    try:
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
                201,
                3001,
                4001,
                41,
                100,
                100,
                4060.0,
                "sell",
                2,
                1000.0,
                -0.09,
                4061.0,
                -1.0,
                0.0,
                -0.18,
                10999.0,
                100,
                1,
                1001.0,
            ),
        )
        conn.execute(
            """
            INSERT INTO recovery_position_state
            (position_id, broker, symbol, direction, volume, closed_at, status, close_reason, close_pnl)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (3001, "ctrader", "XAUUSD", 1, 100.0, 1002.0, "closed_replayed", "broker_close", -0.82),
        )
        conn.execute(
            """
            INSERT INTO recovery_position_state
            (position_id, broker, symbol, direction, volume, closed_at, status, close_reason, close_pnl)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (3002, "ctrader", "XAUUSD", 1, 100.0, 1010.0, "closed_replayed", "restart_replay", 0.55),
        )
        conn.commit()
    finally:
        conn.close()

    result = get_realized_pnl_series(
        from_ts=900.0,
        to_ts=1100.0,
        conn_factory=_conn_factory(db_path),
    )

    assert [point["position_id"] for point in result["points"]] == [3001, 3002]
    assert [point["source"] for point in result["points"]] == ["ctrader_deals", "recovery_position_state"]
    assert result["summary"]["realized_pnl"] == pytest.approx(-0.63)


def test_realized_pnl_series_today_scope_uses_requested_timezone(tmp_path):
    db_path = tmp_path / "state.db"
    _init_db(db_path)

    result = get_realized_pnl_series(
        scope="today",
        to_ts=1782716400.0,
        tz="Asia/Shanghai",
        conn_factory=_conn_factory(db_path),
    )

    assert result["from_ts"] < result["to_ts"]
    assert result["summary"]["trades"] == 0
