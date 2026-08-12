from types import SimpleNamespace

from backend.services.live_scheduler_jobs import (
    make_backend_readiness_refresh_job,
    make_events_sync_job,
    make_external_data_sync_job,
    register_external_sync_jobs,
    register_factor_selection_heartbeat_job,
    register_backend_readiness_refresh_job,
    start_scheduler_catch_up,
    startup_catch_up_jobs,
)


class _FakeScheduler:
    def __init__(self):
        self.jobs = []
        self.ran = []

    def add_job(self, name, cron, func):
        self.jobs.append((name, cron, func))

    def run_job_now(self, name):
        self.ran.append(name)


class _FakeLogger:
    def __init__(self):
        self.messages = []

    def info(self, template, *args):
        self.messages.append(("info", template, args))

    def warning(self, template, *args):
        self.messages.append(("warning", template, args))


def test_backend_readiness_refresh_job_is_periodic_and_single_flight_owned():
    sched = _FakeScheduler()
    logger = _FakeLogger()
    calls = []

    class _Service:
        def open_async_refresh(self):
            calls.append(("open", None))

        def refresh_async(self, *, max_age_seconds):
            calls.append(("refresh", max_age_seconds))
            return {"ok": True, "status": "refresh_started"}

    job = make_backend_readiness_refresh_job(
        logger=logger,
        service_factory=_Service,
    )
    result = job()
    register_backend_readiness_refresh_job(sched, logger=logger)

    assert result["status"] == "refresh_started"
    assert calls == [("open", None), ("refresh", 30.0)]
    assert [(name, cron) for name, cron, _func in sched.jobs] == [
        ("backend_readiness_refresh", "*/2 * * * *")
    ]


def test_factor_selection_heartbeat_is_registered_every_five_minutes():
    sched = _FakeScheduler()
    heartbeat = lambda: {"ok": True}

    register_factor_selection_heartbeat_job(sched, heartbeat=heartbeat)

    assert sched.jobs == [
        ("factor_selection_heartbeat", "*/5 * * * *", heartbeat)
    ]


def test_backend_readiness_refresh_job_contains_failure_without_raising():
    logger = _FakeLogger()

    def fail():
        raise RuntimeError("unavailable")

    result = make_backend_readiness_refresh_job(
        logger=logger,
        service_factory=fail,
    )()

    assert result["ok"] is False
    assert result["status"] == "refresh_failed"
    assert logger.messages[-1][1] == "[backend_readiness_refresh] failed: {}"


def test_register_external_sync_jobs_keeps_legacy_names_and_crons(tmp_path):
    sched = _FakeScheduler()

    register_external_sync_jobs(
        sched,
        repo_root=tmp_path,
        logger=_FakeLogger(),
        runner=lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
        python_executable="python-test",
    )

    assert [(name, cron) for name, cron, _func in sched.jobs] == [
        ("events_sync", "0 8 * * *"),
        ("cot_sync", "0 6 * * 6"),
        ("etf_sync", "0 4 1 */3 *"),
        ("fred_sync", "20 5 * * *"),
        ("cb_sync", "0 7 10 * *"),
        ("etf_daily_sync", "30 4 * * *"),
    ]


def test_events_sync_job_passes_two_week_window(tmp_path):
    script = tmp_path / "scripts" / "fetch_events_calendar.py"
    script.parent.mkdir(parents=True)
    script.write_text("# test\n", encoding="utf-8")
    calls = []

    job = make_events_sync_job(
        repo_root=tmp_path,
        logger=_FakeLogger(),
        runner=lambda *args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace(returncode=0, stdout="", stderr=""),
        python_executable="python-test",
    )

    job()

    assert calls[0][0][0] == ["python-test", str(script), "--weeks", "2"]
    assert calls[0][1]["timeout"] == 60


def test_external_data_sync_job_preserves_force_flag_by_source(tmp_path):
    script = tmp_path / "scripts" / "refresh_external_data.py"
    calls = []

    cot_job = make_external_data_sync_job(
        repo_root=tmp_path,
        source="cot",
        logger=_FakeLogger(),
        timeout=120,
        force=True,
        runner=lambda *args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace(returncode=0, stdout="", stderr=""),
        python_executable="python-test",
    )
    fred_job = make_external_data_sync_job(
        repo_root=tmp_path,
        source="fred",
        logger=_FakeLogger(),
        timeout=180,
        runner=lambda *args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace(returncode=0, stdout="", stderr=""),
        python_executable="python-test",
    )

    cot_job()
    fred_job()

    assert calls[0][0][0] == ["python-test", str(script), "--source", "cot", "--force"]
    assert calls[0][1]["timeout"] == 120
    assert calls[1][0][0] == ["python-test", str(script), "--source", "fred"]
    assert calls[1][1]["timeout"] == 180


def test_startup_catch_up_jobs_excludes_learning_worker_evolution_owner():
    immediate, deferred = startup_catch_up_jobs(run_heavy_jobs=False)

    assert immediate == ["data_sync", "events_sync"]
    assert deferred == []

    _immediate_heavy, deferred_heavy = startup_catch_up_jobs(run_heavy_jobs=True)
    assert deferred_heavy == [
        (720.0, "awe_adapt"),
        (1200.0, "feature_eng"),
    ]


def test_start_scheduler_catch_up_runs_immediate_then_deferred_serially():
    sched = _FakeScheduler()
    logger = _FakeLogger()
    thread_names = []
    sleeps = []
    clock = {"now": 0.0}

    class _ImmediateThread:
        def __init__(self, *, target, name, daemon):
            self.target = target
            self.name = name
            self.daemon = daemon

        def start(self):
            thread_names.append((self.name, self.daemon))
            self.target()

    def _sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds

    start_scheduler_catch_up(
        sched,
        run_heavy_jobs=False,
        logger=logger,
        thread_factory=_ImmediateThread,
        sleep_fn=_sleep,
        monotonic_fn=lambda: clock["now"],
    )

    assert thread_names == [
        ("scheduler_catch_up", True),
        ("scheduler_catch_up_deferred", True),
    ]
    assert sched.ran == [
        "data_sync",
        "events_sync",
    ]
    assert sleeps == []


def test_start_scheduler_catch_up_includes_heavy_jobs_when_enabled():
    sched = _FakeScheduler()
    clock = {"now": 0.0}

    class _ImmediateThread:
        def __init__(self, *, target, name, daemon):
            self.target = target

        def start(self):
            self.target()

    def _sleep(seconds):
        clock["now"] += seconds

    start_scheduler_catch_up(
        sched,
        run_heavy_jobs=True,
        logger=_FakeLogger(),
        thread_factory=_ImmediateThread,
        sleep_fn=_sleep,
        monotonic_fn=lambda: clock["now"],
    )

    assert sched.ran == [
        "data_sync",
        "events_sync",
        "awe_adapt",
        "feature_eng",
    ]
