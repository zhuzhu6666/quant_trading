import sqlite3

import pytest

from backend.core.db import STATE_DB_DDL
from execution.deal_sync import fetch_deals_since_result, sync_close_deals_batch


def _conn(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def test_explicit_deal_fetch_distinguishes_valid_empty_from_failure():
    class _Bridge:
        is_connected = True

        def __init__(self, *, fail=False):
            self.fail = fail

        def get_deals(self, **_kwargs):
            if self.fail:
                raise TimeoutError("deal history timeout")
            return []

    empty = fetch_deals_since_result(_Bridge())
    failed = fetch_deals_since_result(_Bridge(fail=True))

    assert empty.success is True
    assert empty.empty is True
    assert empty.error_code == ""
    assert failed.success is False
    assert failed.empty is False
    assert failed.error_code == "broker_deal_fetch_failed"


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


def test_stale_partial_close_cannot_satisfy_final_close_after_last_open_snapshot(tmp_path):
    db_path = tmp_path / "state.db"
    conn = _conn(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO ctrader_deals
            (deal_id, position_id, exec_timestamp, gross_profit, swap,
             close_commission, closed_volume, is_close)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (9201, 7201, 1000.0, -2.0, 0.0, -0.1, 50, 1),
        )
        conn.commit()

        class _Bridge:
            is_connected = False

        pending = sync_close_deals_batch(
            _Bridge(),
            conn,
            {7201},
            min_exec_timestamp_by_position={7201: 1100.0},
        )
        assert pending == {}

        conn.execute(
            """
            INSERT INTO ctrader_deals
            (deal_id, position_id, exec_timestamp, gross_profit, swap,
             close_commission, closed_volume, is_close)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (9202, 7201, 1101.0, 5.0, 0.0, -0.2, 50, 1),
        )
        conn.commit()
        resolved = sync_close_deals_batch(
            _Bridge(),
            conn,
            {7201},
            min_exec_timestamp_by_position={7201: 1100.0},
        )
    finally:
        conn.close()

    assert resolved[7201]["deal_ids"] == [9201, 9202]
    assert resolved[7201]["net"] == pytest.approx(2.7)


def test_close_volume_delta_requires_a_new_final_deal_even_inside_time_tolerance(tmp_path):
    db_path = tmp_path / "state.db"
    conn = _conn(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO ctrader_deals
            (deal_id, position_id, exec_timestamp, gross_profit, swap,
             close_commission, closed_volume, is_close)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (9301, 7301, 1000.0, -2.0, 0.0, -0.1, 50, 1),
        )
        conn.commit()

        class _Bridge:
            is_connected = True

            def __init__(self):
                self.deals = []

            def get_deals(self, **_kwargs):
                return list(self.deals)

        bridge = _Bridge()
        pending = sync_close_deals_batch(
            bridge,
            conn,
            {7301},
            min_exec_timestamp_by_position={7301: 997.0},
            required_closed_volume_delta_by_position={7301: 50.0},
        )
        assert pending == {}

        bridge.deals = [
            {
                "deal_id": 9302,
                "position_id": 7301,
                "execution_timestamp": 1003.0,
                "execution_price": 2401.0,
                "close_detail": {
                    "gross_profit": 5.0,
                    "swap": 0.0,
                    "commission": -0.2,
                    "closed_volume": 50.0,
                    "balance": 1002.7,
                },
            }
        ]
        resolved = sync_close_deals_batch(
            bridge,
            conn,
            {7301},
            min_exec_timestamp_by_position={7301: 997.0},
            required_closed_volume_delta_by_position={7301: 50.0},
        )
    finally:
        conn.close()

    assert resolved[7301]["deal_ids"] == [9301, 9302]
    assert resolved[7301]["closed_volume"] == pytest.approx(100.0)


def test_delayed_deal_already_in_store_resolves_against_original_cursor(tmp_path):
    db_path = tmp_path / "state.db"
    conn = _conn(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.executemany(
            """
            INSERT INTO ctrader_deals
            (deal_id, position_id, exec_timestamp, gross_profit, swap,
             close_commission, closed_volume, is_close)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (9401, 7401, 1000.0, -2.0, 0.0, -0.1, 50, 1),
                (9402, 7401, 1002.0, 5.0, 0.0, -0.2, 50, 1),
            ],
        )
        conn.commit()

        class _Bridge:
            is_connected = True

            def get_deals(self, **_kwargs):
                # Another writer persisted the delayed deal before recovery.
                return []

        resolved = sync_close_deals_batch(
            _Bridge(),
            conn,
            {7401},
            required_closed_volume_delta_by_position={7401: 50.0},
            baseline_close_cursor_by_position={
                7401: {
                    "baseline_cursor_available": True,
                    "baseline_deal_ids": [9401],
                    "baseline_closed_volume": 50.0,
                }
            },
        )
    finally:
        conn.close()

    assert resolved[7401]["deal_ids"] == [9401, 9402]
    assert resolved[7401]["closed_volume"] == pytest.approx(100.0)
