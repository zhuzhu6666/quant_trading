"""Action executors for live position supervision.

The live service still owns the loop, broker bridge, risk policy, and ledger
instances.  Helpers here only keep action-specific branching out of the large
service module while dependencies are injected by the caller.
"""

from __future__ import annotations

import time
from typing import Any, Callable


class MappingVerdictProxy:
    def __init__(self, payload: dict[str, Any]):
        self._payload = dict(payload or {})

    def to_dict(self) -> dict[str, Any]:
        return dict(self._payload)


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
        return
    amend_res = bridge.amend_position_sltp(pid, sl=planned_sl, tp=planned_tp)
    if getattr(amend_res, "success", False):
        track_local_sl_tp(pid, sl=planned_sl, tp=planned_tp)
        result_payloads = build_tighten_result_payloads(
            result="applied",
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
        log_supervisor_position_event(
            position=position,
            event_type=result_payloads["position_event_type"],
            details=result_payloads["position_event_details"],
        )
        remember_supervisor_state(
            position,
            verdict,
            action_applied="tighten",
            broker=broker,
            strategy_name=strategy_name,
        )
        remember_supervisor_reentry_block(
            position=position,
            action="tighten",
            reason=str(verdict.get("summary_reason") or controls.get("close_reason") or "supervisor_reduce"),
            cfg=cfg,
            current_price=float(position.get("current_price", position.get("price_current", 0.0)) or 0.0),
            tick=tick,
        )
        log_supervisor_trace(
            position=position,
            verdict=verdict,
            cfg=cfg,
            tick=tick,
            **result_payloads["trace_fields"],
            acct=acct,
        )
        tp_suffix = f" tp->{planned_tp:.2f}" if planned_tp != current_tp else ""
        log(f"tick {tick}: supervisor tighten pos={pid} sl->{planned_sl:.2f}{tp_suffix}")
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
) -> None:
    pid = int(position.get("position_id") or position.get("ticket") or 0)
    close_reason = str(controls.get("close_reason") or verdict.get("summary_reason") or "supervisor_close")
    result = bridge.close_position(pid)
    if getattr(result, "success", False):
        remember_close_reason(pid, close_reason)
        remember_close_verdict(pid, MappingVerdictProxy(risk_verdict))
        remember_supervisor_state(
            position,
            verdict,
            action_applied="close",
            broker=broker,
            strategy_name=strategy_name,
        )
        remember_supervisor_reentry_block(
            position=position,
            action="close",
            reason=close_reason,
            cfg=cfg,
            current_price=float(position.get("current_price", position.get("price_current", 0.0)) or 0.0),
            tick=tick,
        )
        log_supervisor_trace(
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
    floor_api_volume_to_step: Callable[[float, dict[str, Any]], float],
    should_full_close_untradeable_reduce: Callable[..., tuple[bool, str]],
    build_close_position_risk_context: Callable[..., dict[str, Any]],
    risk_policy_evaluate: Callable[[str, dict[str, Any]], Any],
    log_supervisor_trace: Callable[..., Any],
    remember_supervisor_state: Callable[..., Any],
    remember_supervisor_reentry_block: Callable[..., Any],
    remember_close_reason: Callable[[int, str], Any],
    remember_close_verdict: Callable[[int, Any], Any],
    result_is_position_not_found: Callable[[Any], bool],
    retire_broker_missing_position: Callable[..., Any],
    record_partial_close_execution: Callable[..., Any] | None = None,
) -> None:
    pid = int(position.get("position_id") or position.get("ticket") or 0)
    current_volume = float(position.get("volume", position.get("api_volume", 0.0)) or 0.0)
    reduce_fraction = float((controls or {}).get("reduce_fraction", 0.0) or 0.0)
    raw_reduce_volume = current_volume * reduce_fraction
    bridge_meta = _resolve_bridge_volume_meta(bridge, position)
    min_volume = float(bridge_meta.get("api_min_volume") or 1.0)
    step_volume = float(bridge_meta.get("api_step_volume") or 1.0)
    reduce_volume = floor_api_volume_to_step(raw_reduce_volume, bridge_meta)

    if reduce_volume >= min_volume and current_volume - reduce_volume >= min_volume:
        result = bridge.close_position(pid, volume=reduce_volume)
        if getattr(result, "success", False):
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
                            close_ts=time.time(),
                            volume=reduce_volume,
                            reason=str(verdict.get("summary_reason") or "supervisor_reduce"),
                        )
                    )
                except Exception as exc:
                    log(f"tick {tick}: partial close accounting failed pos={pid}: {exc}")
            if ledger:
                ledger.log_position_event(
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
                )
            remember_supervisor_state(
                position,
                verdict,
                action_applied="reduce",
                broker=broker,
                strategy_name=strategy_name,
            )
            log_supervisor_trace(
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
                },
                acct=acct,
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

    invalid_reduce_execution = {
        "current_volume": current_volume,
        "reduce_fraction": reduce_fraction,
        "raw_reduce_volume": raw_reduce_volume,
        "reduce_volume": reduce_volume,
        "min_volume": min_volume,
        "step_volume": step_volume,
    }
    upgrade_to_close, upgrade_reason = should_full_close_untradeable_reduce(
        current_volume=current_volume,
        raw_reduce_volume=raw_reduce_volume,
        reduce_volume=reduce_volume,
        min_volume=min_volume,
        verdict=verdict,
    )
    if not upgrade_to_close:
        log_supervisor_trace(
            position=position,
            verdict=verdict,
            cfg=cfg,
            tick=tick,
            stage="execution_skipped",
            outcome="skipped",
            decision_id=decision_id,
            risk_action=risk_action,
            risk_verdict=risk_verdict,
            execution_status="skipped",
            execution_reason="invalid_reduce_volume",
            execution={**invalid_reduce_execution, "fallback_skip_reason": upgrade_reason},
            acct=acct,
        )
        return

    close_reason = str(
        (controls or {}).get("close_reason")
        or verdict.get("summary_reason")
        or "minimum_position_reduce_full_close"
    )
    close_context = build_close_position_risk_context(
        position_id=pid,
        close_reason=close_reason,
        mode="live",
        broker=broker,
        symbol=str(position.get("symbol") or "XAUUSD+"),
        position=position,
        cfg=cfg,
        decision_ts=float(verdict.get("decision_ts") or time.time()),
    )
    close_context.update(
        {
            "supervisor_action": "reduce_to_close",
            "supervisor_confidence": verdict.get("confidence"),
            "supervisor_reason": verdict.get("summary_reason"),
            "supervisor_evidence": verdict.get("evidence") or {},
            "supervisor_decision_ts": verdict.get("decision_ts"),
            "recommended_controls": {
                **(controls or {}),
                "original_action": "reduce",
                "fallback_action": "close",
                "fallback_reason": upgrade_reason,
            },
        }
    )
    close_verdict = risk_policy_evaluate("close_position", close_context).to_dict()
    fallback_execution = {
        **invalid_reduce_execution,
        "fallback_action": "close",
        "fallback_reason": upgrade_reason,
        "applied_controls": controls,
    }
    if not close_verdict.get("allowed", False):
        log_supervisor_trace(
            position=position,
            verdict=verdict,
            cfg=cfg,
            tick=tick,
            stage="risk_rejected",
            outcome="blocked",
            decision_id=decision_id,
            risk_action="close_position",
            risk_verdict=close_verdict,
            execution_status="blocked",
            execution_reason=str(close_verdict.get("reason") or "fallback_close_blocked"),
            execution=fallback_execution,
            acct=acct,
        )
        remember_supervisor_state(position, verdict, broker=broker, strategy_name=strategy_name)
        return

    result = bridge.close_position(pid)
    if getattr(result, "success", False):
        remember_close_reason(pid, close_reason)
        remember_close_verdict(pid, MappingVerdictProxy(close_verdict))
        remember_supervisor_state(
            position,
            verdict,
            action_applied="close",
            broker=broker,
            strategy_name=strategy_name,
        )
        remember_supervisor_reentry_block(
            position=position,
            action="close",
            reason=close_reason,
            cfg=cfg,
            current_price=float(position.get("current_price", position.get("price_current", 0.0)) or 0.0),
            tick=tick,
        )
        log_supervisor_trace(
            position=position,
            verdict=verdict,
            cfg=cfg,
            tick=tick,
            stage="executed",
            outcome="applied",
            decision_id=decision_id,
            risk_action="close_position",
            risk_verdict=close_verdict,
            execution_status="applied",
            execution_reason="minimum_position_reduce_full_close_success",
            execution=fallback_execution,
            acct=acct,
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
        log(f"tick {tick}: supervisor reduce->close sent pos={pid} reason={upgrade_reason}")
    else:
        reason = str(getattr(result, "comment", "") or getattr(result, "error", "") or "fallback_close_failed")
        log_supervisor_trace(
            position=position,
            verdict=verdict,
            cfg=cfg,
            tick=tick,
            stage="execution_failed",
            outcome="failed",
            decision_id=decision_id,
            risk_action="close_position",
            risk_verdict=close_verdict,
            execution_status="failed",
            execution_reason=reason,
            execution=fallback_execution,
            acct=acct,
        )
