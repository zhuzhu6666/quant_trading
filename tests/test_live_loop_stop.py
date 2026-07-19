import threading
from types import SimpleNamespace

from backend.services.live_loop_stop import (
    LiveLoopStopRuntime,
    LoopOwnershipSnapshot,
    stop_live_loop,
)


class _OwnedThread:
    ident = 77

    def __init__(self):
        self.joined = False

    def is_alive(self):
        return True

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


class _Controller:
    def __init__(self, generation=None):
        self.generation = generation
        self.stop_requests = []

    def current(self):
        return self.generation

    def request_stop(self, generation_id):
        self.stop_requests.append(generation_id)

    def status(self):
        return {
            "phase": "draining" if self.stop_requests else "running",
            "generation": (
                self.generation.generation_id if self.generation else ""
            ),
        }


def _runtime(*, generation_enabled, persist=None):
    owned = _OwnedThread()
    stop_flag = threading.Event()
    ownership = {
        "thread": owned,
        "stop_flag": stop_flag,
        "broker": "ctrader",
        "started_at": 900.0,
        "strategy_name": "factor_v4",
    }
    generation = SimpleNamespace(generation_id="generation-123")
    controller = _Controller(generation if generation_enabled else None)
    cleanup_threads = []
    state_updates = []
    kv_updates = []
    clear_calls = []
    persisted_fail_closed = []

    def snapshot():
        return LoopOwnershipSnapshot(**ownership)

    def clear(thread, finished_at):
        clear_calls.append((thread, finished_at))
        if ownership["thread"] is not thread:
            return False
        ownership["thread"] = None
        return True

    def thread_factory(**kwargs):
        thread = _CleanupThread(**kwargs)
        cleanup_threads.append(thread)
        return thread

    runtime = LiveLoopStopRuntime(
        generation_controller_enabled=lambda: generation_enabled,
        state_lock=threading.RLock(),
        snapshot_ownership=snapshot,
        clear_ownership_if=clear,
        ensure_stop_event=lambda: stop_flag,
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
        stop_flag=stop_flag,
        ownership=ownership,
        controller=controller,
        cleanup_threads=cleanup_threads,
        state_updates=state_updates,
        kv_updates=kv_updates,
        clear_calls=clear_calls,
        persisted_fail_closed=persisted_fail_closed,
    )
    return runtime, evidence


def test_generation_stop_retains_ownership_until_join_completes():
    runtime, evidence = _runtime(generation_enabled=True)

    result = stop_live_loop(
        persist_desired=True,
        trigger_reason="manual",
        runtime=runtime,
    )

    assert result["phase"] == "draining"
    assert evidence.controller.stop_requests == ["generation-123"]
    assert evidence.ownership["thread"] is evidence.owned
    assert evidence.clear_calls == []
    assert evidence.cleanup_threads[0].started is True

    evidence.cleanup_threads[0].target()
    assert evidence.owned.joined is True
    assert evidence.ownership["thread"] is None


def test_legacy_stop_linearizes_admission_and_retains_ownership():
    runtime, evidence = _runtime(generation_enabled=False)

    result = stop_live_loop(
        persist_desired=True,
        trigger_reason="manual",
        runtime=runtime,
    )

    assert result["status"] == "draining"
    assert evidence.stop_flag.is_set() is True
    assert evidence.ownership["thread"] is evidence.owned
    assert len(evidence.state_updates) == 2
    assert all(
        update["accepting_new_risk"] is False
        for update in evidence.state_updates
    )

    evidence.cleanup_threads[0].target()
    assert evidence.ownership["thread"] is None
    completed = evidence.kv_updates[-1][1]
    assert completed["status"] == "completed"
    assert completed["ownership_released"] is True


def test_legacy_desired_state_failure_does_not_cancel_draining():
    def unavailable(*_args, **_kwargs):
        raise RuntimeError("postgres unavailable")

    runtime, evidence = _runtime(
        generation_enabled=False,
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
