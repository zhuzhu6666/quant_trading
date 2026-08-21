"""Generation-owned lifecycle controller for the live trading loop."""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from backend.services.live_reconciliation import LIVE_SAFETY_FRESHNESS_SEC


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
    runtime_blockers: list[str] = field(default_factory=list)
    components: dict[str, str] = field(default_factory=dict)
    safety_heartbeat_at: float = 0.0
    alpha_heartbeat_at: float = 0.0
    failed_reason: str = ""
    exit_acknowledged: bool = False


@dataclass(frozen=True)
class LoopOwnershipSnapshot:
    """Read-only ownership view derived from the current generation."""

    thread: threading.Thread | None
    stop_flag: threading.Event | None
    broker: str | None
    started_at: float | None
    strategy_name: str | None


class LiveLoopController:
    """Own exactly one live-loop generation until its thread has exited."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._current: LoopGeneration | None = None
        self._last_exit_at = 0.0

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
            generation.accepting_new_risk = bool(
                generation.ready
                and not generation.runtime_blockers
                and not generation.stop_event.is_set()
            )
            if generation.ready:
                generation.state = "running" if generation.accepting_new_risk else "degraded"
            else:
                generation.state = "starting"
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
            if reason and reason not in generation.runtime_blockers:
                generation.runtime_blockers.append(reason)
            generation.updated_at = self._clock()

    def update_runtime_health(
        self,
        generation_id: str,
        *,
        blockers: list[str] | tuple[str, ...],
    ) -> bool:
        """Update transient fail-closed blockers without reopening the startup barrier.

        The generation becomes runnable again only when every startup step was
        completed and the latest runtime safety snapshot has no blocker.
        """
        with self._lock:
            generation = self._require_owner(generation_id)
            if generation.state == "draining":
                return False
            generation.runtime_blockers = sorted(
                {str(item) for item in blockers if str(item or "").strip()}
            )
            generation.ready = all(generation.startup_barrier.values())
            generation.accepting_new_risk = bool(
                generation.ready
                and not generation.runtime_blockers
                and not generation.stop_event.is_set()
            )
            if generation.ready:
                generation.state = "running" if generation.accepting_new_risk else "degraded"
            else:
                generation.state = "starting"
            generation.blockers = [
                name for name, ok in generation.startup_barrier.items() if not ok
            ]
            generation.updated_at = self._clock()
            return generation.accepting_new_risk

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
            now = self._clock()
            generation.exit_acknowledged = True
            generation.state = "failed" if failed_reason else "stopped"
            generation.failed_reason = str(failed_reason or "")
            generation.ready = False
            generation.accepting_new_risk = False
            generation.updated_at = now
            self._last_exit_at = now

    def clear_thread_if(
        self,
        generation_id: str,
        thread: threading.Thread,
        finished_at: float | None = None,
    ) -> bool:
        """Release a generation's thread identity only after it has exited."""

        with self._lock:
            generation = self._require_owner(generation_id)
            if generation.thread is not thread:
                return False
            is_alive = getattr(thread, "is_alive", None)
            if callable(is_alive) and is_alive():
                return False
            finished = float(finished_at if finished_at is not None else self._clock())
            generation.thread = None
            generation.updated_at = finished
            self._last_exit_at = max(self._last_exit_at, finished)
            return True

    def ownership_snapshot(self) -> LoopOwnershipSnapshot:
        """Return ownership facts without creating a second state store."""

        with self._lock:
            generation = self._current
            if generation is None:
                return LoopOwnershipSnapshot(
                    thread=None,
                    stop_flag=None,
                    broker=None,
                    started_at=None,
                    strategy_name=None,
                )
            return LoopOwnershipSnapshot(
                thread=generation.thread,
                stop_flag=generation.stop_event,
                broker=generation.broker,
                started_at=generation.created_at,
                strategy_name=generation.strategy_name,
            )

    def last_exit_at(self) -> float:
        with self._lock:
            return float(self._last_exit_at)

    def status(self) -> dict[str, Any]:
        with self._lock:
            generation = self._current
            if generation is None:
                return {
                    "phase": "stopped",
                    "generation": "",
                    "thread_alive": False,
                    "thread_id": None,
                    "ready": False,
                    "accepting_new_risk": False,
                    "last_exit_at": self._last_exit_at or None,
                    "safety_heartbeat_at": None,
                    "alpha_heartbeat_at": None,
                    "safety_heartbeat_age_sec": None,
                    "alpha_heartbeat_age_sec": None,
                    "blockers": [],
                }
            thread_alive = bool(generation.thread and generation.thread.is_alive())
            thread_id = getattr(generation.thread, "ident", None)
            now = self._clock()
            safety_age = (
                max(0.0, now - generation.safety_heartbeat_at)
                if generation.safety_heartbeat_at > 0
                else None
            )
            alpha_age = (
                max(0.0, now - generation.alpha_heartbeat_at)
                if generation.alpha_heartbeat_at > 0
                else None
            )
            pending_startup = [
                name for name, ok in generation.startup_barrier.items() if not ok
            ]
            blockers = pending_startup + list(generation.blockers) + list(generation.runtime_blockers)
            heartbeat_healthy = (
                safety_age is not None
                and safety_age <= LIVE_SAFETY_FRESHNESS_SEC
            )
            if generation.state in {"running", "degraded"} and not heartbeat_healthy:
                blockers.append(
                    "safety_heartbeat_unknown" if safety_age is None else "safety_heartbeat_stale"
                )
            if generation.state in {"stopped", "failed"} and thread_alive:
                blockers.append("thread_exit_pending")
            effective_phase = (
                "degraded"
                if generation.state == "running" and not heartbeat_healthy
                else generation.state
            )
            return {
                "phase": effective_phase,
                "generation": generation.generation_id,
                "thread_alive": thread_alive,
                "thread_id": thread_id,
                "ready": generation.ready,
                "accepting_new_risk": bool(
                    generation.accepting_new_risk and heartbeat_healthy
                ),
                "broker": generation.broker,
                "strategy_name": generation.strategy_name,
                "created_at": generation.created_at,
                "updated_at": generation.updated_at,
                "last_exit_at": self._last_exit_at or None,
                "safety_heartbeat_at": generation.safety_heartbeat_at or None,
                "alpha_heartbeat_at": generation.alpha_heartbeat_at or None,
                "safety_heartbeat_age_sec": safety_age,
                "alpha_heartbeat_age_sec": alpha_age,
                "startup_barrier": dict(generation.startup_barrier),
                "blockers": sorted(set(blockers)),
                "failed_reason": generation.failed_reason,
                "components": dict(generation.components),
            }

    def accepting_new_risk(self, generation_id: str | None = None) -> bool:
        with self._lock:
            generation = self._current
            if generation is None:
                return False
            if generation_id is not None and generation.generation_id != generation_id:
                return False
            if generation.state != "running" or generation.stop_event.is_set():
                return False
            if not generation.ready or generation.runtime_blockers:
                return False
            if generation.safety_heartbeat_at <= 0:
                return False
            return (
                self._clock() - generation.safety_heartbeat_at
                <= LIVE_SAFETY_FRESHNESS_SEC
            )

    def current(self) -> LoopGeneration | None:
        with self._lock:
            return self._current

    def _require_owner(self, generation_id: str) -> LoopGeneration:
        generation = self._current
        if generation is None or generation.generation_id != generation_id:
            raise RuntimeError("generation_ownership_mismatch")
        return generation
