from types import SimpleNamespace

import pandas as pd

from backend.services.live_loop_bootstrap import (
    BarWarmupRuntime,
    StartupSafetyRuntime,
    run_startup_safety_cycle,
    warmup_live_bars,
)


def _bars(count=30, *, last_close=2_400.0):
    index = pd.date_range("2026-01-01", periods=count, freq="5min")
    return pd.DataFrame(
        {"close": [last_close] * count},
        index=index,
    )


def test_startup_snapshot_and_safety_precede_account_projection():
    order = []
    state_updates = []
    bridge = SimpleNamespace(is_connected=True)
    account_result = SimpleNamespace(
        account={"balance": 10_000.0},
        observed_at=100.0,
        reconcile_id="account-1",
    )
    runtime = StartupSafetyRuntime(
        get_ctrader=lambda: (bridge, None, False),
        reconcile_positions=lambda value: order.append(
            ("positions", value)
        )
        or {"positions": []},
        run_safety_cycle=lambda **kwargs: order.append(
            ("safety", kwargs["reconcile_result"])
        )
        or {"blockers": []},
        reconcile_account=lambda value: order.append(("account", value))
        or account_result,
        reconcile_value=lambda value, key, default=None: getattr(
            value, key, default
        ),
        live_state_update=lambda **kwargs: state_updates.append(kwargs),
        persist_safety_fail_closed=lambda **_kwargs: None,
    )

    result = run_startup_safety_cycle(
        broker="ctrader",
        generation_id="generation-1",
        log=lambda _message: None,
        runtime=runtime,
    )

    assert [item[0] for item in order] == ["positions", "safety", "account"]
    assert result["ok"] is True
    assert result["broker_ready"] is True
    assert state_updates[0]["account_reconcile_id"] == "account-1"


def test_startup_safety_exception_persists_fail_closed_before_return():
    persisted = []
    logs = []
    runtime = StartupSafetyRuntime(
        get_ctrader=lambda: (SimpleNamespace(is_connected=True), None, False),
        reconcile_positions=lambda _bridge: {"positions": []},
        run_safety_cycle=lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("safety unavailable")
        ),
        reconcile_account=lambda _bridge: None,
        reconcile_value=lambda *_args: None,
        live_state_update=lambda **_kwargs: None,
        persist_safety_fail_closed=lambda **kwargs: persisted.append(kwargs),
    )

    result = run_startup_safety_cycle(
        broker="ctrader",
        generation_id="generation-2",
        log=logs.append,
        runtime=runtime,
    )

    assert result["ok"] is False
    assert result["blockers"] == ["startup_safety_cycle_exception"]
    assert persisted[0]["source"] == "live_loop_startup"
    assert "safety unavailable" in persisted[0]["error"]
    assert logs


def _warmup_runtime(
    *,
    local=None,
    broker_frame=None,
    cache=None,
    published=None,
    saved=None,
    broker_calls=None,
):
    published = published if published is not None else []
    saved = saved if saved is not None else []
    broker_calls = broker_calls if broker_calls is not None else []
    bridge = SimpleNamespace(is_connected=True)
    return BarWarmupRuntime(
        warmup_from_local_db=lambda *_args: local,
        get_ctrader=lambda: (bridge, None, False),
        wait_ctrader_ready=lambda *_args, **_kwargs: None,
        fetch_bars_with_retry=lambda *_args, **_kwargs: broker_calls.append(
            True
        )
        or broker_frame,
        load_bar_cache=lambda: cache,
        publish_latest_price=lambda *args, **kwargs: published.append(
            (args, kwargs)
        ),
        save_bar_cache=lambda frame: saved.append(frame),
        logger_warning=lambda _message: None,
        now=lambda: 1_000.0,
    )


def test_local_monthly_bars_are_fallback_and_published():
    frame = _bars(last_close=2_410.0)
    published = []
    saved = []
    broker_calls = []

    result = warmup_live_bars(
        broker="ctrader",
        timeframe="M5",
        log=lambda _message: None,
        runtime=_warmup_runtime(
            local=frame,
            published=published,
            saved=saved,
            broker_calls=broker_calls,
        ),
    )

    assert result.frame is frame
    assert result.source == "local_db"
    assert broker_calls == [True]
    assert published[0][0] == (2_410.0,)
    assert published[0][1]["source"] == "warmup_local_db"
    assert saved == [frame]


def test_online_history_is_primary_and_seeds_startup_frame():
    local = _bars(last_close=2_410.0)
    broker = _bars(last_close=2_420.0)
    published = []
    saved = []
    broker_calls = []

    result = warmup_live_bars(
        broker="ctrader",
        timeframe="M5",
        log=lambda _message: None,
        runtime=_warmup_runtime(
            local=local,
            broker_frame=broker,
            published=published,
            saved=saved,
            broker_calls=broker_calls,
        ),
    )

    assert result.frame is broker
    assert result.source == "broker"
    assert broker_calls == [True]
    assert published[0][0] == (2_420.0,)
    assert published[0][1]["source"] == "warmup_broker"
    assert saved == [broker]


def test_broker_then_cache_fallback_preserves_minimum_bar_gate():
    cache = _bars(last_close=2_420.0)
    broker_calls = []
    logs = []

    result = warmup_live_bars(
        broker="ctrader",
        timeframe="M5",
        log=logs.append,
        runtime=_warmup_runtime(
            local=_bars(2),
            broker_frame=None,
            cache=cache,
            broker_calls=broker_calls,
        ),
    )

    assert broker_calls == [True]
    assert result.frame is cache
    assert result.source == "cache"
    assert any("backup cache" in message for message in logs)


def test_insufficient_bars_abort_before_factor_initialization():
    logs = []

    result = warmup_live_bars(
        broker="ctrader",
        timeframe="M5",
        log=logs.append,
        runtime=_warmup_runtime(local=None, broker_frame=None, cache=None),
    )

    assert result is None
    assert any("insufficient history bars" in message for message in logs)
