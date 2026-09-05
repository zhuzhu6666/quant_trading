from types import SimpleNamespace

import pandas as pd

from backend.services.live_loop_controller import LiveLoopController
from backend.services.live_loop_shell import (
    acknowledge_prepared_factor_projections,
    compare_spot_quote_to_latest_bar,
    apply_factor_pipeline_config_update,
    bridge_readiness_label,
    build_extra_symbol_factor_pipelines,
    build_warmup_feed,
    cache_age_seconds,
    collect_open_risk_runtime_health,
    cross_asset_symbols_for_config,
    dataframe_to_factor_bars,
    enabled_symbols_from_config,
    execution_gate_config,
    loop_identity_snapshot,
    mark_loop_stopped_for_display,
    market_closed_log_message,
    subscribe_spot_once,
    system_health_snapshot_from_report,
    unique_factor_pipelines,
)


class _LoopThread:
    ident = 42

    def __init__(self, alive: bool):
        self._alive = alive

    def is_alive(self) -> bool:
        return self._alive


def test_factor_projection_ack_failure_is_non_fatal_to_live_loop():
    class _UnavailableLifecycle:
        def acknowledge_loaded_prepared_factors(self, **_kwargs):
            raise RuntimeError("state store unavailable")

    logs: list[str] = []
    result = acknowledge_prepared_factor_projections(
        engine=object(),
        generation_id="generation-3",
        log=logs.append,
        service=_UnavailableLifecycle(),
    )

    assert result["ok"] is False
    assert result["status"] == "projection_ack_unavailable"
    assert result["acknowledged_count"] == 0
    assert logs and "projection_ack_unavailable" in logs[0]


def test_factor_projection_ack_uses_process_boot_id_when_generation_controller_is_off():
    captured = {}

    class _Lifecycle:
        def acknowledge_loaded_prepared_factors(self, **kwargs):
            captured.update(kwargs)
            return {"ok": True, "status": "projection_ack_complete", "acknowledged_count": 0}

    result = acknowledge_prepared_factor_projections(
        engine=object(),
        generation_id="",
        service=_Lifecycle(),
    )

    assert result["ok"] is True
    assert str(captured["boot_id"]).startswith("live-alpha:")


def test_loop_identity_snapshot_reads_running_controller_generation():
    controller = LiveLoopController(clock=lambda: 1_000.0)
    generation = controller.begin_start(
        broker="ctrader",
        strategy_name="factor_v4",
    )
    controller.bind_thread(generation.generation_id, _LoopThread(True))
    for step in controller.status()["startup_barrier"]:
        controller.complete_barrier_step(generation.generation_id, step)
    controller.heartbeat(generation.generation_id, "safety")

    status = loop_identity_snapshot(generation=controller.status())

    assert status == {
        "running": True,
        "pid": 42,
        "broker": "ctrader",
        "started_at": 1_000.0,
        "strategy_name": "factor_v4",
    }


def test_loop_identity_snapshot_preserves_stopped_generation_identity():
    controller = LiveLoopController(clock=lambda: 1_000.0)
    generation = controller.begin_start(
        broker="ctrader",
        strategy_name="factor_v4",
    )
    thread = _LoopThread(False)
    controller.bind_thread(generation.generation_id, thread)
    controller.acknowledge_exit(generation.generation_id)
    controller.clear_thread_if(generation.generation_id, thread, 1_001.0)

    generation_status = controller.status()
    ownership = controller.ownership_snapshot()
    status = loop_identity_snapshot(generation=generation_status)

    assert ownership.thread is None
    assert ownership.broker == "ctrader"
    assert ownership.strategy_name == "factor_v4"
    assert status == {
        "running": False,
        "pid": None,
        "broker": "ctrader",
        "started_at": 1_000.0,
        "strategy_name": "factor_v4",
    }


def test_mark_loop_stopped_for_display_only_clears_loop_display_fields():
    state = {
        "loop_running": True,
        "loop_strategy": "carry",
        "broker": "ctrader",
        "account": {"balance": 999.0},
    }

    def _update(**kwargs):
        state.update(kwargs)

    mark_loop_stopped_for_display(state_update=_update)

    assert state["loop_running"] is False
    assert state["loop_strategy"] is None
    assert state["broker"] == "ctrader"
    assert state["account"]["balance"] == 999.0


def test_cache_age_seconds_marks_missing_timestamp_unknown():
    assert cache_age_seconds(now_ts=100.0, updated_at=90.0) == 10.0
    assert cache_age_seconds(now_ts=100.0, updated_at=0.0) is None
    assert cache_age_seconds(now_ts=100.0, updated_at=120.0) == 0.0


def test_system_health_snapshot_from_report_preserves_live_shape():
    report = SimpleNamespace(
        overall="degraded",
        overall_score=72.5,
        components={
            "disk": SimpleNamespace(status="critical"),
            "cpu": SimpleNamespace(status="degraded"),
            "network": SimpleNamespace(status="ok"),
        },
    )

    assert system_health_snapshot_from_report(report) == {
        "overall": "degraded",
        "overall_score": 72.5,
        "component_status": {
            "disk": "critical",
            "cpu": "degraded",
            "network": "ok",
        },
        "critical_components": ["disk"],
        "degraded_components": ["cpu"],
    }


def test_collect_open_risk_runtime_health_falls_back_when_collectors_unavailable():
    def _raise():
        raise RuntimeError("unavailable")

    payload = collect_open_risk_runtime_health(
        timeframe="M5",
        now_ts=100.0,
        account_updated_at=70.0,
        positions_updated_at=0.0,
        sync_health_provider=_raise,
        system_report_provider=_raise,
    )

    assert payload == {
        "data_lag_seconds": 1_000_000_000_000.0,
        "runtime_health": {
            "data_lag_state": "unknown",
            "account_cache_age_seconds": 30.0,
            "account_cache_age_state": "known",
            "positions_cache_age_seconds": None,
            "positions_cache_age_state": "unknown",
            "sync_health": {},
            "system_health": {},
        },
    }


def test_collect_open_risk_runtime_health_uses_injected_collectors():
    class _SyncHealth:
        def snapshot(self):
            return {"ok": True}

        def last_bar_age_seconds(self, timeframe):
            assert timeframe == "M15"
            return 12.5

    payload = collect_open_risk_runtime_health(
        timeframe="M15",
        now_ts=100.0,
        account_updated_at=80.0,
        positions_updated_at=75.0,
        sync_health_provider=lambda: _SyncHealth(),
        system_report_provider=lambda: SimpleNamespace(
            overall="ok",
            overall_score=99.0,
            components={"disk": SimpleNamespace(status="ok")},
        ),
    )

    assert payload == {
        "data_lag_seconds": 12.5,
        "runtime_health": {
            "data_lag_state": "known",
            "account_cache_age_seconds": 20.0,
            "account_cache_age_state": "known",
            "positions_cache_age_seconds": 25.0,
            "positions_cache_age_state": "known",
            "sync_health": {"ok": True},
            "system_health": {
                "overall": "ok",
                "overall_score": 99.0,
                "component_status": {"disk": "ok"},
                "critical_components": [],
                "degraded_components": [],
            },
        },
    }


def test_dataframe_to_factor_bars_matches_live_warmup_shape():
    df = pd.DataFrame(
        [
            {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 9.0},
            {"open": 2.0, "high": 3.0, "low": 1.5, "close": 2.5, "volume": 10.0},
        ],
        index=pd.to_datetime(["2026-07-05T00:00:00Z", "2026-07-05T00:05:00Z"]),
    )

    assert dataframe_to_factor_bars(df, timeframe="M5") == [
        {
            "open": 1.0,
            "high": 2.0,
            "low": 0.5,
            "close": 1.5,
            "volume": 9.0,
            "time": 1783209600.0,
            "timeframe": "M5",
            "complete": True,
        },
        {
            "open": 2.0,
            "high": 3.0,
            "low": 1.5,
            "close": 2.5,
            "volume": 10.0,
            "time": 1783209900.0,
            "timeframe": "M5",
            "complete": True,
        },
    ]


def test_build_warmup_feed_applies_min_and_runtime_limits():
    df = pd.DataFrame(
        [{"open": i, "high": i + 1, "low": i - 1, "close": i + 0.5} for i in range(10)],
        index=pd.date_range("2026-07-05T00:00:00Z", periods=10, freq="5min"),
    )

    feed = build_warmup_feed(df, timeframe="M5", min_warmup=6, warmup_limit=4)
    capped = build_warmup_feed(df, timeframe="M5", min_warmup=6, warmup_limit=99)

    assert feed["total_bars"] == 10
    assert feed["warmup_limit"] == 6
    assert len(feed["warmup_df"]) == 6
    assert len(feed["warmup_bars"]) == 6
    assert feed["warmup_bars"][0]["open"] == 4.0
    assert capped["warmup_limit"] == 10
    assert len(capped["warmup_bars"]) == 10


def test_execution_gate_does_not_start_cooldown_before_final_admission():
    cfg = SimpleNamespace(
        factor_signal_threshold=0.33,
        strategy_cooldown_bars=4,
        risk_enable_nfp_skip=True,
        risk_enable_gvz_gate=False,
        risk_gvz_drop_pct=-1.5,
    )

    assert execution_gate_config(cfg) == {
        "signal_threshold": 0.33,
        "cooldown_bars": 0,
        "event_filter_authority": "risk_policy",
        "risk_enable_nfp_skip": True,
        "risk_enable_gvz_gate": False,
        "risk_gvz_drop_pct": -1.5,
    }


def test_enabled_symbols_from_config_defaults_to_xauusd():
    assert enabled_symbols_from_config(SimpleNamespace(enabled_symbols=["XAUUSD+", "EURUSD"])) == [
        "XAUUSD+",
        "EURUSD",
    ]
    assert enabled_symbols_from_config(SimpleNamespace(enabled_symbols=[])) == ["XAUUSD+"]
    assert enabled_symbols_from_config(SimpleNamespace()) == ["XAUUSD+"]


def test_unique_factor_pipelines_deduplicates_primary_and_symbol_map():
    primary = {"name": "main"}
    secondary = {"name": "secondary"}

    assert unique_factor_pipelines(primary, {"XAUUSD+": primary, "EURUSD": secondary}) == [
        primary,
        secondary,
    ]


def test_apply_factor_pipeline_config_update_calls_supported_components():
    calls = []

    class _Engine:
        def set_factor_runtime_config(self, cfg):
            calls.append(("engine", cfg))

    class _Normalizer:
        def update_configs(self, cfg):
            calls.append(("normalizer", cfg))

    class _Compositor:
        def reload_configs(self, cfg):
            calls.append(("compositor", cfg))

    cfg = SimpleNamespace(factor_signal_config={"trend": {"enabled": True}})
    updated = apply_factor_pipeline_config_update(
        pipelines=[
            {"engine": _Engine(), "normalizer": _Normalizer(), "compositor": _Compositor()},
            {"engine": object(), "normalizer": None, "compositor": None},
        ],
        cfg=cfg,
        merged_config={"merged": True},
    )

    assert updated == 2
    assert calls == [
        ("engine", {"trend": {"enabled": True}}),
        ("normalizer", {"trend": {"enabled": True}}),
        ("compositor", {"merged": True}),
    ]


def test_build_extra_symbol_factor_pipelines_reuses_primary_and_shared_components():
    calls = []

    class _Engine:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            calls.append(("engine", kwargs))

    class _Normalizer:
        def __init__(self, cfg):
            self.cfg = cfg
            calls.append(("normalizer", cfg))

    class _Compositor:
        def __init__(self, cfg):
            self.cfg = cfg
            calls.append(("compositor", cfg))

    class _Gate:
        def __init__(self, cfg):
            self.cfg = cfg
            calls.append(("gate", cfg))

    cfg = SimpleNamespace(
        factor_signal_config={"factor": True},
        factor_portfolio_weights={"factor": 1.0},
        factor_tactical_alpha=0.2,
        factor_signal_threshold=0.4,
        strategy_cooldown_bars=3,
    )
    primary = {"engine": "primary"}
    shared = {
        "attribution": object(),
        "event_sizing": object(),
    }

    pipelines = build_extra_symbol_factor_pipelines(
        symbols=["XAUUSD+", "EURUSD"],
        primary_symbol="XAUUSD+",
        primary_pipeline=primary,
        cfg=cfg,
        shared_components=shared,
        streaming_engine_cls=_Engine,
        normalizer_cls=_Normalizer,
        compositor_cls=_Compositor,
        gate_cls=_Gate,
        merge_portfolio_configs=lambda *args: {"merged_args": args},
    )

    assert pipelines["XAUUSD+"] is primary
    assert pipelines["EURUSD"]["attribution"] is shared["attribution"]
    assert pipelines["EURUSD"]["event_sizing"] is shared["event_sizing"]
    assert calls[0] == ("engine", {"max_buffer": 200, "factor_runtime_config": {"factor": True}})
    assert calls[-1] == (
        "gate",
        {
            "signal_threshold": 0.4,
            "cooldown_bars": 0,
            "event_filter_authority": "risk_policy",
            "risk_enable_nfp_skip": False,
            "risk_enable_gvz_gate": False,
            "risk_gvz_drop_pct": -2.0,
        },
    )


def test_cross_asset_symbols_for_config_requires_multiple_symbols_and_flag():
    assert cross_asset_symbols_for_config(
        SimpleNamespace(enabled_symbols=["XAUUSD+"], cross_asset_covariance_enabled=True)
    ) == []
    assert cross_asset_symbols_for_config(
        SimpleNamespace(enabled_symbols=["XAUUSD+", "EURUSD"], cross_asset_covariance_enabled=False)
    ) == []
    assert cross_asset_symbols_for_config(
        SimpleNamespace(enabled_symbols=["XAUUSD+", "EURUSD"], cross_asset_covariance_enabled=True)
    ) == ["XAUUSD+", "EURUSD"]


def test_market_closed_log_message_preserves_live_text_shape():
    market_session = {"reason": "weekend", "high_load_allowed": False}

    assert bridge_readiness_label(bridge_ready=True, warming=False) == "ready"
    assert bridge_readiness_label(bridge_ready=False, warming=True) == "warming"
    assert bridge_readiness_label(bridge_ready=False, warming=False) == "disconnected"
    assert market_closed_log_message(
        tick=3,
        market_session=market_session,
        bridge_ready=False,
        warming=True,
    ) == (
        "tick 3: market closed confirmed (weekend), open-market work paused; "
        "high_load_allowed=False; cTrader=warming"
    )
    assert market_closed_log_message(
        tick=4,
        market_session=market_session,
        bridge_ready=True,
        warming=False,
        after_broker_check=True,
    ) == (
        "tick 4: market closed confirmed after broker check (weekend), "
        "open-market work paused; high_load_allowed=False; cTrader=ready"
    )


class _SpotBridge:
    def __init__(self, *, connected: bool):
        self.is_connected = connected
        self.spot_subscriptions = 0

    def subscribe_spots(self):
        self.spot_subscriptions += 1


class _OnlineSpotBridge(_SpotBridge):
    def __init__(self, *, connected: bool):
        super().__init__(connected=connected)
        self.seed_calls: list[tuple[str, object]] = []
        self.trendbar_subscriptions: list[tuple[str, ...]] = []

    def seed_live_bars(self, timeframe, frame):
        self.seed_calls.append((timeframe, frame))
        return len(frame)

    def subscribe_live_trendbars(self, timeframes):
        self.trendbar_subscriptions.append(tuple(timeframes))
        return True

def test_subscribe_spot_once_skips_when_bridge_error():
    bridge = _SpotBridge(connected=True)
    logs: list[str] = []

    subscribe_spot_once(
        get_ctrader=lambda: (bridge, "missing credentials", False),
        wait_ctrader_ready=lambda *_args, **_kwargs: "",
        log=logs.append,
    )

    assert bridge.spot_subscriptions == 0
    assert logs == ["subscribe_spots skipped: missing credentials"]


def test_subscribe_spot_once_waits_then_subscribes_spot():
    bridge = _SpotBridge(connected=False)
    logs: list[str] = []
    waits: list[tuple[object, float]] = []

    def wait_ready(wait_bridge, *, timeout_sec):
        waits.append((wait_bridge, timeout_sec))
        bridge.is_connected = True
        return ""

    subscribe_spot_once(
        get_ctrader=lambda: (bridge, "", True),
        wait_ctrader_ready=wait_ready,
        log=logs.append,
        timeout_sec=7.5,
    )

    assert waits == [(bridge, 7.5)]
    assert bridge.spot_subscriptions == 1
    assert logs == ["subscribed to cTrader spot events"]


def test_subscribe_spot_once_seeds_and_subscribes_online_trendbars():
    bridge = _OnlineSpotBridge(connected=True)
    frame = pd.DataFrame(
        [{"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 3}],
        index=pd.to_datetime(["2026-07-05T00:00:00Z"]),
    )
    logs: list[str] = []

    subscribe_spot_once(
        get_ctrader=lambda: (bridge, "", False),
        wait_ctrader_ready=lambda *_args, **_kwargs: "",
        log=logs.append,
        timeframe="M5",
        seed_frame=frame,
    )

    assert bridge.spot_subscriptions == 1
    assert bridge.seed_calls == [("M5", frame)]
    assert bridge.trendbar_subscriptions == [("M5",)]
    assert logs == [
        "seeded online trendbar feed: timeframe=M5 bars=1",
        "subscribed to cTrader live trendbars (timeframe=M5)",
        "subscribed to cTrader spot events",
    ]


def test_subscribe_spot_once_skips_when_ready_wait_fails():
    bridge = _SpotBridge(connected=False)
    logs: list[str] = []

    subscribe_spot_once(
        get_ctrader=lambda: (bridge, "", True),
        wait_ctrader_ready=lambda *_args, **_kwargs: "warming timeout",
        log=logs.append,
    )

    assert bridge.spot_subscriptions == 0
    assert logs == ["subscribe_spots skipped: warming timeout"]


def test_compare_spot_quote_to_latest_bar_never_mutates_closed_ohlc():
    df = pd.DataFrame(
        [{"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0}],
        index=pd.to_datetime(["2026-07-05T00:00:00Z"]),
    )
    original = df.copy(deep=True)

    result = compare_spot_quote_to_latest_bar(
        df_new=df,
        quote={"mid": 102.0},
        quote_is_fresh=lambda quote: True,
    )

    assert result == {
        "spot": 102.0,
        "last_close": 100.0,
        "within_tolerance": True,
        "too_far": False,
    }
    pd.testing.assert_frame_equal(df, original)


def test_compare_spot_quote_to_latest_bar_marks_too_far_without_mutating():
    df = pd.DataFrame(
        [{"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0}],
        index=pd.to_datetime(["2026-07-05T00:00:00Z"]),
    )

    original = df.copy(deep=True)
    result = compare_spot_quote_to_latest_bar(
        df_new=df,
        quote={"mid": 140.0},
        quote_is_fresh=lambda quote: True,
    )

    assert result == {
        "spot": 140.0,
        "last_close": 100.0,
        "within_tolerance": False,
        "too_far": True,
    }
    pd.testing.assert_frame_equal(df, original)
