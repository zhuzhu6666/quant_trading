import threading
from types import SimpleNamespace

from backend.services.live_loop_controller import LiveLoopController
from backend.services.live_loop_stop import (
    LiveLoopStopRuntime,
    stop_live_loop,
)


class _OwnedThread:
    ident = 77

    def __init__(self):
        self.joined = False

    def is_alive(self):
        return not self.joined

    def join(self):
        self.joined = True


class _CleanupThread:
    def __init__(self, *, target, name, daemon):
        self.target = target
        self.name = name
        self.daemon = daemon
        self.started = False

    def start(self):
        self.started = True


def _runtime(*, persist=None):
    owned = _OwnedThread()
    controller = LiveLoopController(clock=lambda: 1_000.0)
    generation = controller.begin_start(
        broker="ctrader",
        strategy_name="factor_v4",
    )
    controller.bind_thread(generation.generation_id, owned)
    cleanup_threads = []
    state_updates = []
    kv_updates = []
    persisted_fail_closed = []

    def thread_factory(**kwargs):
        thread = _CleanupThread(**kwargs)
        cleanup_threads.append(thread)
        return thread

    runtime = LiveLoopStopRuntime(
        state_lock=threading.RLock(),
        controller=controller,
        admission_lock=threading.RLock(),
        live_state_update=lambda **kwargs: state_updates.append(kwargs),
        persist_desired_state=(persist or (lambda *_args, **_kwargs: None)),
        runtime_kv_set=lambda *args: kv_updates.append(args),
        last_shutdown_key="live.loop.last_shutdown",
        now=lambda: 1_000.0,
        thread_factory=thread_factory,
        persist_safety_fail_closed=lambda **kwargs: (
            persisted_fail_closed.append(kwargs)
        ),
        logger_info=lambda *_args: None,
    )
    evidence = SimpleNamespace(
        owned=owned,
        stop_flag=generation.stop_event,
        generation=generation,
        controller=controller,
        cleanup_threads=cleanup_threads,
        state_updates=state_updates,
        kv_updates=kv_updates,
        persisted_fail_closed=persisted_fail_closed,
    )
    return runtime, evidence


def test_stop_retains_ownership_until_join_completes():
    runtime, evidence = _runtime()

    result = stop_live_loop(
        persist_desired=True,
        trigger_reason="manual",
        runtime=runtime,
    )

    assert result["phase"] == "draining"
    assert result["generation"] == evidence.generation.generation_id
    assert evidence.controller.status()["phase"] == "draining"
    assert evidence.controller.ownership_snapshot().thread is evidence.owned
    assert evidence.cleanup_threads[0].started is True

    evidence.cleanup_threads[0].target()
    assert evidence.owned.joined is True
    assert evidence.controller.ownership_snapshot().thread is None


def test_desired_state_failure_does_not_cancel_draining():
    def unavailable(*_args, **_kwargs):
        raise RuntimeError("postgres unavailable")

    runtime, evidence = _runtime(
        persist=unavailable,
    )

    result = stop_live_loop(
        persist_desired=True,
        trigger_reason="manual",
        runtime=runtime,
    )

    assert result["status"] == "draining"
    assert evidence.stop_flag.is_set() is True
    assert evidence.persisted_fail_closed[0]["blockers"] == [
        "loop_desired_state_persist_failed"
    ]
