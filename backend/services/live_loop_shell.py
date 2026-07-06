"""Small helpers for live loop status presentation.

This module intentionally does not own the loop thread or stop flag yet.  It
only keeps response shaping and display-state mutations outside the large live
service module while the old globals remain the runtime source of truth.
"""

from __future__ import annotations

from typing import Any, Callable


StateGet = Callable[..., Any]
StateUpdate = Callable[..., None]


def loop_status_snapshot(
    *,
    state_get: StateGet,
    thread: Any,
    broker: str | None,
    started_at: float | None,
    strategy_name: str | None,
) -> dict[str, Any]:
    """Return the legacy-compatible live loop status payload."""

    if state_get("loop_running") is False:
        return {
            "running": False,
            "pid": None,
            "broker": state_get("broker") or broker,
            "started_at": state_get("loop_started_at"),
            "strategy_name": state_get("loop_strategy") or strategy_name,
        }

    if state_get("loop_running") and state_get("broker"):
        return {
            "running": True,
            "pid": None,
            "broker": state_get("broker"),
            "started_at": state_get("loop_started_at"),
            "strategy_name": state_get("loop_strategy"),
        }

    if thread is not None and thread.is_alive():
        return {
            "running": True,
            "pid": thread.ident,
            "broker": broker,
            "started_at": started_at,
            "strategy_name": strategy_name,
        }

    return {
        "running": False,
        "pid": None,
        "broker": None,
        "started_at": None,
        "strategy_name": strategy_name,
    }


def mark_loop_stopped_for_display(*, state_update: StateUpdate) -> None:
    """Mark the loop as stopped without clearing cached account/position data."""

    state_update(
        loop_running=False,
        loop_strategy=None,
    )


def cache_age_seconds(*, now_ts: float, updated_at: float) -> float:
    ts = float(updated_at or 0.0)
    if ts <= 0:
        return 0.0
    return max(0.0, float(now_ts or 0.0) - ts)


def system_health_snapshot_from_report(report: Any) -> dict[str, Any]:
    if report is None:
        return {}
    components = getattr(report, "components", {}) or {}
    return {
        "overall": str(getattr(report, "overall", "") or ""),
        "overall_score": float(getattr(report, "overall_score", 0.0) or 0.0),
        "component_status": {
            str(name): str(getattr(component, "status", "") or "")
            for name, component in components.items()
        },
        "critical_components": [
            str(name)
            for name, component in components.items()
            if str(getattr(component, "status", "") or "") == "critical"
        ],
        "degraded_components": [
            str(name)
            for name, component in components.items()
            if str(getattr(component, "status", "") or "") == "degraded"
        ],
    }


def collect_open_risk_runtime_health(
    *,
    timeframe: str,
    now_ts: float,
    account_updated_at: float,
    positions_updated_at: float,
    sync_health_provider: Callable[[], Any] | None = None,
    system_report_provider: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    sync_snapshot: dict[str, Any] = {}
    data_lag_seconds = 0.0
    system_health_snapshot: dict[str, Any] = {}
    try:
        if sync_health_provider is None:
            from data.live_sync.health import SyncHealth

            sync_health = SyncHealth.shared()
        else:
            sync_health = sync_health_provider()
        sync_snapshot = sync_health.snapshot()
        data_lag_seconds = float(sync_health.last_bar_age_seconds(str(timeframe or "M5")) or 0.0)
    except Exception:
        sync_snapshot = {}
        data_lag_seconds = 0.0
    try:
        if system_report_provider is None:
            from monitor.system_health import shared as _system_health_shared

            report = _system_health_shared().get_last_report()
        else:
            report = system_report_provider()
        system_health_snapshot = system_health_snapshot_from_report(report)
    except Exception:
        system_health_snapshot = {}
    return {
        "data_lag_seconds": data_lag_seconds,
        "runtime_health": {
            "account_cache_age_seconds": cache_age_seconds(
                now_ts=now_ts,
                updated_at=account_updated_at,
            ),
            "positions_cache_age_seconds": cache_age_seconds(
                now_ts=now_ts,
                updated_at=positions_updated_at,
            ),
            "sync_health": sync_snapshot,
            "system_health": system_health_snapshot,
        },
    }


def dataframe_to_factor_bars(df: Any, *, timeframe: str) -> list[dict[str, Any]]:
    if df is None or len(df) <= 0:
        return []
    bars: list[dict[str, Any]] = []
    has_volume = "volume" in getattr(df, "columns", [])
    for i in range(len(df)):
        index_value = df.index[i]
        bars.append(
            {
                "open": float(df["open"].iloc[i]),
                "high": float(df["high"].iloc[i]),
                "low": float(df["low"].iloc[i]),
                "close": float(df["close"].iloc[i]),
                "volume": float(df["volume"].iloc[i]) if has_volume else 0.0,
                "time": float(index_value.timestamp()) if hasattr(index_value, "timestamp") else 0.0,
                "timeframe": str(timeframe or ""),
                "complete": True,
            }
        )
    return bars


def build_warmup_feed(df: Any, *, timeframe: str, min_warmup: int, warmup_limit: int) -> dict[str, Any]:
    total_bars = len(df) if df is not None else 0
    limit = int(warmup_limit or 0)
    minimum = int(min_warmup or 0)
    selected = max(minimum, min(limit, total_bars)) if total_bars > 0 else 0
    warmup_df = df.tail(selected) if df is not None and selected > 0 else df
    return {
        "total_bars": total_bars,
        "warmup_limit": selected,
        "warmup_df": warmup_df,
        "warmup_bars": dataframe_to_factor_bars(warmup_df, timeframe=timeframe),
    }


def execution_gate_config(cfg: Any) -> dict[str, Any]:
    return {
        "signal_threshold": cfg.factor_signal_threshold,
        "cooldown_bars": cfg.strategy_cooldown_bars,
        "event_filter_authority": "risk_policy",
        "risk_enable_nfp_skip": getattr(cfg, "risk_enable_nfp_skip", False),
        "risk_enable_gvz_gate": getattr(cfg, "risk_enable_gvz_gate", False),
        "risk_gvz_drop_pct": getattr(cfg, "risk_gvz_drop_pct", -2.0),
    }


def adaptive_weight_config(cfg: Any) -> dict[str, Any]:
    return {
        "awe_sensitivity": cfg.awe_sensitivity,
        "awe_anchor_pull": cfg.awe_anchor_pull,
        "awe_max_single_change": cfg.awe_max_single_change,
        "awe_weight_min": cfg.awe_weight_min,
        "awe_weight_max": cfg.awe_weight_max,
        "awe_min_trades": cfg.awe_min_trades,
        "awe_ic_floor": cfg.awe_ic_floor,
        "awe_health_floor": cfg.awe_health_floor,
        "awe_disable_min_trades": cfg.awe_disable_min_trades,
        "awe_max_type_weight_pct": cfg.awe_max_type_weight_pct,
    }


def enabled_symbols_from_config(cfg: Any) -> list[str]:
    symbols = list(cfg.enabled_symbols) if hasattr(cfg, "enabled_symbols") else ["XAUUSD+"]
    return symbols or ["XAUUSD+"]


def unique_factor_pipelines(primary: dict[str, Any] | None, pipelines: dict[str, Any] | None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[int] = set()
    for pipe in [primary] + list((pipelines or {}).values()):
        if not pipe or id(pipe) in seen:
            continue
        seen.add(id(pipe))
        result.append(pipe)
    return result


def apply_factor_pipeline_config_update(
    *,
    pipelines: list[dict[str, Any]],
    cfg: Any,
    merged_config: dict[str, Any],
) -> int:
    updated = 0
    for pipe in pipelines:
        pipe_engine = pipe.get("engine")
        pipe_normalizer = pipe.get("normalizer")
        pipe_compositor = pipe.get("compositor")
        if pipe_engine and hasattr(pipe_engine, "set_factor_runtime_config"):
            pipe_engine.set_factor_runtime_config(cfg.factor_signal_config)
        if pipe_normalizer and hasattr(pipe_normalizer, "update_configs"):
            pipe_normalizer.update_configs(cfg.factor_signal_config)
        if pipe_compositor and hasattr(pipe_compositor, "reload_configs"):
            pipe_compositor.reload_configs(merged_config)
        updated += 1
    return updated


def build_extra_symbol_factor_pipelines(
    *,
    symbols: list[str],
    primary_symbol: str,
    primary_pipeline: dict[str, Any] | None,
    cfg: Any,
    shared_components: dict[str, Any],
    streaming_engine_cls: Callable[..., Any],
    normalizer_cls: Callable[..., Any],
    compositor_cls: Callable[..., Any],
    gate_cls: Callable[..., Any],
    merge_portfolio_configs: Callable[..., dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    pipelines: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        if symbol == primary_symbol:
            if primary_pipeline is not None:
                pipelines[symbol] = primary_pipeline
            continue
        engine = streaming_engine_cls(max_buffer=200, factor_runtime_config=cfg.factor_signal_config)
        normalizer = normalizer_cls(cfg.factor_signal_config)
        compositor = compositor_cls(
            merge_portfolio_configs(
                cfg.factor_signal_config,
                cfg.factor_portfolio_weights,
                cfg.factor_tactical_alpha,
                cfg.factor_signal_threshold,
            )
        )
        gate = gate_cls(execution_gate_config(cfg))
        pipelines[symbol] = {
            "engine": engine,
            "normalizer": normalizer,
            "compositor": compositor,
            "gate": gate,
            "attribution": shared_components.get("attribution"),
            "awe": shared_components.get("awe"),
            "ic_tracker": shared_components.get("ic_tracker"),
            "event_sizing": shared_components.get("event_sizing"),
        }
    return pipelines


def cross_asset_symbols_for_config(cfg: Any) -> list[str]:
    symbols = enabled_symbols_from_config(cfg)
    if len(symbols) > 1 and bool(getattr(cfg, "cross_asset_covariance_enabled", False)):
        return symbols
    return []


def depth_subscription_required(*, require_l2_depth: bool, l2_collection_enabled: bool) -> bool:
    return bool(require_l2_depth or l2_collection_enabled)


def depth_subscription_followup_message(*, require_l2_depth: bool, l2_collection_enabled: bool) -> str:
    if require_l2_depth:
        return ""
    if l2_collection_enabled:
        return "L2 depth collected for research; risk_require_l2_depth=false so it is not a trading gate"
    return "L2 depth subscription skipped: risk_require_l2_depth=false and l2_collection_enabled=false"


def bridge_readiness_label(*, bridge_ready: bool, warming: bool) -> str:
    if bridge_ready:
        return "ready"
    return "warming" if warming else "disconnected"


def market_closed_log_message(
    *,
    tick: int,
    market_session: dict[str, Any],
    bridge_ready: bool,
    warming: bool,
    after_broker_check: bool = False,
) -> str:
    phase = "market closed confirmed after broker check" if after_broker_check else "market closed confirmed"
    return (
        f"tick {int(tick)}: {phase} "
        f"({(market_session or {}).get('reason')}), open-market work paused; "
        f"high_load_allowed={(market_session or {}).get('high_load_allowed')}; "
        f"cTrader={bridge_readiness_label(bridge_ready=bridge_ready, warming=warming)}"
    )


def subscribe_spot_depth_once(
    *,
    get_ctrader: Callable[[], tuple[Any, str, bool]],
    wait_ctrader_ready: Callable[..., str],
    require_l2_depth: bool,
    l2_collection_enabled: bool,
    log: Callable[[str], None],
    timeout_sec: float = 10.0,
) -> None:
    spot_bridge, spot_err, spot_warming = get_ctrader()
    if spot_err:
        log(f"subscribe_spots skipped: {spot_err}")
        return
    if spot_warming or not spot_bridge.is_connected:
        wait_err = wait_ctrader_ready(spot_bridge, timeout_sec=timeout_sec)
        if wait_err:
            log(f"subscribe_spots skipped: {wait_err}")
            return
    spot_bridge.subscribe_spots()
    if depth_subscription_required(
        require_l2_depth=require_l2_depth,
        l2_collection_enabled=l2_collection_enabled,
    ):
        spot_bridge.subscribe_depth()
    log("subscribed to spot/depth events for real-time price and L2 research")
    depth_message = depth_subscription_followup_message(
        require_l2_depth=require_l2_depth,
        l2_collection_enabled=l2_collection_enabled,
    )
    if depth_message:
        log(depth_message)


def apply_spot_quote_to_latest_bar(
    *,
    df_new: Any,
    quote: dict[str, Any] | None,
    quote_is_fresh: Callable[[dict[str, Any]], bool],
    max_relative_diff: float = 0.20,
) -> dict[str, Any]:
    spot = float((quote or {}).get("mid") or 0.0) if quote_is_fresh(quote or {}) else 0.0
    last_close = float(df_new.iloc[-1]["close"])
    applied = bool(
        spot
        and spot > 0
        and last_close > 0
        and abs(spot - last_close) / last_close < float(max_relative_diff or 0.0)
    )
    if applied:
        df_new.loc[df_new.index[-1], "close"] = spot
        df_new.loc[df_new.index[-1], "high"] = max(df_new.iloc[-1]["high"], spot)
        df_new.loc[df_new.index[-1], "low"] = min(df_new.iloc[-1]["low"], spot)
    return {
        "spot": spot,
        "last_close": last_close,
        "applied": applied,
        "too_far": bool(spot and spot > 0 and not applied),
    }
