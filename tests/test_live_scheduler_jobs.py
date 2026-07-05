from types import SimpleNamespace

import pandas as pd

from backend.services.live_scheduler_jobs import (
    make_initial_ctrader_data_pull,
    make_dukascopy_tick_job,
    make_events_sync_job,
    make_external_data_sync_job,
    register_external_sync_jobs,
    start_initial_ctrader_data_pull,
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


class _FakeStore:
    def __init__(self):
        self.inserts = []

    def insert_bars(self, bars, symbol, timeframe):
        self.inserts.append((bars, symbol, timeframe))


def _bar_df():
    return pd.DataFrame(
        [{"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 7}],
        index=pd.to_datetime(["2026-07-04T00:00:00Z"]),
    )


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
        ("dukascopy_tick", "0 * * * *"),
        ("events_sync", "0 8 * * *"),
        ("cot_sync", "0 6 * * 6"),
        ("etf_sync", "0 4 1 */3 *"),
        ("fred_sync", "20 5 * * *"),
    ]


def test_dukascopy_tick_job_runs_incremental_script_and_logs_last_line(tmp_path):
    script = tmp_path / "scripts" / "debug" / "_pull_dukascopy_incremental.py"
    script.parent.mkdir(parents=True)
    script.write_text("# test\n", encoding="utf-8")
    calls = []
    logger = _FakeLogger()

    job = make_dukascopy_tick_job(
        repo_root=tmp_path,
        logger=logger,
        runner=lambda *args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace(returncode=0, stdout="first\nlast\n", stderr=""),
        python_executable="python-test",
    )

    job()

    assert calls[0][0][0] == ["python-test", str(script)]
    assert calls[0][1]["timeout"] == 180
    assert logger.messages[-1] == ("info", "[dukascopy_tick] {}", ("last",))


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


def test_startup_catch_up_jobs_preserves_legacy_light_and_heavy_order():
    immediate, deferred = startup_catch_up_jobs(run_heavy_jobs=False)

    assert immediate == ["data_sync", "dukascopy_tick", "events_sync"]
    assert deferred == [(300.0, "cot_sync"), (360.0, "etf_sync")]

    _immediate_heavy, deferred_heavy = startup_catch_up_jobs(run_heavy_jobs=True)
    assert deferred_heavy == [
        (300.0, "cot_sync"),
        (360.0, "etf_sync"),
        (480.0, "evolution_hourly"),
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
        "dukascopy_tick",
        "events_sync",
        "cot_sync",
        "etf_sync",
    ]
    assert sleeps == [300.0, 30.0, 30.0, 30.0]


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
        "dukascopy_tick",
        "events_sync",
        "cot_sync",
        "etf_sync",
        "evolution_hourly",
        "awe_adapt",
        "feature_eng",
    ]


def test_initial_ctrader_data_pull_writes_store_bars_for_requested_timeframes():
    store = _FakeStore()
    logger = _FakeLogger()
    fetch_calls = []

    class _Bridge:
        is_connected = True

        def fetch_bars(self, timeframe, n_bars):
            fetch_calls.append((timeframe, n_bars))
            return _bar_df()

    pull = make_initial_ctrader_data_pull(
        get_ctrader=lambda: (_Bridge(), None, False),
        logger=logger,
        data_store_factory=lambda: store,
    )

    pull(["M1", "M5"], n_bars=1200, phase="fast")

    assert fetch_calls == [("M1", 1200), ("M5", 1200)]
    assert [tf for _bars, _symbol, tf in store.inserts] == ["M1", "M5"]
    assert store.inserts[0][0][0] == {
        "time": 1783123200,
        "open": 1.0,
        "high": 2.0,
        "low": 0.5,
        "close": 1.5,
        "volume": 7,
        "spread": 0,
    }
    assert logger.messages[-1] == ("info", "[init:{}] ✅ cTrader 初始数据补充完成", ("fast",))


def test_initial_ctrader_data_pull_waits_for_warming_bridge_then_skips_after_timeout():
    logger = _FakeLogger()
    sleeps = []
    clock = {"now": 0.0}

    class _Bridge:
        is_connected = False

        def fetch_bars(self, timeframe, n_bars):
            raise AssertionError("fetch_bars should not run when bridge never connects")

    def _sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds

    pull = make_initial_ctrader_data_pull(
        get_ctrader=lambda: (_Bridge(), None, True),
        logger=logger,
        sleep_fn=_sleep,
        now_fn=lambda: clock["now"],
    )

    pull(["M1"], n_bars=1200, phase="fast")

    assert len(sleeps) == 30
    assert logger.messages[-1] == (
        "warning",
        "[init:{}] cTrader bridge not connected after 30s, skip initial pull",
        ("fast",),
    )


def test_start_initial_ctrader_data_pull_preserves_fast_and_deferred_schedule():
    calls = []
    thread_names = []
    timers = []

    class _ImmediateThread:
        def __init__(self, *, target, name, daemon):
            self.target = target
            self.name = name
            self.daemon = daemon

        def start(self):
            thread_names.append((self.name, self.daemon))
            self.target()

    class _ImmediateTimer:
        def __init__(self, interval, function):
            self.interval = interval
            self.function = function
            self.daemon = False

        def start(self):
            timers.append((self.interval, self.daemon))
            self.function()

    def _pull(timeframes, n_bars, phase):
        calls.append((timeframes, n_bars, phase))

    timer = start_initial_ctrader_data_pull(
        _pull,
        thread_factory=_ImmediateThread,
        timer_factory=_ImmediateTimer,
    )

    assert thread_names == [("init-ctrader-fast", True)]
    assert timers == [(30.0, True)]
    assert calls == [
        (["M1", "M5"], 1200, "fast"),
        (["M15", "M30", "H1", "H4", "D1"], 5000, "deferred"),
    ]
    assert timer.daemon is True
