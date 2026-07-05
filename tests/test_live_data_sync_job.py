from types import SimpleNamespace

import pandas as pd

from backend.services.live_data_sync_helpers import BAR_FRESHNESS_THRESHOLDS
from backend.services.live_data_sync_job import make_data_sync_job


class _FakeLock:
    def __init__(self, acquired=True):
        self.acquired = acquired
        self.acquire_calls = 0
        self.release_calls = 0

    def acquire(self, blocking=True):
        self.acquire_calls += 1
        return self.acquired

    def release(self):
        self.release_calls += 1


class _FakeLogger:
    def __init__(self):
        self.messages = []

    def debug(self, template, *args):
        self.messages.append(("debug", template, args))

    def info(self, template, *args):
        self.messages.append(("info", template, args))

    def warning(self, template, *args):
        self.messages.append(("warning", template, args))


class _FakeHealth:
    def __init__(self):
        self.successes = []
        self.failures = []

    def record_success(self, **kwargs):
        self.successes.append(kwargs)

    def record_failure(self, message):
        self.failures.append(message)


class _FakeDuckConn:
    def __init__(self, path, *, bar_latest, tick_symbol_latest=0.0, tick_global_latest=0.0):
        self.path = path
        self.bar_latest = bar_latest
        self.tick_symbol_latest = tick_symbol_latest
        self.tick_global_latest = tick_global_latest
        self._last_sql = ""
        self._last_params = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        self._last_sql = sql
        self._last_params = params
        return self

    def fetchone(self):
        if self.path == "bars":
            tf = self._last_params[1]
            return (self.bar_latest.get(tf, 0.0),)
        if self._last_params:
            return (self.tick_symbol_latest,)
        return (self.tick_global_latest,)


class _FakeStore:
    def __init__(self):
        self.inserts = []

    def insert_bars(self, bars, symbol, timeframe):
        self.inserts.append((bars, symbol, timeframe))


def _duckdb_runtime(*, bar_latest, tick_symbol_latest=0.0, tick_global_latest=0.0):
    def _connect(path, snapshot_first=True):
        return _FakeDuckConn(
            path,
            bar_latest=bar_latest,
            tick_symbol_latest=tick_symbol_latest,
            tick_global_latest=tick_global_latest,
        )

    return lambda: ("bars", "ticks", _connect)


def _bar_df():
    return pd.DataFrame(
        [{"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 7}],
        index=pd.to_datetime(["2026-07-04T00:00:00Z"]),
    )


def test_data_sync_skips_when_previous_run_is_active():
    lock = _FakeLock(acquired=False)
    logger = _FakeLogger()
    health = _FakeHealth()
    called = {"bridge": False}

    job = make_data_sync_job(
        lock=lock,
        logger=logger,
        get_ctrader=lambda: called.__setitem__("bridge", True),
        market_session_snapshot=lambda _arg: {},
        health_factory=lambda: health,
    )

    job()

    assert lock.acquire_calls == 1
    assert lock.release_calls == 0
    assert called["bridge"] is False
    assert logger.messages[-1][1] == "[data_sync] previous run still active, skip overlapping trigger"


def test_data_sync_fresh_bars_skip_bridge_even_when_tick_advisory_stale():
    now = 1_000_000.0
    lock = _FakeLock()
    logger = _FakeLogger()
    health = _FakeHealth()
    bar_latest = {tf: now - 1 for tf in BAR_FRESHNESS_THRESHOLDS}
    bridge_called = False

    def _bridge():
        nonlocal bridge_called
        bridge_called = True
        return None, None, False

    job = make_data_sync_job(
        lock=lock,
        logger=logger,
        get_ctrader=_bridge,
        market_session_snapshot=lambda _arg: {},
        health_factory=lambda: health,
        config_factory=lambda: SimpleNamespace(enabled_symbols=["XAUUSD+"]),
        duckdb_runtime_factory=_duckdb_runtime(bar_latest=bar_latest, tick_symbol_latest=0.0),
        now_fn=lambda: now,
    )

    job()

    assert bridge_called is False
    assert lock.release_calls == 1
    assert health.successes == [{"last_bar_ts_by_tf": bar_latest}]


def test_data_sync_stale_bars_skip_pull_when_market_closed():
    now = 1_000_000.0
    lock = _FakeLock()
    health = _FakeHealth()
    bridge_called = False

    def _bridge():
        nonlocal bridge_called
        bridge_called = True
        return None, None, False

    job = make_data_sync_job(
        lock=lock,
        logger=_FakeLogger(),
        get_ctrader=_bridge,
        market_session_snapshot=lambda _arg: {"status": "closed_confirmed", "reason": "weekend"},
        health_factory=lambda: health,
        config_factory=lambda: SimpleNamespace(enabled_symbols=["XAUUSD+"]),
        duckdb_runtime_factory=_duckdb_runtime(bar_latest={}),
        now_fn=lambda: now,
    )

    job()

    assert bridge_called is False
    assert lock.release_calls == 1
    assert health.successes == [{"last_bar_ts_by_tf": None}]


def test_data_sync_stale_bars_pull_with_primary_bridge_and_insert_store_bars():
    now = 1_000_000.0
    lock = _FakeLock()
    health = _FakeHealth()
    store = _FakeStore()
    bridge = SimpleNamespace(is_connected=True, fetch_bars=lambda tf, n_bars: _bar_df())

    job = make_data_sync_job(
        lock=lock,
        logger=_FakeLogger(),
        get_ctrader=lambda: (bridge, None, False),
        market_session_snapshot=lambda _arg: {"status": "open"},
        health_factory=lambda: health,
        config_factory=lambda: SimpleNamespace(enabled_symbols=["XAUUSD+"]),
        duckdb_runtime_factory=_duckdb_runtime(bar_latest={}),
        data_store_factory=lambda: store,
        now_fn=lambda: now,
    )

    job()

    inserted_tfs = [tf for _bars, _symbol, tf in store.inserts]
    assert inserted_tfs == list(BAR_FRESHNESS_THRESHOLDS)
    assert store.inserts[0][0][0] == {
        "time": 1783123200,
        "open": 1.0,
        "high": 2.0,
        "low": 0.5,
        "close": 1.5,
        "volume": 7,
        "spread": 0,
    }
    assert store.inserts[0][1] == "XAUUSD+"
    assert health.successes[-1]["last_bar_ts_by_tf"]["M1"] == 1783123200.0
    assert lock.release_calls == 1
