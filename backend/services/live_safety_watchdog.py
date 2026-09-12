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

from backend.services.live_reconciliation import LIVE_SAFETY_FRESHNESS_SEC


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
    stale_after_sec: float = LIVE_SAFETY_FRESHNESS_SEC,
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
    progress_age = _age(snapshot.get("safety_cycle_progress_at"), checked_at)
    cycle_refreshing = bool(
        snapshot.get("safety_cycle_active")
        and progress_age is not None
        and progress_age <= threshold
    )
    safety_age = ages["safety"]
    safety_completed_current = (
        safety_age is not None and safety_age <= threshold
    )
    blockers: list[str] = []
    if not cycle_refreshing:
        # Account/positions freshness has one canonical owner at the final
        # open-admission boundary (live_reconciliation).  The watchdog only
        # owns Safety completion liveness and unresolved execution intent;
        # retaining account/positions ages above is diagnostic, not a second
        # blocker calculator.
        if safety_age is None:
            if not startup_grace:
                blockers.append("safety_freshness_unknown")
        elif safety_age > threshold:
            blockers.append("safety_freshness_stale")

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
            ages["safety"] is None
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
            "refreshing"
            if cycle_refreshing and not safety_completed_current and not unique
            else
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
        stale_after_sec: float = LIVE_SAFETY_FRESHNESS_SEC,
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


_ls_module = None


def _live_service():
    """Lazy handle to the live loop module (import-order-safe)."""
    global _ls_module
    if _ls_module is None:
        from backend.services import live_service as _module
        _ls_module = _module
    return _ls_module


from backend.services import live_close_settlement, live_open_pipeline
from backend.services.live_reconciliation import (
    LIVE_SAFETY_FRESHNESS_SEC as _LIVE_SAFETY_FRESHNESS_SEC,
)
from backend.services.live_safety_state import activate_no_new_risk_latch, no_new_risk_latch_status, release_no_new_risk_latch_cause, safety_v2_forced_shadow_status

# moved from live_service (2026-09-12 structural repair)

def live_safety_watchdog_probe() -> dict[str, Any]:
    """Return process facts only; the watchdog never calls the broker."""

    safety = _live_service().live_state_get("safety_plane", {}, clone=True) or {}
    heartbeat_at = float(safety.get("heartbeat_at", 0.0) or 0.0)
    controller = _live_service()._LIVE_LOOP_CONTROLLER.status()
    thread_alive = bool(controller.get("thread_alive"))
    heartbeat_at = float(controller.get("safety_heartbeat_at", 0.0) or 0.0)
    unknown_raw = safety.get("unknown_execution_count")
    return {
        # The generation controller is the authoritative lifecycle owner; the
        # watchdog only observes its heartbeat and never calls the broker.
        "enabled": bool(
            thread_alive
            or controller.get("phase") in {"starting", "running", "degraded", "draining"}
        ),
        "running": thread_alive,
        "started_at": float(controller.get("created_at") or 0.0),
        "safety_heartbeat_at": heartbeat_at,
        # This is only a liveness hint for an active serial cycle.  It is not
        # a completed Safety fact and is never used by the open admission
        # boundary as proof of fresh account/positions.
        "safety_cycle_active": bool(
            _live_service().live_state_get("safety_cycle_active", False)
        ),
        "safety_cycle_progress_at": float(
            _live_service().live_state_get("safety_cycle_progress_at", 0.0) or 0.0
        ),
        "account_updated_at": float(_live_service().live_state_get("account_updated_at", 0.0) or 0.0),
        "positions_updated_at": float(_live_service().live_state_get("positions_updated_at", 0.0) or 0.0),
        "unknown_execution_count": unknown_raw,
    }


def persist_safety_fail_closed(
    *,
    blockers: list[str] | tuple[str, ...],
    source: str,
    error: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Durably block new risk without changing any broker action result."""

    normalized = sorted({str(item) for item in blockers if str(item)}) or [
        "safety_state_unavailable"
    ]
    latch = no_new_risk_latch_status(fail_closed=True)
    forced_shadow = "safety_v2_forced_shadow" in normalized
    # A normal freshness failure has no bearing on the V2 candidate authority.
    # Avoid replaying the separate forced-shadow projection (and its history)
    # on every watchdog probe; only the forced-shadow path needs it.
    persisted_forced_shadow = (
        safety_v2_forced_shadow_status()
        if forced_shadow
        else {"active": False}
    )
    latch_cause = "safety_v2_forced_shadow" if forced_shadow else "safety_freshness"
    latch_cause_id = (
        "candidate_comparison" if forced_shadow else str(source or "safety")
    )
    active_cause_keys = {
        (str(item.get("cause") or ""), str(item.get("cause_id") or ""))
        for item in list(latch.get("causes") or [])
        if isinstance(item, dict)
    }
    needs_latch_record = (latch_cause, latch_cause_id) not in active_cause_keys or (
        forced_shadow and not bool(persisted_forced_shadow.get("active"))
    )
    if needs_latch_record:
        try:
            latch = activate_no_new_risk_latch(
                reason=(
                    "safety_v2_forced_shadow"
                    if forced_shadow
                    else "safety_freshness_failed"
                ),
                actor=f"system:{source or 'safety'}",
                metadata={
                    "blockers": normalized,
                    "error": str(error or "")[:1000],
                    "forced_shadow": forced_shadow,
                    **dict(metadata or {}),
                },
                cause=latch_cause,
                cause_id=latch_cause_id,
            )
        except Exception as exc:
            # activate_no_new_risk_latch installs an in-process fail-closed
            # latch before raising on storage failure.
            latch = no_new_risk_latch_status(fail_closed=True)
            error = f"{error}; latch_error={type(exc).__name__}:{exc}".strip("; ")
    payload = {
        "schema_version": "live_safety_failure.v1",
        "status": "no_new_risk_latched",
        "source": str(source or "safety"),
        "blockers": normalized,
        "error": str(error or "")[:2000],
        "detected_at": time.time(),
        "latch": dict(latch or {}),
    }
    _live_service().live_state_update(
        accepting_new_risk=False,
        safety_failure=payload,
        no_new_risk_latch=latch,
    )
    current = _live_service()._LIVE_LOOP_CONTROLLER.current()
    if current is not None:
        try:
            _live_service()._LIVE_LOOP_CONTROLLER.update_runtime_health(
                current.generation_id,
                blockers=tuple(normalized),
            )
        except RuntimeError:
            pass
    if needs_latch_record or error:
        try:
            _live_service().append_safety_outbox(
                event_type="live_safety_fail_closed",
                payload=payload,
                error=str(error or ""),
            )
        except Exception as outbox_exc:
            _live_service().logger.error("[live] safety fail-closed outbox unavailable: {}", outbox_exc)
    return payload


def _on_live_safety_watchdog_violation(result: SafetyFreshnessResult) -> None:
    persist_safety_fail_closed(
        blockers=result.blockers,
        source="safety_watchdog",
    )


def _on_live_safety_watchdog_recovery(result: SafetyFreshnessResult) -> None:
    """Release freshness causes only after sustained, cause-specific recovery."""

    latch = no_new_risk_latch_status(fail_closed=True)
    active_causes = {
        (str(item.get("cause") or ""), str(item.get("cause_id") or "")): item
        for item in list(latch.get("causes") or [])
        if isinstance(item, dict)
    }
    base_evidence = {
        "state": result.state,
        "ages": dict(result.ages),
        "blockers": list(result.blockers),
        "recovery_checks": 3,
    }
    released = latch
    watchdog_cause = ("safety_freshness", "safety_watchdog")
    if watchdog_cause in active_causes:
        released = release_no_new_risk_latch_cause(
            cause=watchdog_cause[0],
            cause_id=watchdog_cause[1],
            reason="safety_freshness_sustained_recovery",
            actor="system:safety_watchdog",
            evidence=base_evidence,
        )

    # ``live_loop`` is a separate cause from the watchdog observation itself.
    # A fresh watchdog snapshot alone is not enough to release it: the
    # canonical tick owner must have completed a normal Safety cycle and both
    # account/position reconciles must be identifiable and current.  Keep the
    # check here, at the existing latch owner, so no caller can accidentally
    # turn a healthy timestamp into a blanket thaw.
    live_loop_cause = ("safety_freshness", "live_loop")
    live_loop_record = active_causes.get(live_loop_cause) or {}
    live_loop_probe: dict[str, Any] = {}
    safety_payload: dict[str, Any] = {}
    account_snapshot: dict[str, Any] = {}
    positions_snapshot: Any = None
    live_loop_recovered = False
    reconciliation_blockers: list[str] = []
    independent_safety_causes = {
        key
        for key in active_causes
        if key[0] == "safety_freshness"
        and key not in {watchdog_cause, live_loop_cause}
    }
    if (
        live_loop_record
        and not independent_safety_causes
        and result.enabled
        and result.running
        and result.ok
        and result.state == "current"
    ):
        try:
            live_loop_probe = dict(live_safety_watchdog_probe() or {})
            safety_payload = dict(
                _live_service().live_state_get("safety_plane", {}, clone=True) or {}
            )
            account_snapshot = dict(
                _live_service().live_state_get("account_reconciled", {}, clone=True) or {}
            )
            positions_snapshot = _live_service().live_state_get(
                "positions_reconciled", None, clone=True
            )
            reconciliation_blockers = live_open_pipeline.new_risk_reconciliation_blockers()
            account_id = str(
                _live_service().live_state_get("account_reconcile_id", "") or ""
            )
            positions_id = str(
                _live_service().live_state_get("positions_reconcile_id", "") or ""
            )
            live_loop_recovered = bool(
                live_loop_probe.get("running")
                and bool(_live_service().live_state_get("loop_running", False))
                and str(
                    _live_service().live_state_get("session_state_status", "unknown")
                    or "unknown"
                )
                == "available"
                and bool(account_snapshot.get("ok"))
                and bool(account_id)
                and isinstance(positions_snapshot, list)
                and bool(positions_id)
                and bool(safety_payload.get("accepting_new_risk"))
                and not bool(live_loop_probe.get("safety_cycle_active"))
                and not reconciliation_blockers
                and str(safety_payload.get("status") or "")
                not in {"", "exception", "failed", "unavailable"}
            )
        except Exception as exc:
            _live_service().logger.debug(
                "[live] live-loop latch recovery evidence unavailable: {}",
                exc,
            )
            live_loop_recovered = False
    if live_loop_record and live_loop_recovered:
        released = release_no_new_risk_latch_cause(
            cause=live_loop_cause[0],
            cause_id=live_loop_cause[1],
            reason="live_loop_safety_reconcile_recovered",
            actor="system:safety_watchdog",
            evidence={
                **base_evidence,
                "live_loop_heartbeat_at": live_loop_probe.get(
                    "safety_heartbeat_at"
                ),
                "account_reconcile_id": str(
                    _live_service().live_state_get("account_reconcile_id", "") or ""
                ),
                "positions_reconcile_id": str(
                    _live_service().live_state_get("positions_reconcile_id", "") or ""
                ),
                "session_state_status": str(
                    _live_service().live_state_get("session_state_status", "unknown")
                    or "unknown"
                ),
                "safety_status": str(safety_payload.get("status") or ""),
                "reconciliation_blockers": reconciliation_blockers,
            },
        )

    supervisor_cause = ("safety_freshness", "supervisor_tighten")
    supervisor_record = active_causes.get(supervisor_cause) or {}
    supervisor_metadata = dict(supervisor_record.get("metadata") or {})
    supervisor_error = str(supervisor_metadata.get("error") or "")
    if (
        supervisor_record
        and supervisor_error
        == "amend_projection_unverified:position_missing_after_amend"
    ):
        open_position_ids = live_close_settlement.fresh_cached_broker_open_position_ids()
        try:
            target_position_id = int(
                supervisor_metadata.get("position_id") or 0
            )
        except (TypeError, ValueError):
            target_position_id = 0
        target_confirmed_absent = (
            open_position_ids is not None
            and (
                target_position_id not in open_position_ids
                if target_position_id > 0
                else not open_position_ids
            )
        )
        if target_confirmed_absent:
            released = release_no_new_risk_latch_cause(
                cause=supervisor_cause[0],
                cause_id=supervisor_cause[1],
                reason="supervisor_tighten_position_absence_confirmed",
                actor="system:safety_watchdog",
                evidence={
                    **base_evidence,
                    "position_id": target_position_id or None,
                    "open_position_ids": sorted(open_position_ids),
                    "positions_reconcile_id": str(
                        _live_service().live_state_get("positions_reconcile_id", "") or ""
                    ),
                },
            )

    safety_failure = _live_service().live_state_get("safety_failure", {}, clone=True) or {}
    updates: dict[str, Any] = {"no_new_risk_latch": released}
    remaining_causes = {
        (str(item.get("cause") or ""), str(item.get("cause_id") or ""))
        for item in list(
            released.get("remaining_causes") or released.get("causes") or []
        )
        if isinstance(item, dict)
    }
    failure_source = str(safety_failure.get("source") or "")
    if (
        failure_source == "safety_watchdog"
        and watchdog_cause not in remaining_causes
    ) or (
        failure_source == "supervisor_tighten"
        and supervisor_cause not in remaining_causes
    ) or (
        failure_source == "live_loop"
        and live_loop_cause not in remaining_causes
    ):
        updates["safety_failure"] = {}
    _live_service().live_state_update(**updates)

    # The watchdog owns the durable recovery edge, but the live loop owns a
    # separate in-memory admission projection.  Keep the two projections
    # linearized here: otherwise a cleared latch can leave the loop degraded
    # until a later tick happens to republish the positive edge.  Recovery is
    # still fail-closed: every current safety/reconcile fact must be known and
    # no independent latch cause may remain before the projection can reopen.
    recovery_blockers: list[str] = []
    recovery_ready = bool(
        result.enabled
        and result.running
        and result.ok
        and result.state == "current"
    )
    if recovery_ready:
        recovery_blockers.extend(str(item) for item in result.blockers if str(item))
        if bool(released.get("active")):
            recovery_blockers.append("no_new_risk_latched")

        projected_failure = (
            {}
            if updates.get("safety_failure") == {}
            else safety_failure
        )
        if projected_failure:
            recovery_blockers.extend(
                str(item)
                for item in (projected_failure.get("blockers") or [])
                if str(item)
            )
            if not projected_failure.get("blockers"):
                recovery_blockers.append(
                    f"safety_failure:{failure_source or 'unknown'}"
                )

        safety_payload = _live_service().live_state_get("safety_plane", {}, clone=True) or {}
        if not isinstance(safety_payload, dict) or not bool(
            safety_payload.get("accepting_new_risk")
        ):
            recovery_blockers.extend(
                str(item)
                for item in (
                    safety_payload.get("blockers", [])
                    if isinstance(safety_payload, dict)
                    else []
                )
                if str(item)
            )
            if not recovery_blockers or recovery_blockers[-1] != "no_new_risk_latched":
                recovery_blockers.append("safety_not_accepting_new_risk")

        recovery_blockers.extend(live_open_pipeline.new_risk_reconciliation_blockers())
        if (
            str(_live_service().live_state_get("session_state_status", "unknown") or "unknown")
            != "available"
        ):
            recovery_blockers.append("session_state_unavailable")
        if bool(_live_service().live_state_get("circuit_breaker", False)):
            recovery_blockers.append("session_circuit_breaker")

    normalized_recovery_blockers = sorted(set(recovery_blockers))
    current = _live_service()._LIVE_LOOP_CONTROLLER.current()
    if current is not None:
        try:
            _live_service()._LIVE_LOOP_CONTROLLER.update_runtime_health(
                current.generation_id,
                blockers=tuple(normalized_recovery_blockers)
                if recovery_ready
                else ("safety_recovery_not_ready",),
            )
            _live_service().live_state_update(
                accepting_new_risk=_live_service()._LIVE_LOOP_CONTROLLER.accepting_new_risk(
                    current.generation_id
                )
            )
        except RuntimeError:
            pass


def start_live_safety_watchdog() -> bool:
    global _live_safety_watchdog
    if _live_safety_watchdog is None:
        _live_safety_watchdog = LiveSafetyWatchdog(
            probe=live_safety_watchdog_probe,
            on_violation=_on_live_safety_watchdog_violation,
            on_recovery=_on_live_safety_watchdog_recovery,
            recovery_checks=3,
            interval_sec=5.0,
            stale_after_sec=_LIVE_SAFETY_FRESHNESS_SEC,
        )
    return _live_safety_watchdog.start()


def stop_live_safety_watchdog() -> None:
    global _live_safety_watchdog
    watchdog = _live_safety_watchdog
    if watchdog is not None:
        watchdog.stop(timeout_sec=2.0)
    _live_safety_watchdog = None


_live_safety_watchdog: LiveSafetyWatchdog | None = None
