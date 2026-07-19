"""Generation-safe stop orchestration for the live loop."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LoopOwnershipSnapshot:
    thread: Any
    stop_flag: Any
    broker: str | None
    started_at: float | None
    strategy_name: str | None


@dataclass(frozen=True)
class LiveLoopStopRuntime:
    generation_controller_enabled: Any
    state_lock: Any
    snapshot_ownership: Any
    clear_ownership_if: Any
    ensure_stop_event: Any
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

    if runtime.generation_controller_enabled():
        return _stop_generation(
            persist_desired=persist_desired,
            trigger_reason=trigger_reason,
            runtime=runtime,
        )
    return _stop_legacy(
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
        state = runtime.snapshot_ownership()
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
            runtime.persist_desired_state(
                False,
                broker=broker or "ctrader",
                strategy_name=strategy_name,
                reason=trigger_reason,
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
            runtime.clear_ownership_if(thread, runtime.now())
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
        "broker": broker,
        "trigger_reason": trigger_reason,
        **runtime.controller.status(),
    }


def _stop_legacy(
    *,
    persist_desired: bool,
    trigger_reason: str,
    runtime: LiveLoopStopRuntime,
) -> dict[str, Any]:
    requested_at = runtime.now()
    with runtime.state_lock:
        state = runtime.snapshot_ownership()
        thread = state.thread
        broker = state.broker
        strategy_name = state.strategy_name or "factor_v4"
        if thread is None or not thread.is_alive():
            runtime.clear_ownership_if(thread, requested_at)
            was_running = False
            draining = None
        else:
            stop_flag = state.stop_flag or runtime.ensure_stop_event()
            stop_flag.set()
            draining = {
                "schema_version": "live_loop_shutdown.v2",
                "status": "draining",
                "phase": "draining",
                "ok": True,
                "was_running": True,
                "broker": broker,
                "strategy_name": strategy_name,
                "thread_id": getattr(thread, "ident", None),
                "thread_alive": True,
                "ready": False,
                "accepting_new_risk": False,
                "ownership_released": False,
                "requested_at": requested_at,
                "trigger_reason": trigger_reason,
            }
            runtime.live_state_update(
                loop_shutdown=draining,
                accepting_new_risk=False,
            )
            was_running = True

    if was_running:
        with runtime.admission_lock:
            runtime.live_state_update(
                loop_shutdown=draining,
                accepting_new_risk=False,
            )

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
                source="legacy_loop_stop",
                error=f"{type(exc).__name__}: {exc}",
            )

    if not was_running:
        return {
            "ok": True,
            "was_running": False,
            "broker": broker,
            "phase": "stopped",
            "thread_alive": False,
            "ready": False,
            "accepting_new_risk": False,
            "msg": "no loop running",
        }

    runtime.runtime_kv_set(runtime.last_shutdown_key, draining)

    def cleanup_legacy() -> None:
        thread.join()
        finished_at = runtime.now()
        with runtime.state_lock:
            ownership_released = runtime.clear_ownership_if(
                thread,
                finished_at,
            )
        completed = {
            **draining,
            "status": "completed",
            "phase": "stopped",
            "thread_alive": False,
            "ownership_released": ownership_released,
            "replacement_detected": not ownership_released,
            "finished_at": finished_at,
        }
        if ownership_released:
            runtime.live_state_update(
                loop_shutdown=completed,
                accepting_new_risk=False,
            )
        runtime.runtime_kv_set(runtime.last_shutdown_key, completed)
        runtime.logger_info(
            "[live] legacy loop fully stopped; ownership_released=%s",
            ownership_released,
        )

    runtime.thread_factory(
        target=cleanup_legacy,
        name="stop_loop_cleanup_legacy",
        daemon=True,
    ).start()
    runtime.logger_info(
        "[live] legacy stop signaled; ownership retained while draining"
    )
    return dict(draining)
