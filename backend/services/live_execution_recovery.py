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


@dataclass(frozen=True)
class PositionRecoveryRuntime:
    read_positions: Callable[[Any], list[Any]]
    normalize_position: Callable[[Any], dict[str, Any]]
    list_active_positions: Callable[[str], list[dict[str, Any]]]
    pending_session_close_causes: Callable[[], dict[int, Any]]
    pending_close_fallback_state: Callable[..., dict[str, Any]]
    pending_close_requirements: Callable[..., dict[str, Any]]
    get_state_connection: Callable[[], Any]
    sync_close_deals_batch: Callable[..., dict[int, Any]]
    pending_close_cursor_overrides: Callable[..., dict[int, Any]]
    pending_close_result_complete: Callable[..., bool]
    release_session_close_latch: Callable[[int, Any], Any]
    defer_close: Callable[..., Any]
    previous_position_ids: set[int]
    zero_confirmations: dict[str, int]
    zero_confirmations_required: int
    replay_lookback_seconds: int
    recovery_replay_lookback_from: Callable[..., int]
    pending_close_required_volume_delta: Callable[..., float]
    replay_recovered_close: Callable[..., Any]
    recovery_missing_position_ids: Callable[..., set[int]]
    open_prices: dict[int, float]
    open_api_volumes: dict[int, float]
    upsert_recovery_position: Callable[..., Any]
    now: Callable[[], float]


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def recover_emergency_execution_intents(
    bridge: Any,
    *,
    enabled: bool,
    read_local_unresolved: Callable[[], list[dict[str, Any]]],
) -> dict[str, Any]:
    """Recover emergency-visible execution outcomes without a PG dependency.

    The broker bridge is authoritative when it implements the immutable
    recovery contract.  The local fsync ledger is compatibility evidence only:
    once outcome v2 is enabled, a missing bridge contract remains unknown even
    when that ledger is empty.
    """

    if hasattr(bridge, "recover_execution_intents"):
        return dict(bridge.recover_execution_intents() or {})
    try:
        unresolved = list(read_local_unresolved() or ())
    except Exception as exc:
        return {
            "schema": "broker_execution_intent_recovery.v1",
            "ready": False,
            "enabled": bool(enabled),
            "unresolved_count": None,
            "unresolved": [],
            "error": (
                "local_execution_recovery_unavailable:"
                f"{type(exc).__name__}:{exc}"
            ),
        }
    if enabled:
        return {
            "schema": "broker_execution_intent_recovery.v1",
            "ready": False,
            "enabled": True,
            "unresolved_count": None,
            "unresolved": unresolved,
            "error": "bridge_execution_recovery_contract_missing",
        }
    return {
        "schema": "broker_execution_intent_recovery.v1",
        "ready": not unresolved,
        "enabled": False,
        "unresolved_count": len(unresolved),
        "unresolved": unresolved,
    }


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


def bootstrap_position_recovery(
    bridge: Any,
    *,
    broker: str,
    strategy_name: str,
    log: Callable[[str], Any],
    runtime: PositionRecoveryRuntime,
) -> bool:
    """Attach broker positions and resolve missing closes without guessing."""

    try:
        current_positions = runtime.read_positions(bridge)
    except Exception as exc:
        log(f"recovery bootstrap skipped: get_positions failed: {exc}")
        return False

    normalized = [runtime.normalize_position(pos) for pos in current_positions]
    current_ids = {
        item["position_id"]
        for item in normalized
        if item["position_id"] > 0
    }
    active_rows = runtime.list_active_positions(broker)
    active_rows_by_id = {
        int(row["position_id"]): row
        for row in active_rows
        if int(row.get("position_id") or 0) > 0
    }
    pending_close_causes = runtime.pending_session_close_causes()
    pending_close_ids = set(pending_close_causes)
    pending_open_ids = pending_close_ids & current_ids
    pending_missing_ids = pending_close_ids - current_ids
    recovery_close_ids = set(active_rows_by_id) | pending_missing_ids

    if pending_open_ids:
        minimum_close_ts = {
            position_id: max(
                0.0,
                float(
                    active_rows_by_id[position_id].get("last_seen_at") or 0.0
                )
                - 5.0,
            )
            for position_id in pending_open_ids
            if position_id in active_rows_by_id
        }
        required_refresh_deltas: dict[int, float] = {}
        for position_id in pending_open_ids:
            state = active_rows_by_id.get(
                position_id
            ) or runtime.pending_close_fallback_state(
                position_id,
                broker=broker,
                recovery_evidence=pending_close_causes.get(position_id),
            )
            requirements = runtime.pending_close_requirements(
                state,
                latch_evidence=pending_close_causes.get(position_id),
            )
            required_delta = float(
                requirements.get("required_closed_volume_delta")
                or state.get("volume")
                or 0.0
            )
            if required_delta > 0.0:
                required_refresh_deltas[position_id] = required_delta
        conn = runtime.get_state_connection()
        try:
            realized = runtime.sync_close_deals_batch(
                bridge,
                conn,
                set(pending_open_ids),
                from_ts=int(
                    max(0.0, runtime.now() - runtime.replay_lookback_seconds)
                ),
                max_rows=500,
                min_exec_timestamp_by_position=minimum_close_ts,
                required_closed_volume_delta_by_position=required_refresh_deltas,
                baseline_close_cursor_by_position=(
                    runtime.pending_close_cursor_overrides(
                        set(pending_open_ids),
                        active_rows_by_id=active_rows_by_id,
                        pending_close_causes=pending_close_causes,
                        broker=broker,
                    )
                ),
            )
        finally:
            conn.close()
        unresolved_open_ids = {
            position_id
            for position_id in pending_open_ids
            if not runtime.pending_close_result_complete(
                realized.get(position_id),
                position_state=(
                    active_rows_by_id.get(position_id)
                    or runtime.pending_close_fallback_state(
                        position_id,
                        broker=broker,
                        recovery_evidence=pending_close_causes.get(position_id),
                    )
                ),
                require_volume_proof=(position_id not in active_rows_by_id),
                recovery_requirements=runtime.pending_close_requirements(
                    active_rows_by_id.get(position_id)
                    or runtime.pending_close_fallback_state(
                        position_id,
                        broker=broker,
                        recovery_evidence=pending_close_causes.get(position_id),
                    ),
                    latch_evidence=pending_close_causes.get(position_id),
                ),
            )
        }
        for position_id in sorted(pending_open_ids - unresolved_open_ids):
            runtime.release_session_close_latch(
                position_id,
                realized[position_id],
            )
        if unresolved_open_ids:
            for position_id in sorted(unresolved_open_ids):
                runtime.defer_close(
                    position_id,
                    broker=broker,
                    tick=0,
                    reason="open_position_realized_leg_unavailable",
                )
            log(
                "recovery bootstrap waiting for realized partial-close deals "
                f"positions={sorted(unresolved_open_ids)}"
            )
            return False

    if not current_ids:
        runtime.previous_position_ids.clear()
        suffix = (
            f" while {len(recovery_close_ids)} pending positions remain"
            if recovery_close_ids
            else ""
        )
        if recovery_close_ids:
            zero_count = runtime.zero_confirmations.get(broker, 0) + 1
            runtime.zero_confirmations[broker] = zero_count
            if zero_count < runtime.zero_confirmations_required:
                log(
                    "recovery bootstrap deferred: broker returned 0 positions"
                    f"{suffix}; confirmation {zero_count}/{runtime.zero_confirmations_required}"
                )
                return False

            missing_ids = set(recovery_close_ids)
            lookback_from = runtime.recovery_replay_lookback_from(
                active_rows=active_rows,
                replay_ids=missing_ids,
                now_ts=runtime.now(),
                lookback_sec=runtime.replay_lookback_seconds,
            )
            minimum_close_ts = {
                int(row["position_id"]): max(
                    0.0,
                    float(row.get("last_seen_at") or 0.0) - 5.0,
                )
                for row in active_rows
                if int(row["position_id"] or 0) in missing_ids
            }
            required_close_deltas = {
                position_id: runtime.pending_close_required_volume_delta(
                    position_id,
                    active_rows_by_id=active_rows_by_id,
                    pending_close_causes=pending_close_causes,
                    broker=broker,
                )
                for position_id in missing_ids
            }
            conn = runtime.get_state_connection()
            try:
                replayed = runtime.sync_close_deals_batch(
                    bridge,
                    conn,
                    missing_ids,
                    from_ts=lookback_from,
                    max_rows=500,
                    min_exec_timestamp_by_position=minimum_close_ts,
                    required_closed_volume_delta_by_position=required_close_deltas,
                    baseline_close_cursor_by_position=(
                        runtime.pending_close_cursor_overrides(
                            missing_ids,
                            active_rows_by_id=active_rows_by_id,
                            pending_close_causes=pending_close_causes,
                            broker=broker,
                        )
                    ),
                )
            finally:
                conn.close()
            unresolved_close_ids = _unresolved_close_ids(
                missing_ids=missing_ids,
                replayed=replayed,
                active_rows_by_id=active_rows_by_id,
                pending_close_causes=pending_close_causes,
                broker=broker,
                runtime=runtime,
            )
            _replay_or_defer_missing_positions(
                missing_ids=missing_ids,
                unresolved_close_ids=unresolved_close_ids,
                replayed=replayed,
                active_rows_by_id=active_rows_by_id,
                pending_close_causes=pending_close_causes,
                broker=broker,
                strategy_name=strategy_name,
                runtime=runtime,
            )
            if unresolved_close_ids:
                log(
                    "recovery bootstrap waiting for authoritative close deals "
                    f"positions={sorted(unresolved_close_ids)}"
                )
                return False
            log(
                "recovery bootstrap reconciled "
                f"{len(missing_ids)} pending positions as closed after broker returned 0"
            )
            return True
        runtime.zero_confirmations.pop(broker, None)
        log("recovery bootstrap confirmed broker has no open positions")
        return True

    runtime.zero_confirmations.pop(broker, None)
    missing_ids = runtime.recovery_missing_position_ids(
        active_rows=active_rows,
        current_ids=current_ids,
    ) | pending_missing_ids
    if missing_ids:
        lookback_from = runtime.recovery_replay_lookback_from(
            active_rows=active_rows,
            replay_ids=missing_ids,
            now_ts=runtime.now(),
            lookback_sec=runtime.replay_lookback_seconds,
        )
        minimum_close_ts = {
            int(row["position_id"]): max(
                0.0,
                float(row.get("last_seen_at") or 0.0) - 5.0,
            )
            for row in active_rows
            if int(row["position_id"] or 0) in missing_ids
        }
        required_close_deltas = {
            position_id: runtime.pending_close_required_volume_delta(
                position_id,
                active_rows_by_id=active_rows_by_id,
                pending_close_causes=pending_close_causes,
                broker=broker,
            )
            for position_id in missing_ids
        }
        conn = runtime.get_state_connection()
        try:
            replayed = runtime.sync_close_deals_batch(
                bridge,
                conn,
                missing_ids,
                from_ts=lookback_from,
                max_rows=500,
                min_exec_timestamp_by_position=minimum_close_ts,
                required_closed_volume_delta_by_position=required_close_deltas,
                baseline_close_cursor_by_position=(
                    runtime.pending_close_cursor_overrides(
                        missing_ids,
                        active_rows_by_id=active_rows_by_id,
                        pending_close_causes=pending_close_causes,
                        broker=broker,
                    )
                ),
            )
        finally:
            conn.close()
        unresolved_close_ids = _unresolved_close_ids(
            missing_ids=missing_ids,
            replayed=replayed,
            active_rows_by_id=active_rows_by_id,
            pending_close_causes=pending_close_causes,
            broker=broker,
            runtime=runtime,
        )
        _replay_or_defer_missing_positions(
            missing_ids=missing_ids,
            unresolved_close_ids=unresolved_close_ids,
            replayed=replayed,
            active_rows_by_id=active_rows_by_id,
            pending_close_causes=pending_close_causes,
            broker=broker,
            strategy_name=strategy_name,
            runtime=runtime,
        )
        if unresolved_close_ids:
            log(
                "recovery bootstrap waiting for authoritative close deals "
                f"positions={sorted(unresolved_close_ids)}"
            )
            return False
        log(f"recovery bootstrap replayed {len(missing_ids)} missing closes")

    for item in normalized:
        position_id = item["position_id"]
        if position_id <= 0:
            continue
        runtime.open_prices[position_id] = item["open_price"]
        runtime.open_api_volumes[position_id] = item["volume"]
        runtime.upsert_recovery_position(
            item["raw"],
            broker=broker,
            strategy_name=strategy_name,
            status="recovered",
            meta={"recovered_at": runtime.now()},
        )

    runtime.previous_position_ids.clear()
    runtime.previous_position_ids.update(current_ids)
    if current_ids:
        log(
            f"recovery bootstrap attached {len(current_ids)} live positions after restart"
        )
    return True


def _unresolved_close_ids(
    *,
    missing_ids: set[int],
    replayed: dict[int, Any],
    active_rows_by_id: dict[int, dict[str, Any]],
    pending_close_causes: dict[int, Any],
    broker: str,
    runtime: PositionRecoveryRuntime,
) -> set[int]:
    unresolved: set[int] = set()
    for raw_position_id in missing_ids:
        position_id = int(raw_position_id)
        state = active_rows_by_id.get(
            position_id
        ) or runtime.pending_close_fallback_state(
            position_id,
            broker=broker,
            recovery_evidence=pending_close_causes.get(position_id),
        )
        if not runtime.pending_close_result_complete(
            replayed.get(position_id),
            position_state=state,
            require_volume_proof=(position_id not in active_rows_by_id),
            recovery_requirements=runtime.pending_close_requirements(
                state,
                latch_evidence=pending_close_causes.get(position_id),
            ),
        ):
            unresolved.add(position_id)
    return unresolved


def _replay_or_defer_missing_positions(
    *,
    missing_ids: set[int],
    unresolved_close_ids: set[int],
    replayed: dict[int, Any],
    active_rows_by_id: dict[int, dict[str, Any]],
    pending_close_causes: dict[int, Any],
    broker: str,
    strategy_name: str,
    runtime: PositionRecoveryRuntime,
) -> None:
    for position_id in sorted(missing_ids):
        state = active_rows_by_id.get(
            position_id
        ) or runtime.pending_close_fallback_state(
            position_id,
            broker=broker,
            recovery_evidence=pending_close_causes.get(position_id),
        )
        if position_id not in unresolved_close_ids:
            runtime.replay_recovered_close(
                broker=broker,
                position_id=position_id,
                position_state=state,
                real_pnl=replayed.get(position_id),
                strategy_name=strategy_name,
            )
        else:
            runtime.defer_close(
                position_id,
                broker=broker,
                tick=0,
                reason="recovery_bootstrap_close_deal_unavailable",
            )


__all__ = [
    "ExecutionRecoveryRuntime",
    "PositionRecoveryRuntime",
    "bootstrap_position_recovery",
    "recover_execution_outcomes_before_alpha",
]
