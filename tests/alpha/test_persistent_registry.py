from __future__ import annotations

import sqlite3

from alpha import persistent_registry
from backend.services.canonical_v2 import record_payload_event
from tests.canonical_fixture import seed_canonical_sqlite_file


class _Adapter:
    def __init__(self) -> None:
        self.registered = []

    def register_runtime(self, name, func, source, description="", log_event=True):
        self.registered.append(
            {
                "name": name,
                "source": source,
                "description": description,
                "log_event": log_event,
                "callable": callable(func),
            }
        )
        return True


def _init_canonical_db(path):
    seed_canonical_sqlite_file(path)
    conn = sqlite3.connect(path)
    return conn


def _write_factor_observation_events(conn, rows):
    for index, (timestamp, event, factor, source, description) in enumerate(rows):
        payload = {
            "timestamp": timestamp,
            "event": event,
            "factor": factor,
            "source": source,
            "lifecycle_source": source,
            "description": description,
            "score": 0.0,
            "status": "UNKNOWN",
            "reason": "",
        }
        record_payload_event(
            conn,
            event_type="factor_observation",
            entity_type="factor_lifecycle",
            entity_id=factor,
            payload=payload,
            observed_at=timestamp,
            producer="test_persistent_registry",
            payload_kind="factor_lifecycle",
            event_id=f"test_factor_lifecycle_{index}",
            idempotency_key=f"test_factor_lifecycle:{index}",
        )


def test_restore_from_canonical_replays_lifecycle_and_only_restores_active_dsl(tmp_path, monkeypatch):
    import backend.core.db as db

    state_db = tmp_path / "state.db"
    conn = _init_canonical_db(state_db)
    rows = [
        (1.0, "register", "dsl_good", "shadow", "rank(close)"),
        (2.0, "promote", "dsl_good", "", ""),
        (3.0, "register", "dsl_retired", "shadow", "rank(open)"),
        (4.0, "retire", "dsl_retired", "removed", ""),
        (5.0, "register", "dsl_unregistered", "shadow", "rank(high)"),
        (6.0, "unregister", "dsl_unregistered", "removed", ""),
        (7.0, "register", "pca_factor", "shadow", "PCA component 1"),
    ]
    _write_factor_observation_events(conn, rows)
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "STATE_DB", state_db)

    def _test_state_conn(read_only=True):
        conn = sqlite3.connect(state_db)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(db, "get_state_pg_conn", _test_state_conn)

    adapter = _Adapter()

    restored = persistent_registry.restore_from_canonical(verbose=False, adapter=adapter)

    assert restored == 1
    assert [item["name"] for item in adapter.registered] == ["dsl_good"]
    assert adapter.registered[0]["source"] == "discovered"
    assert adapter.registered[0]["description"] == "rank(close)"
    assert adapter.registered[0]["log_event"] is False


def test_restore_from_canonical_keeps_preferred_and_limits_cold_runtime_factors(tmp_path, monkeypatch):
    import backend.core.db as db

    state_db = tmp_path / "state.db"
    conn = _init_canonical_db(state_db)
    _write_factor_observation_events(
        conn,
        [
            (1.0, "register", "preferred_old", "discovered", "rank(close)"),
            (2.0, "register", "cold_old", "discovered", "rank(open)"),
            (3.0, "register", "cold_new", "discovered", "rank(high)"),
        ],
    )
    conn.commit()
    conn.close()

    def _test_state_conn(read_only=True):
        conn = sqlite3.connect(state_db)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(db, "get_state_pg_conn", _test_state_conn)
    adapter = _Adapter()
    restored = persistent_registry.restore_from_canonical(
        verbose=False,
        adapter=adapter,
        preferred_names={"preferred_old"},
        discovered_budget=1,
    )

    assert restored == 2
    assert [item["name"] for item in adapter.registered] == ["preferred_old", "cold_new"]
