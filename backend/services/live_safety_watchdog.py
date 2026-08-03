"""Process-local watchdog for the Phase 2 safety freshness contract.

The watchdog never calls the broker.  It only observes timestamps produced by
the single broker-owning loop and asks the safety-state boundary to latch
``no_new_risk`` when that loop can no longer prove fresh protection facts.
"""
from __future__ import annotations

from dataclasses import dataclass
import threading
import time
import logging
from typing import Any, Callable, Mapping


_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class SafetyFreshnessResult:
    enabled: bool
    running: bool
    ok: bool
    state: str
    blockers: tuple[str, ...]
    ages: Mapping[str, float | None]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "live_safety_watchdog.v1",
            "enabled": self.enabled,
            "running": self.running,
            "ok": self.ok,
            "state": self.state,
            "blockers": list(self.blockers),
            "ages": dict(self.ages),
        }


def _age(timestamp: Any, now: float) -> float | None:
    try:
        value = float(timestamp or 0.0)
    except (TypeError, ValueError):
        value = 0.0
    return max(0.0, now - value) if value > 0.0 else None


def evaluate_safety_freshness(
    snapshot: Mapping[str, Any],
    *,
    now: float | None = None,
    stale_after_sec: float = 15.0,
) -> SafetyFreshnessResult:
    checked_at = float(time.time() if now is None else now)
    enabled = bool(snapshot.get("enabled"))
    running = bool(snapshot.get("running"))
    ages = {
        "safety": _age(snapshot.get("safety_heartbeat_at"), checked_at),
        "account": _age(snapshot.get("account_updated_at"), checked_at),
        "positions": _age(snapshot.get("positions_updated_at"), checked_at),
    }
    if not enabled:
        return SafetyFreshnessResult(
            enabled=False,
            running=running,
            ok=True,
            state="not_applicable",
            blockers=(),
            ages=ages,
        )
    if not running:
        return SafetyFreshnessResult(
            enabled=True,
            running=False,
            ok=True,
            state="idle",
            blockers=(),
            ages=ages,
        )

    threshold = max(1.0, float(stale_after_sec))
    started_age = _age(snapshot.get("started_at"), checked_at)
    startup_grace = started_age is not None and started_age <= threshold
    blockers: list[str] = []
    for key, age in ages.items():
        if age is None:
            if not startup_grace:
                blockers.append(f"{key}_freshness_unknown")
        elif age > threshold:
            blockers.append(f"{key}_freshness_stale")

    raw_unknown = snapshot.get("unknown_execution_count")
    try:
        unknown_count = int(raw_unknown) if raw_unknown is not None else None
    except (TypeError, ValueError):
        unknown_count = None
    if unknown_count is None:
        if not startup_grace:
            blockers.append("unknown_execution_status_unavailable")
    elif unknown_count > 0:
        blockers.append("unresolved_execution_intent")

    unique = tuple(sorted(set(blockers)))
    startup_unknown = bool(
        startup_grace
        and (
            any(age is None for age in ages.values())
            or unknown_count is None
        )
    )
    return SafetyFreshnessResult(
        enabled=True,
        running=True,
        ok=not unique,
        # During startup the generation barrier already blocks new risk, so
        # missing facts do not need a durable latch.  They are nevertheless
        # unknown, never a current/fresh observation.
        state=(
            "startup_unknown"
            if startup_unknown and not unique
            else "current"
            if not unique
            else "unsafe"
        ),
        blockers=unique,
        ages=ages,
    )


class LiveSafetyWatchdog:
    """Small daemon that evaluates freshness without touching the broker."""

    def __init__(
        self,
        *,
        probe: Callable[[], Mapping[str, Any]],
        on_violation: Callable[[SafetyFreshnessResult], Any],
        on_recovery: Callable[[SafetyFreshnessResult], Any] | None = None,
        recovery_checks: int = 3,
        interval_sec: float = 5.0,
        stale_after_sec: float = 15.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._probe = probe
        self._on_violation = on_violation
        self._on_recovery = on_recovery
        self._recovery_checks = max(1, int(recovery_checks))
        self._consecutive_current = 0
        self._interval_sec = max(0.1, float(interval_sec))
        self._stale_after_sec = max(1.0, float(stale_after_sec))
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> SafetyFreshnessResult:
        result = evaluate_safety_freshness(
            self._probe(),
            now=self._clock(),
            stale_after_sec=self._stale_after_sec,
        )
        if result.enabled and result.running and not result.ok:
            self._consecutive_current = 0
            self._on_violation(result)
        elif result.enabled and result.running and result.state == "current":
            self._consecutive_current += 1
            if (
                self._on_recovery is not None
                and self._consecutive_current >= self._recovery_checks
            ):
                self._on_recovery(result)
                # Require another complete healthy window before retrying a
                # failed/idempotent release instead of writing every tick.
                self._consecutive_current = 0
        else:
            self._consecutive_current = 0
        return result

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return False
        self._stop.clear()

        def _run() -> None:
            while not self._stop.wait(self._interval_sec):
                try:
                    self.run_once()
                except Exception as exc:
                    # The safety callback installs its own process-local
                    # fail-closed latch on persistence failure.  A watchdog
                    # exception must not terminate future checks.
                    _LOGGER.warning("safety watchdog check failed closed: %s", exc)
                    continue

        self._thread = threading.Thread(
            target=_run,
            name="live-safety-watchdog",
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self, *, timeout_sec: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, float(timeout_sec)))
        self._thread = None
