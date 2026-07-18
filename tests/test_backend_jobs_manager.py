"""JobManager: submit, get, list, cancel, progress emission."""
import asyncio
from pathlib import Path
import re
import threading

import pytest

from backend.jobs import manager as manager_module
from backend.jobs.manager import JobManager
from backend.jobs.progress import ProgressCB
from backend.jobs.state import JobState


@pytest.fixture(autouse=True)
def _clean_persisted_jobs():
    """Remove persisted jobs file before each test to ensure isolation."""
    persist_path = Path("data/charts/jobs.jsonl")
    if persist_path.exists():
        persist_path.unlink()
    yield


@pytest.mark.asyncio
async def test_submit_and_complete_sync_job():
    mgr = JobManager()

    def fn(cb: ProgressCB):
        cb("loading", 10, "loading 100 bars")
        cb("eval", 50, "evaluating")
        cb("done", 100, "complete")
        return {"trades": 5, "pnl": 12.5}

    js = mgr.submit("backtest", {"tf": "M15"}, fn)
    # 同步 job 立即执行, status 可能为 "queued" 或 "running"
    assert js.status in ("queued", "running")
    # Wait for completion
    for _ in range(50):
        await asyncio.sleep(0.05)
        if js.status in ("done", "error", "cancelled"):
            break
    assert js.status == "done"
    assert js.progress_pct == 100.0
    assert js.result == {"trades": 5, "pnl": 12.5}


@pytest.mark.asyncio
async def test_submit_and_complete_async_job():
    mgr = JobManager()

    async def fn(cb: ProgressCB):
        await asyncio.sleep(0.01)
        cb("step1", 50, "half")
        await asyncio.sleep(0.01)
        cb("step2", 100, "done")
        return {"ok": True}

    js = mgr.submit("discover", {"n": 100}, fn)
    for _ in range(50):
        await asyncio.sleep(0.05)
        if js.status in ("done", "error", "cancelled"):
            break
    assert js.status == "done"


@pytest.mark.asyncio
async def test_progress_emitted_in_log_tail():
    mgr = JobManager()

    def fn(cb: ProgressCB):
        for i in range(5):
            cb("eval", i * 20, f"step {i}")
        return {}

    js = mgr.submit("backtest", {}, fn)
    for _ in range(50):
        await asyncio.sleep(0.05)
        if js.status in ("done", "error", "cancelled"):
            break
    assert len(js.log_tail) == 5
    assert "step 0" in js.log_tail[0]
    assert "step 4" in js.log_tail[4]


@pytest.mark.asyncio
async def test_cancel_long_job():
    mgr = JobManager()

    async def long_fn(cb: ProgressCB):
        for i in range(100):
            await asyncio.sleep(0.05)
            cb("loop", i, f"iter {i}")
        return {}

    js = mgr.submit("backtest", {}, long_fn)
    await asyncio.sleep(0.1)  # let it start
    assert js.status == "running"
    cancelled = mgr.cancel(js.id)
    assert cancelled is True
    for _ in range(50):
        await asyncio.sleep(0.05)
        if js.status in ("cancelled", "done", "error"):
            break
    assert js.status == "cancelled"


@pytest.mark.asyncio
async def test_list_filters_by_kind_and_status():
    mgr = JobManager()

    def quick_fn(cb):
        return {}

    js1 = mgr.submit("backtest", {}, quick_fn)
    js2 = mgr.submit("discover", {}, quick_fn)
    # wait both
    for _ in range(50):
        await asyncio.sleep(0.05)
        if js1.status == "done" and js2.status == "done":
            break
    assert mgr.list(kind="backtest") == [js1]
    assert mgr.list(kind="discover") == [js2]
    assert len(mgr.list(status="done")) == 2
    assert mgr.list(status="running") == []


def test_get_returns_none_for_missing():
    mgr = JobManager()
    assert mgr.get("nonexistent") is None


def test_constructor_never_spawns_an_implicit_event_loop_thread():
    before = {thread.ident for thread in threading.enumerate()}

    mgr = JobManager(persistent_enabled=True, persistent_queue=object())

    after_threads = [thread for thread in threading.enumerate() if thread.ident not in before]
    assert mgr.has_implicit_loop_thread is False
    assert not hasattr(mgr, "_thread")
    assert not any(thread.name == "JobManagerLoop" for thread in after_threads)


def test_compatibility_boot_reads_jobs_through_read_only_connection(monkeypatch):
    calls = []

    def connect(*, read_only=False):
        calls.append(read_only)
        raise RuntimeError("isolated_test_no_database")

    monkeypatch.setattr(manager_module, "_state_conn", connect)

    JobManager(persistent_enabled=False)

    assert calls == [True]


def test_persistent_heavy_submit_enqueues_without_running_api_closure():
    class _Queue:
        def __init__(self):
            self.calls = []

        def enqueue(self, kind, params, **kwargs):
            self.calls.append((kind, dict(params), dict(kwargs)))
            return JobState(id="durable-1", kind=kind, params=dict(params))

    queue = _Queue()
    closure_called = False

    def api_process_closure(_progress):
        nonlocal closure_called
        closure_called = True
        return {"unsafe": True}

    mgr = JobManager(persistent_enabled=True, persistent_queue=queue)
    state = mgr.submit(
        "backtest",
        {"symbol": "XAUUSD+", "_idempotency_key": "request-1", "_max_attempts": 4},
        api_process_closure,
    )

    assert state.id == "durable-1"
    assert closure_called is False
    assert mgr.local_executor_started is False
    assert queue.calls == [
        (
            "backtest",
            {"symbol": "XAUUSD+", "_idempotency_key": "request-1", "_max_attempts": 4},
            {"idempotency_key": "request-1", "priority": 0, "max_attempts": 4},
        )
    ]


def test_external_refresh_is_a_persistent_heavy_job_kind():
    assert "external_refresh" in JobManager.PERSISTENT_JOB_KINDS


def test_every_production_literal_job_submission_has_a_persistent_worker_handler():
    root = Path(__file__).resolve().parents[1]
    submitted: set[str] = set()
    pattern = re.compile(r"\.submit\(\s*[\"']([a-z0-9_]+)[\"']")
    for path in (root / "backend").rglob("*.py"):
        submitted.update(pattern.findall(path.read_text(encoding="utf-8")))

    from backend.jobs.handlers import PERSISTENT_JOB_HANDLERS

    assert submitted == JobManager.PERSISTENT_JOB_KINDS
    assert submitted == set(PERSISTENT_JOB_HANDLERS)


@pytest.mark.asyncio
async def test_sync_local_jobs_use_owned_bounded_executor_and_shutdown_once():
    class _Executor:
        def __init__(self):
            self.inner = __import__("concurrent.futures").futures.ThreadPoolExecutor(
                max_workers=2,
                thread_name_prefix="pytest-owned-job-manager",
            )
            self.shutdown_calls = []

        def submit(self, *args, **kwargs):
            return self.inner.submit(*args, **kwargs)

        def shutdown(self, wait=True, *, cancel_futures=False):
            self.shutdown_calls.append((wait, cancel_futures))
            return self.inner.shutdown(wait=wait, cancel_futures=cancel_futures)

    executor = _Executor()
    mgr = JobManager(
        persistent_enabled=False,
        local_executor_factory=lambda: executor,
    )
    mgr._append_persisted = lambda *_args, **_kwargs: None
    manager_module._manager = mgr

    first_started = threading.Event()
    second_started = threading.Event()
    release = threading.Event()

    def first(_cb):
        first_started.set()
        assert release.wait(2.0)
        return {"worker": 1}

    def second(_cb):
        second_started.set()
        assert release.wait(2.0)
        return {"worker": 2}

    first_job = mgr.submit("local_light", {}, first)
    second_job = mgr.submit("local_light", {}, second)
    for _ in range(100):
        if first_started.is_set() and second_started.is_set():
            break
        await asyncio.sleep(0.01)
    assert first_started.is_set()
    assert second_started.is_set()
    assert mgr.local_executor_started is True
    release.set()
    for _ in range(100):
        if first_job.status == second_job.status == "done":
            break
        await asyncio.sleep(0.01)
    assert first_job.status == second_job.status == "done"

    first_shutdown = mgr.shutdown()
    second_shutdown = mgr.shutdown()

    assert first_shutdown["status"] == "completed"
    assert second_shutdown["status"] == "idle"
    assert executor.shutdown_calls == [(True, True)]
    with pytest.raises(RuntimeError, match="job_manager_draining"):
        mgr.submit("local_light", {}, lambda _cb: {})


def test_concurrent_shutdown_has_one_executor_join_owner() -> None:
    entered = threading.Event()
    release = threading.Event()

    class _Executor:
        def __init__(self) -> None:
            self.shutdown_calls = 0

        def submit(self, *_args, **_kwargs):
            raise AssertionError("submit is not part of this shutdown test")

        def shutdown(self, wait=True, *, cancel_futures=False):
            assert wait is True
            assert cancel_futures is True
            self.shutdown_calls += 1
            entered.set()
            assert release.wait(2.0)

    executor = _Executor()
    mgr = JobManager(persistent_enabled=True, local_executor_factory=lambda: executor)
    with mgr._lock:
        mgr._local_executor = executor
    results = []
    first = threading.Thread(target=lambda: results.append(mgr.shutdown()))
    second = threading.Thread(target=lambda: results.append(mgr.shutdown()))

    first.start()
    assert entered.wait(1.0)
    second.start()
    release.set()
    first.join(2.0)
    second.join(2.0)

    assert not first.is_alive() and not second.is_alive()
    assert executor.shutdown_calls == 1
    assert sorted(result["status"] for result in results) == ["completed", "idle"]
