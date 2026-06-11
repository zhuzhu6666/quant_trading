"""In-process job queue + state. Single-process, in-memory, not persisted (v1)."""
import asyncio
import inspect
import json
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from backend.jobs.progress import ProgressCB
from backend.jobs.state import JobState, new_job_id


class JobManager:
    """Manages long-running tasks. v1: single process, in-memory dict + JSONL persist.

    Persisted to data/charts/jobs.jsonl so jobs survive backend restart.
    """

    PERSIST_PATH = Path("data/charts/jobs.jsonl")
    MAX_PERSISTED = 200

    def __init__(self) -> None:
        self._jobs: dict[str, JobState] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._ensure_loop()
        self._load_persisted()

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

    def _load_persisted(self) -> None:
        """Load previously persisted jobs from JSONL into self._jobs."""
        path = self.PERSIST_PATH
        if not path.exists():
            return
        try:
            lines = path.read_text(encoding="utf-8").strip().splitlines()
        except Exception:
            logger.warning("failed to read persisted jobs")
            return
        for line in lines[-self.MAX_PERSISTED:]:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            started_at = datetime.fromisoformat(data["started_at"].rstrip("Z")).replace(tzinfo=None)
            finished_at = (
                datetime.fromisoformat(data["finished_at"].rstrip("Z")).replace(tzinfo=None)
                if data.get("finished_at") else None
            )
            js = JobState(
                id=data["id"],
                kind=data["kind"],
                status=data["status"],
                progress_pct=data.get("progress_pct", 0.0),
                current_step=data.get("current_step", ""),
                started_at=started_at,
                finished_at=finished_at,
                params=data.get("params", {}),
                result=data.get("result"),
                error=data.get("error"),
                log_tail=data.get("log_tail", []),
            )
            self._jobs[js.id] = js

    def _append_persisted(self, js: JobState) -> None:
        """Append job to JSONL persist file, then trim to MAX_PERSISTED lines."""
        path = self.PERSIST_PATH
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(str(path), "a", encoding="utf-8") as f:
                f.write(json.dumps(js.to_dict(), ensure_ascii=False) + "\n")
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            if len(lines) > self.MAX_PERSISTED:
                path.write_text(
                    "\n".join(lines[-self.MAX_PERSISTED:]) + "\n",
                    encoding="utf-8",
                )
        except Exception:
            logger.warning("failed to persist job {}", js.id)

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
            self._append_persisted(js)

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
