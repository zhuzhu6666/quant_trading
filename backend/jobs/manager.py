"""In-process job queue + state. Single-process, in-memory, not persisted (v1)."""
import asyncio
import inspect
import traceback
from typing import Any, Callable

from loguru import logger

from backend.jobs.progress import ProgressCB
from backend.jobs.state import JobState, new_job_id


class JobManager:
    """Manages long-running tasks. v1: single process, in-memory dict."""

    def __init__(self) -> None:
        self._jobs: dict[str, JobState] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def submit(
        self,
        kind: str,
        params: dict[str, Any],
        fn: Callable[[ProgressCB], Any],
    ) -> JobState:
        """Queue a job. fn signature: (progress_cb) -> result (any JSON-serializable)."""
        js = JobState(id=new_job_id(), kind=kind, params=params)
        self._jobs[js.id] = js
        task = asyncio.create_task(self._run(js, fn))
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
                # Allow sync functions too (run in default executor)
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, fn, cb)

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


# Singleton accessor
_manager: JobManager | None = None


def get_job_manager() -> JobManager:
    global _manager
    if _manager is None:
        _manager = JobManager()
    return _manager
