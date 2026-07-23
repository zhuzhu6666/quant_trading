"""Safety-first orchestration for one generation-bound live tick."""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from typing import Any

from execution.base import PositionReconcileResult


@dataclass(frozen=True)
class LiveLoopTickRuntime:
    phase2_active: Any
    legacy_tick_body: Any
    get_ctrader: Any
    reconcile_positions: Any
    run_safety_cycle: Any
    persist_safety_fail_closed: Any
    reconcile_account: Any
    reconcile_value: Any
    mark_account_reconcile_failed: Any
    live_state_update: Any
    loop_controller: Any
    set_loop_diagnostic: Any
    recover_execution_outcomes: Any
    attempt_startup_barrier: Any
    live_state_get: Any
    bootstrap_position_recovery: Any
    loop_strategy_name: str
    restore_session_state: Any
    session_circuit_breaker_enforced: Any
    evaluate_daily_drawdown: Any
    market_session_snapshot: Any
    ensure_spot_subscription: Any
    warmup_from_local_db: Any
    ensure_decision_bars_fresh: Any
    get_safety_plane: Any
    process_tick: Any


@dataclass(frozen=True)
class LegacyLiveLoopTickRuntime:
    get_ctrader: Any
    reconcile_positions: Any
    run_safety_cycle: Any
    persist_safety_fail_closed: Any
    live_state_update: Any
    market_session_snapshot: Any
    set_loop_diagnostic: Any
    market_closed_log_message: Any
    bridge_readiness_label: Any
    ensure_spot_subscription: Any
    logger_debug: Any
    kickoff_account_refresh: Any
    live_state_get: Any
    retry_session_restore: Any
    loop_strategy_name: str
    bootstrap_position_recovery: Any
    session_circuit_breaker_enforced: Any
    evaluate_daily_drawdown: Any
    warmup_from_local_db: Any
    ensure_decision_bars_fresh: Any
    new_risk_reconciliation_blockers: Any
    no_new_risk_latched: Any
    process_shutdown_requested: Any
    compare_spot_to_bar: Any
    quote_is_fresh: Any
    process_tick: Any


def run_legacy_live_loop_tick_body(
    *,
    broker: str,
    bridge_cfg: Any,
    timeframe: str,
    tick: int,
    recovery_bootstrapped: bool,
    stop_requested: Any,
    log: Any,
    runtime: LegacyLiveLoopTickRuntime,
) -> dict[str, Any]:
    """Run the compatibility/off path with the same safety-first boundary."""

    del bridge_cfg
    try:
        bridge, broker_error, warming = runtime.get_ctrader()
        bridge_ready = bool(
            bridge is not None
            and not warming
            and getattr(bridge, "is_connected", False)
        )
        reconcile = runtime.reconcile_positions(
            bridge if bridge_ready else None
        )
        safety = runtime.run_safety_cycle(
            bridge=bridge if bridge_ready else None,
            broker=broker,
            tick=tick,
            log=log,
            reconcile_result=reconcile,
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        runtime.persist_safety_fail_closed(
            blockers=("legacy_safety_cycle_exception",),
            source="legacy_live_loop",
            error=error,
        )
        log(
            f"tick {tick}: legacy safety failed closed; retry in 5s: "
            f"{error}"
        )
        return _legacy_tick_result(
            recovery_bootstrapped=recovery_bootstrapped,
            wait_seconds=5.0,
        )

    wait_seconds = _legacy_safety_wait_seconds(safety)
    runtime.live_state_update(accepting_new_risk=False)
    market_session = runtime.market_session_snapshot(
        bridge if bridge_ready else None,
        broker_error=str(broker_error or ""),
    )
    if str(market_session.get("status") or "") == "closed_confirmed":
        runtime.set_loop_diagnostic(
            tick,
            "market_closed",
            bridge_ready=bridge_ready,
        )
        log(
            runtime.market_closed_log_message(
                tick=tick,
                market_session=market_session,
                bridge_ready=bridge_ready,
                warming=warming,
                after_broker_check=True,
            )
        )
        return _legacy_tick_result(
            recovery_bootstrapped=recovery_bootstrapped,
            wait_seconds=wait_seconds,
        )
    runtime.set_loop_diagnostic(
        tick,
        runtime.bridge_readiness_label(
            bridge_ready=bridge_ready,
            warming=warming,
        ),
        bridge_ready=bridge_ready,
    )
    if not bridge_ready:
        log(
            f"tick {tick}: "
            f"{broker_error or 'cTrader warming/disconnected'}; "
            "safety remains active"
        )
        return _legacy_tick_result(
            recovery_bootstrapped=recovery_bootstrapped,
            wait_seconds=5.0,
        )

    try:
        runtime.ensure_spot_subscription(
            bridge,
            log=log,
            market_session=market_session,
        )
    except Exception as exc:
        runtime.logger_debug(
            "[live] spot subscription refresh skipped: %s",
            exc,
        )
    runtime.kickoff_account_refresh(bridge, broker, interval_sec=5.0)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if (
        not recovery_bootstrapped
        or str(runtime.live_state_get("trade_date", "") or "") != today
        or str(runtime.live_state_get("session_state_status", "") or "")
        != "available"
    ):
        restored = runtime.retry_session_restore(
            broker=broker,
            strategy_name=str(runtime.loop_strategy_name or "factor_v4"),
            trade_date=today,
            log=log,
        )
        if not restored or str(
            runtime.live_state_get("session_state_status", "unknown")
            or "unknown"
        ) != "available":
            runtime.live_state_update(accepting_new_risk=False)
            log(
                f"tick {tick}: session risk restore pending for {today}; "
                "safety completed and new risk remains blocked"
            )
            return _legacy_tick_result(
                recovery_bootstrapped=False,
                wait_seconds=5.0,
            )
        recovery_bootstrapped = True

    if not recovery_bootstrapped:
        try:
            recovery_bootstrapped = runtime.bootstrap_position_recovery(
                bridge,
                broker=broker,
                strategy_name=str(runtime.loop_strategy_name or "factor_v4"),
                log=log,
            )
        except Exception as exc:
            log(
                f"tick {tick}: recovery bootstrap failed (non-fatal): "
                f"{exc}"
            )
            recovery_bootstrapped = False
        if not recovery_bootstrapped:
            runtime.live_state_update(accepting_new_risk=False)
            log(
                f"tick {tick}: recovery/deal authority unavailable; "
                "new risk remains blocked"
            )
            return _legacy_tick_result(
                recovery_bootstrapped=False,
                wait_seconds=5.0,
            )

    circuit_enforced = bool(runtime.session_circuit_breaker_enforced())
    if circuit_enforced and runtime.live_state_get("circuit_breaker", False):
        log(
            f"tick {tick}: circuit breaker tripped; safety completed, "
            "skip alpha"
        )
        return _legacy_tick_result(
            recovery_bootstrapped=recovery_bootstrapped,
            wait_seconds=wait_seconds,
        )
    drawdown = runtime.evaluate_daily_drawdown()
    if circuit_enforced and drawdown["tripped"]:
        log(
            f"tick {tick}: CIRCUIT BREAKER: daily drawdown "
            f"{drawdown['dd_pct']:.1f}%; safety completed"
        )
        return _legacy_tick_result(
            recovery_bootstrapped=recovery_bootstrapped,
            wait_seconds=wait_seconds,
        )

    frame = runtime.warmup_from_local_db("XAUUSD+", timeframe, 5)
    if frame is None or len(frame) == 0:
        log(
            f"tick {tick}: local DB has no bars "
            "(waiting for CTraderPuller)"
        )
        return _legacy_tick_result(
            recovery_bootstrapped=recovery_bootstrapped,
            wait_seconds=wait_seconds,
        )
    frame = runtime.ensure_decision_bars_fresh(
        bridge=bridge if bridge_ready else None,
        symbol="XAUUSD+",
        timeframe=timeframe,
        df_new=frame,
        tick=tick,
        log=log,
        market_session=market_session,
    )
    if frame is None or len(frame) == 0:
        log(f"tick {tick}: no closed decision bars available after repair")
        return _legacy_tick_result(
            recovery_bootstrapped=recovery_bootstrapped,
            wait_seconds=wait_seconds,
        )

    reconcile_blockers = runtime.new_risk_reconciliation_blockers()
    accepting_new_risk = bool(
        safety.get("accepting_new_risk", False)
        and not reconcile_blockers
        and str(runtime.live_state_get("session_state_status", "") or "")
        == "available"
        and (
            not circuit_enforced
            or not runtime.live_state_get("circuit_breaker", False)
        )
        and bool(market_session.get("can_open_positions", False))
        and not runtime.no_new_risk_latched(fail_closed=True)
        and not runtime.process_shutdown_requested()
        and not stop_requested()
    )
    runtime.live_state_update(
        accepting_new_risk=accepting_new_risk,
        new_risk_reconcile_blockers=reconcile_blockers,
    )

    quote = (
        bridge.get_spot_quote()
        if bridge is not None and hasattr(bridge, "get_spot_quote")
        else {}
    )
    if quote:
        runtime.live_state_update(spot_quote=quote)
    spot_result = runtime.compare_spot_to_bar(
        df_new=frame,
        quote=quote,
        quote_is_fresh=runtime.quote_is_fresh,
    )
    if spot_result["too_far"]:
        log(
            f"tick {tick}: spot={spot_result['spot']:.2f} too far from "
            f"bar close={spot_result['last_close']:.2f}, using "
            "DataStore price"
        )

    runtime.process_tick(
        bridge,
        None,
        frame,
        frame.iloc[-1],
        broker,
        tick,
        log,
        stop_requested=stop_requested,
        protection_already_run=True,
    )
    if stop_requested():
        log(f"tick {tick}: stop requested during processing, exiting")
        return _legacy_tick_result(
            recovery_bootstrapped=recovery_bootstrapped,
            wait_seconds=None,
            break_loop=True,
        )
    return _legacy_tick_result(
        recovery_bootstrapped=recovery_bootstrapped,
        wait_seconds=wait_seconds,
    )


def run_live_loop_tick_body(
    *,
    broker: str,
    bridge_cfg: Any,
    timeframe: str,
    tick: int,
    recovery_bootstrapped: bool,
    stop_requested: Any,
    log: Any,
    runtime: LiveLoopTickRuntime,
    generation_id: str = "",
) -> dict[str, Any]:
    if not runtime.phase2_active():
        return runtime.legacy_tick_body(
            broker=broker,
            bridge_cfg=bridge_cfg,
            timeframe=timeframe,
            tick=tick,
            recovery_bootstrapped=recovery_bootstrapped,
            stop_requested=stop_requested,
            log=log,
        )

    safety_result = _run_safety_boundary(
        broker=broker,
        tick=tick,
        generation_id=generation_id,
        log=log,
        runtime=runtime,
    )
    if not safety_result["ok"]:
        return _tick_result(
            recovery_bootstrapped=recovery_bootstrapped,
            wait_seconds=5.0,
            safety=safety_result["safety"],
        )
    bridge = safety_result["bridge"]
    bridge_ready = safety_result["bridge_ready"]
    broker_error = safety_result["broker_error"]
    reconcile = safety_result["reconcile"]
    safety = safety_result["safety"]

    account_reconcile, account_blockers = _reconcile_alpha_account(
        bridge=bridge if bridge_ready else None,
        broker=broker,
        positions_reconcile=reconcile,
        runtime=runtime,
    )
    wait_seconds = _safety_wait_seconds(safety)
    if account_blockers:
        runtime.live_state_update(accepting_new_risk=False)
        _update_generation_blockers(
            generation_id,
            safety=safety,
            extra_blockers=account_blockers,
            runtime=runtime,
        )

    if not bridge_ready:
        runtime.set_loop_diagnostic(
            tick,
            "bridge_unavailable",
            bridge_ready=False,
        )
        log(
            f"tick {tick}: "
            f"{broker_error or 'cTrader warming/disconnected'}; "
            "safety failed closed"
        )
        return _tick_result(
            recovery_bootstrapped=recovery_bootstrapped,
            wait_seconds=5.0,
            safety=safety,
        )

    # The bridge can become ready just after the one-shot startup subscription
    # times out.  Retry from the serial tick owner so a missing/stale quote can
    # recover without a process restart.  Subscription failure is advisory;
    # safety and reconciliation must continue to run fail-closed.
    try:
        runtime.ensure_spot_subscription(bridge, log=log)
    except Exception as exc:
        log(f"tick {tick}: spot subscription refresh failed (non-fatal): {exc}")

    safety, execution_recovery_ready = runtime.recover_execution_outcomes(
        bridge=bridge,
        broker=broker,
        tick=tick,
        log=log,
        generation_id=generation_id,
        safety_result=safety,
    )
    wait_seconds = _safety_wait_seconds(safety)
    if not execution_recovery_ready:
        return _tick_result(
            recovery_bootstrapped=recovery_bootstrapped,
            wait_seconds=5.0,
            safety=safety,
        )

    if generation_id and not runtime.loop_controller.status().get("ready"):
        if not runtime.attempt_startup_barrier(
            generation_id=generation_id,
            bridge=bridge,
            broker=broker,
            tick=tick,
            log=log,
            account_reconcile=account_reconcile,
            positions_reconcile=reconcile,
            safety_result=safety,
        ):
            return _tick_result(
                recovery_bootstrapped=recovery_bootstrapped,
                wait_seconds=wait_seconds,
                safety=safety,
            )
        recovery_bootstrapped = True

    if account_blockers:
        log(
            f"tick {tick}: fresh account unavailable; "
            "safety completed and alpha blocked"
        )
        return _tick_result(
            recovery_bootstrapped=recovery_bootstrapped,
            wait_seconds=5.0,
            safety=safety,
        )

    position_ids = [int(pid) for pid in safety.get("position_ids") or []]
    session_result = _ensure_authoritative_session(
        bridge=bridge,
        broker=broker,
        generation_id=generation_id,
        recovery_bootstrapped=recovery_bootstrapped,
        position_ids=position_ids,
        safety=safety,
        wait_seconds=wait_seconds,
        runtime=runtime,
        log=log,
    )
    if not session_result["ready"]:
        return session_result["result"]
    recovery_bootstrapped = session_result["recovery_bootstrapped"]

    circuit_enforced = bool(runtime.session_circuit_breaker_enforced())
    drawdown = runtime.evaluate_daily_drawdown()
    if circuit_enforced and (
        runtime.live_state_get("circuit_breaker", False)
        or drawdown["tripped"]
    ):
        log(
            f"tick {tick}: session circuit blocks new risk; "
            "safety already completed"
        )
        return _tick_result(
            recovery_bootstrapped=recovery_bootstrapped,
            wait_seconds=wait_seconds,
            safety=safety,
        )

    market_session = runtime.market_session_snapshot(bridge)
    if str(market_session.get("status") or "") == "closed_confirmed":
        runtime.set_loop_diagnostic(tick, "market_closed", bridge_ready=True)
        return _tick_result(
            recovery_bootstrapped=recovery_bootstrapped,
            wait_seconds=wait_seconds,
            safety=safety,
        )

    frame = runtime.warmup_from_local_db("XAUUSD+", timeframe, 5)
    if frame is None or len(frame) == 0:
        log(f"tick {tick}: local DB has no bars; safety remains active")
        return _tick_result(
            recovery_bootstrapped=recovery_bootstrapped,
            wait_seconds=wait_seconds,
            safety=safety,
        )
    frame = runtime.ensure_decision_bars_fresh(
        bridge=bridge,
        symbol="XAUUSD+",
        timeframe=timeframe,
        df_new=frame,
        tick=tick,
        log=log,
        market_session=market_session,
    )
    if frame is None or len(frame) == 0:
        return _tick_result(
            recovery_bootstrapped=recovery_bootstrapped,
            wait_seconds=wait_seconds,
            safety=safety,
        )

    plane = runtime.get_safety_plane(generation_id)
    last_index = frame.index[-1]
    closed_bar_id = str(
        last_index.isoformat()
        if hasattr(last_index, "isoformat")
        else last_index
    )
    if not plane.alpha_due(closed_bar_id=closed_bar_id):
        return _tick_result(
            recovery_bootstrapped=recovery_bootstrapped,
            wait_seconds=wait_seconds,
            safety=safety,
        )
    if generation_id and not runtime.loop_controller.accepting_new_risk(
        generation_id
    ):
        runtime.live_state_update(accepting_new_risk=False)
        return _tick_result(
            recovery_bootstrapped=recovery_bootstrapped,
            wait_seconds=wait_seconds,
            safety=safety,
        )
    if not generation_id and not bool(safety.get("accepting_new_risk", False)):
        return _tick_result(
            recovery_bootstrapped=recovery_bootstrapped,
            wait_seconds=wait_seconds,
            safety=safety,
        )

    runtime.process_tick(
        bridge,
        None,
        frame,
        frame.iloc[-1],
        broker,
        tick,
        log,
        stop_requested=stop_requested,
        protection_already_run=True,
    )
    plane.mark_alpha_run(closed_bar_id=closed_bar_id)
    if generation_id:
        runtime.loop_controller.heartbeat(generation_id, "alpha")
    stop_after_tick = bool(stop_requested())
    return _tick_result(
        recovery_bootstrapped=recovery_bootstrapped,
        wait_seconds=None if stop_after_tick else wait_seconds,
        break_loop=stop_after_tick,
        safety=safety,
    )


def _run_safety_boundary(
    *,
    broker: str,
    tick: int,
    generation_id: str,
    log: Any,
    runtime: LiveLoopTickRuntime,
) -> dict[str, Any]:
    try:
        bridge, broker_error, warming = runtime.get_ctrader()
        bridge_ready = bool(
            bridge is not None
            and not warming
            and getattr(bridge, "is_connected", False)
        )
        reconcile = runtime.reconcile_positions(
            bridge if bridge_ready else None
        )
        safety = runtime.run_safety_cycle(
            bridge=bridge if bridge_ready else None,
            broker=broker,
            tick=tick,
            log=log,
            generation_id=generation_id,
            reconcile_result=reconcile,
        )
        return {
            "ok": True,
            "bridge": bridge,
            "bridge_ready": bridge_ready,
            "broker_error": broker_error,
            "reconcile": reconcile,
            "safety": safety,
        }
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        failure = runtime.persist_safety_fail_closed(
            blockers=("safety_cycle_exception",),
            source="live_loop",
            error=error,
        )
        log(
            f"tick {tick}: safety cycle failed closed; retry in 5s: "
            f"{error}"
        )
        return {
            "ok": False,
            "safety": {
                "ok": False,
                "status": "exception",
                "accepting_new_risk": False,
                "blockers": ["safety_cycle_exception"],
                "failure": failure,
            },
        }


def _reconcile_alpha_account(
    *,
    bridge: Any,
    broker: str,
    positions_reconcile: Any,
    runtime: LiveLoopTickRuntime,
) -> tuple[Any, list[str]]:
    if isinstance(positions_reconcile, PositionReconcileResult):
        account_reconcile = runtime.reconcile_account(
            bridge,
            positions_reconcile=positions_reconcile,
        )
    else:
        account_reconcile = runtime.reconcile_account(bridge)
    blockers: list[str] = []
    if account_reconcile is None:
        blockers.append("fresh_account_unavailable")
        runtime.mark_account_reconcile_failed("fresh_account_unavailable")
        return account_reconcile, blockers
    account = runtime.reconcile_value(account_reconcile, "account", None)
    if account is None:
        blockers.append("fresh_account_missing")
        runtime.mark_account_reconcile_failed("fresh_account_missing")
        return account_reconcile, blockers
    payload = asdict(account) if is_dataclass(account) else dict(account)
    payload.update({"ok": True, "broker": broker})
    runtime.live_state_update(
        account=payload,
        account_reconciled=copy.deepcopy(payload),
        account_updated_at=float(
            runtime.reconcile_value(account_reconcile, "observed_at", 0.0)
            or 0.0
        ),
        account_reconcile_id=str(
            runtime.reconcile_value(account_reconcile, "reconcile_id", "")
            or ""
        ),
        account_reconcile_failed_at=None,
        account_reconcile_error=None,
    )
    return account_reconcile, blockers


def _ensure_authoritative_session(
    *,
    bridge: Any,
    broker: str,
    generation_id: str,
    recovery_bootstrapped: bool,
    position_ids: list[int],
    safety: dict[str, Any],
    wait_seconds: float,
    runtime: LiveLoopTickRuntime,
    log: Any,
) -> dict[str, Any]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    session_status = str(
        runtime.live_state_get("session_state_status", "") or ""
    )
    if (
        str(runtime.live_state_get("trade_date", "") or "") != today
        or session_status != "available"
    ):
        if not runtime.bootstrap_position_recovery(
            bridge,
            broker=broker,
            strategy_name=str(runtime.loop_strategy_name or "factor_v4"),
            log=log,
        ):
            _update_generation_blockers(
                generation_id,
                safety=safety,
                extra_blockers=("session_close_deal_unavailable",),
                runtime=runtime,
            )
            runtime.live_state_update(accepting_new_risk=False)
            return {
                "ready": False,
                "recovery_bootstrapped": False,
                "result": _tick_result(
                    recovery_bootstrapped=False,
                    wait_seconds=5.0,
                    safety=safety,
                ),
            }
        recovery_bootstrapped = True
        if not runtime.restore_session_state(
            today,
            broker_open_position_ids=set(position_ids),
        ):
            _update_generation_blockers(
                generation_id,
                safety=safety,
                extra_blockers=("session_state_unavailable",),
                runtime=runtime,
            )
            runtime.live_state_update(accepting_new_risk=False)
            return {
                "ready": False,
                "recovery_bootstrapped": recovery_bootstrapped,
                "result": _tick_result(
                    recovery_bootstrapped=recovery_bootstrapped,
                    wait_seconds=wait_seconds,
                    safety=safety,
                ),
            }
    if (
        str(runtime.live_state_get("session_state_status", "") or "")
        != "available"
    ):
        runtime.live_state_update(accepting_new_risk=False)
        return {
            "ready": False,
            "recovery_bootstrapped": recovery_bootstrapped,
            "result": _tick_result(
                recovery_bootstrapped=recovery_bootstrapped,
                wait_seconds=wait_seconds,
                safety=safety,
            ),
        }
    return {
        "ready": True,
        "recovery_bootstrapped": recovery_bootstrapped,
        "result": None,
    }


def _update_generation_blockers(
    generation_id: str,
    *,
    safety: dict[str, Any],
    extra_blockers: Any,
    runtime: LiveLoopTickRuntime,
) -> None:
    if not generation_id:
        return
    runtime.loop_controller.update_runtime_health(
        generation_id,
        blockers=tuple(safety.get("blockers") or ())
        + tuple(extra_blockers or ()),
    )


def _safety_wait_seconds(safety: dict[str, Any]) -> float:
    # The public account/position fact contract expires after 15 seconds.
    # A ten-second idle wait plus two broker RPCs and scheduler jitter made a
    # healthy empty account cross that boundary on alternating cycles. Keep
    # the serial broker owner, but wake it every five seconds in every state.
    del safety
    return 5.0


def _tick_result(
    *,
    recovery_bootstrapped: bool,
    wait_seconds: float | None,
    safety: dict[str, Any],
    break_loop: bool = False,
) -> dict[str, Any]:
    return {
        "recovery_bootstrapped": recovery_bootstrapped,
        "wait_seconds": wait_seconds,
        "break_loop": break_loop,
        "safety": safety,
    }


def _legacy_safety_wait_seconds(safety: dict[str, Any]) -> float:
    return (
        5.0
        if (
            list(safety.get("position_ids") or [])
            or int(safety.get("unknown_execution_count") or 0) > 0
            or str(safety.get("reconciliation_state") or "unknown")
            != "fresh"
            or list(safety.get("blockers") or [])
        )
        else 10.0
    )


def _legacy_tick_result(
    *,
    recovery_bootstrapped: bool,
    wait_seconds: float | None,
    break_loop: bool = False,
) -> dict[str, Any]:
    return {
        "recovery_bootstrapped": recovery_bootstrapped,
        "wait_seconds": wait_seconds,
        "break_loop": break_loop,
    }
