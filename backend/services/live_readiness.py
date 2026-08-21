"""Pure live execution readiness projection.

Broker probes and in-memory state reads remain in the live façade.  This
module projects the canonical reconciliation, loop-generation and Safety
heartbeat contracts so the authorization contract can be tested without
touching broker or database state.
"""

from __future__ import annotations

from typing import Any, Mapping

from backend.services.live_reconciliation import (
    LIVE_SAFETY_FRESHNESS_SEC,
    evaluate_reconciliation_snapshot,
)


def _canonical_loop_blockers(
    *,
    loop: Mapping[str, Any],
    loop_running: bool,
    loop_ready: bool,
    loop_accepting_new_risk: bool,
) -> list[str]:
    """Return the loop owner's blockers without adding duplicate summaries.

    ``LiveLoopController`` already makes ``accepting_new_risk`` depend on the
    startup barrier, runtime blockers, and the safety heartbeat.  Readiness
    must not append a second generic blocker for the same false value; it only
    supplies a fallback when the owner did not publish a specific reason.
    """

    blockers = sorted(
        {
            str(item)
            for item in list(loop.get("blockers") or [])
            if str(item)
        }
    )
    if loop_running and not blockers:
        if not loop_ready:
            blockers.append("loop_not_ready")
        elif not loop_accepting_new_risk:
            blockers.append("loop_not_accepting_new_risk")
    return blockers


def _has_safety_reason(blockers: list[str], *reasons: str) -> bool:
    """Match a safety fact across its legacy and v2 reason spellings."""

    return bool(set(blockers).intersection(reasons))


def build_live_readiness(
    *,
    loop: Mapping[str, Any],
    state: Mapping[str, Any],
    positions: list[Any],
    checked_at: float,
    v2_active: bool,
    broker_status: str,
    broker_error: Any,
    freshness_seconds: float = LIVE_SAFETY_FRESHNESS_SEC,
) -> dict[str, Any]:
    account = dict(state.get("account_reconciled") or {})
    diag = dict(state.get("diag") or {})
    account_updated_at = float(state.get("account_updated_at") or 0.0)
    positions_updated_at = float(state.get("positions_updated_at") or 0.0)
    account_reconcile_id = str(state.get("account_reconcile_id") or "")
    positions_reconcile_id = str(state.get("positions_reconcile_id") or "")
    account_reconcile_failed_at = float(
        state.get("account_reconcile_failed_at") or 0.0
    )
    positions_reconcile_failed_at = float(
        state.get("positions_reconcile_failed_at") or 0.0
    )
    account_reconcile_error = str(state.get("account_reconcile_error") or "")
    positions_reconcile_error = str(
        state.get("positions_reconcile_error") or ""
    )
    loop_running = bool(loop.get("running"))
    reconciliation = evaluate_reconciliation_snapshot(
        account=account,
        account_updated_at=account_updated_at,
        account_reconcile_id=account_reconcile_id,
        account_reconcile_failed_at=account_reconcile_failed_at,
        positions=state.get("positions_reconciled", positions),
        positions_updated_at=positions_updated_at,
        positions_reconcile_id=positions_reconcile_id,
        positions_reconcile_failed_at=positions_reconcile_failed_at,
        checked_at=checked_at,
        freshness_seconds=freshness_seconds,
    )
    account_age = reconciliation["account_age_sec"]
    positions_age = reconciliation["positions_age_sec"]
    safety_age_raw = loop.get("safety_heartbeat_age_sec")
    safety_age = float(safety_age_raw) if safety_age_raw is not None else None
    safety_payload = (
        loop.get("safety") if isinstance(loop.get("safety"), dict) else {}
    )
    safety_reconciliation_state = str(
        safety_payload.get("reconciliation_state") or "unknown"
    )
    safety_blockers = sorted(
        {
            str(item)
            for item in list(safety_payload.get("blockers") or [])
            if str(item)
        }
    )
    safety_accepting_new_risk = bool(
        safety_payload.get("accepting_new_risk", False)
    )
    unknown_raw = safety_payload.get("unknown_execution_count")
    try:
        unknown_execution_count = (
            int(unknown_raw) if unknown_raw is not None else None
        )
    except (TypeError, ValueError):
        unknown_execution_count = None

    account_ready = bool(reconciliation["account_ready"])
    positions_ready = bool(reconciliation["positions_ready"])
    safety_ready = bool(
        not loop_running
        or (
            safety_age is not None
            and safety_age <= freshness_seconds
            and unknown_execution_count == 0
            and safety_reconciliation_state == "fresh"
            and not safety_blockers
            and safety_accepting_new_risk
        )
    )
    loop_phase = str(
        loop.get("phase") or ("running" if loop_running else "stopped")
    )
    loop_ready = bool(loop.get("ready"))
    loop_accepting_new_risk = bool(loop.get("accepting_new_risk"))
    # The serial generation cannot enter accepting_new_risk until its startup
    # barrier has an authenticated bridge.  Reuse that current positive fact
    # when an earlier warming diagnostic missed the recovery edge.  A broker
    # disconnect still wins immediately, so this cannot mask a lost bridge.
    bridge_ready = bool(
        diag.get("bridge_ready")
        or (
            broker_status == "connected"
            and loop_running
            and loop_accepting_new_risk
        )
    )
    loop_blockers = _canonical_loop_blockers(
        loop=loop,
        loop_running=loop_running,
        loop_ready=loop_ready,
        loop_accepting_new_risk=loop_accepting_new_risk,
    )
    loop_contract_ready = bool(
        loop_running
        and loop_phase == "running"
        and loop_accepting_new_risk
        and not loop_blockers
    )
    overall_ready = bool(
        loop_contract_ready
        and bridge_ready
        and account_ready
        and positions_ready
        and safety_ready
    )

    state_label = "idle"
    if loop_running:
        if overall_ready:
            state_label = "ready"
        elif (
            loop_phase == "starting"
            and broker_status in {"connected", "warming_up"}
        ):
            state_label = "warming_up"
        else:
            state_label = "degraded"
    elif broker_status == "connected":
        state_label = "idle_connected"
    elif broker_status == "warming_up":
        state_label = "warming_up"
    elif broker_status in {"disconnected", "error", "no_token"}:
        state_label = "degraded"

    reasons: list[str] = []
    if not bridge_ready and loop_running:
        reasons.append("bridge_not_ready")
    if loop_running and loop_phase != "running":
        reasons.append(f"loop_phase_{loop_phase}")
    reasons.extend(loop_blockers)
    reasons.extend(reconciliation["blockers"])
    if loop_running and not safety_ready:
        if safety_age is None:
            reasons.append("safety_heartbeat_unknown")
        elif safety_age > freshness_seconds:
            reasons.append("safety_heartbeat_stale")
        if unknown_execution_count is None:
            if not _has_safety_reason(
                safety_blockers,
                "unknown_execution",
                "unresolved_execution_intent",
                "unknown_execution_status_unavailable",
            ):
                reasons.append("unknown_execution_status_unavailable")
        elif unknown_execution_count > 0:
            if not _has_safety_reason(
                safety_blockers,
                "unknown_execution",
                "unresolved_execution_intent",
                "unknown_execution_status_unavailable",
            ):
                reasons.append("unresolved_execution_intent")
        if safety_reconciliation_state != "fresh":
            if not _has_safety_reason(
                safety_blockers,
                "positions_reconciliation_failed",
                "position_reconcile_failed",
                "positions_reconcile_failed",
                "position_reconciliation_failed",
            ):
                reasons.append("safety_position_reconcile_not_fresh")
        if not safety_accepting_new_risk and not safety_blockers:
            reasons.append("safety_not_accepting_new_risk")
        reasons.extend(safety_blockers)
    if broker_error:
        reasons.append("broker_error")

    return {
        "ok": overall_ready,
        "state": state_label,
        "broker_status": broker_status,
        "broker_error": broker_error,
        "loop_running": loop_running,
        "loop_phase": loop_phase,
        "loop_ready": loop_ready,
        "loop_accepting_new_risk": loop_accepting_new_risk,
        "loop_blockers": loop_blockers,
        "bridge_ready": bridge_ready,
        "account_ready": account_ready,
        "positions_ready": positions_ready,
        "safety_ready": safety_ready,
        "safety_reconciliation_state": safety_reconciliation_state,
        "safety_accepting_new_risk": safety_accepting_new_risk,
        "safety_blockers": safety_blockers,
        "safety_authority": "governed_supervisor_executor",
        "safety_heartbeat_state": (
            "current"
            if safety_age is not None and safety_age <= freshness_seconds
            else "unknown"
            if safety_age is None
            else "stale"
        ),
        "safety_heartbeat_age_sec": safety_age,
        "account_reconcile_age_sec": account_age,
        "positions_reconcile_age_sec": positions_age,
        "unknown_execution_count": unknown_execution_count,
        "account_reconcile_id": account_reconcile_id or None,
        "positions_reconcile_id": positions_reconcile_id or None,
        "account_reconcile_failed_at": account_reconcile_failed_at or None,
        "positions_reconcile_failed_at": positions_reconcile_failed_at or None,
        "account_reconcile_error": account_reconcile_error or None,
        "positions_reconcile_error": positions_reconcile_error or None,
        "account_updated_at": account_updated_at or None,
        "positions_updated_at": positions_updated_at or None,
        "account_event_updated_at": state.get("account_event_updated_at") or None,
        "positions_event_updated_at": (
            state.get("positions_event_updated_at") or None
        ),
        "account_event_reason": state.get("account_event_reason"),
        "positions_event_reason": state.get("positions_event_reason"),
        "positions_component_facts": dict(
            state.get("positions_component_facts") or {}
        ),
        "positions_count": len(positions),
        "positions": positions,
        "reasons": sorted(set(reasons)),
    }
