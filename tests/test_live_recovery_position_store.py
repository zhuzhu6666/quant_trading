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


def test_recovery_position_store_binds_text_position_ids_for_postgres(tmp_path):
    path = tmp_path / "state.db"
    connect = _connection_factory(path)
    conn = connect()
    try:
        conn.executescript(STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()

    def execute(conn, sql, params=()):
        compact = " ".join(sql.lower().split())
        if "recovery_position_state" in compact:
            position_param = (
                params[0] if compact.startswith("insert into") else params[-1]
            )
            assert isinstance(position_param, str)
        return conn.execute(sql, params)

    store = RecoveryPositionStore(
        RecoveryPositionStoreRuntime(
            get_read_connection=connect,
            get_write_connection=connect,
            execute=execute,
            normalize_position=normalize_position_snapshot,
            normalize_row=normalize_recovery_position_row,
            lookup_entry_decision_id=lambda _position_id: "decision-text-id",
            build_meta_update_payload=build_recovery_meta_update_payload,
            build_closed_update_payload=build_recovery_closed_update_payload,
            now=lambda: 100.0,
            local_open_volumes={},
        )
    )

    store.upsert(
        {
            "position_id": 41,
            "symbol": "XAUUSD+",
            "direction": 1,
            "open_price": 2400.0,
            "volume": 100.0,
        },
        broker="ctrader",
        strategy_name="factor_v4",
    )
    assert store.load(41)["position_id"] == 41
    store.merge_meta(41, {"close_deal_pending": {"status": "pending"}})
    assert store.last_seen_by_position({41}) == {41: 95.0}
    assert store.remaining_volume_by_position({41}) == {41: 100.0}
    assert store.context_integrity(41, default="partial") == "full"
    store.mark_closed(
        41,
        close_reason="broker_close",
        close_pnl=1.0,
        closed_at=110.0,
    )


def test_recovery_position_store_purges_only_unbrokered_rows_without_entry_lineage(
    tmp_path,
):
    path = tmp_path / "state.db"
    connect = _connection_factory(path)
    conn = connect()
    try:
        conn.executescript(STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()

    store = RecoveryPositionStore(
        RecoveryPositionStoreRuntime(
            get_read_connection=connect,
            get_write_connection=connect,
            execute=lambda conn, sql, params=(): conn.execute(sql, params),
            normalize_position=normalize_position_snapshot,
            normalize_row=normalize_recovery_position_row,
            lookup_entry_decision_id=lambda position_id: (
                "entry-904" if int(position_id) == 904 else ""
            ),
            build_meta_update_payload=build_recovery_meta_update_payload,
            build_closed_update_payload=build_recovery_closed_update_payload,
            now=lambda: 100.0,
            local_open_volumes={},
        )
    )
    for position_id in (902, 903):
        store.upsert(
            {
                "position_id": position_id,
                "symbol": "XAUUSD+",
                "direction": 1,
                "open_price": 2400.0,
                "volume": 100.0,
            },
            broker="ctrader",
            strategy_name="factor_v4",
        )
    store.upsert(
        {
            "position_id": 904,
            "symbol": "XAUUSD+",
            "direction": 1,
            "open_price": 2400.0,
            "volume": 100.0,
            "entry_decision_id": "entry-904",
        },
        broker="ctrader",
        strategy_name="factor_v4",
    )

    assert store.purge_unbrokered(
        {902, 903},
        broker="ctrader",
        broker_position_ids=set(),
    ) == [902, 903]
    assert store.load(902) == {}
    assert store.load(903) == {}
    assert store.load(904)["entry_decision_id"] == "entry-904"

    with pytest.raises(ValueError, match="present at broker"):
        store.purge_unbrokered(
            {904},
            broker="ctrader",
            broker_position_ids={904},
        )


def test_recovery_store_keeps_lineage_when_index_is_stale(tmp_path):
    """Regression: 2026-08-21 orphan purge of position 284485647.

    A fresh open persists its recovery row while the position-decision index
    has not been rebuilt yet, so lookup_entry_decision_id resolves to "".  The
    caller-supplied entry_decision_id must survive normalization and the row
    must then be protected from purge_unbrokered after the broker close.
    """

    path = tmp_path / "state.db"
    connect = _connection_factory(path)
    conn = connect()
    try:
        conn.executescript(STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()

    store = RecoveryPositionStore(
        RecoveryPositionStoreRuntime(
            get_read_connection=connect,
            get_write_connection=connect,
            execute=lambda conn, sql, params=(): conn.execute(sql, params),
            normalize_position=normalize_position_snapshot,
            normalize_row=normalize_recovery_position_row,
            lookup_entry_decision_id=lambda _position_id: "",  # stale index
            build_meta_update_payload=build_recovery_meta_update_payload,
            build_closed_update_payload=build_recovery_closed_update_payload,
            now=lambda: 100.0,
            local_open_volumes={},
        )
    )

    # Same shape as _lifecycle_build_filled_open_recovery_payloads state_payload.
    store.upsert(
        {
            "position_id": 284485647,
            "symbol": "XAUUSD+",
            "direction": -1,
            "open_price": 4534.20,
            "volume": 100.0,
            "entry_decision_id": "dec_87ae51b36fe44742",
        },
        broker="ctrader",
        strategy_name="factor_v4",
        status="open",
        context_integrity="full",
    )
    row = store.load(284485647)
    assert row["entry_decision_id"] == "dec_87ae51b36fe44742"
    assert row["context_integrity"] == "full"

    # Broker closed the position: the row must NOT be purgeable as an orphan.
    assert (
        store.purge_unbrokered(
            {284485647},
            broker="ctrader",
            broker_position_ids=set(),
        )
        == []
    )
    assert store.load(284485647)["entry_decision_id"] == "dec_87ae51b36fe44742"
