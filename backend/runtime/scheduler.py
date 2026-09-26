"""backend/runtime/scheduler.py — InProcessScheduler (T14.3, 2026-06-11)

Phase 2.4 进程内 Scheduler. 包装 apscheduler.BackgroundScheduler.

用法:
    scheduler = InProcessScheduler()
    scheduler.start()
    scheduler.add_job("health_check", "0 * * * *", my_health_func)
    scheduler.list_jobs()
    scheduler.stop()
"""

from __future__ import annotations

import logging
import threading
import time as _time
from dataclasses import dataclass, field
from typing import Any, Callable

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_SUBMITTED
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)


@dataclass
class JobInfo:
    """调度任务的状态快照"""

    name: str
    cron_expr: str
    running: bool = False
    next_run_time: float = 0.0
    last_run_time: float = 0.0
    run_count: int = 0
    error_count: int = 0
    last_error: str = ""


def _next_run_epoch(job: Any) -> float:
    try:
        next_run_time = getattr(job, "next_run_time", None)
    except AttributeError:
        return 0.0
    try:
        return next_run_time.timestamp() if next_run_time else 0.0
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# InProcessScheduler
# ---------------------------------------------------------------------------
class InProcessScheduler:
    """进程内 Scheduler.

    包装 apscheduler.BackgroundScheduler.
    线程安全的单例.
    """

    _instance: InProcessScheduler | None = None
    _instance_lock = threading.Lock()

    def __new__(cls) -> InProcessScheduler:
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    obj = super().__new__(cls)
                    obj._initialized = False
                    cls._instance = obj
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        self._lock = threading.Lock()
        self._started = False

        self._apscheduler = BackgroundScheduler(
            daemon=True,
            timezone="UTC",
        )
        self._apscheduler.add_listener(self._aps_listener, mask=0xFFFF)
        self._jobs_aps: dict[str, str] = {}  # name -> job_id
        self._job_state: dict[str, JobInfo] = {}
        logger.info("[InProcessScheduler] using apscheduler backend")

    # ── 生命周期 ────────────────────────────────────────────────────────

    def start(self) -> None:
        """启动 Scheduler."""
        with self._lock:
            if self._started:
                logger.warning("[InProcessScheduler] already started")
                return
            self._apscheduler.start()
            self._started = True
            logger.info("[InProcessScheduler] started")

    def stop(self, wait: bool = True) -> None:
        """停止 Scheduler."""
        with self._lock:
            if not self._started:
                return
            self._apscheduler.shutdown(wait=wait)
            self._started = False
            logger.info("[InProcessScheduler] stopped")

    @property
    def started(self) -> bool:
        return self._started

    # ── 任务管理 ────────────────────────────────────────────────────────

    def add_job(self, name: str, cron_expr: str, fn: Callable[[], Any]) -> bool:
        """注册一个定时任务.

        Args:
            name: 任务名 (唯一)
            cron_expr: cron 表达式, 如 "0 1 * * *"
            fn: 可调用, 签名 () -> Any

        Returns:
            True 成功, False 失败 (已存在同名任务)
        """
        with self._lock:
            if name in self._jobs_aps:
                logger.warning(f"[InProcessScheduler] job {name} already exists")
                return False
            try:
                trigger = CronTrigger.from_crontab(cron_expr, timezone="UTC")
                job = self._apscheduler.add_job(
                    fn,
                    trigger=trigger,
                    name=name,
                    id=name,
                    replace_existing=False,
                    max_instances=1,
                    coalesce=True,
                    misfire_grace_time=300,
                )
                self._jobs_aps[name] = job.id
                self._job_state[name] = JobInfo(
                    name=name,
                    cron_expr=cron_expr,
                    running=True,
                    next_run_time=_next_run_epoch(job),
                )
            except Exception as e:
                logger.error(f"[InProcessScheduler] add_job({name}) failed: {e}")
                return False
            logger.info(f"[InProcessScheduler] add_job {name} ({cron_expr})")
            return True

    def run_job_now(self, name: str) -> bool:
        """立即执行指定任务一次, 计入 run_count。"""
        with self._lock:
            job_id = self._jobs_aps.get(name)
            if job_id is None:
                logger.warning(f"[InProcessScheduler] job {name} not found")
                return False
            started_at = _time.time()
            try:
                job = self._apscheduler.get_job(job_id)
                if job:
                    job.func()
                    info = self._job_state.get(name)
                    if info is None:
                        info = JobInfo(name=name, cron_expr=str(job.trigger), running=True)
                        self._job_state[name] = info
                    info.last_run_time = _time.time()
                    info.run_count += 1
                    info.last_error = ""
            except Exception as e:
                info = self._job_state.get(name)
                if info is not None:
                    info.last_run_time = started_at
                    info.error_count += 1
                    info.last_error = str(e)[:500]
                logger.error(f"[InProcessScheduler] run_job_now({name}) failed: {e}")
                return False
            return True

    def remove_job(self, name: str) -> bool:
        """移除一个定时任务."""
        with self._lock:
            job_id = self._jobs_aps.pop(name, None)
            if job_id is None:
                logger.warning(f"[InProcessScheduler] job {name} not found")
                return False
            self._apscheduler.remove_job(job_id)
            self._job_state.pop(name, None)
            logger.info(f"[InProcessScheduler] remove_job {name}")
            return True

    def list_jobs(self) -> list[JobInfo]:
        """列出所有任务的状态."""
        infos: list[JobInfo] = []
        with self._lock:
            for name, job_id in self._jobs_aps.items():
                try:
                    job = self._apscheduler.get_job(job_id)
                    if job:
                        nrt = _next_run_epoch(job)
                        state = self._job_state.get(name)
                        if state is None:
                            state = JobInfo(name=name, cron_expr=str(job.trigger), running=True)
                            self._job_state[name] = state
                        state.next_run_time = nrt
                        state.running = True
                        infos.append(JobInfo(
                            name=name,
                            cron_expr=state.cron_expr,
                            running=state.running,
                            next_run_time=nrt,
                            last_run_time=state.last_run_time,
                            run_count=state.run_count,
                            error_count=state.error_count,
                            last_error=state.last_error,
                        ))
                except Exception:
                    infos.append(JobInfo(name=name, cron_expr="", running=False))
        return infos

    def get_job(self, name: str) -> JobInfo | None:
        """查询单个任务的状态."""
        for info in self.list_jobs():
            if info.name == name:
                return info
        return None

    # ── 内部: apscheduler 事件监听 (可选可观测) ────────────────────────

    def _aps_listener(self, event: Any) -> None:
        """监听 apscheduler 事件, 记录/metrics."""
        event_code = event.code if hasattr(event, "code") else 0
        job_id = event.job_id if hasattr(event, "job_id") else ""
        if job_id and self._apscheduler:
            try:
                info = self._job_state.get(job_id)
                if info is not None:
                    job = self._apscheduler.get_job(job_id)
                    if job:
                        info.next_run_time = _next_run_epoch(job)
                    if event_code == EVENT_JOB_SUBMITTED:
                        info.running = True
                    elif event_code == EVENT_JOB_EXECUTED:
                        info.running = True
                        info.last_run_time = _time.time()
                        info.run_count += 1
                        info.last_error = ""
                    elif event_code == EVENT_JOB_ERROR:
                        info.running = True
                        info.last_run_time = _time.time()
                        info.error_count += 1
                        info.last_error = str(getattr(event, "exception", "") or "")[:500]
            except Exception:
                pass
        try:
            from backend.runtime.runtime_state import RuntimeState

            RuntimeState.shared().emit_metric("scheduler_event", {
                "code": event_code,
                "job_id": job_id,
            })
        except Exception:
            pass

    # ── 工具 ────────────────────────────────────────────────────────────

    def clear(self) -> None:
        """清空所有任务 (不停止 scheduler)."""
        with self._lock:
            for job_id in list(self._jobs_aps.values()):
                try:
                    self._apscheduler.remove_job(job_id)
                except Exception:
                    pass
            self._jobs_aps.clear()
            self._job_state.clear()
            logger.info("[InProcessScheduler] all jobs cleared")
