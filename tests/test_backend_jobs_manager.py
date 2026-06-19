"""JobManager: submit, get, list, cancel, progress emission."""
import asyncio
from pathlib import Path

import pytest

from backend.jobs.manager import JobManager
from backend.jobs.progress import ProgressCB


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
