"""Generation-safe stop orchestration for the live loop."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

@dataclass(frozen=True)
class LiveLoopStopRuntime:
    state_lock: Any
    controller: Any
    admission_lock: Any
    live_state_update: Any
    persist_desired_state: Any
    runtime_kv_set: Any
    last_shutdown_key: str
    now: Any
    thread_factory: Any
    persist_safety_fail_closed: Any
    logger_info: Any


def stop_live_loop(
    *,
    persist_desired: bool,
    trigger_reason: str,
    runtime: LiveLoopStopRuntime,
) -> dict[str, Any]:
    """Request draining while retaining ownership until the thread exits."""

    return _stop_generation(
        persist_desired=persist_desired,
        trigger_reason=trigger_reason,
        runtime=runtime,
    )


def _stop_generation(
    *,
    persist_desired: bool,
    trigger_reason: str,
    runtime: LiveLoopStopRuntime,
) -> dict[str, Any]:
    with runtime.state_lock:
        state = runtime.controller.ownership_snapshot()
        thread = state.thread
        broker = state.broker
        strategy_name = state.strategy_name or "factor_v4"
        generation = runtime.controller.current()
        if thread is None or not thread.is_alive() or generation is None:
            if persist_desired:
                runtime.persist_desired_state(
                    False,
                    broker=broker or "ctrader",
                    strategy_name=strategy_name,
                    reason=trigger_reason,
                )
            return {
                "ok": True,
                "was_running": False,
                "broker": broker,
                **runtime.controller.status(),
                "msg": "no loop running",
            }
        runtime.controller.request_stop(generation.generation_id)
        with runtime.admission_lock:
            runtime.live_state_update(accepting_new_risk=False)
        if persist_desired:
            try:
                runtime.persist_desired_state(
                    False,
                    broker=broker or "ctrader",
                    strategy_name=strategy_name,
                    reason=trigger_reason,
                )
            except Exception as exc:
                runtime.persist_safety_fail_closed(
                    blockers=["loop_desired_state_persist_failed"],
                    source="live_loop_stop",
                    error=f"{type(exc).__name__}: {exc}",
                )
        runtime.runtime_kv_set(
            runtime.last_shutdown_key,
            {
                "broker": broker,
                "generation": generation.generation_id,
                "status": "draining",
                "ts": runtime.now(),
                "trigger_reason": trigger_reason,
            },
        )

    def cleanup_generation() -> None:
        thread.join()
        with runtime.state_lock:
            runtime.controller.clear_thread_if(
                generation.generation_id,
                thread,
                runtime.now(),
            )
        runtime.logger_info(
            "[live] generation %s fully stopped; ownership released",
            generation.generation_id,
        )

    runtime.thread_factory(
        target=cleanup_generation,
        name=f"stop_loop_cleanup_{generation.generation_id[:8]}",
        daemon=True,
    ).start()
    return {
        "ok": True,
        "was_running": True,
        "status": "draining",
        "broker": broker,
        "trigger_reason": trigger_reason,
        **runtime.controller.status(),
    }
