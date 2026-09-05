"""Factor pipeline initialization helpers for the live-loop bootstrap."""

from __future__ import annotations

import traceback
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FactorWarmupRuntime:
    build_warmup_feed: Any
    build_factor_votes: Any
    build_snapshot_summary: Any
    set_factor_snapshot: Any
    acknowledge_projections: Any
    now: Any
    build_low_frequency_snapshots: Any | None = None


@dataclass(frozen=True)
class FactorInitializationRuntime:
    config_factory: Any
    engine_cls: Any
    normalizer_cls: Any
    compositor_cls: Any
    gate_cls: Any
    attribution_cls: Any
    selection_factory: Any
    projection_service_factory: Any
    event_sizing_factory: Any
    subscribe_config: Any
    generation_active: Any
    merge_portfolio_configs: Any
    execution_gate_config: Any
    unique_factor_pipelines: Any
    apply_config_update: Any
    acknowledge_projections: Any
    enabled_symbols: Any
    build_extra_symbol_pipelines: Any
    cross_asset_symbols: Any
    covariance_cls: Any
    logger_warning: Any
    logger_debug: Any


@dataclass(frozen=True)
class FactorInitializationResult:
    config: Any
    pipeline: dict[str, Any] | None
    pipelines: dict[str, dict[str, Any]]
    cross_asset_covariance: Any
    error: str = ""


def initialize_factor_pipelines(
    *,
    generation_id: str,
    log: Any,
    runtime: FactorInitializationRuntime,
) -> FactorInitializationResult:
    """Build primary/multi-symbol factor state and hot-reload wiring."""

    cfg = None
    holder: dict[str, Any] = {
        "pipeline": None,
        "pipelines": {},
    }
    try:
        cfg = runtime.config_factory()
        engine = runtime.engine_cls(
            max_buffer=200,
            factor_runtime_config=cfg.factor_signal_config,
        )
        normalizer = runtime.normalizer_cls(cfg.factor_signal_config)
        compositor = runtime.compositor_cls(
            runtime.merge_portfolio_configs(
                cfg.factor_signal_config,
                cfg.factor_portfolio_weights,
                cfg.factor_tactical_alpha,
                cfg.factor_signal_threshold,
            )
        )
        _publish_factor_selection(
            cfg.factor_signal_config,
            runtime=runtime,
            failure_message="[live] factor selection projection publish failed: %s",
        )
        gate = runtime.gate_cls(runtime.execution_gate_config(cfg))
        attribution = runtime.attribution_cls()
        event_sizing = _initialize_event_sizing(runtime)
        primary = {
            "engine": engine,
            "normalizer": normalizer,
            "compositor": compositor,
            "gate": gate,
            "attribution": attribution,
            "event_sizing": event_sizing,
        }
        holder["pipeline"] = primary
        log(
            "Factor Takeover v4 pipeline initialized "
            f"(ctrader_demo={cfg.ctrader_send_orders})"
        )
        _subscribe_factor_config(
            holder=holder,
            generation_id=generation_id,
            log=log,
            runtime=runtime,
        )

        pipelines = _initialize_extra_symbol_pipelines(
            primary=primary,
            attribution=attribution,
            event_sizing=event_sizing,
            log=log,
            runtime=runtime,
        )
        holder["pipelines"] = pipelines
        covariance = _initialize_cross_asset_covariance(
            cfg=cfg,
            log=log,
            runtime=runtime,
        )
        return FactorInitializationResult(
            config=cfg,
            pipeline=primary,
            pipelines=pipelines,
            cross_asset_covariance=covariance,
        )
    except Exception as exc:
        log(f"Factor pipeline init failed: {exc}")
        log(f"  Traceback: {traceback.format_exc()[-600:]}")
        return FactorInitializationResult(
            config=cfg,
            pipeline=None,
            pipelines={},
            cross_asset_covariance=None,
            error=f"{type(exc).__name__}: {exc}",
        )


def _publish_factor_selection(
    signal_config: Any,
    *,
    runtime: FactorInitializationRuntime,
    failure_message: str,
) -> None:
    try:
        selection = runtime.selection_factory(signal_config)
        if selection is not None:
            runtime.projection_service_factory().publish(selection)
    except Exception as exc:
        runtime.logger_warning(failure_message, exc)


def _initialize_event_sizing(runtime: FactorInitializationRuntime) -> Any:
    try:
        return runtime.event_sizing_factory()
    except Exception as exc:
        runtime.logger_debug("[live] event sizing init skipped: %s", exc)
        return None


def _subscribe_factor_config(
    *,
    holder: dict[str, Any],
    generation_id: str,
    log: Any,
    runtime: FactorInitializationRuntime,
) -> None:
    def on_config_change(cfg: Any, version: int) -> None:
        try:
            if not runtime.generation_active(generation_id):
                return
            merged = runtime.merge_portfolio_configs(
                cfg.factor_signal_config,
                cfg.factor_portfolio_weights,
                cfg.factor_tactical_alpha,
                cfg.factor_signal_threshold,
            )
            runtime.apply_config_update(
                pipelines=runtime.unique_factor_pipelines(
                    holder["pipeline"],
                    holder["pipelines"],
                ),
                cfg=cfg,
                merged_config=merged,
            )
            _publish_factor_selection(
                cfg.factor_signal_config,
                runtime=runtime,
                failure_message=(
                    "[live] factor selection projection refresh failed: %s"
                ),
            )
            runtime.acknowledge_projections(
                engine=(holder["pipeline"] or {}).get("engine"),
                generation_id=str(generation_id or ""),
                log=log,
            )
            runtime.logger_debug(
                "[live] factor pipeline hot-reloaded (v%d)",
                version,
            )
        except Exception as exc:
            runtime.logger_debug(
                "[live] factor pipeline hot-reload: %s",
                exc,
            )

    try:
        runtime.subscribe_config(on_config_change)
        log(
            "RuntimeConfig subscription active: factor pipeline will "
            "hot-reload configs"
        )
    except Exception as exc:
        log(f"RuntimeConfig subscription skipped: {exc}")


def _initialize_extra_symbol_pipelines(
    *,
    primary: dict[str, Any],
    attribution: Any,
    event_sizing: Any,
    log: Any,
    runtime: FactorInitializationRuntime,
) -> dict[str, dict[str, Any]]:
    try:
        cfg = runtime.config_factory()
        symbols = runtime.enabled_symbols(cfg)
        pipelines = runtime.build_extra_symbol_pipelines(
            symbols=symbols,
            primary_symbol="XAUUSD+",
            primary_pipeline=primary,
            cfg=cfg,
            shared_components={
                "attribution": attribution,
                "event_sizing": event_sizing,
            },
            streaming_engine_cls=runtime.engine_cls,
            normalizer_cls=runtime.normalizer_cls,
            compositor_cls=runtime.compositor_cls,
            gate_cls=runtime.gate_cls,
            merge_portfolio_configs=runtime.merge_portfolio_configs,
        )
        if len(symbols) > 1:
            log(f"Multi-symbol pipelines initialized: {symbols}")
        return pipelines
    except Exception as exc:
        log(f"Multi-symbol pipeline init skipped: {exc}")
        return {"XAUUSD+": primary}


def _initialize_cross_asset_covariance(
    *,
    cfg: Any,
    log: Any,
    runtime: FactorInitializationRuntime,
) -> Any:
    try:
        symbols = runtime.cross_asset_symbols(cfg)
        if not symbols:
            return None
        covariance = runtime.covariance_cls(
            symbols,
            window=cfg.cross_asset_covariance_window,
        )
        log(f"Cross-asset covariance initialized: {symbols}")
        return covariance
    except Exception as exc:
        log(f"Cross-asset covariance init skipped: {exc}")
        return None


def warmup_factor_pipeline(
    pipeline: dict[str, Any] | None,
    frame: Any,
    *,
    cfg: Any,
    timeframe: str,
    generation_id: str,
    log: Any,
    runtime: FactorWarmupRuntime,
) -> dict[str, Any]:
    """Feed closed bars, publish the initial vote, and acknowledge load."""

    if pipeline is None:
        return {
            "ok": False,
            "skipped": True,
            "reason": "factor_pipeline_unavailable",
            "warm": False,
            "snapshot_count": 0,
        }

    try:
        engine = pipeline["engine"]
        engine.reset()
        snapshots: list[Any] = []
        minimum_warmup = int(getattr(engine, "MIN_BARS", 50) or 50)
        warmup_limit = int(
            getattr(cfg, "live_factor_warmup_bars", 80) or 80
        )
        warmup_feed = runtime.build_warmup_feed(
            frame,
            timeframe=timeframe,
            min_warmup=minimum_warmup,
            warmup_limit=warmup_limit,
        )
        warmup_frame = warmup_feed["warmup_df"]
        warmup_bars = warmup_feed["warmup_bars"]
        log(
            f"Factor pipeline warmup feeding {len(warmup_frame)} / "
            f"{len(frame)} bars"
        )
        if hasattr(engine, "warmup_bars"):
            snapshots = list(engine.warmup_bars(warmup_bars) or [])
        else:
            for bar in warmup_bars:
                values = engine.append_bar(bar)
                if values:
                    snapshots.append(values)
        low_frequency_warmup = {
            "snapshots": [],
            "factor_counts": {},
            "daily_bar_count": 0,
        }
        if runtime.build_low_frequency_snapshots is not None:
            try:
                as_of = warmup_frame.index[-1] if len(warmup_frame) else None
                low_frequency_warmup = runtime.build_low_frequency_snapshots(
                    signal_config=getattr(cfg, "factor_signal_config", {}) or {},
                    as_of=as_of,
                ) or low_frequency_warmup
                daily_snapshots = list(
                    low_frequency_warmup.get("snapshots") or []
                )
                normalizer = pipeline["normalizer"]
                if hasattr(normalizer, "configure_low_frequency_fallback"):
                    normalizer.configure_low_frequency_fallback(
                        runtime.build_low_frequency_snapshots,
                        getattr(cfg, "factor_signal_config", {}) or {},
                    )
                if hasattr(normalizer, "seed_low_frequency_fallback"):
                    normalizer.seed_low_frequency_fallback(
                        low_frequency_warmup,
                        refreshed_at=as_of,
                    )
                if daily_snapshots:
                    normalizer.warmup(daily_snapshots)
                log(
                    "Low-frequency factor warmup: "
                    f"daily_bars={int(low_frequency_warmup.get('daily_bar_count', 0) or 0)} "
                    f"snapshots={len(daily_snapshots)} "
                    f"factors={low_frequency_warmup.get('factor_counts', {})}"
                )
            except Exception as exc:
                log(
                    "Low-frequency factor warmup failed non-fatally: "
                    f"{type(exc).__name__}: {exc}"
                )
        if snapshots:
            # Intraday history follows daily history so the newest closed M5
            # observation is the final sample seen by the normalizer.
            pipeline["normalizer"].warmup(snapshots)

        initial_signal = _publish_initial_factor_signal(
            pipeline=pipeline,
            frame=frame,
            timeframe=timeframe,
            snapshots=snapshots,
            log=log,
            runtime=runtime,
        )
        log(
            f"Factor pipeline warmed up: {len(frame)} bars, "
            f"buffer={engine.buffer_size}, warm={engine.is_warm}"
        )
        projection_ack = None
        if engine.is_warm:
            projection_ack = runtime.acknowledge_projections(
                engine=engine,
                generation_id=str(generation_id or ""),
                log=log,
            )
        return {
            "ok": True,
            "skipped": False,
            "reason": "warmed",
            "warm": bool(engine.is_warm),
            "snapshot_count": len(snapshots),
            "low_frequency_warmup": {
                "daily_bar_count": int(
                    low_frequency_warmup.get("daily_bar_count", 0) or 0
                ),
                "snapshot_count": len(
                    low_frequency_warmup.get("snapshots") or []
                ),
                "factor_counts": dict(
                    low_frequency_warmup.get("factor_counts") or {}
                ),
                "factor_errors": dict(
                    low_frequency_warmup.get("factor_errors") or {}
                ),
            },
            "initial_signal": initial_signal,
            "projection_ack": projection_ack,
        }
    except Exception as exc:
        log(f"Factor pipeline warmup failed: {exc}")
        return {
            "ok": False,
            "skipped": False,
            "reason": f"warmup_failed:{type(exc).__name__}",
            "warm": False,
            "snapshot_count": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _publish_initial_factor_signal(
    *,
    pipeline: dict[str, Any],
    frame: Any,
    timeframe: str,
    snapshots: list[Any],
    log: Any,
    runtime: FactorWarmupRuntime,
) -> dict[str, Any]:
    engine = pipeline["engine"]
    if not engine.is_warm or not snapshots:
        return {"published": False, "reason": "not_warm"}
    try:
        last_values = dict(snapshots[-1] or {})
        normalizer = pipeline["normalizer"]
        if hasattr(normalizer, "resolve_factor_values"):
            last_values = normalizer.resolve_factor_values(last_values)
        pipeline["last_factor_values"] = dict(last_values or {})
        last_bar = {
            "open": float(frame["open"].iloc[-1]),
            "high": float(frame["high"].iloc[-1]),
            "low": float(frame["low"].iloc[-1]),
            "close": float(frame["close"].iloc[-1]),
            "volume": (
                float(frame["volume"].iloc[-1])
                if "volume" in frame.columns
                else 0.0
            ),
            "time": (
                float(frame.index[-1].timestamp())
                if hasattr(frame.index[-1], "timestamp")
                else 0.0
            ),
            "timeframe": timeframe,
            "complete": True,
        }
        signals = normalizer.normalize(last_values)
        composite = pipeline["compositor"].compose(signals, last_values)
        gate_result = pipeline["gate"].filter(
            composite,
            last_values,
            last_bar,
        )
        pipeline["gate"].tick()
        runtime.set_factor_snapshot(
            runtime.build_factor_votes(
                signals,
                last_values,
                getattr(composite, "factor_roles", {}),
                getattr(composite, "active_weights", {}),
            ),
            runtime.build_snapshot_summary(
                composite,
                gate_result,
                now=runtime.now(),
                decision_bar_ts=last_bar.get("time"),
            ),
        )
        direction_name = {1: "LONG", -1: "SHORT"}.get(
            composite.direction,
            "FLAT",
        )
        log(
            f"warmup signal: {direction_name} "
            f"score={composite.score:.4f} "
            f"n={composite.n_active_factors} gate={gate_result.reason}"
        )
        return {
            "published": True,
            "direction": int(composite.direction),
            "score": float(composite.score),
            "gate_reason": str(gate_result.reason or ""),
        }
    except Exception as exc:
        log(f"warmup signal generation failed (non-fatal): {exc}")
        return {
            "published": False,
            "reason": f"initial_signal_failed:{type(exc).__name__}",
            "error": f"{type(exc).__name__}: {exc}",
        }
