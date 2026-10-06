"""Fail-closed latch handling around newly opened position protection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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


def release_entry_protection_pending_latch_after_broker_close(
    position_id: int,
    *,
    close_reason: str,
    close_ts: float,
    total_pnl: float,
    release_latch_cause: Any,
) -> bool:
    """Release the pending cause once the broker proves the position is gone.

    ``release_entry_protection_pending_latch`` is the only other release site
    and it needs the amended SL/TP echoed back by a fresh reconcile.  When the
    broker already reports the position as fully closed with an authoritative
    close deal there is no residual new risk left to block, so the cause must
    not outlive the position.  Without this the cause survives restarts by
    design and keeps ``no_new_risk`` latched with no remaining position.
    """

    pid = int(position_id or 0)
    if pid <= 0:
        return False
    try:
        release_latch_cause(
            cause="entry_protection_pending",
            cause_id=str(pid),
            reason="position_closed_at_broker_no_residual_new_risk",
            actor="system:position_close",
            correlation_id=str(pid),
            evidence={
                "position_id": pid,
                "close_reason": str(close_reason or ""),
                "close_ts": float(close_ts or 0.0),
                "total_pnl": float(total_pnl or 0.0),
                "broker_position_absent": True,
                "authoritative_close_deal": True,
            },
        )
    except Exception:
        # A failed release must never fail closed-position attribution. The
        # latch stays active and blocks new risk, which is the safe direction.
        return False
    return True


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
