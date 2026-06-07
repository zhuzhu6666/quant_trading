"""In-process job queue + state. Single-process, in-memory, not persisted (v1)."""
import asyncio
import inspect
import threading
import traceback
from typing import Any, Callable

from loguru import logger

from backend.jobs.progress import ProgressCB
from backend.jobs.state import JobState, new_job_id


class JobManager:
    """Manages long-running tasks. v1: single process, in-memory dict.

    Runs a dedicated background event loop in its own thread, so submit()
    works from any caller (main thread, FastAPI threadpool, sync test
    harness) without needing the caller's thread to have a running loop.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, JobState] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._ensure_loop()

    def _ensure_loop(self) -> None:
        with self._lock:
            if self._loop is not None:
                return
            loop = asyncio.new_event_loop()

            def runner() -> None:
                asyncio.set_event_loop(loop)
                try:
                    loop.run_forever()
                finally:
                    loop.close()

            t = threading.Thread(target=runner, name="JobManagerLoop", daemon=True)
            t.start()
            self._thread = t
            self._loop = loop

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Optional: rebind to a specific loop (e.g. the FastAPI app loop).
        If not called, JobManager runs its own background loop."""
        with self._lock:
            self._loop = loop

    def submit(
        self,
        kind: str,
        params: dict[str, Any],
        fn: Callable[[ProgressCB], Any],
    ) -> JobState:
        """Queue a job. fn signature: (progress_cb) -> result (any JSON-serializable).

        Caller is responsible for binding params into fn (e.g. via a closure)
        if the service function needs them. params are also stored in JobState
        for visibility via to_dict().

        Always schedules onto self._loop (the background loop or the bound loop)
        via run_coroutine_threadsafe, so it works from any thread.
        """
        self._ensure_loop()
        js = JobState(id=new_job_id(), kind=kind, params=params)
        self._jobs[js.id] = js
        # Try to use a running loop in the calling thread first (cheaper),
        # else fall back to the background loop via run_coroutine_threadsafe.
        try:
            caller_loop = asyncio.get_running_loop()
        except RuntimeError:
            caller_loop = None
        if caller_loop is not None and caller_loop is self._loop:
            task = caller_loop.create_task(self._run(js, fn))
        else:
            assert self._loop is not None  # _ensure_loop set it
            future = asyncio.run_coroutine_threadsafe(self._run(js, fn), self._loop)
            task = _FutureShim(future)
        self._tasks[js.id] = task
        return js

    async def _run(self, js: JobState, fn: Callable[[ProgressCB], Any]) -> None:
        js.status = "running"
        try:
            def cb(step: str, pct: float, msg: str) -> None:
                js.progress_pct = max(0.0, min(100.0, pct))
                js.current_step = step
                if len(js.log_tail) >= 50:
                    js.log_tail = js.log_tail[-49:]
                js.log_tail.append(f"[{step} {pct:.0f}%] {msg}")

            if inspect.iscoroutinefunction(fn):
                result = await fn(cb)
            else:
                # Allow sync functions too (run in default executor of the job loop).
                running_loop = asyncio.get_running_loop()
                result = await running_loop.run_in_executor(None, fn, cb)

            js.result = result if isinstance(result, dict) else {"value": result}
            js.progress_pct = 100.0
            js.status = "done"
        except asyncio.CancelledError:
            js.status = "cancelled"
            logger.info(f"job {js.id} ({js.kind}) cancelled")
            raise
        except Exception as e:
            js.status = "error"
            js.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()[-500:]}"
            logger.error(f"job {js.id} ({js.kind}) failed: {e}")
        finally:
            from datetime import datetime
            js.finished_at = datetime.utcnow()
            self._tasks.pop(js.id, None)

    def get(self, job_id: str) -> JobState | None:
        return self._jobs.get(job_id)

    def list(self, kind: str | None = None, status: str | None = None) -> list[JobState]:
        out = list(self._jobs.values())
        if kind is not None:
            out = [j for j in out if j.kind == kind]
        if status is not None:
            out = [j for j in out if j.status == status]
        return sorted(out, key=lambda j: j.started_at, reverse=True)

    def cancel(self, job_id: str) -> bool:
        task = self._tasks.get(job_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True


class _FutureShim:
    """Adapter so cancel() can target both asyncio.Task and concurrent.futures.Future.

    concurrent.futures.Future (returned by run_coroutine_threadsafe) has
    .done() and .cancel() that match the asyncio.Task interface used here.
    """

    __slots__ = ("_f",)

    def __init__(self, f) -> None:
        self._f = f

    def done(self) -> bool:
        return self._f.done()

    def cancel(self) -> bool:
        return self._f.cancel()


# Singleton accessor
_manager: JobManager | None = None
_manager_lock = threading.Lock()


def get_job_manager() -> JobManager:
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = JobManager()
    return _manager
