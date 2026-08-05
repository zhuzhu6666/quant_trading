import sqlite3

import pytest

from backend.core.db import STATE_DB_DDL
from execution.deal_sync import (
    fetch_deals_since_result,
    find_close_deal,
    store_deals,
    sync_close_deals_batch,
)


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


def test_explicit_deal_fetch_honors_ctrader_compat_failure_marker():
    class _Bridge:
        is_connected = True
        _last_deals_fetch_ok = False

        def get_deals(self, **_kwargs):
            return []

    failed = fetch_deals_since_result(_Bridge())

    assert failed.success is False
    assert failed.empty is False
    assert failed.error_code == "broker_deal_fetch_failed"


def test_store_deals_records_raw_price_contract_and_unknown_fails_closed(tmp_path):
    conn = _conn(tmp_path / "state.db")
    try:
        conn.executescript(STATE_DB_DDL)
        store_deals(
            conn,
            [
                {
                    "deal_id": 1,
                    "position_id": 10,
                    "execution_price": 4050.25,
                    "price_quality": "broker_reported",
                    "close_detail": {"gross_profit": 1.0, "closed_volume": 100},
                },
                {
                    "deal_id": 2,
                    "position_id": 20,
                    "execution_price": 0.0,
                    "close_detail": {"gross_profit": 1.0, "closed_volume": 100},
                },
            ],
        )
        rows = {
            row["deal_id"]: row
            for row in conn.execute(
                "SELECT * FROM ctrader_deals ORDER BY deal_id"
            ).fetchall()
        }
        assert rows[1]["raw_execution_price"] == pytest.approx(4050.25)
        assert rows[1]["price_contract"] == "ctrader.deal.execution_price.raw.v1"
        assert rows[1]["price_quality"] == "broker_reported"
        assert rows[2]["price_quality"] == "unknown"

        class _Bridge:
            is_connected = False

        assert sync_close_deals_batch(_Bridge(), conn, {10})[10]["exec_price"] == pytest.approx(4050.25)
        unknown_price = sync_close_deals_batch(_Bridge(), conn, {20})[20]
        assert unknown_price["net"] == pytest.approx(1.0)
        assert unknown_price["exec_price"] == 0.0
        assert unknown_price["price_quality"] == "unknown"
    finally:
        conn.close()


def test_store_deals_preserves_explicit_unknown_price_quality(tmp_path):
    conn = _conn(tmp_path / "state.db")
    try:
        conn.executescript(STATE_DB_DDL)
        store_deals(
            conn,
            [
                {
                    "deal_id": 12,
                    "position_id": 103,
                    "execution_price": 4125.0,
                    "price_contract": "legacy_unknown",
                    "price_quality": "unknown",
                    "close_detail": {
                        "gross_profit": 1.0,
                        "balance": 101.0,
                        "closed_volume": 100,
                    },
                }
            ],
        )

        row = conn.execute(
            """
            SELECT exec_price, raw_execution_price, price_contract, price_quality
            FROM ctrader_deals WHERE deal_id=12
            """
        ).fetchone()
        assert row["exec_price"] == 0.0
        assert row["raw_execution_price"] == pytest.approx(4125.0)
        assert row["price_contract"] == "legacy_unknown"
        assert row["price_quality"] == "unknown"
        close = find_close_deal(conn, 103)
        assert close is not None
        assert close["price_quality"] == "unknown"
    finally:
        conn.close()


def test_store_deals_can_promote_unknown_price_from_new_broker_evidence(tmp_path):
    conn = _conn(tmp_path / "state.db")
    try:
        conn.executescript(STATE_DB_DDL)
        base = {
            "deal_id": 13,
            "position_id": 104,
            "execution_price": 41.25,
            "price_contract": "legacy_unknown",
            "price_quality": "unknown",
            "close_detail": {
                "gross_profit": 1.0,
                "balance": 101.0,
                "closed_volume": 100,
            },
        }
        store_deals(conn, [base])
        store_deals(
            conn,
            [
                {
                    **base,
                    "price_contract": "ctrader.deal.execution_price.raw.v1",
                    "price_quality": "broker_reported",
                }
            ],
        )
        still_unknown = conn.execute(
            "SELECT exec_price, price_quality FROM ctrader_deals WHERE deal_id=13"
        ).fetchone()
        assert still_unknown["exec_price"] == 0.0
        assert still_unknown["price_quality"] == "unknown"
        store_deals(
            conn,
            [
                {
                    **base,
                    "execution_price": 4125.0,
                    "price_contract": "ctrader.deal.execution_price.raw.v1",
                    "price_quality": "broker_reconciled",
                }
            ],
        )

        row = conn.execute(
            """
            SELECT exec_price, raw_execution_price, price_contract, price_quality
            FROM ctrader_deals WHERE deal_id=13
            """
        ).fetchone()
        assert row["exec_price"] == pytest.approx(4125.0)
        assert row["raw_execution_price"] == pytest.approx(4125.0)
        assert row["price_quality"] == "broker_reconciled"
    finally:
        conn.close()


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


def test_close_deal_already_in_store_resolves_with_empty_baseline(tmp_path):
    """回归:close deal 已入库 + 空 baseline 应直接 resolved。

    生产死锁场景(2026-08-05):broker 仓位消失检测晚于 deal 入库时,
    sync_close_deals_batch 缺省把"当前库里已有的 close deal"当作 baseline
    (observed_baseline),导致 observed_ids - baseline_ids 恒为空集、
    delta_proven 永远 False。tick 路径现显式传空 baseline —— 已入库的
    close deal 本身就是证据,不应要求"出现新 deal"。
    """
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
                (9501, 7501, 1000.0, 0.0, 0.0, 0.0, 0, 0),
                (9502, 7501, 1002.0, 8.39, 0.0, -0.18, 100, 1),
            ],
        )
        conn.commit()

        class _Bridge:
            is_connected = True

            def get_deals(self, **_kwargs):
                # 库里已有 close deal,broker 无需再返回新 deal。
                return []

        resolved = sync_close_deals_batch(
            _Bridge(),
            conn,
            {7501},
            min_exec_timestamp_by_position={7501: 900.0},
            required_closed_volume_delta_by_position={7501: 100.0},
            baseline_close_cursor_by_position={
                7501: {
                    "baseline_cursor_available": True,
                    "baseline_deal_ids": [],
                    "baseline_closed_volume": 0.0,
                }
            },
        )
    finally:
        conn.close()

    assert 7501 in resolved
    assert resolved[7501]["net"] == pytest.approx(8.21)
    assert resolved[7501]["deal_ids"] == [9502]
