"""Strict emergency-close orchestration independent from live alpha and PG.

The broker contract and local fsynced safety latch/outbox are authoritative.
Runtime callbacks are injected by ``live_service`` only for process wiring and
read-only state projection updates.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from loguru import logger

from backend.services.live_reconciliation import fresh_observation_timestamp, reconcile_value
from backend.services.live_safety_state import (
    SafetyStatePersistenceError,
    activate_no_new_risk_latch,
    append_safety_outbox,
)


def broker_value(value: Any, field: str, default: Any = None) -> Any:
    return reconcile_value(value, field, default)


def emergency_position_id(position: Any) -> int:
    try:
        return int(
            broker_value(position, "position_id", 0)
            or broker_value(position, "ticket", 0)
            or broker_value(position, "positionId", 0)
            or 0
        )
    except (TypeError, ValueError):
        return 0


def emergency_position_matches_symbol(position: Any, symbol: str | None) -> bool:
    if not symbol:
        return True
    target = str(symbol)
    return (
        str(broker_value(position, "symbol_id", "") or "") == target
        or str(broker_value(position, "symbol", "") or "") == target
        or str(broker_value(position, "symbolName", "") or "") == target
    )


def fresh_emergency_position_reconcile(bridge: Any) -> dict[str, Any]:
    """Normalize the immutable fresh-only broker position contract."""

    generated_at = time.time()
    if bridge is None or not bool(getattr(bridge, "is_connected", False)):
        return {
            "success": False,
            "fresh": False,
            "authoritative": False,
            "status": "failed",
            "reconcile_id": f"reconcile_{uuid.uuid4()}",
            "positions": (),
            "observed_at": 0.0,
            "generated_at": generated_at,
            "error_code": "broker_not_connected",
            "error_message": "broker is not connected",
        }

    if hasattr(bridge, "reconcile_positions"):
        try:
            raw = bridge.reconcile_positions(force=True, allow_cache_fallback=False)
        except Exception as exc:
            return {
                "success": False,
                "fresh": False,
                "authoritative": False,
                "status": "failed",
                "reconcile_id": f"reconcile_{uuid.uuid4()}",
                "positions": (),
                "observed_at": 0.0,
                "generated_at": generated_at,
                "error_code": "reconcile_exception",
                "error_message": f"{type(exc).__name__}: {exc}",
            }
        status = str(broker_value(raw, "status", "failed") or "failed").strip().lower()
        positions = tuple(broker_value(raw, "positions", ()) or ())
        observed_at = float(broker_value(raw, "observed_at", 0.0) or 0.0)
        timestamp_fresh = fresh_observation_timestamp(observed_at)
        fresh = (
            bool(broker_value(raw, "fresh", status == "fresh"))
            and status == "fresh"
            and timestamp_fresh
        )
        authoritative = bool(broker_value(raw, "authoritative", fresh)) and fresh
        success = bool(broker_value(raw, "success", status != "failed")) and fresh and authoritative
        error_code = str(broker_value(raw, "error_code", "") or "")
        if status == "fresh" and not timestamp_fresh and not error_code:
            error_code = (
                "position_reconcile_timestamp_unknown"
                if observed_at <= 0
                else "position_reconcile_stale"
            )
        return {
            "success": success,
            "fresh": fresh,
            "authoritative": authoritative,
            "status": status if timestamp_fresh else "failed",
            "reconcile_id": str(
                broker_value(raw, "reconcile_id", "") or f"reconcile_{uuid.uuid4()}"
            ),
            "positions": positions,
            "observed_at": observed_at,
            "generated_at": float(broker_value(raw, "generated_at", generated_at) or generated_at),
            "error_code": error_code,
            "error_message": str(broker_value(raw, "error_message", "") or ""),
        }

    return {
        "success": False,
        "fresh": False,
        "authoritative": False,
        "status": "failed",
        "reconcile_id": f"reconcile_contract_missing_{uuid.uuid4()}",
        "positions": (),
        "observed_at": 0.0,
        "generated_at": time.time(),
        "error_code": "explicit_position_reconcile_missing",
        "error_message": "emergency requires reconcile_positions explicit contract",
    }


class EmergencyRiskReducingVerdict:
    allowed = True
    reason = "emergency_risk_reducing_action"

    def __init__(self, *, position_id: int, error: str = "") -> None:
        self.audit_payload = {
            "action": "close_position",
            "position_id": int(position_id),
            "close_reason": "emergency_close",
            "policy_evaluation_error": str(error or ""),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": True,
            "reason": self.reason,
            "audit_payload": dict(self.audit_payload),
        }


def _append_emergency_outbox(
    event_type: str,
    *,
    emergency_id: str,
    payload: dict[str, Any] | None = None,
    error: str = "",
) -> None:
    try:
        append_safety_outbox(
            event_type=event_type,
            payload=payload or {},
            error=error,
            correlation_id=emergency_id,
        )
    except Exception as outbox_exc:
        logger.error(
            "[live] emergency safety outbox append failed event={} emergency_id={} error={}",
            event_type,
            emergency_id,
            outbox_exc,
        )


def emergency_response(
    *,
    status: str,
    broker: str,
    symbol: str | None,
    emergency_id: str,
    latch: dict[str, Any] | None,
    pre_reconcile: dict[str, Any] | None = None,
    post_reconcile: dict[str, Any] | None = None,
    attempted: int = 0,
    closed: int = 0,
    failures: list[dict[str, Any]] | None = None,
    remaining_position_ids: list[int] | None = None,
    unknown_position_ids: list[int] | None = None,
    unknown_execution_intent_ids: list[str] | None = None,
    execution_recovery: dict[str, Any] | None = None,
    error: str = "",
) -> dict[str, Any]:
    failure_items = list(failures or [])
    remaining = sorted({int(pid) for pid in (remaining_position_ids or []) if int(pid or 0) > 0})
    unknown = sorted({int(pid) for pid in (unknown_position_ids or []) if int(pid or 0) > 0})
    unknown_intents = sorted({
        str(intent_id)
        for intent_id in (unknown_execution_intent_ids or [])
        if str(intent_id or "").strip()
    })
    ok = status in {"completed", "no_positions"}
    return {
        "schema_version": "live_emergency_close.v2",
        "status": status,
        "ok": ok,
        "broker": broker,
        "symbol": symbol or "ALL",
        "emergency_id": emergency_id,
        "attempted": int(attempted),
        "closed": int(closed),
        "failed": max(int(attempted) - int(closed), len(failure_items)),
        "failures": failure_items,
        "pre_reconcile_id": str((pre_reconcile or {}).get("reconcile_id") or ""),
        "post_reconcile_id": str((post_reconcile or {}).get("reconcile_id") or ""),
        "remaining_position_ids": remaining,
        "unknown_position_ids": unknown,
        "unknown_execution_intent_ids": unknown_intents,
        "resume_required": True,
        "no_new_risk_latch": dict(latch or {}),
        "reconciliation": {
            "pre": {key: value for key, value in (pre_reconcile or {}).items() if key != "positions"},
            "post": {key: value for key, value in (post_reconcile or {}).items() if key != "positions"},
        },
        "execution_recovery": dict(execution_recovery or {}),
        **({"error": str(error)} if error else {}),
    }


@dataclass(frozen=True)
class EmergencyCloseRuntime:
    update_live_state: Callable[..., None]
    admission_lock: Any
    get_ctrader: Callable[[], tuple[Any, str | None, bool]]
    wait_ctrader_ready: Callable[..., str | None]
    reconcile_positions: Callable[[Any], dict[str, Any]]
    position_volume: Callable[[Any], float]
    build_close_risk_context: Callable[..., dict[str, Any]]
    risk_policy: Any
    remember_close_reason: Callable[[int, str], None]
    remember_close_verdict: Callable[[int, Any], None]
    recover_execution_intents: Callable[[Any], dict[str, Any]]
    post_reconcile_timeout_sec: float = 20.0
    post_reconcile_interval_sec: float = 0.5
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


def _execution_recovery_state(runtime: EmergencyCloseRuntime, bridge: Any) -> dict[str, Any]:
    """Read/recover unknown broker intents without making close authority depend on it."""

    try:
        raw = dict(runtime.recover_execution_intents(bridge) or {})
    except Exception as exc:
        return {
            "schema": "broker_execution_intent_recovery.v2",
            "ready": False,
            "unresolved_count": None,
            "unresolved": [],
            "error": f"{type(exc).__name__}:{exc}",
        }
    unresolved = list(raw.get("unresolved") or [])
    raw_count = raw.get("unresolved_count")
    try:
        unresolved_count = int(raw_count) if raw_count is not None else None
    except (TypeError, ValueError):
        unresolved_count = None
    ready = bool(raw.get("ready") is True and unresolved_count == 0 and not unresolved)
    return {
        **raw,
        "ready": ready,
        "unresolved_count": unresolved_count,
        "unresolved": unresolved,
    }


def _unknown_execution_intent_ids(status: dict[str, Any] | None) -> list[str]:
    values: set[str] = set()
    for item in list((status or {}).get("unresolved") or []):
        if isinstance(item, dict):
            value = item.get("intent_id") or item.get("id")
        else:
            value = getattr(item, "intent_id", "") or getattr(item, "id", "")
        if str(value or "").strip():
            values.add(str(value))
    return sorted(values)


def run_emergency_close(
    broker: str,
    symbol: str | None,
    *,
    runtime: EmergencyCloseRuntime,
) -> dict[str, Any]:
    """Close selected positions and prove completion by fresh reconcile."""

    emergency_id = f"emergency_{uuid.uuid4()}"
    try:
        latch = activate_no_new_risk_latch(
            reason="emergency_close",
            correlation_id=emergency_id,
            metadata={"broker": broker, "symbol": symbol or "ALL"},
        )
    except SafetyStatePersistenceError as exc:
        # ``activate_no_new_risk_latch`` has already installed the in-process
        # fail-closed latch.  Durability loss must be visible and requires an
        # operator resume, but it must not take away the close-only escape
        # hatch when broker connectivity is still available.
        latch = {
            "schema_version": "live_no_new_risk_latch.v1",
            "active": True,
            "state": "persistence_failed_fail_closed",
            "reason": "latch_persistence_failed",
            "error": str(exc),
        }
        logger.critical(
            "[live] emergency latch durability failed; continuing close-only flow emergency_id={} error={}",
            emergency_id,
            exc,
        )
        _append_emergency_outbox(
            "emergency_latch_persistence_failed",
            emergency_id=emergency_id,
            payload={"broker": broker, "symbol": symbol or "ALL"},
            error=str(exc),
        )

    runtime.update_live_state(accepting_new_risk=False, no_new_risk_latch=latch)
    with runtime.admission_lock:
        pass

    if broker != "ctrader":
        return emergency_response(
            status="reconciliation_failed",
            broker=broker,
            symbol=symbol,
            emergency_id=emergency_id,
            latch=latch,
            error=f"unknown broker: {broker}",
        )

    bridge, err, warming = runtime.get_ctrader()
    if err or bridge is None:
        return emergency_response(
            status="reconciliation_failed",
            broker=broker,
            symbol=symbol,
            emergency_id=emergency_id,
            latch=latch,
            error=str(err or "cTrader bridge unavailable"),
        )
    if warming or not bridge.is_connected:
        wait_err = runtime.wait_ctrader_ready(bridge, timeout_sec=5.0)
        if wait_err:
            return emergency_response(
                status="reconciliation_failed",
                broker=broker,
                symbol=symbol,
                emergency_id=emergency_id,
                latch=latch,
                error=f"cTrader not ready: {wait_err}",
            )

    pre = runtime.reconcile_positions(bridge)
    if not pre.get("success"):
        _append_emergency_outbox(
            "emergency_pre_reconcile_failed",
            emergency_id=emergency_id,
            payload={key: value for key, value in pre.items() if key != "positions"},
            error=str(pre.get("error_message") or pre.get("error_code") or "fresh reconcile unavailable"),
        )
        return emergency_response(
            status="reconciliation_failed",
            broker=broker,
            symbol=symbol,
            emergency_id=emergency_id,
            latch=latch,
            pre_reconcile=pre,
            error=str(pre.get("error_message") or pre.get("error_code") or "fresh reconcile unavailable"),
        )

    target_positions = [
        position
        for position in pre.get("positions", ())
        if emergency_position_matches_symbol(position, symbol)
    ]
    if not target_positions:
        # An admitted open RPC may have timed out just before the emergency
        # barrier drained.  A single fresh empty snapshot is not proof of no
        # future fill while an execution intent is unresolved.  Keep polling
        # broker truth and recovery evidence for the same bounded 20-second
        # window used by post-close reconciliation; never report no_positions
        # while intent authority is unknown.
        deadline = runtime.monotonic() + max(0.0, float(runtime.post_reconcile_timeout_sec))
        recovery = _execution_recovery_state(runtime, bridge)
        while not recovery.get("ready"):
            if runtime.monotonic() >= deadline:
                _append_emergency_outbox(
                    "emergency_open_outcome_unresolved",
                    emergency_id=emergency_id,
                    payload={
                        "reconcile_id": str(pre.get("reconcile_id") or ""),
                        "unknown_execution_intent_ids": _unknown_execution_intent_ids(recovery),
                        "unresolved_count": recovery.get("unresolved_count"),
                    },
                    error=str(recovery.get("error") or "execution intent remains unresolved"),
                )
                return emergency_response(
                    status="outcome_unknown",
                    broker=broker,
                    symbol=symbol,
                    emergency_id=emergency_id,
                    latch=latch,
                    pre_reconcile=pre,
                    unknown_execution_intent_ids=_unknown_execution_intent_ids(recovery),
                    execution_recovery=recovery,
                    error=str(recovery.get("error") or "execution intent remains unresolved"),
                )
            runtime.sleep(
                min(
                    float(runtime.post_reconcile_interval_sec),
                    max(0.0, deadline - runtime.monotonic()),
                )
            )
            pre = runtime.reconcile_positions(bridge)
            if not pre.get("success"):
                return emergency_response(
                    status="reconciliation_failed",
                    broker=broker,
                    symbol=symbol,
                    emergency_id=emergency_id,
                    latch=latch,
                    pre_reconcile=pre,
                    execution_recovery=recovery,
                    error=str(
                        pre.get("error_message")
                        or pre.get("error_code")
                        or "fresh reconcile unavailable during execution recovery"
                    ),
                )
            target_positions = [
                position
                for position in pre.get("positions", ())
                if emergency_position_matches_symbol(position, symbol)
            ]
            if target_positions:
                break
            recovery = _execution_recovery_state(runtime, bridge)
        if not target_positions:
            return emergency_response(
                status="no_positions",
                broker=broker,
                symbol=symbol,
                emergency_id=emergency_id,
                latch=latch,
                pre_reconcile=pre,
                execution_recovery=recovery,
            )

    target_ids = {emergency_position_id(position) for position in target_positions}
    valid_target_ids = {pid for pid in target_ids if pid > 0}
    failures: list[dict[str, Any]] = []
    outcome_by_position: dict[int, str] = {}
    missing_position_identity = 0 in target_ids

    for position in target_positions:
        pid = emergency_position_id(position)
        if pid <= 0:
            failures.append({
                "position_id": 0,
                "error_code": "missing_position_id",
                "comment": "fresh broker position has no stable position ID",
            })
            continue
        volume = runtime.position_volume(position)
        if volume <= 0:
            outcome_by_position[pid] = "unknown"
            failures.append({
                "position_id": pid,
                "error_code": "invalid_close_volume",
                "comment": f"live broker position has invalid volume={volume}",
            })
            continue

        close_context = runtime.build_close_risk_context(
            position_id=pid,
            close_reason="emergency_close",
            mode="live",
            broker=broker,
            symbol=str(broker_value(position, "symbol", symbol or "") or symbol or ""),
            position=position,
        )
        try:
            close_verdict = runtime.risk_policy.evaluate("close_position", close_context)
            if not bool(getattr(close_verdict, "allowed", False)):
                _append_emergency_outbox(
                    "emergency_risk_policy_block_ignored",
                    emergency_id=emergency_id,
                    payload={
                        "position_id": pid,
                        "reason": str(getattr(close_verdict, "reason", "") or ""),
                        "context": close_context,
                    },
                )
        except Exception as exc:
            close_verdict = EmergencyRiskReducingVerdict(
                position_id=pid,
                error=f"{type(exc).__name__}: {exc}",
            )
            _append_emergency_outbox(
                "emergency_risk_policy_unavailable",
                emergency_id=emergency_id,
                payload={"position_id": pid, "context": close_context},
                error=f"{type(exc).__name__}: {exc}",
            )

        try:
            result = bridge.close_position(pid, volume=volume)
            outcome = str(getattr(result, "outcome", "") or "").strip().lower()
            if outcome not in {"confirmed", "rejected", "unknown"}:
                # A malformed bridge result is not broker confirmation. Only
                # the fresh post-reconcile may prove disappearance.
                outcome = "unknown"
            outcome_by_position[pid] = outcome
            if outcome == "rejected":
                failures.append({
                    "position_id": pid,
                    "error_code": str(getattr(result, "error_code", "") or "broker_rejected"),
                    "comment": str(getattr(result, "comment", "") or "close rejected"),
                    "outcome": outcome,
                })
            else:
                if outcome == "unknown":
                    failures.append({
                        "position_id": pid,
                        "error_code": str(
                            getattr(result, "error_code", "") or "close_outcome_unknown"
                        ),
                        "comment": str(
                            getattr(result, "comment", "")
                            or "close RPC did not provide a confirmed broker outcome"
                        ),
                        "outcome": outcome,
                    })
                try:
                    runtime.remember_close_reason(pid, "emergency_close")
                    runtime.remember_close_verdict(pid, close_verdict)
                except Exception as exc:
                    _append_emergency_outbox(
                        "emergency_close_audit_deferred",
                        emergency_id=emergency_id,
                        payload={
                            "position_id": pid,
                            "outcome": outcome,
                            "close_verdict": (
                                close_verdict.to_dict()
                                if hasattr(close_verdict, "to_dict")
                                else {}
                            ),
                        },
                        error=f"{type(exc).__name__}: {exc}",
                    )
        except Exception as exc:
            outcome_by_position[pid] = "unknown"
            failures.append({
                "position_id": pid,
                "error_code": "close_rpc_exception",
                "comment": f"{type(exc).__name__}: {exc}",
                "outcome": "unknown",
            })
            _append_emergency_outbox(
                "emergency_close_rpc_exception",
                emergency_id=emergency_id,
                payload={"position_id": pid, "volume": volume},
                error=f"{type(exc).__name__}: {exc}",
            )

    deadline = runtime.monotonic() + max(0.0, float(runtime.post_reconcile_timeout_sec))
    post: dict[str, Any] | None = None
    remaining = set(valid_target_ids)
    while True:
        candidate_post = runtime.reconcile_positions(bridge)
        post = candidate_post
        if candidate_post.get("success"):
            current_ids = {
                emergency_position_id(position)
                for position in candidate_post.get("positions", ())
            }
            remaining = valid_target_ids.intersection(pid for pid in current_ids if pid > 0)
            if not remaining:
                break
            if remaining and all(outcome_by_position.get(pid) == "rejected" for pid in remaining):
                break
        if runtime.monotonic() >= deadline:
            break
        runtime.sleep(
            min(
                float(runtime.post_reconcile_interval_sec),
                max(0.0, deadline - runtime.monotonic()),
            )
        )

    if post is None or not post.get("success"):
        _append_emergency_outbox(
            "emergency_post_reconcile_failed",
            emergency_id=emergency_id,
            payload={key: value for key, value in (post or {}).items() if key != "positions"},
            error=str(
                (post or {}).get("error_message")
                or (post or {}).get("error_code")
                or "fresh reconcile unavailable"
            ),
        )
        return emergency_response(
            status="reconciliation_failed",
            broker=broker,
            symbol=symbol,
            emergency_id=emergency_id,
            latch=latch,
            pre_reconcile=pre,
            post_reconcile=post,
            attempted=len(target_positions),
            closed=len(valid_target_ids - remaining),
            failures=failures,
            remaining_position_ids=sorted(remaining or valid_target_ids),
            unknown_position_ids=sorted(
                pid for pid, outcome in outcome_by_position.items() if outcome == "unknown"
            ),
            error=str((post or {}).get("error_message") or "post-close reconciliation failed"),
        )

    execution_recovery = _execution_recovery_state(runtime, bridge)
    # Recovery itself may be the operation that resolves a delayed market-open
    # receipt.  Its broker evidence can therefore be newer than the post-close
    # snapshot above.  Never report completed/no-positions without one final
    # fresh reconcile *after* recovery.
    final_post = runtime.reconcile_positions(bridge)
    if not final_post.get("success"):
        _append_emergency_outbox(
            "emergency_post_recovery_reconcile_failed",
            emergency_id=emergency_id,
            payload={
                key: value for key, value in final_post.items() if key != "positions"
            },
            error=str(
                final_post.get("error_message")
                or final_post.get("error_code")
                or "fresh reconcile unavailable after execution recovery"
            ),
        )
        return emergency_response(
            status="reconciliation_failed",
            broker=broker,
            symbol=symbol,
            emergency_id=emergency_id,
            latch=latch,
            pre_reconcile=pre,
            post_reconcile=final_post,
            attempted=len(target_positions),
            closed=len(valid_target_ids - remaining),
            failures=failures,
            remaining_position_ids=sorted(remaining or valid_target_ids),
            unknown_position_ids=sorted(
                pid for pid, outcome in outcome_by_position.items() if outcome == "unknown"
            ),
            unknown_execution_intent_ids=_unknown_execution_intent_ids(execution_recovery),
            execution_recovery=execution_recovery,
            error=str(
                final_post.get("error_message")
                or final_post.get("error_code")
                or "post-recovery reconciliation failed"
            ),
        )

    post = final_post
    final_target_positions = [
        position
        for position in final_post.get("positions", ())
        if emergency_position_matches_symbol(position, symbol)
    ]
    final_target_ids = {
        emergency_position_id(position) for position in final_target_positions
    }
    missing_position_identity = missing_position_identity or 0 in final_target_ids
    remaining = {pid for pid in final_target_ids if pid > 0}
    late_position_ids = remaining - valid_target_ids
    for pid in sorted(late_position_ids):
        outcome_by_position.setdefault(pid, "unknown")
        failures.append(
            {
                "position_id": pid,
                "error_code": "position_materialized_after_execution_recovery",
                "comment": (
                    "fresh broker reconciliation found a target position after "
                    "execution-intent recovery; completion is not proven"
                ),
                "outcome": "unknown",
            }
        )

    closed_ids = valid_target_ids - remaining
    unknown_remaining = {
        pid for pid in remaining if outcome_by_position.get(pid) == "unknown"
    }
    if not execution_recovery.get("ready"):
        status = "outcome_unknown"
    elif not remaining and not missing_position_identity:
        status = "completed"
    elif missing_position_identity or unknown_remaining:
        status = "outcome_unknown"
    else:
        status = "partial"
    for pid in sorted(remaining):
        if not any(int(item.get("position_id") or 0) == pid for item in failures):
            failures.append({
                "position_id": pid,
                "error_code": "position_still_open",
                "comment": "fresh broker reconciliation still reports the target position",
                "outcome": outcome_by_position.get(pid, "unknown"),
            })
    return emergency_response(
        status=status,
        broker=broker,
        symbol=symbol,
        emergency_id=emergency_id,
        latch=latch,
        pre_reconcile=pre,
        post_reconcile=post,
        attempted=len(target_positions),
        closed=len(closed_ids),
        failures=failures,
        remaining_position_ids=sorted(remaining),
        unknown_position_ids=sorted(unknown_remaining),
        unknown_execution_intent_ids=_unknown_execution_intent_ids(execution_recovery),
        execution_recovery=execution_recovery,
    )
