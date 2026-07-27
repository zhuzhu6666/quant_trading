"""Pure live execution readiness projection.

Broker probes and in-memory state reads remain in the live façade.  This
module owns freshness, loop-generation and Safety heartbeat interpretation so
the authorization contract can be tested without touching broker or database
state.
"""

from __future__ import annotations

from typing import Any, Mapping


def build_live_readiness(
    *,
    loop: Mapping[str, Any],
    state: Mapping[str, Any],
    positions: list[Any],
    checked_at: float,
    v2_active: bool,
    broker_status: str,
    broker_error: Any,
    freshness_seconds: float = 15.0,
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
    account_age = (
        max(0.0, checked_at - account_updated_at)
        if account_updated_at > 0
        else None
    )
    positions_age = (
        max(0.0, checked_at - positions_updated_at)
        if positions_updated_at > 0
        else None
    )
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

    account_ready = bool(
        account
        and account.get("ok")
        and account_updated_at > 0
        and bool(account_reconcile_id)
        and account_age is not None
        and account_age <= freshness_seconds
        and account_reconcile_failed_at <= account_updated_at
    )
    positions_ready = bool(
        positions_updated_at > 0
        and bool(positions_reconcile_id)
        and positions_age is not None
        and positions_age <= freshness_seconds
        and positions_reconcile_failed_at <= positions_updated_at
    )
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
    loop_blockers = sorted(
        {str(item) for item in list(loop.get("blockers") or []) if str(item)}
    )
    loop_contract_ready = bool(
        loop_running
        and loop_phase == "running"
        and loop_ready
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
    if loop_running and not loop_ready:
        reasons.append("loop_not_ready")
    if loop_running and not loop_accepting_new_risk:
        reasons.append("loop_not_accepting_new_risk")
    reasons.extend(loop_blockers)
    if not account_ready:
        if not account_reconcile_id or account_updated_at <= 0 or not account:
            reasons.append("account_reconcile_unknown")
        elif account_age is None or account_age > freshness_seconds:
            reasons.append("account_reconcile_stale")
        elif not account.get("ok"):
            reasons.append("account_reconcile_invalid")
        elif account_reconcile_failed_at > account_updated_at:
            reasons.append("account_reconcile_failed")
    if positions_updated_at <= 0:
        reasons.append("positions_reconcile_unknown")
    elif not positions_reconcile_id:
        reasons.append("positions_reconcile_identity_missing")
    elif positions_age is None or positions_age > freshness_seconds:
        reasons.append("positions_reconcile_stale")
    elif positions_reconcile_failed_at > positions_updated_at:
        reasons.append("positions_reconcile_failed")
    if loop_running and not safety_ready:
        if safety_age is None:
            reasons.append("safety_heartbeat_unknown")
        elif safety_age > freshness_seconds:
            reasons.append("safety_heartbeat_stale")
        if unknown_execution_count is None:
            reasons.append("unknown_execution_status_unavailable")
        elif unknown_execution_count > 0:
            reasons.append("unresolved_execution_intent")
        if safety_reconciliation_state != "fresh":
            reasons.append("safety_position_reconcile_not_fresh")
        if not safety_accepting_new_risk:
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
        "safety_authority": (
            "phase2_serial_safety_plane" if v2_active else "legacy_authoritative"
        ),
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
