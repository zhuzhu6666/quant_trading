from __future__ import annotations

import threading

from backend.api import db_health


def _reset_owner() -> None:
    db_health._stop_background_refresh(timeout_sec=2.0)


def test_db_health_refresh_is_single_non_daemon_process_owned_thread(monkeypatch) -> None:
    _reset_owner()
    entered = threading.Event()

    def compute() -> dict:
        entered.set()
        return {"ok": True, "checked_at": 1.0, "databases": []}

    monkeypatch.setattr(db_health, "_compute_db_health", compute)
    first = db_health._start_background_refresh(initial_delay_sec=0.0)
    assert entered.wait(1.0)
    thread = db_health._refresh_thread
    second = db_health._start_background_refresh(initial_delay_sec=0.0)

    assert first["status"] == "started"
    assert second["status"] == "already_running"
    assert thread is not None and thread.daemon is False

    stopped = db_health._stop_background_refresh(timeout_sec=2.0)
    assert stopped == {"ok": True, "status": "completed", "thread_alive": False}
    assert db_health._refresh_thread is None


def test_db_health_delayed_start_is_interruptible_without_running_compute(monkeypatch) -> None:
    _reset_owner()
    called = threading.Event()
    monkeypatch.setattr(
        db_health,
        "_compute_db_health",
        lambda: called.set() or {"ok": True},
    )

    started = db_health._start_background_refresh(initial_delay_sec=60.0)
    stopped = db_health._stop_background_refresh(timeout_sec=2.0)

    assert started["status"] == "started"
    assert stopped["status"] == "completed"
    assert called.is_set() is False
