import threading
from types import SimpleNamespace

from backend.services.live_loop_start import (
    LiveLoopStartRuntime,
    start_live_loop,
)
from backend.services.live_loop_controller import LiveLoopController


class _LoopThread:
    ident = 88

    def __init__(self, *, target=None, args=(), name="", daemon=True, fail=False):
        self.target = target
        self.args = args
        self.name = name
        self.daemon = daemon
        self.fail = fail
        self.started = False

    def start(self):
        if self.fail:
            raise RuntimeError("thread start unavailable")
        self.started = True

    def is_alive(self):
        return self.started


def _runtime(
    *,
    existing_thread=None,
    existing_stop=None,
    last_end=0.0,
    thread_fail=False,
    sleep=None,
):
    controller = LiveLoopController(clock=lambda: 1_000.0)
    if existing_thread is not None:
        generation = controller.begin_start(
            broker="ctrader",
            strategy_name="factor_v4",
        )
        controller.bind_thread(generation.generation_id, existing_thread)
        if existing_stop is not None and existing_stop.is_set():
            controller.request_stop(generation.generation_id)

    persisted = []
    primed = []
    state_updates = []
    components = []
    threads = []

    def thread_factory(**kwargs):
        thread = _LoopThread(fail=thread_fail, **kwargs)
        threads.append(thread)
        return thread

    runtime = LiveLoopStartRuntime(
        state_lock=threading.RLock(),
        process_shutdown_requested=lambda: False,
        controller=controller,
        last_loop_end=lambda: last_end,
        now=lambda: 1_000.0,
        sleep=(sleep or (lambda _seconds: None)),
        logger_warning=lambda *_args: None,
        logger_info=lambda *_args: None,
        persist_desired_state=lambda *args, **kwargs: persisted.append(
            (args, kwargs)
        ),
        prime_live_loop_state=lambda **kwargs: primed.append(kwargs),
        start_safety_watchdog=lambda: components.append("watchdog_start"),
        start_scheduler=lambda: components.append("scheduler_start"),
        stop_scheduler=lambda: components.append("scheduler_stop"),
        stop_safety_watchdog=lambda: components.append("watchdog_stop"),
        thread_factory=thread_factory,
        loop_target=lambda *_args: None,
        live_state_update=lambda **kwargs: state_updates.append(kwargs),
    )
    evidence = SimpleNamespace(
        controller=controller,
        persisted=persisted,
        primed=primed,
        state_updates=state_updates,
        components=components,
        threads=threads,
    )
    return runtime, evidence


def test_draining_thread_rejects_start():
    existing = _LoopThread()
    existing.started = True
    stop_event = threading.Event()
    stop_event.set()
    runtime, evidence = _runtime(
        existing_thread=existing,
        existing_stop=stop_event,
    )

    result = start_live_loop(
        "ctrader",
        "factor_v4",
        persist_desired=True,
        trigger_reason="manual",
        runtime=runtime,
    )

    assert result["ok"] is False
    assert result["error"].startswith("live_loop_generation_busy:draining:")
    assert result["phase"] == "draining"
    assert evidence.threads == []


def test_generation_start_binds_components_and_owned_thread():
    runtime, evidence = _runtime()

    result = start_live_loop(
        "ctrader",
        "factor_v4",
        persist_desired=True,
        trigger_reason="manual",
        runtime=runtime,
    )

    assert result["ok"] is True
    assert result["phase"] == "starting"
    assert result["generation"] == evidence.controller.status()["generation"]
    assert evidence.controller.ownership_snapshot().thread is evidence.threads[0]
    assert evidence.threads[0].started is True
    generation = evidence.controller.status()["generation"]
    assert evidence.controller.status()["components"] == {
        "scheduler": generation,
        "refresh_worker_inline": generation,
    }
    assert evidence.primed[0]["account_observed"] is False


def test_thread_start_failure_releases_ownership_and_marks_generation_failed():
    runtime, evidence = _runtime(
        thread_fail=True,
    )

    result = start_live_loop(
        "ctrader",
        "factor_v4",
        persist_desired=True,
        trigger_reason="manual",
        runtime=runtime,
    )

    assert result["ok"] is False
    assert "thread start unavailable" in result["error"]
    assert evidence.controller.ownership_snapshot().thread is None
    assert evidence.controller.status()["phase"] == "failed"
    assert evidence.state_updates[0]["accepting_new_risk"] is False
    assert evidence.components[-2:] == ["scheduler_stop", "watchdog_stop"]


def test_backoff_second_check_rejects_competing_start():
    runtime, evidence = _runtime(
        last_end=999.0,
    )

    def competing_start(_seconds):
        thread = _LoopThread()
        thread.started = True
        generation = evidence.controller.begin_start(
            broker="ctrader",
            strategy_name="factor_v4",
        )
        evidence.controller.bind_thread(generation.generation_id, thread)

    runtime = LiveLoopStartRuntime(
        **{**runtime.__dict__, "sleep": competing_start}
    )

    result = start_live_loop(
        "ctrader",
        "factor_v4",
        persist_desired=True,
        trigger_reason="auto_recovery",
        runtime=runtime,
    )

    assert result["ok"] is False
    assert result["error"].startswith("live_loop_generation_exit_pending:")
    assert evidence.threads == []
