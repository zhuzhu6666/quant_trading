"""Fail-closed latch handling around newly opened position protection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class EntryProtectionLatchRuntime:
    activate_latch: Any
    release_latch_cause: Any
    latch_status: Any
    append_safety_outbox: Any
    live_state_update: Any
    reconcile_value: Any
    pending_open_attach_until: dict[int, float]
    now: Any


def activate_entry_protection_pending_latch(
    position_id: int,
    *,
    broker: str,
    tick: int,
    runtime: EntryProtectionLatchRuntime,
) -> dict[str, Any]:
    """Block new risk before any fallible post-fill processing."""

    pid = int(position_id or 0)
    cause_id = str(pid) if pid > 0 else f"tick:{int(tick)}"
    error = ""
    try:
        runtime.activate_latch(
            reason="entry_protection_pending",
            actor="system:live_open",
            correlation_id=cause_id,
            metadata={
                "broker": str(broker or ""),
                "position_id": pid,
                "tick": int(tick),
            },
            cause="entry_protection_pending",
            cause_id=cause_id,
        )
    except Exception as exc:
        error = f"{type(exc).__name__}:{exc}"
        _append_outbox_best_effort(
            runtime,
            event_type="entry_protection_pending_latch_persist_failed",
            payload={
                "broker": str(broker or ""),
                "position_id": pid,
                "tick": int(tick),
            },
            error=error,
        )
    latch = runtime.latch_status(fail_closed=True)
    runtime.live_state_update(
        accepting_new_risk=False,
        no_new_risk_latch=latch,
        entry_protection_pending={
            "position_id": pid,
            "tick": int(tick),
            "status": "pending",
            "error": error,
        },
    )
    return latch


def release_entry_protection_pending_latch(
    position_id: int,
    *,
    reconcile: Any,
    expected_stop_loss: float,
    expected_take_profit: float,
    runtime: EntryProtectionLatchRuntime,
) -> dict[str, Any]:
    """Release only the matching cause after fresh broker proof."""

    pid = int(position_id or 0)
    reconcile_id = str(
        runtime.reconcile_value(reconcile, "reconcile_id", "") or ""
    )
    evidence = {
        "position_id": pid,
        "reconcile_id": reconcile_id,
        "observed_at": float(
            runtime.reconcile_value(reconcile, "observed_at", 0.0) or 0.0
        ),
        "expected_stop_loss": float(expected_stop_loss or 0.0),
        "expected_take_profit": float(expected_take_profit or 0.0),
    }
    try:
        runtime.release_latch_cause(
            cause="entry_protection_pending",
            cause_id=str(pid),
            reason="entry_protection_verified_by_fresh_broker_reconcile",
            actor="system:live_safety",
            correlation_id=reconcile_id,
            evidence=evidence,
        )
        runtime.pending_open_attach_until.pop(pid, None)
        _release_projection_unverified_cause_for_position(
            pid,
            latch=runtime.latch_status(fail_closed=True) or {},
            release_latch_cause=runtime.release_latch_cause,
            reason="entry_protection_projection_verified_by_fresh_broker_reconcile",
            actor="system:live_safety",
            evidence=evidence,
        )
        latch = runtime.latch_status(fail_closed=True)
        runtime.live_state_update(
            no_new_risk_latch=latch,
            entry_protection_pending={
                **evidence,
                "status": "verified",
                "verified_at": runtime.now(),
            },
        )
        return latch
    except Exception as exc:
        error = f"{type(exc).__name__}:{exc}"
        _append_outbox_best_effort(
            runtime,
            event_type="entry_protection_pending_latch_release_failed",
            payload=evidence,
            error=error,
        )
        latch = runtime.latch_status(fail_closed=True)
        runtime.live_state_update(
            accepting_new_risk=False,
            no_new_risk_latch=latch,
            entry_protection_pending={
                **evidence,
                "status": "release_failed",
                "error": error,
            },
        )
        return latch


def _release_projection_unverified_cause_for_position(
    position_id: int,
    *,
    latch: Mapping[str, Any],
    release_latch_cause: Any,
    reason: str,
    actor: str,
    evidence: dict[str, Any],
) -> bool:
    """Release the projection-unverified cause only when it names this position.

    The cause is bound to the position whose protection amend could not be
    verified.  A cause bound to another position, or one without a binding,
    keeps blocking: the latch stays fail-closed until its own target resolves.
    """

    pid = int(position_id or 0)
    if pid <= 0:
        return False
    for item in list(latch.get("causes") or []):
        if not isinstance(item, dict):
            continue
        if str(item.get("cause") or "") != "safety_freshness":
            continue
        if str(item.get("cause_id") or "") != "entry_protection":
            continue
        try:
            bound_pid = int((item.get("metadata") or {}).get("position_id") or 0)
        except (TypeError, ValueError):
            bound_pid = 0
        if bound_pid != pid:
            continue
        try:
            release_latch_cause(
                cause="safety_freshness",
                cause_id="entry_protection",
                reason=str(reason),
                actor=str(actor),
                correlation_id=str(pid),
                evidence=dict(evidence),
            )
        except Exception:
            return False
        return True
    return False


def release_entry_protection_pending_latch_after_broker_close(
    position_id: int,
    *,
    close_reason: str,
    close_ts: float,
    total_pnl: float,
    release_latch_cause: Any,
    latch_status: Any,
) -> bool:
    """Release entry-protection causes once the broker proves the position is gone.

    ``release_entry_protection_pending_latch`` is the other release site and it
    needs the amended SL/TP echoed back by a fresh reconcile.  When the broker
    already reports the position as fully closed with an authoritative close
    deal there is no residual new risk left to block, so neither the pending
    cause nor the unverified-projection cause bound to this position may
    outlive it.  Without this they survive restarts by design and keep
    ``no_new_risk`` latched with no remaining position.

    Only active causes are released, so a close that carries no
    entry-protection latch leaves the append-only ledger untouched.
    """

    pid = int(position_id or 0)
    if pid <= 0:
        return False
    try:
        latch = dict(latch_status(fail_closed=True) or {})
    except Exception:
        return False
    evidence = {
        "position_id": pid,
        "close_reason": str(close_reason or ""),
        "close_ts": float(close_ts or 0.0),
        "total_pnl": float(total_pnl or 0.0),
        "broker_position_absent": True,
        "authoritative_close_deal": True,
    }
    active = {
        (str(item.get("cause") or ""), str(item.get("cause_id") or ""))
        for item in list(latch.get("causes") or [])
        if isinstance(item, dict)
    }
    released = False
    if ("entry_protection_pending", str(pid)) in active:
        try:
            release_latch_cause(
                cause="entry_protection_pending",
                cause_id=str(pid),
                reason="position_closed_at_broker_no_residual_new_risk",
                actor="system:position_close",
                correlation_id=str(pid),
                evidence=evidence,
            )
            released = True
        except Exception:
            # A failed release must never fail closed-position attribution.
            # The latch stays active and blocks new risk, the safe direction.
            pass
    if _release_projection_unverified_cause_for_position(
        pid,
        latch=latch,
        release_latch_cause=release_latch_cause,
        reason="entry_protection_projection_unresolved_position_closed",
        actor="system:position_close",
        evidence=evidence,
    ):
        released = True
    return released


def _append_outbox_best_effort(
    runtime: EntryProtectionLatchRuntime,
    *,
    event_type: str,
    payload: dict[str, Any],
    error: str,
) -> None:
    try:
        runtime.append_safety_outbox(
            event_type=event_type,
            payload=payload,
            error=error,
        )
    except Exception:
        pass
