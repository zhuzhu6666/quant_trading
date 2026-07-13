"""Scheduler job registration helpers for the live backend."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable

from backend.services.live_data_sync_helpers import dataframe_to_store_bars


Runner = Callable[..., Any]
ThreadFactory = Callable[..., Any]
SleepFn = Callable[[float], Any]
MonotonicFn = Callable[[], float]
TimerFactory = Callable[..., Any]


def _python_executable() -> str:
    return sys.executable or "python"


def _default_data_store_factory():
    from data.store import DataStore

    return DataStore()


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
                (480.0, "evolution_hourly"),
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


def make_initial_ctrader_data_pull(
    *,
    get_ctrader: Callable[[], tuple[Any, str | None, bool]],
    logger,
    data_store_factory: Callable[[], Any] = _default_data_store_factory,
    sleep_fn: SleepFn = time.sleep,
    now_fn: Callable[[], float] = time.time,
    default_timeframes: list[str] | None = None,
):
    """Build the legacy startup cTrader bar pull function."""

    def _initial_ctrader_data_pull(timeframes=None, n_bars: int = 5000, phase: str = "startup"):
        """启动后立即从 cTrader 拉最近数据写入 DB."""
        selected_timeframes = list(timeframes or default_timeframes or ["M1", "M5", "M15", "M30", "H1", "H4", "D1"])
        try:
            bridge, err, warming = get_ctrader()
            if err:
                logger.warning("[init:{}] cTrader bridge unavailable: {}, skip initial pull", phase, err)
                return
            if warming:
                # bridge 还在后台连接中, 等最多 30s
                t0 = now_fn()
                while now_fn() - t0 < 30:
                    if bridge.is_connected:
                        break
                    sleep_fn(1)
                if not bridge.is_connected:
                    logger.warning("[init:{}] cTrader bridge not connected after 30s, skip initial pull", phase)
                    return
            # 启动阶段优先保障交易所需周期, 其它周期延后错峰补齐.
            store = data_store_factory()
            for tf in selected_timeframes:
                try:
                    df = None
                    for attempt in range(2):
                        df = bridge.fetch_bars(tf, n_bars=n_bars)
                        if df is not None and not df.empty:
                            break
                        if attempt == 0:
                            sleep_fn(1.0)
                    if df is None or df.empty:
                        logger.warning("[init:{}] {} pull returned empty", phase, tf)
                        continue
                    bars = dataframe_to_store_bars(df)
                    store.insert_bars(bars, "XAUUSD+", tf)
                    logger.info(
                        "[init:{}] {}: +{} bars ({} → {})",
                        phase,
                        tf,
                        len(bars),
                        time.strftime("%m-%d %H:%M", time.gmtime(bars[0]["time"])),
                        time.strftime("%m-%d %H:%M", time.gmtime(bars[-1]["time"])),
                    )
                except Exception as e:
                    logger.warning("[init:{}] {} pull failed: {}", phase, tf, e)
            logger.info("[init:{}] ✅ cTrader 初始数据补充完成", phase)
        except Exception as exc:
            logger.warning("[init:{}] initial pull failed: {}", phase, exc)

    return _initial_ctrader_data_pull


def start_initial_ctrader_data_pull(
    pull_func,
    *,
    thread_factory: ThreadFactory = threading.Thread,
    timer_factory: TimerFactory = threading.Timer,
):
    """Start the legacy fast and deferred startup cTrader data pulls."""

    thread_factory(
        target=lambda: pull_func(["M1", "M5"], n_bars=1200, phase="fast"),
        daemon=True,
        name="init-ctrader-fast",
    ).start()
    deferred_timer = timer_factory(
        30.0,
        lambda: pull_func(["M15", "M30", "H1", "H4", "D1"], n_bars=5000, phase="deferred"),
    )
    deferred_timer.daemon = True
    deferred_timer.start()
    return deferred_timer
