import json

from backend.core.db import connect_sqlite
from backend.services.runtime_kv_store import set_on_conn


def _read(path, key):
    conn = connect_sqlite(path)
    try:
        row = conn.execute(
            "SELECT value_json, updated_at FROM runtime_kv WHERE key=?", (key,)
        ).fetchone()
        return (json.loads(row[0]), float(row[1])) if row else None
    finally:
        conn.close()


def test_runtime_kv_same_readiness_payload_only_updates_row_freshness(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    first_payload = {
        "schema_version": "backend_readiness_snapshot.v1",
        "snapshot": {"ready": True, "generated_at": 10.0, "build_seconds": 0.12},
        "updated_at": 10.0,
    }
    second_payload = {
        "schema_version": "backend_readiness_snapshot.v1",
        "snapshot": {"ready": True, "generated_at": 20.0, "build_seconds": 9.99},
        "updated_at": 20.0,
    }
    assert set_on_conn(conn, "backend_readiness_snapshot.v1", first_payload, updated_at=10.0)["changed"]
    conn.commit()
    first_stored = _read(db_path, "backend_readiness_snapshot.v1")

    result = set_on_conn(conn, "backend_readiness_snapshot.v1", second_payload, updated_at=20.0)
    conn.commit()
    second_stored = _read(db_path, "backend_readiness_snapshot.v1")

    assert result["changed"] is False
    assert result["heartbeat_only"] is True
    assert second_stored[0] == first_stored[0]
    assert second_stored[1] == 20.0


def test_runtime_kv_readiness_nested_refresh_telemetry_does_not_rewrite_value(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    first_payload = {
        "schema_version": "backend_readiness.v1",
        "config_hash": "cfg-1",
        "readiness": {
            "status": "degraded",
            "error": "market_closed",
            "age_seconds": 1.0,
            "updated_at": 10.0,
            "reconcile_id": "reconcile-1",
            "observed_at": 10.0,
        },
        "process_static_feature_flags": {"process_started_at": 5.0},
    }
    second_payload = {
        "schema_version": "backend_readiness.v1",
        "config_hash": "cfg-1",
        "readiness": {
            "status": "degraded",
            "error": "market_closed",
            "age_seconds": 999.0,
            "updated_at": 20.0,
            "reconcile_id": "reconcile-1",
            "observed_at": 20.0,
        },
        "process_static_feature_flags": {"process_started_at": 5.0},
    }
    set_on_conn(conn, "backend_readiness_snapshot.v1", first_payload, updated_at=10.0)
    conn.commit()
    first_stored = _read(db_path, "backend_readiness_snapshot.v1")

    result = set_on_conn(
        conn, "backend_readiness_snapshot.v1", second_payload, updated_at=20.0
    )
    conn.commit()
    second_stored = _read(db_path, "backend_readiness_snapshot.v1")

    assert result["changed"] is False
    assert second_stored[0] == first_stored[0]
    assert second_stored[1] == 20.0


def test_runtime_kv_semantic_field_change_rewrites_value(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    first = {"schema_version": "runtime_factor_selection.v1", "selection": ["a"], "updated_at": 1.0}
    changed = {"schema_version": "runtime_factor_selection.v1", "selection": ["b"], "updated_at": 2.0}
    set_on_conn(conn, "runtime_factor_selection.v1", first, updated_at=1.0)
    conn.commit()
    result = set_on_conn(conn, "runtime_factor_selection.v1", changed, updated_at=2.0)
    conn.commit()

    assert result["changed"] is True
    assert _read(db_path, "runtime_factor_selection.v1")[0]["selection"] == ["b"]


def test_runtime_kv_unknown_key_does_not_drop_fields(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    first = {"semantic": "same", "heartbeat_at": 1.0}
    second = {"semantic": "same", "heartbeat_at": 2.0}
    set_on_conn(conn, "new_projection.v1", first, updated_at=1.0)
    conn.commit()
    result = set_on_conn(conn, "new_projection.v1", second, updated_at=2.0)

    assert result["changed"] is True
    conn.rollback()
