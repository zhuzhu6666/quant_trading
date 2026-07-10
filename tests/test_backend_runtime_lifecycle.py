from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from backend.services.backend_runtime_lifecycle import (
    BackendRuntimeLifecycle,
    BackendRuntimeLifecycleCallbacks,
)


class _Logger:
    def __init__(self, events: list[Any]) -> None:
        self.events = events

    def info(self, message: str) -> None:
        self.events.append(("info", message))

    def warning(self, message: str) -> None:
        self.events.append(("warning", message))


def _record(events: list[Any], name: str, result: Any = None):
    def _callback(*args: Any, **kwargs: Any) -> Any:
        events.append(("call", name, args, kwargs))
        return result

    return _callback


def _fail(events: list[Any], name: str, message: str):
    def _callback(*args: Any, **kwargs: Any) -> Any:
        events.append(("call", name, args, kwargs))
        raise RuntimeError(message)

    return _callback


def _start_callbacks(events: list[Any], **overrides: Any) -> BackendRuntimeLifecycleCallbacks:
    values = {
        "warm_data_store": _record(events, "data_store"),
        "warmup_ctrader": _record(events, "ctrader"),
        "schedule_auto_resume_loop": _record(events, "auto_resume", True),
        "schedule_learning_backfill": _record(events, "backfill", True),
        "schedule_supervisor_learning": _record(events, "supervisor", True),
        "schedule_autonomous_learning": _record(events, "autonomous", True),
        "warm_db_health": _record(events, "db_health"),
    }
    values.update(overrides)
    return BackendRuntimeLifecycleCallbacks(**values)


def _call_names(events: list[Any]) -> list[str]:
    return [event[1] for event in events if event[0] == "call"]


def test_start_preserves_success_order_arguments_and_logs():
    events: list[Any] = []
    lifecycle = BackendRuntimeLifecycle(
        _start_callbacks(events),
        env_enabled=lambda name, default: (name, default)
        == ("QUANT_BACKEND_LEARNING_SCHEDULERS", "0"),
    )

    lifecycle.start(_Logger(events))

    assert events == [
        ("call", "data_store", (), {}),
        ("info", "[lifespan] DataStore warmed up"),
        ("call", "ctrader", (), {"timeout_sec": 0.0}),
        ("call", "auto_resume", (), {}),
        ("info", "[lifespan] auto-resume loop scheduled from persisted desired state"),
        (
            "call",
            "backfill",
            (),
            {
                "delay_sec": 180.0,
                "limit": 100,
                "allow_partial": False,
                "rebuild_learning": True,
            },
        ),
        ("info", "[lifespan] learning backfill scheduled"),
        (
            "call",
            "supervisor",
            (),
            {"delay_sec": 300.0, "interval_sec": 1800.0, "limit": 200},
        ),
        ("info", "[lifespan] supervisor learning scheduled"),
        (
            "call",
            "autonomous",
            (),
            {
                "delay_sec": 420.0,
                "interval_sec": 1800.0,
                "sample_limit": 500,
                "recommendation_limit": 20,
            },
        ),
        ("info", "[lifespan] autonomous learning scheduled"),
        ("call", "db_health", (), {}),
        ("info", "[lifespan] db-health cache warmup scheduled"),
    ]


def test_start_defaults_backend_learning_schedulers_to_disabled(monkeypatch):
    events: list[Any] = []
    monkeypatch.delenv("QUANT_BACKEND_LEARNING_SCHEDULERS", raising=False)

    BackendRuntimeLifecycle(_start_callbacks(events)).start(_Logger(events))

    assert _call_names(events) == ["data_store", "ctrader", "auto_resume", "db_health"]
    assert (
        "info",
        "[lifespan] backend learning schedulers disabled by QUANT_BACKEND_LEARNING_SCHEDULERS",
    ) in events


@pytest.mark.parametrize("disabled_value", ["0", "false", "no", "off", "disabled"])
def test_start_honors_disabled_environment_values(monkeypatch, disabled_value):
    events: list[Any] = []
    monkeypatch.setenv("QUANT_BACKEND_LEARNING_SCHEDULERS", disabled_value)

    BackendRuntimeLifecycle(_start_callbacks(events)).start(_Logger(events))

    assert "backfill" not in _call_names(events)
    assert "supervisor" not in _call_names(events)
    assert "autonomous" not in _call_names(events)


def test_data_store_failure_is_non_fatal_and_later_steps_continue():
    events: list[Any] = []
    callbacks = _start_callbacks(
        events,
        warm_data_store=_fail(events, "data_store", "store unavailable"),
    )

    BackendRuntimeLifecycle(callbacks, env_enabled=lambda _name, _default: False).start(
        _Logger(events)
    )

    assert _call_names(events) == ["data_store", "ctrader", "auto_resume", "db_health"]
    assert (
        "warning",
        "[lifespan] DataStore warmup failed (non-fatal): store unavailable",
    ) in events


def test_ctrader_failure_skips_auto_resume_but_later_steps_continue():
    events: list[Any] = []
    callbacks = _start_callbacks(
        events,
        warmup_ctrader=_fail(events, "ctrader", "broker unavailable"),
    )

    BackendRuntimeLifecycle(callbacks, env_enabled=lambda _name, _default: True).start(
        _Logger(events)
    )

    assert _call_names(events) == [
        "data_store",
        "ctrader",
        "backfill",
        "supervisor",
        "autonomous",
        "db_health",
    ]
    assert (
        "warning",
        "[lifespan] cTrader warmup failed (non-fatal): broker unavailable",
    ) in events


def test_auto_resume_failure_uses_ctrader_boundary_and_later_steps_continue():
    events: list[Any] = []
    callbacks = _start_callbacks(
        events,
        schedule_auto_resume_loop=_fail(events, "auto_resume", "resume unavailable"),
    )

    BackendRuntimeLifecycle(callbacks, env_enabled=lambda _name, _default: False).start(
        _Logger(events)
    )

    assert _call_names(events) == ["data_store", "ctrader", "auto_resume", "db_health"]
    assert (
        "warning",
        "[lifespan] cTrader warmup failed (non-fatal): resume unavailable",
    ) in events


def test_learning_failures_are_independent_and_db_health_still_runs():
    events: list[Any] = []
    callbacks = _start_callbacks(
        events,
        schedule_learning_backfill=_fail(events, "backfill", "backfill failed"),
        schedule_supervisor_learning=_fail(events, "supervisor", "supervisor failed"),
        schedule_autonomous_learning=_fail(events, "autonomous", "autonomous failed"),
    )

    BackendRuntimeLifecycle(callbacks, env_enabled=lambda _name, _default: True).start(
        _Logger(events)
    )

    assert _call_names(events) == [
        "data_store",
        "ctrader",
        "auto_resume",
        "backfill",
        "supervisor",
        "autonomous",
        "db_health",
    ]
    assert (
        "warning",
        "[lifespan] learning backfill schedule failed (non-fatal): backfill failed",
    ) in events
    assert (
        "warning",
        "[lifespan] supervisor learning schedule failed (non-fatal): supervisor failed",
    ) in events
    assert (
        "warning",
        "[lifespan] autonomous learning schedule failed (non-fatal): autonomous failed",
    ) in events


def test_db_health_failure_is_non_fatal():
    events: list[Any] = []
    callbacks = _start_callbacks(
        events,
        schedule_auto_resume_loop=_record(events, "auto_resume", False),
        warm_db_health=_fail(events, "db_health", "health unavailable"),
    )

    BackendRuntimeLifecycle(callbacks, env_enabled=lambda _name, _default: False).start(
        _Logger(events)
    )

    assert (
        "warning",
        "[lifespan] db-health warmup failed (non-fatal): health unavailable",
    ) in events
    assert (
        "info",
        "[lifespan] auto-resume loop scheduled from persisted desired state",
    ) not in events


def test_stop_preserves_order_and_each_failure_is_independent():
    events: list[Any] = []
    callbacks = BackendRuntimeLifecycleCallbacks(
        stop_live_loop_for_process_shutdown=_fail(
            events,
            "stop_live_loop",
            "live stop failed",
        ),
        stop_learning_backfill=_fail(events, "stop_backfill", "backfill stop failed"),
        stop_supervisor_learning=_fail(events, "stop_supervisor", "supervisor stop failed"),
        stop_autonomous_learning=_fail(events, "stop_autonomous", "autonomous stop failed"),
        stop_live_scheduler=_fail(events, "stop_live_scheduler", "scheduler stop failed"),
    )

    BackendRuntimeLifecycle(callbacks).stop(_Logger(events))

    assert _call_names(events) == [
        "stop_live_loop",
        "stop_backfill",
        "stop_supervisor",
        "stop_autonomous",
        "stop_live_scheduler",
    ]
    assert events == [
        ("call", "stop_live_loop", (), {"timeout_sec": 30.0}),
        ("warning", "[lifespan] live loop process shutdown failed: live stop failed"),
        ("call", "stop_backfill", (), {}),
        ("warning", "[lifespan] learning backfill stop failed: backfill stop failed"),
        ("call", "stop_supervisor", (), {}),
        ("warning", "[lifespan] supervisor learning stop failed: supervisor stop failed"),
        ("call", "stop_autonomous", (), {}),
        ("warning", "[lifespan] autonomous learning stop failed: autonomous stop failed"),
        ("call", "stop_live_scheduler", (), {}),
        ("warning", "[lifespan] InProcessScheduler stop failed: scheduler stop failed"),
    ]


def test_stop_runs_live_loop_first_and_logs_completed_status():
    events: list[Any] = []
    callbacks = BackendRuntimeLifecycleCallbacks(
        stop_live_loop_for_process_shutdown=_record(
            events,
            "stop_live_loop",
            {"status": "completed", "recovery_required": False},
        ),
        stop_learning_backfill=_record(events, "stop_backfill"),
        stop_supervisor_learning=_record(events, "stop_supervisor"),
        stop_autonomous_learning=_record(events, "stop_autonomous"),
        stop_live_scheduler=_record(events, "stop_live_scheduler"),
    )

    BackendRuntimeLifecycle(callbacks).stop(_Logger(events))

    assert _call_names(events) == [
        "stop_live_loop",
        "stop_backfill",
        "stop_supervisor",
        "stop_autonomous",
        "stop_live_scheduler",
    ]
    assert events[:2] == [
        ("call", "stop_live_loop", (), {"timeout_sec": 30.0}),
        ("info", "[lifespan] live loop process shutdown status=completed"),
    ]


def test_stop_timeout_warns_and_does_not_block_remaining_stops():
    events: list[Any] = []
    callbacks = BackendRuntimeLifecycleCallbacks(
        stop_live_loop_for_process_shutdown=_record(
            events,
            "stop_live_loop",
            {"status": "timed_out", "recovery_required": True},
        ),
        stop_learning_backfill=_record(events, "stop_backfill"),
        stop_supervisor_learning=_record(events, "stop_supervisor"),
        stop_autonomous_learning=_record(events, "stop_autonomous"),
        stop_live_scheduler=_record(events, "stop_live_scheduler"),
    )

    BackendRuntimeLifecycle(callbacks).stop(_Logger(events))

    assert _call_names(events) == [
        "stop_live_loop",
        "stop_backfill",
        "stop_supervisor",
        "stop_autonomous",
        "stop_live_scheduler",
    ]
    assert (
        "warning",
        "[lifespan] live loop process shutdown timed out; recovery required",
    ) in events


@pytest.mark.asyncio
async def test_backend_lifespan_stops_runtime_when_context_exits_with_error(monkeypatch):
    import backend.app as app_module
    import backend.core.auth as auth_module
    import backend.core.db as db_module
    import backend.services.execution_semantics as semantics_module
    import backend.services.parameter_templates as parameter_module
    import backend.services.position_supervisor_templates as supervisor_template_module
    import backend.services.runtime_config_startup as startup_module
    import backend.services.startup_status as startup_status_module
    import alpha.persistent_registry as registry_module

    events = []
    runtime_config = SimpleNamespace(position_supervisor_template_id="")

    class _Lifecycle:
        def start(self, _logger):
            events.append("start")

        def stop(self, _logger):
            events.append("stop")

    class _JobManager:
        def bind_loop(self, _loop):
            events.append("bind_loop")

    monkeypatch.setattr(app_module, "setup_logging", lambda: None)
    monkeypatch.setattr(app_module, "_init_observability", lambda: None)
    monkeypatch.setattr(app_module, "BackendRuntimeLifecycle", _Lifecycle)
    monkeypatch.setattr(app_module, "get_job_manager", lambda: _JobManager())
    monkeypatch.setattr(startup_status_module, "clear_startup_issues", lambda: None)
    monkeypatch.setattr(startup_status_module, "record_startup_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(auth_module, "validate_auth_config", lambda: None)
    monkeypatch.setattr(
        semantics_module,
        "validate_execution_semantics",
        lambda _yaml, _runtime: SimpleNamespace(effective_send_orders=False),
    )
    monkeypatch.setattr(startup_module, "load_yaml_runtime_config", lambda: (runtime_config, {}))
    monkeypatch.setattr(
        startup_module,
        "restore_runtime_config_on_startup",
        lambda config, **_kwargs: {"config": config, "overlay": {}},
    )
    monkeypatch.setattr(db_module, "init_all", lambda: None)
    monkeypatch.setattr(parameter_module.ParameterTemplateService, "sync_runtime_config", lambda self: None)
    monkeypatch.setattr(
        supervisor_template_module,
        "latest_applied_position_supervisor_template_id",
        lambda **_kwargs: "",
    )
    monkeypatch.setattr(registry_module, "restore_from_log", lambda **_kwargs: 0)

    with pytest.raises(RuntimeError, match="lifespan body failed"):
        async with app_module.lifespan(SimpleNamespace()):
            assert events[-1] == "start"
            raise RuntimeError("lifespan body failed")

    assert events == ["bind_loop", "start", "stop"]
