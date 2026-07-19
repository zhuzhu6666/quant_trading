import threading
from types import SimpleNamespace

from backend.services.live_loop_start import (
    LiveLoopStartRuntime,
    start_live_loop,
)
from backend.services.live_loop_stop import LoopOwnershipSnapshot


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


class _Controller:
    def __init__(self, *, phase="stopped"):
        self.phase = phase
        self.generation = None
        self.bound_components = []
        self.bound_threads = []
        self.exits = []

    def status(self):
        return {
            "phase": self.phase,
            "generation": (
                self.generation.generation_id if self.generation else ""
            ),
        }

    def begin_start(self, **_kwargs):
        self.phase = "starting"
        self.generation = SimpleNamespace(
            generation_id="generation-123",
            stop_event=threading.Event(),
        )
        return self.generation

    def bind_component(self, generation_id, component):
        self.bound_components.append((generation_id, component))

    def bind_thread(self, generation_id, thread):
        self.bound_threads.append((generation_id, thread))

    def acknowledge_exit(self, generation_id, **kwargs):
        self.exits.append((generation_id, kwargs))
        self.phase = "failed"


def _runtime(
    *,
    generation_enabled=True,
    existing_thread=None,
    existing_stop=None,
    last_end=0.0,
    thread_fail=False,
    sleep=None,
):
    ownership = {
        "thread": existing_thread,
        "stop_flag": existing_stop,
        "broker": "ctrader" if existing_thread else None,
        "started_at": 900.0 if existing_thread else None,
        "strategy_name": "factor_v4" if existing_thread else None,
    }
    controller = _Controller()
    prepared = []
    reset = []
    persisted = []
    primed = []
    state_updates = []
    components = []
    threads = []

    def snapshot():
        return LoopOwnershipSnapshot(**ownership)

    def prepare(**kwargs):
        prepared.append(kwargs)
        ownership.update(
            stop_flag=kwargs["stop_flag"],
            broker=kwargs["broker"],
            started_at=kwargs["started_at"],
            strategy_name=kwargs["strategy_name"],
        )

    def reset_ownership():
        reset.append(True)
        ownership.update(
            thread=None,
            stop_flag=None,
            broker=None,
            started_at=None,
            strategy_name=None,
        )

    def thread_factory(**kwargs):
        thread = _LoopThread(fail=thread_fail, **kwargs)
        threads.append(thread)
        return thread

    def install(thread):
        ownership["thread"] = thread

    runtime = LiveLoopStartRuntime(
        generation_controller_enabled=lambda: generation_enabled,
        state_lock=threading.RLock(),
        snapshot_ownership=snapshot,
        process_shutdown_requested=lambda: False,
        controller=controller,
        last_loop_end=lambda: last_end,
        now=lambda: 1_000.0,
        sleep=(sleep or (lambda _seconds: None)),
        logger_warning=lambda *_args: None,
        logger_info=lambda *_args: None,
        event_factory=threading.Event,
        prepare_ownership=prepare,
        reset_start_ownership=reset_ownership,
        persist_desired_state=lambda *args, **kwargs: persisted.append(
            (args, kwargs)
        ),
        prime_live_loop_state=lambda **kwargs: primed.append(kwargs),
        phase2_active=lambda: generation_enabled,
        start_safety_watchdog=lambda: components.append("watchdog_start"),
        start_scheduler=lambda: components.append("scheduler_start"),
        stop_scheduler=lambda: components.append("scheduler_stop"),
        stop_safety_watchdog=lambda: components.append("watchdog_stop"),
        thread_factory=thread_factory,
        loop_target=lambda *_args: None,
        install_loop_thread=install,
        live_state_update=lambda **kwargs: state_updates.append(kwargs),
        live_state_get=lambda key, default=None: {
            "loop_running": True,
            "accepting_new_risk": True,
            "session_state_status": "available",
        }.get(key, default),
        no_new_risk_latched=lambda **_kwargs: False,
    )
    evidence = SimpleNamespace(
        ownership=ownership,
        controller=controller,
        prepared=prepared,
        reset=reset,
        persisted=persisted,
        primed=primed,
        state_updates=state_updates,
        components=components,
        threads=threads,
    )
    return runtime, evidence


def test_legacy_draining_thread_rejects_start():
    existing = _LoopThread()
    existing.started = True
    stop_event = threading.Event()
    stop_event.set()
    runtime, evidence = _runtime(
        generation_enabled=False,
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
    assert result["error"] == "live_loop_draining"
    assert evidence.threads == []


def test_generation_start_binds_components_and_owned_thread():
    runtime, evidence = _runtime(generation_enabled=True)

    result = start_live_loop(
        "ctrader",
        "factor_v4",
        persist_desired=True,
        trigger_reason="manual",
        runtime=runtime,
    )

    assert result["ok"] is True
    assert result["phase"] == "starting"
    assert result["generation"] == "generation-123"
    assert evidence.ownership["thread"] is evidence.threads[0]
    assert evidence.threads[0].started is True
    assert evidence.controller.bound_components == [
        ("generation-123", "scheduler"),
        ("generation-123", "refresh_worker_inline"),
    ]
    assert evidence.primed[0]["account_observed"] is False


def test_thread_start_failure_releases_ownership_and_marks_generation_failed():
    runtime, evidence = _runtime(
        generation_enabled=True,
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
    assert evidence.reset == [True]
    assert evidence.ownership["thread"] is None
    assert evidence.controller.exits[0][0] == "generation-123"
    assert evidence.state_updates[0]["accepting_new_risk"] is False
    assert evidence.components[-2:] == ["scheduler_stop", "watchdog_stop"]


def test_backoff_second_check_rejects_competing_start():
    runtime, evidence = _runtime(
        generation_enabled=False,
        last_end=999.0,
    )

    def competing_start(_seconds):
        thread = _LoopThread()
        thread.started = True
        evidence.ownership["thread"] = thread

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
    assert result["error"] == "another loop started during backoff wait"
    assert evidence.threads == []
