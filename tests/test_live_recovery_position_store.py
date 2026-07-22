from __future__ import annotations

import sqlite3

import pytest

from backend.core.db import STATE_DB_DDL
from backend.services.live_position_lifecycle import (
    build_recovery_closed_update_payload,
    build_recovery_meta_update_payload,
    normalize_position_snapshot,
    normalize_recovery_position_row,
)
from backend.services.live_recovery_position_store import (
    RecoveryPositionStore,
    RecoveryPositionStoreRuntime,
)


def _connection_factory(path):
    def connect():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn

    return connect


def test_recovery_position_store_owns_complete_persistence_lifecycle(tmp_path):
    path = tmp_path / "state.db"
    connect = _connection_factory(path)
    conn = connect()
    try:
        conn.executescript(STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()

    clock = [100.0]
    store = RecoveryPositionStore(
        RecoveryPositionStoreRuntime(
            get_read_connection=connect,
            get_write_connection=connect,
            execute=lambda conn, sql, params=(): conn.execute(sql, params),
            normalize_position=normalize_position_snapshot,
            normalize_row=normalize_recovery_position_row,
            lookup_entry_decision_id=lambda _position_id: "decision-1",
            build_meta_update_payload=build_recovery_meta_update_payload,
            build_closed_update_payload=build_recovery_closed_update_payload,
            now=lambda: clock[0],
            local_open_volumes={2: 75.0},
        )
    )

    store.upsert(
        {
            "position_id": 1,
            "symbol": "XAUUSD+",
            "direction": 1,
            "open_price": 2400.0,
            "volume": 100.0,
        },
        broker="ctrader",
        strategy_name="factor_v4",
        meta={"origin": "fresh_reconcile"},
    )
    clock[0] = 120.0
    store.upsert(
        {
            "position_id": 1,
            "symbol": "XAUUSD+",
            "direction": 1,
            "open_price": 2401.0,
            "volume": 0.0,
        },
        broker="ctrader",
        strategy_name="factor_v4",
    )

    row = store.load(1)
    assert row["volume"] == pytest.approx(100.0)
    assert row["open_price"] == pytest.approx(2401.0)
    assert row["context_integrity"] == "full"
    assert row["recovery_meta"]["origin"] == "fresh_reconcile"
    assert [item["position_id"] for item in store.list_active("ctrader")] == [1]
    assert store.last_seen_by_position({0, 1}) == {1: 115.0}
    assert store.remaining_volume_by_position({1, 2}) == {1: 100.0, 2: 75.0}
    assert store.context_integrity(1, default="partial") == "full"

    clock[0] = 130.0
    store.merge_meta(1, {"pending_close": True})
    row = store.load(1)
    assert row["recovery_meta"]["pending_close"] is True
    assert row["last_seen_at"] == pytest.approx(120.0)

    store.mark_closed(
        1,
        close_reason="supervisor_close",
        close_pnl=12.5,
        closed_at=140.0,
        meta={"deal_id": 99},
    )
    closed = store.load(1)
    assert closed["status"] == "closed_replayed"
    assert closed["close_pnl"] == pytest.approx(12.5)
    assert closed["recovery_meta"]["deal_id"] == 99
    assert store.list_active("ctrader") == []
