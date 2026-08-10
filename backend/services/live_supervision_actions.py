"""Action executors for live position supervision.

The live service still owns the loop, broker bridge, risk policy, and ledger
instances.  Helpers here only keep action-specific branching out of the large
service module while dependencies are injected by the caller.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from backend.services.live_reconciliation import (
    explicit_position_reconcile,
    verify_position_protection_projection,
)


class MappingVerdictProxy:
    def __init__(self, payload: dict[str, Any]):
        self._payload = dict(payload or {})

    def to_dict(self) -> dict[str, Any]:
        return dict(self._payload)


def _best_effort_post_broker_effect(
    callback: Callable[[], Any],
    *,
    position_id: int,
    action: str,
    stage: str,
    record_aux_failure: Callable[..., Any] | None,
    log: Callable[[str], Any],
) -> bool:
    """Keep audit/projection failures from rewriting a broker success.

    Each effect is isolated so a failed ledger write cannot skip the local
    cooldown/projection effects that prevent a duplicate mutation next tick.
    """

    try:
        callback()
        return True
    except Exception as exc:
        if record_aux_failure is not None:
            try:
                record_aux_failure(
                    "risk_reduction_post_broker_aux_failure",
                    position_id=int(position_id or 0),
                    action=str(action or ""),
                    error=exc,
                    payload={"stage": str(stage or "unknown")},
                )
            except Exception:
                pass
        try:
            log(
                f"risk-reduction post-broker auxiliary failure "
                f"pos={int(position_id or 0)} action={action} stage={stage}: {exc}"
            )
        except Exception:
            pass
        return False


def _resolve_bridge_volume_meta(bridge: Any, position: dict[str, Any]) -> dict[str, Any]:
    meta = getattr(bridge, "_symbol_meta", None) or {}
    if not isinstance(meta, dict):
        meta = {}
    if not meta.get("api_min_volume") and bridge is not None and hasattr(bridge, "_resolve_symbol_id"):
        try:
            bridge._resolve_symbol_id()
            meta = getattr(bridge, "_symbol_meta", None) or {}
            if not isinstance(meta, dict):
                meta = {}
        except Exception:
            meta = {}
    symbol = str(position.get("symbol") or getattr(bridge, "symbol", "") or "").upper()
    if not meta.get("api_min_volume") and symbol.startswith("XAUUSD"):
        meta = {"api_min_volume": 100, "api_step_volume": 100}
    return dict(meta or {})


def plan_supervisor_reduce_action(
    *,
    bridge: Any,
    position: dict[str, Any],
    verdict: dict[str, Any],
    controls: dict[str, Any],
    floor_api_volume_to_step: Callable[[float, dict[str, Any]], float],
    should_full_close_untradeable_reduce: Callable[..., tuple[bool, str]],
) -> dict[str, Any]:
    """Resolve a reduce intent into one broker-executable canonical action."""

    current_volume = float(
        position.get("volume", position.get("api_volume", 0.0)) or 0.0
    )
    reduce_fraction = float((controls or {}).get("reduce_fraction", 0.0) or 0.0)
    raw_reduce_volume = current_volume * reduce_fraction
    bridge_meta = _resolve_bridge_volume_meta(bridge, position)
    min_volume = float(bridge_meta.get("api_min_volume") or 1.0)
    step_volume = float(bridge_meta.get("api_step_volume") or 1.0)
    reduce_volume = floor_api_volume_to_step(raw_reduce_volume, bridge_meta)
    payload = {
        "current_volume": current_volume,
        "reduce_fraction": reduce_fraction,
        "raw_reduce_volume": raw_reduce_volume,
        "reduce_volume": reduce_volume,
        "min_volume": min_volume,
        "step_volume": step_volume,
    }
    if reduce_volume >= min_volume and current_volume - reduce_volume >= min_volume:
        return {**payload, "effective_action": "reduce", "reason": "partial_close_tradeable"}

    upgrade_to_close, reason = should_full_close_untradeable_reduce(
        current_volume=current_volume,
        raw_reduce_volume=raw_reduce_volume,
        reduce_volume=reduce_volume,
        min_volume=min_volume,
        verdict=verdict,
    )
    if upgrade_to_close:
        return {**payload, "effective_action": "close", "reason": str(reason)}
    return {**payload, "effective_action": "hold", "reason": str(reason)}


def normalize_supervisor_reduce_verdict(
    verdict: dict[str, Any],
    execution_plan: dict[str, Any],
) -> dict[str, Any]:
    """Project an evaluated reduce verdict to its canonical executable action."""

    normalized = dict(verdict or {})
    requested_action = str(normalized.get("action") or "reduce").strip().lower()
    requested_reason = str(normalized.get("summary_reason") or "")
    effective_action = str(
        (execution_plan or {}).get("effective_action") or "hold"
    ).strip().lower()
    normalized["requested_action"] = requested_action
    normalized["requested_summary_reason"] = requested_reason
    normalized["effective_action"] = effective_action
    normalized["reduce_execution_plan"] = dict(execution_plan or {})
    if effective_action == "reduce":
        return normalized
    controls = dict(normalized.get("recommended_controls") or {})
    normalized["action"] = effective_action
    if effective_action == "close":
        close_reason = str(
            (execution_plan or {}).get("reason")
            or "minimum_position_reduce_full_close"
        )
        normalized["summary_reason"] = close_reason
        normalized["recommended_controls"] = {
            **controls,
            "reduce_fraction": 0.0,
            "close_reason": close_reason,
            "protection_mode": "full_exit",
            "original_action": requested_action,
            "original_summary_reason": requested_reason,
        }
    return normalized


def execute_supervisor_tighten_action(
    *,
    bridge: Any,
    position: dict[str, Any],
    verdict: dict[str, Any],
    risk_action: str,
    risk_verdict: dict[str, Any],
    decision_id: str,
    cfg: Any,
    tick: int,
    acct: dict[str, Any] | None,
    controls: dict[str, Any],
    log: Callable[[str], Any],
    broker: str = "ctrader",
    strategy_name: str = "factor_v4",
    build_tighten_execution_plan: Callable[..., dict[str, Any]],
    build_tighten_result_payloads: Callable[..., dict[str, Any]],
    log_supervisor_position_event: Callable[..., Any],
    log_supervisor_trace: Callable[..., Any],
    remember_supervisor_state: Callable[..., Any],
    remember_supervisor_reentry_block: Callable[..., Any],
    track_local_sl_tp: Callable[..., Any],
    result_is_position_not_found: Callable[[Any], bool],
    retire_broker_missing_position: Callable[..., Any],
    record_aux_failure: Callable[..., Any] | None = None,
    reconcile_positions: Callable[[Any], Any] = explicit_position_reconcile,
    verify_protection_projection: Callable[..., dict[str, Any]] = verify_position_protection_projection,
    publish_fresh_positions: Callable[[Any], Any] | None = None,
    persist_safety_fail_closed: Callable[..., Any] | None = None,
) -> None:
    pid = int(position.get("position_id") or position.get("ticket") or 0)
    stop_policy = {
        "quote_max_age_seconds": getattr(cfg, "supervisor_quote_max_age_seconds", 10.0),
        "min_stop_distance_points": getattr(cfg, "supervisor_min_stop_distance_points", 0.20),
        "stop_safety_buffer_ratio": getattr(cfg, "supervisor_stop_safety_buffer_ratio", 0.00008),
        "min_tighten_delta_points": getattr(cfg, "supervisor_min_tighten_delta_points", 0.01),
        "precision": int(position.get("digits", 2) or 2),
        "require_side_quote": True,
    }
    quote = bridge.get_spot_quote() if hasattr(bridge, "get_spot_quote") else {}
    tighten_plan = build_tighten_execution_plan(
        position=position,
        controls=controls,
        quote=quote,
        policy=stop_policy,
    )
    target_sl = float(tighten_plan.get("target_sl") or 0.0)
    current_tp = float(tighten_plan.get("current_tp") or 0.0)
    target_tp = float(tighten_plan.get("target_tp") or 0.0)
    planned_tp = float(tighten_plan.get("planned_tp") or 0.0)
    sl_plan = tighten_plan.get("sl_plan") or {}
    if not sl_plan["allowed"]:
        result_payloads = build_tighten_result_payloads(
            result="skipped",
            action="tighten",
            verdict=verdict,
            risk_action=risk_action,
            risk_verdict=risk_verdict,
            decision_id=decision_id,
            controls=controls,
            sl_plan=sl_plan,
        )
        log_supervisor_position_event(
            position=position,
            event_type=result_payloads["position_event_type"],
            details=result_payloads["position_event_details"],
        )
        log_supervisor_trace(
            position=position,
            verdict=verdict,
            cfg=cfg,
            tick=tick,
            **result_payloads["trace_fields"],
            acct=acct,
        )
        log(f"tick {tick}: supervisor tighten SKIP pos={pid} reason={sl_plan.get('reason')}")
        remember_supervisor_state(position, verdict, broker=broker, strategy_name=strategy_name)
        return

    # Re-read bid/ask immediately before touching the broker. The first quote
    # can become invalid while risk evaluation and audit payloads are built.
    # Replanning also rechecks direction, freshness, stop distance, precision,
    # and whether the resulting stop still tightens the existing protection.
    latest_quote = bridge.get_spot_quote() if hasattr(bridge, "get_spot_quote") else quote
    tighten_plan = build_tighten_execution_plan(
        position=position,
        controls=controls,
        quote=latest_quote,
        policy=stop_policy,
    )
    target_sl = float(tighten_plan.get("target_sl") or 0.0)
    current_tp = float(tighten_plan.get("current_tp") or 0.0)
    target_tp = float(tighten_plan.get("target_tp") or 0.0)
    planned_tp = float(tighten_plan.get("planned_tp") or 0.0)
    sl_plan = tighten_plan.get("sl_plan") or {}
    if not sl_plan.get("allowed"):
        result_payloads = build_tighten_result_payloads(
            result="skipped",
            action="tighten",
            verdict=verdict,
            risk_action=risk_action,
            risk_verdict=risk_verdict,
            decision_id=decision_id,
            controls=controls,
            sl_plan=sl_plan,
        )
        log_supervisor_position_event(
            position=position,
            event_type=result_payloads["position_event_type"],
            details=result_payloads["position_event_details"],
        )
        log_supervisor_trace(
            position=position,
            verdict=verdict,
            cfg=cfg,
            tick=tick,
            **result_payloads["trace_fields"],
            acct=acct,
        )
        log(f"tick {tick}: supervisor tighten SKIP pos={pid} reason={sl_plan.get('reason')}")
        remember_supervisor_state(position, verdict, broker=broker, strategy_name=strategy_name)
        return

    planned_sl = float(tighten_plan.get("planned_sl") or 0.0)
    if planned_sl <= 0:
        result_payloads = build_tighten_result_payloads(
            result="failed",
            action="tighten",
            verdict=verdict,
            risk_action=risk_action,
            risk_verdict=risk_verdict,
            decision_id=decision_id,
            controls=controls,
            sl_plan={
                **dict(sl_plan or {}),
                "allowed": False,
                "reason": "planned_stop_missing",
            },
            failure_reason="planned_stop_missing",
        )
        log_supervisor_position_event(
            position=position,
            event_type=result_payloads["position_event_type"],
            details=result_payloads["position_event_details"],
        )
        log_supervisor_trace(
            position=position,
            verdict=verdict,
            cfg=cfg,
            tick=tick,
            **result_payloads["trace_fields"],
            acct=acct,
        )
        return
    amend_res = bridge.amend_position_sltp(pid, sl=planned_sl, tp=planned_tp)
    if getattr(amend_res, "success", False):
        try:
            projection = reconcile_positions(bridge)
            verification = verify_protection_projection(
                projection,
                position_id=pid,
                expected_stop_loss=planned_sl,
                expected_take_profit=planned_tp,
                precision=int(position.get("digits", 2) or 2),
            )
        except Exception as exc:
            projection = None
            verification = {
                "ok": False,
                "position_id": pid,
                "reason": "position_reconcile_exception",
                "error": f"{type(exc).__name__}: {exc}",
            }

        projection_reason = str(
            verification.get("reason") or "position_reconcile_failed"
        )
        position_closed_after_amend = (
            not bool(verification.get("ok"))
            and projection_reason == "position_missing_after_amend"
            and str(verification.get("reconcile_status") or "") == "fresh"
        )
        if not bool(verification.get("ok")) and not position_closed_after_amend:
            failure_reason = f"amend_projection_unverified:{projection_reason}"
            if record_aux_failure is not None:
                try:
                    record_aux_failure(
                        "supervisor_amend_projection_unverified",
                        position_id=pid,
                        action="tighten_position",
                        error=failure_reason,
                        payload={"verification": verification},
                    )
                except Exception:
                    pass
            if persist_safety_fail_closed is not None:
                try:
                    persist_safety_fail_closed(
                        blockers=("amend_projection_unverified",),
                        source="supervisor_tighten",
                        error=failure_reason,
                        metadata={
                            "position_id": pid,
                            "verification": dict(verification or {}),
                        },
                    )
                except Exception as exc:
                    if record_aux_failure is not None:
                        try:
                            record_aux_failure(
                                "supervisor_amend_fail_closed_unavailable",
                                position_id=pid,
                                action="tighten_position",
                                error=exc,
                                payload={"verification": verification},
                            )
                        except Exception:
                            pass

            result_payloads = build_tighten_result_payloads(
                result="failed",
                action="tighten",
                verdict=verdict,
                risk_action=risk_action,
                risk_verdict=risk_verdict,
                decision_id=decision_id,
                controls=controls,
                sl_plan=sl_plan,
                target_sl=target_sl,
                planned_sl=planned_sl,
                target_tp=target_tp,
                planned_tp=planned_tp,
                current_tp=current_tp,
                failure_reason=failure_reason,
            )
            _best_effort_post_broker_effect(
                lambda: log_supervisor_position_event(
                    position=position,
                    event_type=result_payloads["position_event_type"],
                    details=result_payloads["position_event_details"],
                ),
                position_id=pid,
                action="tighten_position",
                stage="position_event",
                record_aux_failure=record_aux_failure,
                log=log,
            )
            _best_effort_post_broker_effect(
                lambda: remember_supervisor_state(
                    position,
                    verdict,
                    broker=broker,
                    strategy_name=strategy_name,
                ),
                position_id=pid,
                action="tighten_position",
                stage="supervisor_state",
                record_aux_failure=record_aux_failure,
                log=log,
            )
            _best_effort_post_broker_effect(
                lambda: log_supervisor_trace(
                    position=position,
                    verdict=verdict,
                    cfg=cfg,
                    tick=tick,
                    **result_payloads["trace_fields"],
                    acct=acct,
                ),
                position_id=pid,
                action="tighten_position",
                stage="supervisor_trace",
                record_aux_failure=record_aux_failure,
                log=log,
            )
            log(
                f"tick {tick}: supervisor tighten UNVERIFIED "
                f"pos={pid}: {failure_reason}"
            )
            return

        if publish_fresh_positions is not None:
            _best_effort_post_broker_effect(
                lambda: publish_fresh_positions(projection),
                position_id=pid,
                action="tighten_position",
                stage="publish_fresh_position_reconcile",
                record_aux_failure=record_aux_failure,
                log=log,
            )
        if not position_closed_after_amend:
            _best_effort_post_broker_effect(
                lambda: track_local_sl_tp(pid, sl=planned_sl, tp=planned_tp),
                position_id=pid,
                action="tighten_position",
                stage="track_local_sl_tp",
                record_aux_failure=record_aux_failure,
                log=log,
            )
        result_payloads = build_tighten_result_payloads(
            result=(
                "applied_position_closed"
                if position_closed_after_amend
                else "applied"
            ),
            action="tighten",
            verdict=verdict,
            risk_action=risk_action,
            risk_verdict=risk_verdict,
            decision_id=decision_id,
            controls=controls,
            sl_plan=sl_plan,
            target_sl=target_sl,
            planned_sl=planned_sl,
            target_tp=target_tp,
            planned_tp=planned_tp,
            current_tp=current_tp,
        )
        _best_effort_post_broker_effect(
            lambda: log_supervisor_position_event(
                position=position,
                event_type=result_payloads["position_event_type"],
                details=result_payloads["position_event_details"],
            ),
            position_id=pid,
            action="tighten_position",
            stage="position_event",
            record_aux_failure=record_aux_failure,
            log=log,
        )
        _best_effort_post_broker_effect(
            lambda: remember_supervisor_state(
                position,
                verdict,
                action_applied="tighten",
                broker=broker,
                strategy_name=strategy_name,
            ),
            position_id=pid,
            action="tighten_position",
            stage="supervisor_state",
            record_aux_failure=record_aux_failure,
            log=log,
        )
        _best_effort_post_broker_effect(
            lambda: remember_supervisor_reentry_block(
                position=position,
                action="tighten",
                reason=str(verdict.get("summary_reason") or controls.get("close_reason") or "supervisor_reduce"),
                cfg=cfg,
                current_price=float(position.get("current_price", position.get("price_current", 0.0)) or 0.0),
                tick=tick,
            ),
            position_id=pid,
            action="tighten_position",
            stage="reentry_block",
            record_aux_failure=record_aux_failure,
            log=log,
        )
        _best_effort_post_broker_effect(
            lambda: log_supervisor_trace(
                position=position,
                verdict=verdict,
                cfg=cfg,
                tick=tick,
                **result_payloads["trace_fields"],
                acct=acct,
            ),
            position_id=pid,
            action="tighten_position",
            stage="supervisor_trace",
            record_aux_failure=record_aux_failure,
            log=log,
        )
        tp_suffix = f" tp->{planned_tp:.2f}" if planned_tp != current_tp else ""
        close_suffix = (
            " position_closed_after_amend"
            if position_closed_after_amend
            else ""
        )
        log(
            f"tick {tick}: supervisor tighten pos={pid} "
            f"sl->{planned_sl:.2f}{tp_suffix}{close_suffix}"
        )
        return

    comment = str(getattr(amend_res, "comment", "") or getattr(amend_res, "error", "") or "amend_failed")
    result_payloads = build_tighten_result_payloads(
        result="failed",
        action="tighten",
        verdict=verdict,
        risk_action=risk_action,
        risk_verdict=risk_verdict,
        decision_id=decision_id,
        controls=controls,
        sl_plan=sl_plan,
        failure_reason=comment,
    )
    log_supervisor_position_event(
        position=position,
        event_type=result_payloads["position_event_type"],
        details=result_payloads["position_event_details"],
    )
    remember_supervisor_state(position, verdict, broker=broker, strategy_name=strategy_name)
    log_supervisor_trace(
        position=position,
        verdict=verdict,
        cfg=cfg,
        tick=tick,
        **result_payloads["trace_fields"],
        acct=acct,
    )
    log(f"tick {tick}: supervisor tighten AMEND FAILED pos={pid}: {comment}")
    if result_is_position_not_found(amend_res):
        retire_broker_missing_position(
            bridge,
            pid,
            broker=broker,
            strategy_name=strategy_name,
            reason=comment,
            log=log,
        )


def execute_supervisor_close_action(
    *,
    bridge: Any,
    position: dict[str, Any],
    verdict: dict[str, Any],
    risk_action: str,
    risk_verdict: dict[str, Any],
    decision_id: str,
    cfg: Any,
    tick: int,
    acct: dict[str, Any] | None,
    controls: dict[str, Any],
    log: Callable[[str], Any],
    broker: str = "ctrader",
    strategy_name: str = "factor_v4",
    log_supervisor_trace: Callable[..., Any],
    remember_supervisor_state: Callable[..., Any],
    remember_supervisor_reentry_block: Callable[..., Any],
    remember_close_reason: Callable[[int, str], Any],
    remember_close_verdict: Callable[[int, Any], Any],
    result_is_position_not_found: Callable[[Any], bool],
    retire_broker_missing_position: Callable[..., Any],
    record_aux_failure: Callable[..., Any] | None = None,
) -> None:
    pid = int(position.get("position_id") or position.get("ticket") or 0)
    close_reason = str(controls.get("close_reason") or verdict.get("summary_reason") or "supervisor_close")
    broker_volume = float(
        position.get("volume", position.get("api_volume", 0.0)) or 0.0
    )
    result = (
        bridge.close_position(pid, volume=broker_volume)
        if broker_volume > 0.0
        else bridge.close_position(pid)
    )
    if getattr(result, "success", False):
        _best_effort_post_broker_effect(
            lambda: remember_close_reason(pid, close_reason),
            position_id=pid,
            action="close_position",
            stage="close_reason",
            record_aux_failure=record_aux_failure,
            log=log,
        )
        _best_effort_post_broker_effect(
            lambda: remember_close_verdict(pid, MappingVerdictProxy(risk_verdict)),
            position_id=pid,
            action="close_position",
            stage="close_verdict",
            record_aux_failure=record_aux_failure,
            log=log,
        )
        _best_effort_post_broker_effect(
            lambda: remember_supervisor_state(
                position,
                verdict,
                action_applied="close",
                broker=broker,
                strategy_name=strategy_name,
            ),
            position_id=pid,
            action="close_position",
            stage="supervisor_state",
            record_aux_failure=record_aux_failure,
            log=log,
        )
        _best_effort_post_broker_effect(
            lambda: remember_supervisor_reentry_block(
                position=position,
                action="close",
                reason=close_reason,
                cfg=cfg,
                current_price=float(position.get("current_price", position.get("price_current", 0.0)) or 0.0),
                tick=tick,
            ),
            position_id=pid,
            action="close_position",
            stage="reentry_block",
            record_aux_failure=record_aux_failure,
            log=log,
        )
        _best_effort_post_broker_effect(
            lambda: log_supervisor_trace(
                position=position,
                verdict=verdict,
                cfg=cfg,
                tick=tick,
                stage="executed",
                outcome="applied",
                decision_id=decision_id,
                risk_action=risk_action,
                risk_verdict=risk_verdict,
                execution_status="applied",
                execution_reason="close_position_success",
                execution={"applied_controls": controls},
                acct=acct,
            ),
            position_id=pid,
            action="close_position",
            stage="supervisor_trace",
            record_aux_failure=record_aux_failure,
            log=log,
        )
        if result_is_position_not_found(result):
            retire_broker_missing_position(
                bridge,
                pid,
                broker=broker,
                strategy_name=strategy_name,
                reason=close_reason,
                log=log,
            )
        log(f"tick {tick}: supervisor close sent pos={pid} reason={verdict.get('summary_reason')}")
        return

    reason = str(getattr(result, "comment", "") or getattr(result, "error", "") or "close_failed")
    log_supervisor_trace(
        position=position,
        verdict=verdict,
        cfg=cfg,
        tick=tick,
        stage="execution_failed",
        outcome="failed",
        decision_id=decision_id,
        risk_action=risk_action,
        risk_verdict=risk_verdict,
        execution_status="failed",
        execution_reason=reason,
        execution={"applied_controls": controls},
        acct=acct,
    )


def execute_supervisor_reduce_action(
    *,
    bridge: Any,
    position: dict[str, Any],
    verdict: dict[str, Any],
    risk_action: str,
    risk_verdict: dict[str, Any],
    decision_id: str,
    cfg: Any,
    tick: int,
    acct: dict[str, Any] | None,
    controls: dict[str, Any],
    log: Callable[[str], Any],
    ledger: Any = None,
    broker: str = "ctrader",
    strategy_name: str = "factor_v4",
    execution_plan: dict[str, Any],
    log_supervisor_trace: Callable[..., Any],
    remember_supervisor_state: Callable[..., Any],
    remember_supervisor_reentry_block: Callable[..., Any],
    remember_close_reason: Callable[[int, str], Any],
    remember_close_verdict: Callable[[int, Any], Any],
    result_is_position_not_found: Callable[[Any], bool],
    retire_broker_missing_position: Callable[..., Any],
    record_partial_close_execution: Callable[..., Any] | None = None,
    capture_partial_close_session_cursor: Callable[..., Any] | None = None,
    sync_partial_close_session_fact: Callable[..., Any] | None = None,
    record_aux_failure: Callable[..., Any] | None = None,
) -> None:
    pid = int(position.get("position_id") or position.get("ticket") or 0)
    current_volume = float((execution_plan or {}).get("current_volume") or 0.0)
    reduce_volume = float((execution_plan or {}).get("reduce_volume") or 0.0)

    if str((execution_plan or {}).get("effective_action") or "") == "reduce":
        deal_cursor: dict[str, Any] = {
            "status": "unavailable",
            "baseline_cursor_available": False,
        }
        if capture_partial_close_session_cursor is not None:
            try:
                captured = capture_partial_close_session_cursor(position_id=pid)
                if isinstance(captured, dict):
                    deal_cursor = dict(captured)
            except Exception as exc:
                deal_cursor["error"] = f"{type(exc).__name__}:{exc}"
                if record_aux_failure is not None:
                    try:
                        record_aux_failure(
                            "partial_close_deal_cursor_unavailable",
                            position_id=pid,
                            action="reduce_position",
                            error=exc,
                            payload={"stage": "pre_broker_deal_cursor"},
                        )
                    except Exception:
                        pass
        result = bridge.close_position(pid, volume=reduce_volume)
        if getattr(result, "success", False):
            close_ts = time.time()
            accounting_recorded = None
            if record_partial_close_execution is not None:
                try:
                    accounting_recorded = bool(
                        record_partial_close_execution(
                            position_id=pid,
                            close_price=float(
                                getattr(result, "price", 0.0)
                                or position.get("current_price", position.get("price_current", 0.0))
                                or 0.0
                            ),
                            close_ts=close_ts,
                            volume=reduce_volume,
                            reason=str(verdict.get("summary_reason") or "supervisor_reduce"),
                        )
                    )
                except Exception as exc:
                    log(f"tick {tick}: partial close accounting failed pos={pid}: {exc}")
                    if record_aux_failure is not None:
                        try:
                            record_aux_failure(
                                "risk_reduction_post_broker_aux_failure",
                                position_id=pid,
                                action="reduce_position",
                                error=exc,
                                payload={"stage": "partial_close_accounting"},
                            )
                        except Exception:
                            pass
            session_fact_recorded = None
            if sync_partial_close_session_fact is not None:
                try:
                    session_fact_recorded = bool(
                        sync_partial_close_session_fact(
                            position_id=pid,
                            close_ts=close_ts,
                            volume=reduce_volume,
                            deal_cursor=deal_cursor,
                        )
                    )
                except Exception as exc:
                    log(
                        f"tick {tick}: partial close session fact failed "
                        f"pos={pid}: {exc}"
                    )
                    if record_aux_failure is not None:
                        try:
                            record_aux_failure(
                                "risk_reduction_post_broker_aux_failure",
                                position_id=pid,
                                action="reduce_position",
                                error=exc,
                                payload={"stage": "partial_close_session_fact"},
                            )
                        except Exception:
                            pass
            if ledger:
                _best_effort_post_broker_effect(
                    lambda: ledger.log_position_event(
                        position_id=str(pid),
                        trade_id=str(pid),
                        symbol=str(position.get("symbol") or "XAUUSD+"),
                        event_type="reduced",
                        net_volume=max(0.0, current_volume - reduce_volume),
                        avg_price=float(position.get("current_price", position.get("price_current", 0.0)) or 0.0),
                        # The final ``closed`` event owns the aggregate PnL for
                        # the position. Partial-close PnL remains in execution
                        # details and must not be summed again at lifecycle level.
                        realized_pnl=0.0,
                        details={
                            "supervisor_action": "reduce",
                            "supervisor_reason": verdict.get("summary_reason"),
                            "risk_verdict_reason": risk_verdict.get("reason"),
                            "applied_controls": {**(controls or {}), "reduce_volume": reduce_volume},
                            "realized_pnl_scope": "execution_detail_only",
                        },
                    ),
                    position_id=pid,
                    action="reduce_position",
                    stage="position_event",
                    record_aux_failure=record_aux_failure,
                    log=log,
                )
            _best_effort_post_broker_effect(
                lambda: remember_supervisor_state(
                    position,
                    verdict,
                    action_applied="reduce",
                    broker=broker,
                    strategy_name=strategy_name,
                ),
                position_id=pid,
                action="reduce_position",
                stage="supervisor_state",
                record_aux_failure=record_aux_failure,
                log=log,
            )
            _best_effort_post_broker_effect(
                lambda: log_supervisor_trace(
                    position=position,
                    verdict=verdict,
                    cfg=cfg,
                    tick=tick,
                    stage="executed",
                    outcome="applied",
                    decision_id=decision_id,
                    risk_action=risk_action,
                    risk_verdict=risk_verdict,
                    execution_status="applied",
                    execution_reason="partial_close_success",
                    execution={
                        "reduce_volume": reduce_volume,
                        "applied_controls": controls,
                        "accounting_recorded": accounting_recorded,
                        "session_fact_recorded": session_fact_recorded,
                    },
                    acct=acct,
                ),
                position_id=pid,
                action="reduce_position",
                stage="supervisor_trace",
                record_aux_failure=record_aux_failure,
                log=log,
            )
            if result_is_position_not_found(result):
                retire_broker_missing_position(
                    bridge,
                    pid,
                    broker=broker,
                    strategy_name=strategy_name,
                    reason="position_not_found_after_reduce",
                    log=log,
                )
            log(f"tick {tick}: supervisor reduce pos={pid} vol={reduce_volume:.0f}")
        else:
            reason = str(getattr(result, "comment", "") or getattr(result, "error", "") or "reduce_failed")
            log_supervisor_trace(
                position=position,
                verdict=verdict,
                cfg=cfg,
                tick=tick,
                stage="execution_failed",
                outcome="failed",
                decision_id=decision_id,
                risk_action=risk_action,
                risk_verdict=risk_verdict,
                execution_status="failed",
                execution_reason=reason,
                execution={"reduce_volume": reduce_volume, "applied_controls": controls},
                acct=acct,
            )
        return

    raise ValueError("supervisor_reduce_execution_plan_not_tradeable")
