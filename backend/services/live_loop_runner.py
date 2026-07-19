"""Generation-bound serial tick runner for the live trading loop."""

from __future__ import annotations

import traceback
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SerialLiveTickRuntime:
    set_loop_diagnostic: Any
    run_tick_body: Any
    factor_pipeline: Any
    acknowledge_factor_projections: Any
    live_state_update: Any
    phase2_active: Any
    update_risk_metrics: Any


def run_serial_live_ticks(
    *,
    broker: str,
    stop_flag: Any,
    bridge_cfg: Any,
    timeframe: str,
    generation_id: str,
    log: Any,
    runtime: SerialLiveTickRuntime,
) -> dict[str, Any]:
    """Run broker mutations serially until stop, break, or fatal boundary."""

    tick = 0
    recovery_bootstrapped = False
    exit_reason = "stop_requested"
    while not stop_flag.is_set():
        tick += 1
        runtime.set_loop_diagnostic(tick, "checking")
        try:
            tick_result = runtime.run_tick_body(
                broker=broker,
                bridge_cfg=bridge_cfg,
                timeframe=timeframe,
                tick=tick,
                recovery_bootstrapped=recovery_bootstrapped,
                stop_requested=stop_flag.is_set,
                log=log,
                generation_id=generation_id,
            )
            recovery_bootstrapped = bool(
                tick_result["recovery_bootstrapped"]
            )
            _acknowledge_warm_factor_projection(
                generation_id=generation_id,
                runtime=runtime,
            )
            if tick_result["break_loop"]:
                exit_reason = "tick_requested_break"
                break
            wait_seconds = tick_result.get("wait_seconds")
            if wait_seconds is not None:
                if stop_flag.wait(float(wait_seconds)):
                    exit_reason = "stop_during_tick_wait"
                    break
                continue
        except Exception as exc:
            log(
                f"tick {tick} error: {type(exc).__name__}: {exc}\n"
                f"{traceback.format_exc()[-300:]}"
            )
            runtime.live_state_update(accepting_new_risk=False)
            if runtime.phase2_active():
                if stop_flag.wait(5.0):
                    exit_reason = "stop_during_safety_retry"
                    break
                continue

        runtime.update_risk_metrics(tick=tick, log=log)
        if stop_flag.wait(60.0):
            exit_reason = "stop_during_alpha_wait"
            break

    log(f"loop stopped after {tick} ticks")
    return {
        "tick_count": tick,
        "recovery_bootstrapped": recovery_bootstrapped,
        "exit_reason": exit_reason,
    }


def _acknowledge_warm_factor_projection(
    *,
    generation_id: str,
    runtime: SerialLiveTickRuntime,
) -> None:
    pipeline = runtime.factor_pipeline() or {}
    engine = pipeline.get("engine")
    if engine is None or not bool(getattr(engine, "is_warm", False)):
        return
    pipeline["factor_projection_ack"] = (
        runtime.acknowledge_factor_projections(
            engine=engine,
            generation_id=str(generation_id or ""),
        )
    )
