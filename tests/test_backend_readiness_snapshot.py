from backend.core.db import connect_sqlite
from backend.services.backend_readiness_snapshot import BackendReadinessSnapshotService


def test_readiness_snapshot_publishes_and_reuses_persistent_projection(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    conn.execute("CREATE TABLE runtime_kv (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at REAL NOT NULL)")
    conn.commit()
    conn.close()

    service = BackendReadinessSnapshotService(db_path)
    built = service.refresh(lambda: {"ok": True, "ready_for_frontend": True})
    latest = service.latest()
    fresh = service.refresh_async(max_age_seconds=60.0)

    assert built["snapshot"]["generated_in_background"] is True
    assert latest["ok"] is True
    assert latest["payload"]["ready_for_frontend"] is True
    assert fresh["status"] == "fresh"
