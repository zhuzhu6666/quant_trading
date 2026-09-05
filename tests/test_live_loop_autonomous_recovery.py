from types import SimpleNamespace

import pytest

from backend.services import live_service


def test_loop_failure_keeps_process_scheduler_and_schedules_auto_resume(monkeypatch):
    events: list[str] = []

    monkeypatch.setattr(
        live_service,
        "_run_loop_body",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(
        live_service,
        "_live_state_update",
        lambda **_kwargs: events.append("state_closed"),
    )
    monkeypatch.setattr(
        live_service,
        "_stop_live_safety_watchdog",
        lambda: events.append("watchdog_stopped"),
    )
    monkeypatch.setattr(
        live_service,
        "_stop_live_scheduler",
        lambda: events.append("scheduler_stopped"),
    )
    monkeypatch.setattr(
        live_service,
        "schedule_auto_resume_loop",
        lambda: events.append("resume_scheduled") or True,
    )
    monkeypatch.setattr(live_service, "_process_shutdown_requested", False)

    with pytest.raises(RuntimeError, match="boom"):
        live_service._run_loop("ctrader", SimpleNamespace(), generation_id="")

    assert "watchdog_stopped" in events
    assert "resume_scheduled" in events
    assert "scheduler_stopped" not in events


def test_process_shutdown_does_not_schedule_loop_auto_resume(monkeypatch):
    events: list[str] = []

    monkeypatch.setattr(live_service, "_run_loop_body", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(live_service, "_live_state_update", lambda **_kwargs: None)
    monkeypatch.setattr(live_service, "_stop_live_safety_watchdog", lambda: None)
    monkeypatch.setattr(
        live_service,
        "_stop_live_scheduler",
        lambda: events.append("scheduler_stopped"),
    )
    monkeypatch.setattr(
        live_service,
        "schedule_auto_resume_loop",
        lambda: events.append("resume_scheduled") or True,
    )
    monkeypatch.setattr(live_service, "_process_shutdown_requested", True)

    live_service._run_loop("ctrader", SimpleNamespace(), generation_id="")

    assert events == []
