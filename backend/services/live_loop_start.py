"""Generation-safe start orchestration for the live loop."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LiveLoopStartRuntime:
    state_lock: Any
    process_shutdown_requested: Any
    controller: Any
    last_loop_end: Any
    now: Any
    sleep: Any
    logger_warning: Any
    logger_info: Any
    persist_desired_state: Any
    prime_live_loop_state: Any
    start_safety_watchdog: Any
    start_scheduler: Any
    stop_scheduler: Any
    stop_safety_watchdog: Any
    thread_factory: Any
    loop_target: Any
    live_state_update: Any


def start_live_loop(
    broker: str,
    strategy_name: str,
    *,
    persist_desired: bool,
    trigger_reason: str,
    runtime: LiveLoopStartRuntime,
) -> dict[str, Any]:
    """Start one owned generation after double-checked admission."""

    with runtime.state_lock:
        rejected = _start_rejection(
            broker=broker,
            strategy_name=strategy_name,
            runtime=runtime,
        )
        if rejected is not None:
            return rejected
        last_end = float(runtime.last_loop_end() or 0.0)
        since_end = runtime.now() - last_end if last_end else 999.0

    if last_end and since_end < 3.0:
        wait_seconds = 3.0 - since_end
        runtime.logger_warning(
            f"[live] restart backoff: waiting {wait_seconds:.1f}s"
        )
        runtime.sleep(wait_seconds)

    with runtime.state_lock:
        rejected = _post_backoff_rejection(
            broker=broker,
            strategy_name=strategy_name,
            runtime=runtime,
        )
        if rejected is not None:
            return rejected
        return _start_admitted_generation(
            broker=broker,
            strategy_name=strategy_name,
            persist_desired=persist_desired,
            trigger_reason=trigger_reason,
            runtime=runtime,
        )


def _start_rejection(
    *,
    broker: str,
    strategy_name: str,
    runtime: LiveLoopStartRuntime,
) -> dict[str, Any] | None:
    if runtime.process_shutdown_requested():
        return {
            "ok": False,
            "error": "process_shutdown_in_progress",
            "broker": broker,
            "strategy_name": strategy_name,
        }
    status = runtime.controller.status()
    if status.get("phase") in {
        "starting",
        "running",
        "degraded",
        "draining",
    }:
        return {
            "ok": False,
            "error": (
                "live_loop_generation_busy:"
                f"{status.get('phase')}:{status.get('generation')}"
            ),
            **status,
        }
    if status.get("thread_alive"):
        return {
            "ok": False,
            "error": (
                "live_loop_generation_exit_pending:"
                f"{status.get('generation')}"
            ),
            **status,
        }
    if broker != "ctrader":
        return {"ok": False, "error": f"unknown broker: {broker}"}
    return None


def _post_backoff_rejection(
    *,
    broker: str,
    strategy_name: str,
    runtime: LiveLoopStartRuntime,
) -> dict[str, Any] | None:
    if runtime.process_shutdown_requested():
        return {
            "ok": False,
            "error": "process_shutdown_in_progress",
            "broker": broker,
            "strategy_name": strategy_name,
        }
    status = runtime.controller.status()
    if not status.get("thread_alive"):
        return None
    return {
        "ok": False,
        "error": (
            "live_loop_generation_exit_pending:"
            f"{status.get('generation')}"
        ),
        **status,
    }


def _start_admitted_generation(
    *,
    broker: str,
    strategy_name: str,
    persist_desired: bool,
    trigger_reason: str,
    runtime: LiveLoopStartRuntime,
) -> dict[str, Any]:
    placeholder_account = {
        "ok": True,
        "broker": broker,
        "balance": 0,
        "equity": 0,
        "margin": 0,
        "margin_free": 0,
        "leverage": 0,
        "currency": "",
    }
    try:
        generation = runtime.controller.begin_start(
            broker=broker,
            strategy_name=strategy_name,
        )
    except RuntimeError as exc:
        return {
            "ok": False,
            "error": str(exc),
            **runtime.controller.status(),
        }

    thread = None
    started_at = float(generation.created_at)
    try:
        stop_event = generation.stop_event
        if persist_desired:
            runtime.persist_desired_state(
                True,
                broker=broker,
                strategy_name=strategy_name,
                reason=trigger_reason,
            )
        runtime.prime_live_loop_state(
            broker=broker,
            strategy_name=strategy_name,
            started_at=started_at,
            account=placeholder_account,
            accepting_new_risk=False,
            restore_session=True,
            account_observed=False,
        )
        runtime.start_safety_watchdog()
        runtime.start_scheduler()
        runtime.controller.bind_component(
            generation.generation_id,
            "scheduler",
        )
        runtime.controller.bind_component(
            generation.generation_id,
            "refresh_worker_inline",
        )
        thread = runtime.thread_factory(
            target=runtime.loop_target,
            args=(
                broker,
                stop_event,
                generation.generation_id,
            ),
            name=f"live_loop_{broker}",
            daemon=True,
        )
        runtime.controller.bind_thread(generation.generation_id, thread)
        thread.start()
        runtime.logger_info(
            f"live loop started: broker={broker} strategy={strategy_name} "
            f"thread_id={thread.ident}"
        )
    except Exception as exc:
        failed_reason = f"start_failed:{type(exc).__name__}:{exc}"
        runtime.controller.acknowledge_exit(
            generation.generation_id,
            failed_reason=failed_reason,
        )
        if thread is not None:
            runtime.controller.clear_thread_if(
                generation.generation_id,
                thread,
                runtime.now(),
            )
        runtime.live_state_update(
            loop_running=False,
            accepting_new_risk=False,
            startup_blocker=failed_reason,
        )
        try:
            runtime.stop_scheduler()
        except Exception:
            pass
        runtime.stop_safety_watchdog()
        return {
            "ok": False,
            "error": failed_reason,
            **runtime.controller.status(),
        }

    # The canonical V2 tick owner must complete its broker/safety/session
    # barrier before the start endpoint can advertise readiness or new-risk
    # admission.  The old phase2/off-path shortcut is intentionally gone.
    ready = False
    return {
        "ok": True,
        "broker": broker,
        "started_at": started_at,
        "thread_id": thread.ident,
        "pid": thread.ident,
        "strategy_name": strategy_name,
        "trigger_reason": trigger_reason,
        "generation": generation.generation_id,
        "phase": "starting",
        "ready": ready,
        "accepting_new_risk": ready,
        "msg": (
            "live loop thread started. Read /api/live/loop-status to monitor."
        ),
    }
