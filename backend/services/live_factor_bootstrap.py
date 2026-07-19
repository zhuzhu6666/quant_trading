"""Factor pipeline initialization helpers for the live-loop bootstrap."""

from __future__ import annotations

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
        if snapshots:
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
        last_values = snapshots[-1]
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
        signals = pipeline["normalizer"].normalize(last_values)
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
