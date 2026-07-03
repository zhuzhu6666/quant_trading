"""In-process job queue + state. Single-process, in-memory, not persisted (v1)."""
import asyncio
import inspect
import json
import threading
import traceback
import weakref
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from backend.jobs.progress import ProgressCB
from backend.jobs.state import JobState, new_job_id


def _state_conn():
    from backend.core.db import get_state_pg_conn

    return get_state_pg_conn()


def _state_sql(sql: str) -> str:
    return sql.replace("%", "%%").replace("?", "%s")


class JobManager:
    """Manages long-running tasks. v1: single process, in-memory dict + JSONL persist.

    Persisted to data/charts/jobs.jsonl so jobs survive backend restart.
    """

    PERSIST_PATH = Path("data/charts/jobs.jsonl")
    MAX_PERSISTED = 200
    _instances: "weakref.WeakSet[JobManager]" = weakref.WeakSet()

    def __init__(self) -> None:
        self._jobs: dict[str, JobState] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._owns_loop = False
        self._lock = threading.Lock()
        self._instances.add(self)
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
                    try:
                        loop.run_until_complete(loop.shutdown_asyncgens())
                        loop.run_until_complete(loop.shutdown_default_executor())
                    except Exception:
                        pass
                    loop.close()

            t = threading.Thread(target=runner, name="JobManagerLoop", daemon=True)
            t.start()
            self._thread = t
            self._loop = loop
            self._owns_loop = True

    def _load_persisted(self) -> None:
        """从 PostgreSQL state store 加载持久化 jobs。降级到 JSONL。"""
        try:
            conn = _state_conn()
            try:
                rows = conn.execute(
                    _state_sql("SELECT * FROM jobs WHERE status IN ('running','pending') ORDER BY updated_at DESC LIMIT ?"),
                    (self.MAX_PERSISTED,)
                ).fetchall()
                for r in rows:
                    try:
                        params = json.loads(r["params_json"]) if r["params_json"] else {}
                        result = json.loads(r["result_json"]) if r["result_json"] else None
                        js = JobState(
                            id=r["id"], kind=r["kind"], status=r["status"],
                            progress_pct=r["progress"] or 0.0,
                            started_at=datetime.fromtimestamp(r["created_at"], tz=timezone.utc) if r["created_at"] else datetime.now(timezone.utc),
                            params=params, result=result, error=r["error"] or "",
                        )
                        self._jobs[js.id] = js
                    except Exception:
                        continue
                if self._jobs:
                    return
            finally:
                conn.close()
        except Exception:
            pass

        # 降级: JSONL
        path = self.PERSIST_PATH
        if not path.exists():
            return
        try:
            lines = path.read_text(encoding="utf-8").strip().splitlines()
        except Exception:
            return
        for line in lines[-self.MAX_PERSISTED:]:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                started_at = datetime.fromisoformat(data["started_at"].rstrip("Z")).replace(tzinfo=timezone.utc)
                finished_at = (
                    datetime.fromisoformat(data["finished_at"].rstrip("Z")).replace(tzinfo=timezone.utc)
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
            except (KeyError, ValueError, TypeError, AttributeError):
                logger.warning("skipping malformed persisted job line: %s", line[:120])
                continue

    def _append_persisted(self, js: JobState, *, status: str | None = None) -> None:
        """持久化 job 到 PostgreSQL state store + JSONL 备份."""
        status_value = status or js.status
        payload = js.to_dict()
        payload["status"] = status_value
        # 主存储: PostgreSQL state store
        try:
            conn = _state_conn()
            try:
                now = js.finished_at.timestamp() if js.finished_at else None
                created = js.started_at.timestamp() if js.started_at else None
                conn.execute(
                    _state_sql("""
                    INSERT INTO jobs
                    (id, kind, status, params_json, result_json, progress, error, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        kind=excluded.kind,
                        status=excluded.status,
                        params_json=excluded.params_json,
                        result_json=excluded.result_json,
                        progress=excluded.progress,
                        error=excluded.error,
                        created_at=excluded.created_at,
                        updated_at=excluded.updated_at
                    """),
                    (js.id, js.kind, status_value,
                     json.dumps(js.params, ensure_ascii=False),
                     json.dumps(js.result, ensure_ascii=False) if js.result else "{}",
                     js.progress_pct, js.error or "", created, now)
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass
        # 降级: JSONL
        path = self.PERSIST_PATH
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(str(path), "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
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
            self._owns_loop = False

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
        terminal_status = "done"
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
        except asyncio.CancelledError:
            terminal_status = "cancelled"
            logger.info(f"job {js.id} ({js.kind}) cancelled")
        except Exception as e:
            terminal_status = "error"
            js.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()[-500:]}"
            logger.error(f"job {js.id} ({js.kind}) failed: {e}")
        finally:
            from datetime import datetime
            js.finished_at = datetime.now(timezone.utc)
            self._append_persisted(js, status=terminal_status)
            js.status = terminal_status
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

    def shutdown(self, *, timeout: float = 5.0) -> None:
        """Stop the owned background loop and its default executor.

        FastAPI may bind a manager to an application loop; that loop is owned
        by the framework, so this only stops loops created by JobManager itself.
        """
        loop = self._loop
        thread = self._thread
        if loop is None or not self._owns_loop:
            return
        for task in list(self._tasks.values()):
            try:
                if not task.done():
                    task.cancel()
            except Exception:
                pass

        async def _cancel_pending() -> None:
            current = asyncio.current_task()
            pending = [task for task in asyncio.all_tasks(loop) if task is not current and not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        if loop.is_running():
            try:
                future = asyncio.run_coroutine_threadsafe(_cancel_pending(), loop)
                future.result(timeout=timeout)
            except Exception:
                pass
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._loop = None
        self._thread = None
        self._owns_loop = False


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


def shutdown_job_managers_for_tests() -> None:
    """Shutdown every JobManager loop created in this process."""
    global _manager
    for manager in list(JobManager._instances):
        manager.shutdown()
    _manager = None
