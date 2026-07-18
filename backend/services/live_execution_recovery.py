"""Execution-outcome recovery gate for the serial live loop.

The gate is intentionally independent of generation ownership and Safety v2
release modes.  Enabling the immutable broker-outcome contract must always
recover or fail closed before alpha, even during a staged rollout where the
other Phase 2 flags remain disabled.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ExecutionRecoveryRuntime:
    get_cached_recovery: Callable[[], dict[str, Any]]
    update_live_state: Callable[..., None]
    explicit_position_reconcile: Callable[[Any], Any]
    run_safety_cycle: Callable[..., dict[str, Any]]
    update_generation_health: Callable[[str, tuple[str, ...]], None]


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def recover_execution_outcomes_before_alpha(
    *,
    enabled: bool,
    bridge: Any,
    broker: str,
    tick: int,
    log,
    generation_id: str,
    generation_startup_pending: bool,
    safety_result: dict[str, Any],
    runtime: ExecutionRecoveryRuntime,
) -> tuple[dict[str, Any], bool]:
    """Resolve broker intents without resubmission before alpha admission.

    An incomplete generation startup barrier owns its ordered recovery attempt;
    otherwise this function covers independently enabled outcome recovery and
    unknown outcomes that appear after startup.
    """

    if not enabled or (generation_id and generation_startup_pending):
        return safety_result, True

    cached = dict(runtime.get_cached_recovery() or {})
    unknown_count = max(0, _safe_int(safety_result.get("unknown_execution_count"), 1))
    if unknown_count == 0 and bool(cached.get("ready")):
        return safety_result, True

    def blocked(status: dict[str, Any], reason: str) -> tuple[dict[str, Any], bool]:
        payload = dict(safety_result or {})
        payload["accepting_new_risk"] = False
        payload["unknown_execution_count"] = max(
            1,
            _safe_int(payload.get("unknown_execution_count"), 1),
        )
        payload["blockers"] = sorted(
            set(payload.get("blockers") or ()) | {str(reason)}
        )
        runtime.update_live_state(
            accepting_new_risk=False,
            execution_recovery=dict(status),
            safety_plane=payload,
        )
        if generation_id:
            runtime.update_generation_health(
                generation_id,
                tuple(payload["blockers"]),
            )
        log(f"tick {tick}: execution recovery blocks alpha ({reason})")
        return payload, False

    if bridge is None or not hasattr(bridge, "recover_execution_intents"):
        return blocked(
            {
                "schema": "broker_execution_intent_recovery.v1",
                "enabled": True,
                "ready": False,
                "unresolved_count": None,
                "status": "contract_missing",
                "error": "bridge_execution_recovery_contract_missing",
            },
            "execution_recovery_contract_missing",
        )

    try:
        status = dict(bridge.recover_execution_intents() or {})
    except Exception as exc:
        return blocked(
            {
                "schema": "broker_execution_intent_recovery.v1",
                "enabled": True,
                "ready": False,
                "unresolved_count": None,
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            },
            "execution_recovery_failed",
        )

    status.setdefault("schema", "broker_execution_intent_recovery.v1")
    status["enabled"] = True
    runtime.update_live_state(execution_recovery=status)
    unresolved_count = _safe_int(status.get("unresolved_count"), -1)
    if not bool(status.get("ready")) or unresolved_count != 0:
        return blocked(
            status,
            (
                "unknown_execution_unresolved"
                if unresolved_count > 0
                else "execution_recovery_state_unavailable"
            ),
        )

    # A confirmed recovery can reveal any broker-side position transition.
    # Rejections/no-op checks do not repeat the protection cycle.
    recovered = list(status.get("recovered") or ())
    broker_state_changed = any(
        isinstance(item, dict)
        and str(item.get("outcome") or "").strip().lower() == "confirmed"
        for item in recovered
    )
    if not broker_state_changed:
        return safety_result, True

    post_reconcile = runtime.explicit_position_reconcile(bridge)
    post_safety = runtime.run_safety_cycle(
        bridge=bridge,
        broker=broker,
        tick=tick,
        log=log,
        generation_id=generation_id,
        reconcile_result=post_reconcile,
        force_full_cycle=True,
    )
    if str(post_safety.get("reconciliation_state") or "") != "fresh":
        return blocked(status, "post_recovery_positions_unavailable")
    if _safe_int(post_safety.get("unknown_execution_count"), 1) != 0:
        return blocked(status, "post_recovery_execution_unknown")
    return post_safety, True


__all__ = [
    "ExecutionRecoveryRuntime",
    "recover_execution_outcomes_before_alpha",
]
