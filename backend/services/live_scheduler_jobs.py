"""Scheduler job registration helpers for the live backend."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable

Runner = Callable[..., Any]
ThreadFactory = Callable[..., Any]
SleepFn = Callable[[float], Any]
MonotonicFn = Callable[[], float]


def _python_executable() -> str:
    return sys.executable or "python"


def _default_readiness_snapshot_service_factory():
    from backend.services.backend_readiness_snapshot import BackendReadinessSnapshotService

    return BackendReadinessSnapshotService()


def make_backend_readiness_refresh_job(
    *,
    logger,
    service_factory: Callable[[], Any] = _default_readiness_snapshot_service_factory,
    max_age_seconds: float = 30.0,
):
    """Build a single-flight refresh trigger owned by the existing scheduler."""

    def _scheduled_backend_readiness_refresh():
        try:
            service = service_factory()
            service.open_async_refresh()
            result = service.refresh_async(max_age_seconds=max_age_seconds)
            status = str(result.get("status") or "unknown")
            if status not in {
                "fresh",
                "refresh_started",
                "refresh_in_progress",
            }:
                logger.warning(
                    "[backend_readiness_refresh] unexpected status: {}", status
                )
            return result
        except Exception as exc:
            logger.warning("[backend_readiness_refresh] failed: {}", exc)
            return {
                "ok": False,
                "status": "refresh_failed",
                "error": f"{type(exc).__name__}:{exc}"[:300],
            }

    return _scheduled_backend_readiness_refresh


def register_backend_readiness_refresh_job(sched, *, logger) -> None:
    """Keep persistent readiness fresh without API/operator polling."""

    sched.add_job(
        "backend_readiness_refresh",
        "*/2 * * * *",
        make_backend_readiness_refresh_job(logger=logger),
    )


def register_factor_selection_heartbeat_job(
    sched,
    *,
    heartbeat: Callable[[], Any],
) -> None:
    """Keep the live-process factor selection projection inside its TTL."""

    sched.add_job(
        "factor_selection_heartbeat",
        "*/5 * * * *",
        heartbeat,
    )


def make_events_sync_job(
    *,
    repo_root: Path,
    logger,
    runner: Runner = subprocess.run,
    python_executable: str | None = None,
):
    script = repo_root / "scripts" / "fetch_events_calendar.py"

    def _scheduled_events_sync():
        try:
            if not script.exists():
                logger.warning("[events_sync] script not found")
                return
            result = runner(
                [python_executable or _python_executable(), str(script), "--weeks", "2"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode == 0:
                logger.info("[events_sync] ok")
            else:
                logger.warning("[events_sync] failed (rc={}): {}", result.returncode, (result.stderr or "")[:200])
        except Exception as e:
            logger.warning("[events_sync] error: {}", e)

    return _scheduled_events_sync


def make_external_data_sync_job(
    *,
    repo_root: Path,
    source: str,
    logger,
    timeout: int,
    force: bool = False,
    runner: Runner = subprocess.run,
    python_executable: str | None = None,
):
    script = repo_root / "scripts" / "refresh_external_data.py"
    label = f"{source}_sync"

    def _scheduled_external_data_sync():
        try:
            args = [python_executable or _python_executable(), str(script), "--source", source]
            if force:
                args.append("--force")
            result = runner(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if result.returncode == 0:
                logger.info("[{}] ok", label)
            else:
                logger.warning("[{}] failed (rc={}): {}", label, result.returncode, (result.stderr or "")[:200])
        except Exception as e:
            logger.warning("[{}] error: {}", label, e)

    return _scheduled_external_data_sync


def register_external_sync_jobs(
    sched,
    *,
    repo_root: Path,
    logger,
    runner: Runner = subprocess.run,
    python_executable: str | None = None,
) -> None:
    """Register external data/script jobs with the legacy names and cron specs."""

    sched.add_job(
        "events_sync",
        "0 8 * * *",
        make_events_sync_job(
            repo_root=repo_root,
            logger=logger,
            runner=runner,
            python_executable=python_executable,
        ),
    )
    sched.add_job(
        "cot_sync",
        "0 6 * * 6",
        make_external_data_sync_job(
            repo_root=repo_root,
            source="cot",
            logger=logger,
            timeout=120,
            force=True,
            runner=runner,
            python_executable=python_executable,
        ),
    )
    sched.add_job(
        "etf_sync",
        "0 4 1 */3 *",
        make_external_data_sync_job(
            repo_root=repo_root,
            source="etf",
            logger=logger,
            timeout=300,
            force=True,
            runner=runner,
            python_executable=python_executable,
        ),
    )
    sched.add_job(
        "fred_sync",
        "20 5 * * *",
        make_external_data_sync_job(
            repo_root=repo_root,
            source="fred",
            logger=logger,
            timeout=300,
            runner=runner,
            python_executable=python_executable,
        ),
    )
    sched.add_job(
        "cb_sync",
        "0 7 10 * *",
        make_external_data_sync_job(
            repo_root=repo_root,
            source="cb",
            logger=logger,
            timeout=120,
            runner=runner,
            python_executable=python_executable,
        ),
    )
    sched.add_job(
        "etf_daily_sync",
        "30 4 * * *",
        make_external_data_sync_job(
            repo_root=repo_root,
            source="etf_daily",
            logger=logger,
            timeout=120,
            runner=runner,
            python_executable=python_executable,
        ),
    )


def startup_catch_up_jobs(*, run_heavy_jobs: bool) -> tuple[list[str], list[tuple[float, str]]]:
    """Return legacy startup catch-up job order."""

    immediate_jobs = [
        "data_sync",
        "events_sync",
    ]
    # External refreshers are cron-owned and self-throttled.  Running them on
    # every backend restart caused overlapping SEC/CFTC jobs and DuckDB locks.
    deferred_jobs = []
    if run_heavy_jobs:
        deferred_jobs.extend(
            [
                (720.0, "awe_adapt"),
                (1200.0, "feature_eng"),
            ]
        )
    return immediate_jobs, deferred_jobs


def start_scheduler_catch_up(
    sched,
    *,
    run_heavy_jobs: bool,
    logger,
    thread_factory: ThreadFactory = threading.Thread,
    sleep_fn: SleepFn = time.sleep,
    monotonic_fn: MonotonicFn = time.monotonic,
) -> None:
    """Start legacy startup catch-up threads for light and deferred jobs."""

    def _catch_up_all_jobs():
        def _run_job(name: str):
            try:
                logger.info("[catch-up] running {} ...", name)
                sched.run_job_now(name)
                logger.info("[catch-up] {} done", name)
            except Exception as e:
                logger.warning("[catch-up] {} failed: {}\n{}", name, e, traceback.format_exc()[-200:])

        immediate_jobs, deferred_jobs = startup_catch_up_jobs(run_heavy_jobs=run_heavy_jobs)
        for name in immediate_jobs:
            _run_job(name)
        started_at = monotonic_fn()

        def _run_deferred_jobs_serially():
            for delay_sec, name in deferred_jobs:
                remain = delay_sec - (monotonic_fn() - started_at)
                if remain > 0:
                    logger.info("[catch-up] defer {} by {}s", name, int(remain))
                    sleep_fn(remain)
                _run_job(name)
                # 给实时交易和 API 留一点喘息空间, 避免补跑任务背靠背长期霸占 CPU
                sleep_fn(30.0)

        thread_factory(
            target=_run_deferred_jobs_serially,
            name="scheduler_catch_up_deferred",
            daemon=True,
        ).start()

    thread_factory(
        target=_catch_up_all_jobs,
        name="scheduler_catch_up",
        daemon=True,
    ).start()
