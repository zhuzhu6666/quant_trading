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
    def __init__(self, path, *, bar_latest):
        self.path = path
        self.bar_latest = bar_latest
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
        return (0.0,)


class _FakeStore:
    def __init__(self):
        self.inserts = []

    def insert_bars(self, bars, symbol, timeframe):
        self.inserts.append((bars, symbol, timeframe))


def _duckdb_runtime(*, bar_latest):
    def _connect(path, snapshot_first=True):
        return _FakeDuckConn(
            path,
            bar_latest=bar_latest,
        )

    return lambda: ("bars", _connect)


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


def test_data_sync_fresh_bars_skip_bridge():
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
        duckdb_runtime_factory=_duckdb_runtime(bar_latest=bar_latest),
        now_fn=lambda: now,
    )

    job()

    assert bridge_called is False
    assert lock.release_calls == 1
    assert health.successes == [{"last_bar_ts_by_tf": bar_latest}]


def test_data_sync_pulls_decision_m5_when_latest_closed_bar_is_missing():
    now = 1_000_000.0
    lock = _FakeLock()
    health = _FakeHealth()
    store = _FakeStore()
    bar_latest = {tf: now - 1 for tf in BAR_FRESHNESS_THRESHOLDS}
    # Keep M5 inside its broad age budget while missing the latest closed
    # decision bar.  The scheduler must still repair it after the M5 boundary.
    bar_latest["M5"] = now - 600.0
    fetch_calls = []

    def _fetch_bars(timeframe, n_bars):
        fetch_calls.append((timeframe, n_bars))
        return _bar_df()

    bridge = SimpleNamespace(is_connected=True, fetch_bars=_fetch_bars)
    job = make_data_sync_job(
        lock=lock,
        logger=_FakeLogger(),
        get_ctrader=lambda: (bridge, None, False),
        market_session_snapshot=lambda _arg: {"status": "open"},
        health_factory=lambda: health,
        config_factory=lambda: SimpleNamespace(enabled_symbols=["XAUUSD+"]),
        duckdb_runtime_factory=_duckdb_runtime(bar_latest=bar_latest),
        data_store_factory=lambda: store,
        now_fn=lambda: now,
    )

    job()

    assert fetch_calls == [("M5", 5)]
    assert [tf for _bars, _symbol, tf in store.inserts] == ["M5"]
    assert lock.release_calls == 1


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


def test_data_sync_stale_bars_skip_pull_during_bounded_maintenance_wait():
    now = 1_000_000.0
    latest = now - 74 * 60
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
        market_session_snapshot=lambda _arg: {
            "status": "open_pending_quote",
            "api_available": True,
            "broker_connected": True,
            "evidence": ["market_data_stale"],
        },
        health_factory=lambda: health,
        config_factory=lambda: SimpleNamespace(
            enabled_symbols=["XAUUSD+"],
            market_open_pending_quote_grace_seconds=4500.0,
        ),
        duckdb_runtime_factory=_duckdb_runtime(
            bar_latest={tf: latest for tf in BAR_FRESHNESS_THRESHOLDS}
        ),
        now_fn=lambda: now,
    )

    job()

    assert bridge_called is False
    assert health.successes


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
