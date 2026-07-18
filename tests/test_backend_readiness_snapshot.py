import threading
import time

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


def test_async_refresh_is_single_flight_and_shutdown_joins_owned_worker(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    conn.execute(
        "CREATE TABLE runtime_kv "
        "(key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at REAL NOT NULL)"
    )
    conn.commit()
    conn.close()

    service = BackendReadinessSnapshotService(db_path)
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    monkeypatch.setattr(
        service,
        "latest",
        lambda: {"ok": False, "status": "missing", "age_seconds": None},
    )

    def _refresh():
        calls.append("refresh")
        started.set()
        assert release.wait(2.0)
        return {"ok": True}

    monkeypatch.setattr(service, "refresh", _refresh)

    first = service.refresh_async(max_age_seconds=60.0)
    assert started.wait(1.0)
    second = service.refresh_async(max_age_seconds=60.0)

    assert first["status"] == "refresh_started"
    assert second["status"] == "refresh_in_progress"
    assert first["generation"] == second["generation"]
    assert calls == ["refresh"]
    owned_thread = service._refresh_owner._thread
    assert owned_thread is not None
    assert owned_thread.daemon is False

    stopped: dict[str, object] = {}
    drain_thread = threading.Thread(
        target=lambda: stopped.update(
            service.shutdown_async_refresh(timeout_sec=2.0)
        ),
        name="pytest-readiness-drain",
    )
    drain_thread.start()
    deadline = time.time() + 1.0
    while service.async_refresh_status()["accepting"] and time.time() < deadline:
        time.sleep(0.01)

    rejected = service.refresh_async(max_age_seconds=60.0)
    assert rejected["status"] == "refresh_draining"
    release.set()
    drain_thread.join(2.0)

    assert not drain_thread.is_alive()
    assert stopped == {
        "ok": True,
        "status": "completed",
        "generation": first["generation"],
        "thread_alive": False,
    }
    assert service.async_refresh_status()["thread_alive"] is False
