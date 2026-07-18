"""Generation-owned lifecycle controller for the live trading loop."""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable


LOOP_STATES = frozenset({"starting", "running", "degraded", "draining", "stopped", "failed"})
START_BLOCKING_STATES = frozenset({"starting", "running", "degraded", "draining"})
STARTUP_BARRIER_STEPS = (
    "broker_ready",
    "fresh_account",
    "fresh_positions",
    "unknown_execution_recovered",
    "session_restored",
    "recovery_attached",
    "initial_safety_cycle",
    "factor_warmup",
)


@dataclass
class LoopGeneration:
    generation_id: str
    broker: str
    strategy_name: str
    state: str
    stop_event: threading.Event
    created_at: float
    updated_at: float
    thread: threading.Thread | None = None
    ready: bool = False
    accepting_new_risk: bool = False
    startup_barrier: dict[str, bool] = field(
        default_factory=lambda: {step: False for step in STARTUP_BARRIER_STEPS}
    )
    blockers: list[str] = field(default_factory=list)
    components: dict[str, str] = field(default_factory=dict)
    safety_heartbeat_at: float = 0.0
    alpha_heartbeat_at: float = 0.0
    failed_reason: str = ""
    exit_acknowledged: bool = False


class LiveLoopController:
    """Own exactly one live-loop generation until its thread has exited."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._current: LoopGeneration | None = None

    def begin_start(self, *, broker: str, strategy_name: str) -> LoopGeneration:
        with self._lock:
            current = self._current
            if current is not None:
                alive = bool(current.thread and current.thread.is_alive())
                if current.state in START_BLOCKING_STATES or alive:
                    raise RuntimeError(
                        f"live_loop_generation_busy:{current.state}:{current.generation_id}"
                    )
            now = self._clock()
            generation = LoopGeneration(
                generation_id=str(uuid.uuid4()),
                broker=str(broker),
                strategy_name=str(strategy_name),
                state="starting",
                stop_event=threading.Event(),
                created_at=now,
                updated_at=now,
            )
            self._current = generation
            return generation

    def bind_thread(self, generation_id: str, thread: threading.Thread) -> None:
        with self._lock:
            generation = self._require_owner(generation_id)
            if generation.thread is not None and generation.thread is not thread:
                raise RuntimeError("generation_thread_already_bound")
            generation.thread = thread
            generation.updated_at = self._clock()

    def bind_component(self, generation_id: str, name: str) -> None:
        with self._lock:
            generation = self._require_owner(generation_id)
            generation.components[str(name)] = generation_id
            generation.updated_at = self._clock()

    def complete_barrier_step(self, generation_id: str, step: str) -> bool:
        if step not in STARTUP_BARRIER_STEPS:
            raise ValueError(f"unknown_startup_barrier_step:{step}")
        with self._lock:
            generation = self._require_owner(generation_id)
            if generation.state not in {"starting", "degraded"}:
                raise RuntimeError(f"barrier_update_invalid_state:{generation.state}")
            generation.startup_barrier[step] = True
            generation.ready = all(generation.startup_barrier.values())
            generation.accepting_new_risk = generation.ready and not generation.stop_event.is_set()
            generation.state = "running" if generation.ready else "starting"
            generation.blockers = [name for name, ok in generation.startup_barrier.items() if not ok]
            generation.updated_at = self._clock()
            return generation.ready

    def mark_degraded(self, generation_id: str, reason: str) -> None:
        with self._lock:
            generation = self._require_owner(generation_id)
            if generation.state == "draining":
                return
            generation.state = "degraded"
            generation.ready = False
            generation.accepting_new_risk = False
            if reason and reason not in generation.blockers:
                generation.blockers.append(reason)
            generation.updated_at = self._clock()

    def heartbeat(self, generation_id: str, plane: str) -> None:
        with self._lock:
            generation = self._require_owner(generation_id)
            now = self._clock()
            if plane == "safety":
                generation.safety_heartbeat_at = now
            elif plane == "alpha":
                generation.alpha_heartbeat_at = now
            else:
                raise ValueError(f"unknown_heartbeat_plane:{plane}")
            generation.updated_at = now

    def request_stop(self, generation_id: str | None = None) -> LoopGeneration | None:
        with self._lock:
            generation = self._current
            if generation is None:
                return None
            if generation_id is not None and generation.generation_id != generation_id:
                raise RuntimeError("generation_ownership_mismatch")
            generation.state = "draining"
            generation.ready = False
            generation.accepting_new_risk = False
            generation.stop_event.set()
            generation.updated_at = self._clock()
            return generation

    def acknowledge_exit(self, generation_id: str, *, failed_reason: str = "") -> None:
        with self._lock:
            generation = self._require_owner(generation_id)
            generation.exit_acknowledged = True
            generation.state = "failed" if failed_reason else "stopped"
            generation.failed_reason = str(failed_reason or "")
            generation.ready = False
            generation.accepting_new_risk = False
            generation.updated_at = self._clock()

    def status(self) -> dict[str, Any]:
        with self._lock:
            generation = self._current
            if generation is None:
                return {
                    "phase": "stopped",
                    "generation": "",
                    "thread_alive": False,
                    "ready": False,
                    "accepting_new_risk": False,
                    "safety_heartbeat_at": 0.0,
                    "alpha_heartbeat_at": 0.0,
                    "blockers": [],
                }
            thread_alive = bool(generation.thread and generation.thread.is_alive())
            blockers = list(generation.blockers)
            if generation.state in {"stopped", "failed"} and thread_alive:
                blockers.append("thread_exit_pending")
            return {
                "phase": generation.state,
                "generation": generation.generation_id,
                "thread_alive": thread_alive,
                "ready": generation.ready,
                "accepting_new_risk": generation.accepting_new_risk,
                "broker": generation.broker,
                "strategy_name": generation.strategy_name,
                "created_at": generation.created_at,
                "updated_at": generation.updated_at,
                "safety_heartbeat_at": generation.safety_heartbeat_at,
                "alpha_heartbeat_at": generation.alpha_heartbeat_at,
                "startup_barrier": dict(generation.startup_barrier),
                "blockers": sorted(set(blockers)),
                "failed_reason": generation.failed_reason,
                "components": dict(generation.components),
            }

    def current(self) -> LoopGeneration | None:
        with self._lock:
            return self._current

    def _require_owner(self, generation_id: str) -> LoopGeneration:
        generation = self._current
        if generation is None or generation.generation_id != generation_id:
            raise RuntimeError("generation_ownership_mismatch")
        return generation
