"""backend/runtime/scheduler.py — InProcessScheduler (T14.3, 2026-06-11)

Phase 2.4 进程内 Scheduler. 包装 apscheduler.BackgroundScheduler,
当 apscheduler 未安装时降级到 threading.Timer 模式.

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

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 尝试导入 apscheduler
# ---------------------------------------------------------------------------
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    HAS_APSCHEDULER = True
except ImportError:
    HAS_APSCHEDULER = False
    logger.info("apscheduler not installed, falling back to threading.Timer")


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


# ---------------------------------------------------------------------------
# Timer-mode 任务包装
# ---------------------------------------------------------------------------
class _TimerJob:
    """threading.Timer 模式下的单个定时任务."""

    def __init__(
        self,
        name: str,
        cron_expr: str,
        fn: Callable[[], Any],
        on_error: Callable[[str, Exception], None] | None = None,
    ):
        self.name = name
        self.cron_expr = cron_expr
        self.fn = fn
        self.on_error = on_error
        self._timer: threading.Timer | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._run_count = 0
        self._error_count = 0
        self._last_error = ""
        self._last_run_time = 0.0
        self._finished_at: float = 0.0

    def _parse_interval_seconds(self) -> float:
        """从 cron 表达式推算调度间隔(秒).

        支持:
          '*/5 * * * *' → 300, '0 * * * *' → 3600,
          '0 */6 * * *' → 21600, '0 5 * * 0' → 604800.
        """
        parts = self.cron_expr.strip().split()
        if len(parts) != 5:
            return 3600.0
        minute_str, hour_str, dom_str, month_str, dow_str = parts

        # */N minute → 每 N 分钟
        if minute_str.startswith("*/"):
            try:
                n = int(minute_str[2:])
                if n > 0:
                    return float(n * 60)
            except ValueError:
                pass

        # * * * * * → 每分钟
        if minute_str == "*" and hour_str == "*":
            return 60.0

        # N * * * * → 每 N 分钟 (N > 0)
        if minute_str.isdigit() and int(minute_str) > 0 and hour_str == "*":
            return float(int(minute_str) * 60)

        # 0 */N * * * → 每 N 小时
        if minute_str == "0" and hour_str.startswith("*/"):
            try:
                n = int(hour_str[2:])
                if n > 0:
                    return float(n * 3600)
            except ValueError:
                pass

        # 0 N * * * → 每 N 小时
        if minute_str == "0" and hour_str.isdigit():
            return float(int(hour_str) * 3600)

        # Weekly: 0 H * * D → every 7 days (604800s)
        if (minute_str.isdigit() and hour_str.isdigit() and
                dom_str == "*" and month_str == "*" and dow_str.isdigit()):
            return 604800.0  # 7 days

        return 3600.0  # 默认 1 小时

    def start(self) -> None:
        if self._stop_event.is_set():
            return
        interval = self._parse_interval_seconds()
        self._schedule_next(interval)

    def _schedule_next(self, interval: float) -> None:
        if self._stop_event.is_set():
            return
        self._timer = threading.Timer(interval, self._run)
        self._timer.daemon = True
        self._timer.start()

    def _run(self) -> None:
        if self._stop_event.is_set():
            return
        try:
            self.fn()
            self._run_count += 1
            self._last_run_time = _time.time()
        except Exception as e:
            self._error_count += 1
            self._last_error = str(e)
            logger.error(f"[scheduler][{self.name}] run error: {e}")
            if self.on_error:
                self.on_error(self.name, e)
        # 重调度
        if not self._stop_event.is_set():
            interval = self._parse_interval_seconds()
            self._schedule_next(interval)
        self._finished_at = _time.time()

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None

    def run_now(self) -> None:
        """立即执行一次 (不改变定时器), 并计入 run_count。"""
        try:
            self.fn()
            self._run_count += 1
            self._last_run_time = _time.time()
        except Exception as e:
            self._error_count += 1
            self._last_error = str(e)
            logger.error(f"[scheduler][{self.name}] run_now error: {e}")
            if self.on_error:
                self.on_error(self.name, e)

    @property
    def info(self) -> JobInfo:
        next_run = (
            self._finished_at + self._parse_interval_seconds()
            if self._finished_at > 0
            else _time.time() + self._parse_interval_seconds()
        )
        return JobInfo(
            name=self.name,
            cron_expr=self.cron_expr,
            running=not self._stop_event.is_set(),
            next_run_time=next_run,
            last_run_time=self._last_run_time,
            run_count=self._run_count,
            error_count=self._error_count,
            last_error=self._last_error,
        )


# ---------------------------------------------------------------------------
# InProcessScheduler
# ---------------------------------------------------------------------------
class InProcessScheduler:
    """进程内 Scheduler.

    包装 apscheduler.BackgroundScheduler, 降级到 threading.Timer.
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

        if HAS_APSCHEDULER:
            self._apscheduler = BackgroundScheduler(
                daemon=True,
                timezone="UTC",
            )
            self._apscheduler.add_listener(self._aps_listener, mask=0xFFFF)
            self._jobs_aps: dict[str, str] = {}  # name -> job_id
            logger.info("[InProcessScheduler] using apscheduler backend")
        else:
            self._apscheduler = None
            self._jobs_timer: dict[str, _TimerJob] = {}
            logger.info("[InProcessScheduler] using threading.Timer fallback backend")

    # ── 生命周期 ────────────────────────────────────────────────────────

    def start(self) -> None:
        """启动 Scheduler."""
        with self._lock:
            if self._started:
                logger.warning("[InProcessScheduler] already started")
                return
            if HAS_APSCHEDULER and self._apscheduler:
                self._apscheduler.start()
            else:
                # audit v9: 修复 Timer job 完全不启动的 bug
                # add_job 时 _started 还是 False, 所以 job.start() 没被调
                # 必须在 start() 里补启动所有已注册的 Timer job
                for job in self._jobs_timer.values():
                    job.start()
            self._started = True
            logger.info("[InProcessScheduler] started")

    def stop(self, wait: bool = True) -> None:
        """停止 Scheduler."""
        with self._lock:
            if not self._started:
                return
            if HAS_APSCHEDULER and self._apscheduler:
                self._apscheduler.shutdown(wait=wait)
            else:
                for job in list(self._jobs_timer.values()):
                    job.stop()
                self._jobs_timer.clear()
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
            if HAS_APSCHEDULER and self._apscheduler:
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
                    )
                    self._jobs_aps[name] = job.id
                except Exception as e:
                    logger.error(f"[InProcessScheduler] add_job({name}) failed: {e}")
                    return False
            else:
                if name in self._jobs_timer:
                    logger.warning(f"[InProcessScheduler] job {name} already exists")
                    return False
                job = _TimerJob(name, cron_expr, fn)
                if self._started:
                    job.start()
                self._jobs_timer[name] = job
            logger.info(f"[InProcessScheduler] add_job {name} ({cron_expr})")
            return True

    def run_job_now(self, name: str) -> bool:
        """立即执行指定任务一次, 计入 run_count。"""
        with self._lock:
            if HAS_APSCHEDULER and self._apscheduler:
                job_id = self._jobs_aps.get(name)
                if job_id is None:
                    logger.warning(f"[InProcessScheduler] job {name} not found")
                    return False
                try:
                    job = self._apscheduler.get_job(job_id)
                    if job:
                        job.func()
                except Exception as e:
                    logger.error(f"[InProcessScheduler] run_job_now({name}) failed: {e}")
                    return False
                return True
            else:
                job = self._jobs_timer.get(name)
                if job is None:
                    logger.warning(f"[InProcessScheduler] job {name} not found")
                    return False
                job.run_now()
                return True

    def remove_job(self, name: str) -> bool:
        """移除一个定时任务."""
        with self._lock:
            if HAS_APSCHEDULER and self._apscheduler:
                job_id = self._jobs_aps.pop(name, None)
                if job_id is None:
                    logger.warning(f"[InProcessScheduler] job {name} not found")
                    return False
                self._apscheduler.remove_job(job_id)
            else:
                job = self._jobs_timer.pop(name, None)
                if job is None:
                    logger.warning(f"[InProcessScheduler] job {name} not found")
                    return False
                job.stop()
            logger.info(f"[InProcessScheduler] remove_job {name}")
            return True

    def list_jobs(self) -> list[JobInfo]:
        """列出所有任务的状态."""
        infos: list[JobInfo] = []
        with self._lock:
            if HAS_APSCHEDULER and self._apscheduler:
                for name, job_id in self._jobs_aps.items():
                    try:
                        job = self._apscheduler.get_job(job_id)
                        if job:
                            nrt = (
                                job.next_run_time.timestamp()
                                if job.next_run_time
                                else 0.0
                            )
                            infos.append(JobInfo(
                                name=name,
                                cron_expr=str(job.trigger),
                                running=True,
                                next_run_time=nrt,
                            ))
                    except Exception:
                        infos.append(JobInfo(name=name, cron_expr="", running=False))
            else:
                for job in self._jobs_timer.values():
                    infos.append(job.info)
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
        try:
            from backend.runtime.runtime_state import RuntimeState

            event_code = event.code if hasattr(event, "code") else 0
            job_id = event.job_id if hasattr(event, "job_id") else ""
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
            if HAS_APSCHEDULER and self._apscheduler:
                for job_id in list(self._jobs_aps.values()):
                    try:
                        self._apscheduler.remove_job(job_id)
                    except Exception:
                        pass
                self._jobs_aps.clear()
            else:
                for job in self._jobs_timer.values():
                    job.stop()
                self._jobs_timer.clear()
            logger.info("[InProcessScheduler] all jobs cleared")
