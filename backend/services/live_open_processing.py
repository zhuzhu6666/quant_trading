"""Post-fill open attribution, recovery, ledger, and failure processing."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class FilledOpenRequest:
    attr_engine: Any
    broker: str
    cfg: Any
    bar: dict[str, Any]
    tick: int
    pid: int
    actual_api_volume: float
    requested_volume: float
    fill_price: float
    current_price: float
    sl_price: float
    tp_price: float
    acct: dict[str, Any]
    pos: list[Any]
    composite: Any
    gate_result: Any
    risk_verdict: Any = None
    market_session: dict[str, Any] | None = None
    base_requested_volume: float | None = None
    event_sizing_context: dict[str, Any] | None = None
    sizing_trace: dict[str, Any] | None = None
    sl_dist: float = 0.0
    tp_dist: float = 0.0
    bridge: Any = None
    parent_decision_id: str = ""
    execution_intent_id: str = ""
    position_supervisor_binding: dict[str, Any] | None = None


@dataclass(frozen=True)
class FilledOpenRuntime:
    ledger_available: bool
    record_attribution: Callable[..., Any]
    build_learning_context: Callable[..., dict[str, Any]]
    log_ledger: Callable[..., str]
    upsert_recovery: Callable[..., Any]
    debug: Callable[..., Any]


def _learning_context_kwargs(
    request: FilledOpenRequest,
    *,
    base_volume: float,
    include_sizing_trace: bool,
) -> dict[str, Any]:
    kwargs = {
        "bridge": request.bridge,
        "bar": request.bar,
        "positions_before": request.pos,
        "composite": request.composite,
        "symbol": "XAUUSD+",
        "pid": int(request.pid),
        "actual_api_volume": float(request.actual_api_volume or 0.0),
        "requested_volume": float(request.requested_volume or 0.0),
        "base_requested_volume": base_volume,
        "current_price": float(request.current_price or 0.0),
        "fill_price": float(request.fill_price or 0.0),
        "sl_price": float(request.sl_price or 0.0),
        "tp_price": float(request.tp_price or 0.0),
        "sl_dist": float(request.sl_dist or 0.0),
        "tp_dist": float(request.tp_dist or 0.0),
        "event_sizing_context": request.event_sizing_context or {},
        "risk_verdict": request.risk_verdict,
        "market_session": request.market_session,
    }
    if include_sizing_trace:
        kwargs["sizing_trace"] = request.sizing_trace or {}
    if request.position_supervisor_binding:
        kwargs["position_supervisor_binding"] = dict(
            request.position_supervisor_binding
        )
    return kwargs


def record_filled_position_open_context(
    request: FilledOpenRequest,
    *,
    runtime: FilledOpenRuntime,
) -> str:
    """Persist filled-open recovery even when optional ledger work fails."""

    base_volume = float(
        request.base_requested_volume
        if request.base_requested_volume is not None
        else request.requested_volume or 0.0
    )
    trade_attribution_payload = runtime.record_attribution(
        attr_engine=request.attr_engine,
        pid=request.pid,
        current_price=request.current_price,
        actual_api_volume=request.actual_api_volume,
        composite=request.composite,
    )
    entry_decision_id = ""
    if runtime.ledger_available:
        try:
            ledger_learning_context = runtime.build_learning_context(
                **_learning_context_kwargs(
                    request,
                    base_volume=base_volume,
                    include_sizing_trace=True,
                )
            )
            entry_decision_id = runtime.log_ledger(
                cfg=request.cfg,
                bar=request.bar,
                tick=request.tick,
                pid=request.pid,
                actual_api_volume=request.actual_api_volume,
                requested_volume=request.requested_volume,
                fill_price=request.fill_price,
                current_price=request.current_price,
                sl_price=request.sl_price,
                tp_price=request.tp_price,
                acct=request.acct,
                pos=request.pos,
                composite=request.composite,
                gate_result=request.gate_result,
                learning_context=ledger_learning_context,
                risk_verdict=request.risk_verdict,
                sizing_trace=request.sizing_trace,
                parent_decision_id=request.parent_decision_id,
                execution_intent_id=request.execution_intent_id,
            )
        except Exception as exc:
            runtime.debug(
                "[live] ledger open persist failed for pos %s: %s",
                request.pid,
                exc,
            )
    try:
        recovery_learning_context = runtime.build_learning_context(
            **_learning_context_kwargs(
                request,
                base_volume=base_volume,
                include_sizing_trace=False,
            )
        )
        runtime.upsert_recovery(
            broker=request.broker,
            tick=request.tick,
            pid=request.pid,
            actual_api_volume=request.actual_api_volume,
            requested_volume=request.requested_volume,
            fill_price=request.fill_price,
            current_price=request.current_price,
            sl_price=request.sl_price,
            tp_price=request.tp_price,
            composite=request.composite,
            entry_decision_id=entry_decision_id,
            execution_intent_id=request.execution_intent_id,
            trade_attribution_payload=trade_attribution_payload,
            learning_context=recovery_learning_context,
            position_supervisor_binding=request.position_supervisor_binding,
        )
    except Exception as exc:
        runtime.debug(
            "[live] recovery open persist failed for pos %s: %s",
            request.pid,
            exc,
        )
    return entry_decision_id


@dataclass(frozen=True)
class AmendedOpenSuccessRequest:
    attr_engine: Any
    bridge: Any
    broker: str
    cfg: Any
    bar: dict[str, Any]
    tick: int
    pid: int
    actual_api_volume: float
    requested_volume: float
    base_requested_volume: float
    fill_price: float
    current_price: float
    sl_price: float
    tp_price: float
    sl_dist: float
    tp_dist: float
    acct: dict[str, Any]
    pos: list[Any]
    composite: Any
    gate_result: Any
    risk_verdict: Any
    market_session: dict[str, Any]
    event_sizing_context: dict[str, Any]
    sizing_trace: dict[str, Any]
    entry_protection_plan: dict[str, Any]
    direction_name: str
    log: Callable[[str], Any]
    submit_started_at: float | None = None
    fill_received_at: float | None = None
    parent_decision_id: str = ""
    execution_intent_id: str = ""
    position_supervisor_binding: dict[str, Any] | None = None


@dataclass(frozen=True)
class AmendedOpenSuccessRuntime:
    mark_local_state: Callable[..., Any]
    record_execution_quality: Callable[..., Any]
    record_attribution: Callable[..., Any]
    build_learning_context: Callable[..., dict[str, Any]]
    log_ledger: Callable[..., str]
    upsert_recovery: Callable[..., Any]


def record_amended_open_success_context(
    request: AmendedOpenSuccessRequest,
    *,
    runtime: AmendedOpenSuccessRuntime,
) -> None:
    """Publish confirmed protection context in the canonical order."""

    runtime.mark_local_state(
        pid=request.pid,
        sl_price=request.sl_price,
        tp_price=request.tp_price,
        tick=request.tick,
        actual_api_volume=request.actual_api_volume,
        composite=request.composite,
        direction_name=request.direction_name,
        log=request.log,
    )
    runtime.record_execution_quality(
        bar=request.bar,
        current_price=request.current_price,
        fill_price=request.fill_price,
        composite=request.composite,
        actual_api_volume=request.actual_api_volume,
        pid=request.pid,
        submit_started_at=request.submit_started_at,
        fill_received_at=request.fill_received_at,
    )
    try:
        trade_attribution = runtime.record_attribution(
            attr_engine=request.attr_engine,
            pid=request.pid,
            current_price=request.current_price,
            actual_api_volume=request.actual_api_volume,
            composite=request.composite,
            tick=request.tick,
            log=request.log,
        )
        learning_context = runtime.build_learning_context(
            bridge=request.bridge,
            bar=request.bar,
            positions_before=request.pos,
            composite=request.composite,
            symbol="XAUUSD+",
            pid=int(request.pid),
            actual_api_volume=float(request.actual_api_volume or 0.0),
            requested_volume=float(request.requested_volume or 0.0),
            base_requested_volume=float(request.base_requested_volume or 0.0),
            current_price=float(request.current_price or 0.0),
            fill_price=float(request.fill_price or 0.0),
            sl_price=float(request.sl_price or 0.0),
            tp_price=float(request.tp_price or 0.0),
            sl_dist=float(request.sl_dist or 0.0),
            tp_dist=float(request.tp_dist or 0.0),
            event_sizing_context=request.event_sizing_context,
            sizing_trace=request.sizing_trace,
            risk_verdict=request.risk_verdict,
            market_session=request.market_session,
            position_supervisor_binding=(
                request.position_supervisor_binding
                or dict(
                    (request.entry_protection_plan or {}).get(
                        "supervisor_binding"
                    )
                    or {}
                )
            ),
        )
        entry_decision_id = runtime.log_ledger(
            cfg=request.cfg,
            bar=request.bar,
            tick=request.tick,
            pid=request.pid,
            actual_api_volume=request.actual_api_volume,
            requested_volume=request.requested_volume,
            base_requested_volume=request.base_requested_volume,
            fill_price=request.fill_price,
            current_price=request.current_price,
            sl_price=request.sl_price,
            tp_price=request.tp_price,
            acct=request.acct,
            pos=request.pos,
            composite=request.composite,
            gate_result=request.gate_result,
            risk_verdict=request.risk_verdict,
            event_sizing_context=request.event_sizing_context,
            sizing_trace=request.sizing_trace,
            learning_context=learning_context,
            parent_decision_id=request.parent_decision_id,
            execution_intent_id=request.execution_intent_id,
        )
        runtime.upsert_recovery(
            broker=request.broker,
            tick=request.tick,
            pid=request.pid,
            actual_api_volume=request.actual_api_volume,
            requested_volume=request.requested_volume,
            fill_price=request.fill_price,
            current_price=request.current_price,
            sl_price=request.sl_price,
            tp_price=request.tp_price,
            composite=request.composite,
            entry_decision_id=entry_decision_id,
            execution_intent_id=request.execution_intent_id,
            entry_protection_plan=request.entry_protection_plan,
            trade_attr=trade_attribution,
            event_sizing_context=request.event_sizing_context,
            sizing_trace=request.sizing_trace,
            learning_context=learning_context,
        )
    except Exception as exc:
        request.log(
            f"tick {request.tick}: attribution record_open error: {exc}"
        )


@dataclass(frozen=True)
class AmendFailureRequest:
    attr_engine: Any
    bridge: Any
    broker: str
    cfg: Any
    bar: dict[str, Any]
    tick: int
    pid: int
    actual_api_volume: float
    requested_volume: float
    base_requested_volume: float
    fill_price: float
    current_price: float
    sl_price: float
    tp_price: float
    sl_dist: float
    tp_dist: float
    acct: dict[str, Any]
    pos: list[Any]
    composite: Any
    gate_result: Any
    risk_verdict: Any
    market_session: dict[str, Any] | None
    event_sizing_context: dict[str, Any]
    sizing_trace: dict[str, Any]
    status_error: str
    ledger_action_reason: str
    ledger_comment: str = ""
    ledger_error: str = ""
    ledger_debug_message: str = (
        "[live] ledger amend failed event failed for pos %s: %s"
    )
    failure_log: str = ""
    log: Callable[[str], Any] | None = None
    parent_decision_id: str = ""
    execution_intent_id: str = ""
    position_supervisor_binding: dict[str, Any] | None = None


@dataclass(frozen=True)
class AmendFailureRuntime:
    persist_fail_closed: Callable[..., Any]
    record_aux_failure: Callable[..., Any]
    record_filled_context: Callable[[FilledOpenRequest], str]
    update_plan_status: Callable[..., Any]
    ledger_available: bool
    build_failed_payloads: Callable[..., dict[str, Any]]
    get_risk_state: Callable[[], dict[str, Any]]
    log_composite_decision: Callable[..., Any]
    log_order_event: Callable[..., Any]
    debug: Callable[..., Any]
    now: Callable[[], float]


def record_amend_failure_after_fill(
    request: AmendFailureRequest,
    *,
    runtime: AmendFailureRuntime,
) -> None:
    """Latch first, then preserve filled-open recovery and failure audit."""

    try:
        runtime.persist_fail_closed(
            blockers=("entry_protection_unverified",),
            source="entry_protection",
            error=str(
                request.status_error
                or request.ledger_action_reason
                or "entry_protection_failed"
            ),
        )
    except Exception as exc:
        try:
            runtime.record_aux_failure(
                "entry_protection_fail_closed_unavailable",
                position_id=int(request.pid or 0),
                action="amend_position_sltp",
                error=exc,
                payload={"status_error": str(request.status_error or "")},
            )
        except Exception:
            pass
    if request.failure_log and request.log is not None:
        request.log(request.failure_log)
    runtime.record_filled_context(
        FilledOpenRequest(
            attr_engine=request.attr_engine,
            broker=request.broker,
            cfg=request.cfg,
            bar=request.bar,
            tick=request.tick,
            pid=request.pid,
            actual_api_volume=request.actual_api_volume,
            requested_volume=request.requested_volume,
            fill_price=request.fill_price,
            current_price=request.current_price,
            sl_price=request.sl_price,
            tp_price=request.tp_price,
            acct=request.acct,
            pos=request.pos,
            composite=request.composite,
            gate_result=request.gate_result,
            risk_verdict=request.risk_verdict,
            market_session=request.market_session,
            base_requested_volume=request.base_requested_volume,
            event_sizing_context=request.event_sizing_context,
            sizing_trace=request.sizing_trace,
            sl_dist=request.sl_dist,
            tp_dist=request.tp_dist,
            bridge=request.bridge,
            parent_decision_id=request.parent_decision_id,
            execution_intent_id=request.execution_intent_id,
            position_supervisor_binding=request.position_supervisor_binding,
        )
    )
    runtime.update_plan_status(
        int(request.pid),
        status="failed",
        error=request.status_error,
        attempted=True,
    )
    if not runtime.ledger_available:
        return
    try:
        payloads = runtime.build_failed_payloads(
            composite=request.composite,
            gate_result=request.gate_result,
            cfg=request.cfg,
            bar=request.bar,
            account=request.acct,
            positions_before=request.pos,
            risk_state=runtime.get_risk_state(),
            pid=int(request.pid),
            requested_volume=float(request.requested_volume),
            fill_price=float(request.fill_price),
            sl_price=float(request.sl_price),
            tp_price=float(request.tp_price),
            actual_api_volume=float(request.actual_api_volume),
            tick=request.tick,
            action_reason=request.ledger_action_reason,
            comment=request.ledger_comment,
            error=request.ledger_error,
            decision_ts_fallback=runtime.now(),
        )
        decision_payload = dict(payloads["decision"])
        decision_payload["action_json"] = {
            **dict(decision_payload.get("action_json") or {}),
            "parent_decision_id": str(request.parent_decision_id or ""),
            "execution_intent_id": str(request.execution_intent_id or ""),
            "event_stage": "protection_failed",
        }
        decision_id = runtime.log_composite_decision(
            **decision_payload
        )
        lineage_decision_id = str(request.parent_decision_id or decision_id or "")
        order_event = dict(payloads["order_event"])
        order_event["details"] = {
            **dict(order_event.get("details") or {}),
            "decision_id": lineage_decision_id,
            "child_decision_id": str(decision_id or ""),
            "parent_decision_id": str(request.parent_decision_id or ""),
            "execution_intent_id": str(request.execution_intent_id or ""),
        }
        runtime.log_order_event(
            decision_id=lineage_decision_id,
            **order_event,
        )
    except Exception as exc:
        runtime.debug(
            request.ledger_debug_message,
            request.pid,
            exc,
        )


_ls_module = None


def _live_service():
    """Lazy handle to the live loop module (import-order-safe)."""
    global _ls_module
    if _ls_module is None:
        from backend.services import live_service as _module
        _ls_module = _module
    return _ls_module


from backend.services import live_close_settlement
from backend.services.live_open_processing import (
    AmendFailureRequest,
    AmendFailureRuntime,
    AmendedOpenSuccessRequest,
    AmendedOpenSuccessRuntime,
    FilledOpenRequest,
    FilledOpenRuntime,
    record_amend_failure_after_fill as _runtime_record_amend_failure,
    record_amended_open_success_context as _runtime_record_amended_success,
    record_filled_position_open_context as _runtime_record_filled_open,
)
from backend.services.live_position_lifecycle import (
    build_applied_entry_protection_plan_payload as _lifecycle_build_applied_entry_protection_plan_payload,
    build_filled_open_ledger_payloads as _lifecycle_build_filled_open_ledger_payloads,
    build_filled_open_recovery_payloads as _lifecycle_build_filled_open_recovery_payloads,
)
from backend.services.live_state_store import live_state_get
from backend.services.live_tick_pipeline import (
    build_amend_failed_ledger_payloads as _tick_build_amend_failed_ledger_payloads,
    build_open_ledger_payloads as _tick_build_open_ledger_payloads,
)
from execution.analytics import (
    TradeExecution as _ExecTrade,
)
from loguru import logger
from typing import Any
import time

# # moved from live_service (2026-09-12 structural repair)

def _record_filled_open_attribution(
    *,
    attr_engine: Any,
    pid: int,
    current_price: float,
    actual_api_volume: float,
    composite: Any,
) -> dict[str, Any]:
    trade_attribution_payload: dict[str, Any] = {}
    try:
        from alpha.attribution_engine import TradeAttribution

        trade_attribution_payload = _live_service()._trade_attribution_payload_from_composite(
            position_id=pid,
            open_ts=time.time(),
            open_price=current_price,
            direction=composite.direction,
            actual_api_volume=actual_api_volume,
            composite=composite,
        )
        trade_attr = TradeAttribution.from_jsonable(trade_attribution_payload)
        if attr_engine is not None and trade_attr is not None:
            attr_engine.record_open(pid, trade_attr)
            trade_attribution_payload = trade_attr.to_jsonable()
        _live_service()._pos_open_prices[pid] = current_price
        _live_service()._pos_open_api_volume[pid] = float(actual_api_volume)
    except Exception as attr_err:
        logger.debug("[live] attribution open persist failed for pos {}: {}", pid, attr_err)
    return trade_attribution_payload


def _log_filled_open_ledger(
    *,
    cfg: Any,
    bar: dict,
    tick: int,
    pid: int,
    actual_api_volume: float,
    requested_volume: float,
    fill_price: float,
    current_price: float,
    sl_price: float,
    tp_price: float,
    acct: dict,
    pos: list,
    composite: Any,
    gate_result: Any,
    learning_context: dict[str, Any],
    risk_verdict: Any = None,
    sizing_trace: dict[str, Any] | None = None,
    parent_decision_id: str = "",
    execution_intent_id: str = "",
) -> str:
    if not _live_service()._LEDGER:
        return ""
    try:
        ledger_payloads = _lifecycle_build_filled_open_ledger_payloads(
            cfg=cfg,
            bar=bar,
            tick=tick,
            pid=pid,
            actual_api_volume=actual_api_volume,
            requested_volume=requested_volume,
            fill_price=fill_price,
            current_price=current_price,
            sl_price=sl_price,
            tp_price=tp_price,
            acct=acct,
            positions_before=pos,
            composite=composite,
            gate_result=gate_result,
            learning_context={"sizing_trace": sizing_trace or {}, **learning_context},
            risk_state=(
                _live_service()._risk_state_with_verdict(risk_verdict)
                if risk_verdict is not None
                else (live_state_get("risk", {}, clone=True) or {})
            ),
            session_pnl=float(live_state_get("session_pnl", 0) or 0.0),
            risk_verdict=risk_verdict,
            decision_ts_fallback=time.time(),
            event_ts=time.time(),
        )
        lineage = {
            "parent_decision_id": str(parent_decision_id or ""),
            "execution_intent_id": str(execution_intent_id or ""),
        }
        decision_payload = ledger_payloads["composite_decision_payload"]
        decision_payload["action_json"] = {
            **dict(decision_payload.get("action_json") or {}),
            **lineage,
            "event_stage": "fill",
        }
        for payload_key in ("submitted_order_payload", "filled_order_payload"):
            order_payload = ledger_payloads[payload_key]
            order_payload["details"] = {
                **dict(order_payload.get("details") or {}),
                **lineage,
            }
        ledger_payloads["position_event_payload"]["details"] = {
            **dict(ledger_payloads["position_event_payload"].get("details") or {}),
            **lineage,
        }
        entry_decision_id = _live_service()._LEDGER.log_composite_decision(
            **ledger_payloads["composite_decision_payload"]
        )
        lineage_decision_id = str(parent_decision_id or entry_decision_id or "")
        for payload_key in ("submitted_order_payload", "filled_order_payload"):
            order_payload = ledger_payloads[payload_key]
            order_payload["details"] = {
                **dict(order_payload.get("details") or {}),
                "decision_id": lineage_decision_id,
                "child_decision_id": str(entry_decision_id or ""),
            }
        ledger_payloads["position_event_payload"]["details"] = {
            **dict(ledger_payloads["position_event_payload"].get("details") or {}),
            "decision_id": lineage_decision_id,
            "child_decision_id": str(entry_decision_id or ""),
        }
        _live_service()._pos_entry_decisions[int(pid)] = lineage_decision_id
        _live_service()._LEDGER.log_order_event(
            decision_id=lineage_decision_id,
            **ledger_payloads["submitted_order_payload"],
        )
        _live_service()._LEDGER.log_order_event(
            decision_id=lineage_decision_id,
            **ledger_payloads["filled_order_payload"],
        )
        _live_service()._LEDGER.log_position_event(
            decision_id=lineage_decision_id,
            **ledger_payloads["position_event_payload"],
        )
        return lineage_decision_id
    except Exception as ledger_err:
        logger.debug("[live] ledger open persist failed for pos {}: {}", pid, ledger_err)
        return ""


def _upsert_filled_open_recovery(
    *,
    broker: str,
    tick: int,
    pid: int,
    actual_api_volume: float,
    requested_volume: float,
    fill_price: float,
    current_price: float,
    sl_price: float,
    tp_price: float,
    composite: Any,
    entry_decision_id: str,
    trade_attribution_payload: dict[str, Any],
    learning_context: dict[str, Any],
    execution_intent_id: str = "",
    position_supervisor_binding: dict[str, Any] | None = None,
) -> None:
    try:
        entry_protection_plan = _live_service()._entry_protection_plan_payload(
            position_id=pid,
            direction=composite.direction,
            entry_price=float(fill_price or current_price),
            target_stop_loss=sl_price,
            target_take_profit=tp_price,
            requested_volume=requested_volume,
            actual_api_volume=actual_api_volume,
            tick=tick,
            status="pending",
            supervisor_binding=position_supervisor_binding,
        )
        recovery_payloads = _lifecycle_build_filled_open_recovery_payloads(
            position_id=pid,
            broker=broker,
            strategy_name=_live_service()._current_loop_strategy_name(),
            direction=composite.direction,
            fill_price=fill_price,
            current_price=current_price,
            sl_price=sl_price,
            tp_price=tp_price,
            requested_volume=requested_volume,
            actual_api_volume=actual_api_volume,
            tick=tick,
            entry_decision_id=entry_decision_id or live_close_settlement.lookup_entry_decision_id(int(pid)),
            entry_protection_plan=entry_protection_plan,
            trade_attribution_payload=trade_attribution_payload,
            learning_context=learning_context,
            context_integrity=_live_service()._RECOVERY_CONTEXT_FULL,
        )
        recovery_payloads["meta"] = {
            **dict(recovery_payloads.get("meta") or {}),
            "parent_decision_id": str(entry_decision_id or ""),
            "execution_intent_id": str(execution_intent_id or ""),
        }
        live_close_settlement.upsert_recovery_position_state(
            recovery_payloads["state_payload"],
            **recovery_payloads["state_kwargs"],
            meta=recovery_payloads["meta"],
        )
    except Exception as recovery_err:
        logger.debug("[live] recovery open persist failed for pos {}: {}", pid, recovery_err)


def _filled_open_processing_runtime() -> FilledOpenRuntime:
    return FilledOpenRuntime(
        ledger_available=bool(_live_service()._LEDGER),
        record_attribution=_record_filled_open_attribution,
        build_learning_context=_live_service()._open_learning_context_payload,
        log_ledger=_log_filled_open_ledger,
        upsert_recovery=_upsert_filled_open_recovery,
        debug=logger.debug,
    )


def _record_filled_position_open_context(
    *,
    attr_engine,
    broker: str,
    cfg,
    bar: dict,
    tick: int,
    pid: int,
    actual_api_volume: float,
    requested_volume: float,
    fill_price: float,
    current_price: float,
    sl_price: float,
    tp_price: float,
    acct: dict,
    pos: list,
    composite,
    gate_result,
    risk_verdict=None,
    market_session: dict[str, Any] | None = None,
    base_requested_volume: float | None = None,
    event_sizing_context: dict[str, Any] | None = None,
    sizing_trace: dict[str, Any] | None = None,
    sl_dist: float = 0.0,
    tp_dist: float = 0.0,
    bridge: Any = None,
    parent_decision_id: str = "",
    execution_intent_id: str = "",
    position_supervisor_binding: dict[str, Any] | None = None,
) -> str:
    return _runtime_record_filled_open(
        FilledOpenRequest(
            attr_engine=attr_engine,
            broker=broker,
            cfg=cfg,
            bar=bar,
            tick=tick,
            pid=pid,
            actual_api_volume=actual_api_volume,
            requested_volume=requested_volume,
            fill_price=fill_price,
            current_price=current_price,
            sl_price=sl_price,
            tp_price=tp_price,
            acct=acct,
            pos=pos,
            composite=composite,
            gate_result=gate_result,
            risk_verdict=risk_verdict,
            market_session=market_session,
            base_requested_volume=base_requested_volume,
            event_sizing_context=event_sizing_context,
            sizing_trace=sizing_trace,
            sl_dist=sl_dist,
            tp_dist=tp_dist,
            bridge=bridge,
            parent_decision_id=parent_decision_id,
            execution_intent_id=execution_intent_id,
            position_supervisor_binding=position_supervisor_binding,
        ),
        runtime=_filled_open_processing_runtime(),
    )


def _mark_amended_open_success_local_state(
    *,
    pid: int,
    sl_price: float,
    tp_price: float,
    tick: int,
    actual_api_volume: float,
    composite: Any,
    direction_name: str,
    log,
) -> None:
    _live_service()._track_local_sl_tp(pid, sl=sl_price, tp=tp_price)
    try:
        _live_service()._update_entry_protection_plan_status(
            int(pid),
            status="applied",
            attempted=True,
            applied_sl=sl_price,
            applied_tp=tp_price,
        )
    except Exception as _plan_update_err:
        logger.debug(
            "[live] entry protection applied update failed for pos {}: {}",
            pid,
            _plan_update_err,
        )
    _live_service()._pos_entry_scores[pid] = composite.score
    log(f"tick {tick}: v4 {direction_name} ORDER+AMEND OK "
        f"api_volume={actual_api_volume:.0f} pos={pid} score={composite.score:.4f}")


def _record_amended_open_execution_quality(
    *,
    bar: dict,
    current_price: float,
    fill_price: float,
    composite: Any,
    actual_api_volume: float,
    pid: int,
    submit_started_at: float | None = None,
    fill_received_at: float | None = None,
) -> None:
    try:
        submitted_at = float(submit_started_at or time.time())
        filled_at = float(fill_received_at or time.time())
        _live_service()._exec_quality.record(_ExecTrade(
            signal_time=submitted_at,
            submit_time=submitted_at,
            fill_time=filled_at,
            signal_price=current_price,
            fill_price=fill_price,
            symbol="XAUUSD+",
            direction=composite.direction,
            volume=actual_api_volume,
            order_id=pid,
        ))
    except Exception:
        pass


def _record_amended_open_attribution(
    *,
    attr_engine: Any,
    pid: int,
    current_price: float,
    actual_api_volume: float,
    composite: Any,
    tick: int,
    log,
) -> Any:
    from alpha.attribution_engine import TradeAttribution

    trade_attribution_payload = _live_service()._trade_attribution_payload_from_composite(
        position_id=int(pid),
        open_ts=time.time(),
        open_price=float(current_price),
        direction=int(composite.direction),
        actual_api_volume=float(actual_api_volume),
        composite=composite,
    )
    trade_attr = TradeAttribution.from_jsonable(trade_attribution_payload)
    attr_engine.record_open(pid, trade_attr)
    _live_service()._pos_open_prices[pid] = current_price
    _live_service()._pos_open_api_volume[pid] = float(actual_api_volume)
    log(f"tick {tick}: attribution recorded open pos={pid}")
    return trade_attr


def _log_amended_open_ledger(
    *,
    cfg: Any,
    bar: dict,
    tick: int,
    pid: int,
    actual_api_volume: float,
    requested_volume: float,
    base_requested_volume: float,
    fill_price: float,
    current_price: float,
    sl_price: float,
    tp_price: float,
    acct: dict,
    pos: list,
    composite: Any,
    gate_result: Any,
    risk_verdict: Any,
    event_sizing_context: dict[str, Any],
    sizing_trace: dict[str, Any],
    learning_context: dict[str, Any],
    parent_decision_id: str = "",
    execution_intent_id: str = "",
) -> str:
    if not _live_service()._LEDGER:
        return ""
    try:
        open_ledger_payloads = _tick_build_open_ledger_payloads(
            composite=composite,
            gate_result=gate_result,
            cfg=cfg,
            bar=bar,
            account=acct,
            positions_before=pos,
            session_pnl=live_state_get("session_pnl", 0),
            risk_state=_live_service()._risk_state_with_verdict(risk_verdict),
            risk_verdict=risk_verdict,
            pid=int(pid),
            requested_volume=float(requested_volume),
            base_requested_volume=float(base_requested_volume),
            actual_api_volume=float(actual_api_volume),
            current_price=float(current_price),
            fill_price=float(fill_price),
            sl_price=float(sl_price),
            tp_price=float(tp_price),
            tick=tick,
            event_sizing_context=event_sizing_context,
            sizing_trace=sizing_trace,
            learning_context=learning_context,
            decision_ts_fallback=time.time(),
            event_ts=time.time(),
        )
        decision_payload = open_ledger_payloads["decision"]
        decision_payload["action_json"] = {
            **dict(decision_payload.get("action_json") or {}),
            "parent_decision_id": str(parent_decision_id or ""),
            "execution_intent_id": str(execution_intent_id or ""),
            "event_stage": "protection_confirmed",
        }
        for payload_key in ("submitted_order", "filled_order"):
            order_payload = open_ledger_payloads[payload_key]
            order_payload["details"] = {
                **dict(order_payload.get("details") or {}),
                "parent_decision_id": str(parent_decision_id or ""),
                "execution_intent_id": str(execution_intent_id or ""),
            }
        open_ledger_payloads["position_event"]["details"] = {
            **dict(open_ledger_payloads["position_event"].get("details") or {}),
            "parent_decision_id": str(parent_decision_id or ""),
            "execution_intent_id": str(execution_intent_id or ""),
        }
        entry_decision_id = _live_service()._LEDGER.log_composite_decision(
            **open_ledger_payloads["decision"]
        )
        lineage_decision_id = str(parent_decision_id or entry_decision_id or "")
        for payload_key in ("submitted_order", "filled_order"):
            order_payload = open_ledger_payloads[payload_key]
            order_payload["details"] = {
                **dict(order_payload.get("details") or {}),
                "decision_id": lineage_decision_id,
                "child_decision_id": str(entry_decision_id or ""),
            }
        open_ledger_payloads["position_event"]["details"] = {
            **dict(open_ledger_payloads["position_event"].get("details") or {}),
            "decision_id": lineage_decision_id,
            "child_decision_id": str(entry_decision_id or ""),
        }
        _live_service()._pos_entry_decisions[int(pid)] = lineage_decision_id
        _live_service()._LEDGER.log_order_event(
            decision_id=lineage_decision_id,
            **open_ledger_payloads["submitted_order"],
        )
        _live_service()._LEDGER.log_order_event(
            decision_id=lineage_decision_id,
            **open_ledger_payloads["filled_order"],
        )
        _live_service()._LEDGER.log_position_event(
            decision_id=lineage_decision_id,
            **open_ledger_payloads["position_event"],
        )
        return lineage_decision_id
    except Exception as _ledger_err:
        logger.debug("[live] ledger open failed for pos {}: {}", pid, _ledger_err)
        return ""


def _upsert_amended_open_recovery(
    *,
    broker: str,
    tick: int,
    pid: int,
    actual_api_volume: float,
    requested_volume: float,
    fill_price: float,
    current_price: float,
    sl_price: float,
    tp_price: float,
    composite: Any,
    entry_decision_id: str,
    entry_protection_plan: dict[str, Any],
    trade_attr: Any,
    event_sizing_context: dict[str, Any],
    sizing_trace: dict[str, Any],
    learning_context: dict[str, Any],
    execution_intent_id: str = "",
) -> None:
    try:
        recovery_payloads = _lifecycle_build_filled_open_recovery_payloads(
            position_id=int(pid),
            broker=broker,
            strategy_name=_live_service()._current_loop_strategy_name(),
            direction=int(composite.direction),
            fill_price=float(fill_price),
            current_price=float(current_price),
            sl_price=float(sl_price),
            tp_price=float(tp_price),
            requested_volume=float(requested_volume),
            actual_api_volume=float(actual_api_volume),
            tick=tick,
            entry_decision_id=entry_decision_id or live_close_settlement.lookup_entry_decision_id(int(pid)),
            entry_protection_plan=_lifecycle_build_applied_entry_protection_plan_payload(
                plan=entry_protection_plan,
                updated_at=time.time(),
                applied_sl=sl_price,
                applied_tp=tp_price,
            ),
            trade_attribution_payload=trade_attr.to_jsonable(),
            learning_context={
                "event_sizing": event_sizing_context,
                "sizing_trace": sizing_trace,
                **learning_context,
            },
            context_integrity=_live_service()._RECOVERY_CONTEXT_FULL,
        )
        recovery_payloads["meta"] = {
            **dict(recovery_payloads.get("meta") or {}),
            "parent_decision_id": str(entry_decision_id or ""),
            "execution_intent_id": str(execution_intent_id or ""),
        }
        live_close_settlement.upsert_recovery_position_state(
            recovery_payloads["state_payload"],
            **recovery_payloads["state_kwargs"],
            meta=recovery_payloads["meta"],
        )
    except Exception as _recovery_open_err:
        logger.debug("[live] recovery open persist failed for pos {}: {}", pid, _recovery_open_err)


def _amended_open_success_processing_runtime() -> AmendedOpenSuccessRuntime:
    return AmendedOpenSuccessRuntime(
        mark_local_state=_mark_amended_open_success_local_state,
        record_execution_quality=_record_amended_open_execution_quality,
        record_attribution=_record_amended_open_attribution,
        build_learning_context=_live_service()._open_learning_context_payload,
        log_ledger=_log_amended_open_ledger,
        upsert_recovery=_upsert_amended_open_recovery,
    )


def record_amended_open_success_context_from_live(
    *,
    attr_engine: Any,
    bridge: Any,
    broker: str,
    cfg: Any,
    bar: dict,
    tick: int,
    pid: int,
    actual_api_volume: float,
    requested_volume: float,
    base_requested_volume: float,
    fill_price: float,
    current_price: float,
    sl_price: float,
    tp_price: float,
    sl_dist: float,
    tp_dist: float,
    acct: dict,
    pos: list,
    composite: Any,
    gate_result: Any,
    risk_verdict: Any,
    market_session: dict[str, Any],
    event_sizing_context: dict[str, Any],
    sizing_trace: dict[str, Any],
    entry_protection_plan: dict[str, Any],
    direction_name: str,
    log,
    submit_started_at: float | None = None,
    fill_received_at: float | None = None,
    parent_decision_id: str = "",
    execution_intent_id: str = "",
    position_supervisor_binding: dict[str, Any] | None = None,
) -> None:
    _runtime_record_amended_success(
        AmendedOpenSuccessRequest(
            attr_engine=attr_engine,
            bridge=bridge,
            broker=broker,
            cfg=cfg,
            bar=bar,
            tick=tick,
            pid=pid,
            actual_api_volume=actual_api_volume,
            requested_volume=requested_volume,
            base_requested_volume=base_requested_volume,
            fill_price=fill_price,
            current_price=current_price,
            sl_price=sl_price,
            tp_price=tp_price,
            sl_dist=sl_dist,
            tp_dist=tp_dist,
            acct=acct,
            pos=pos,
            composite=composite,
            gate_result=gate_result,
            risk_verdict=risk_verdict,
            market_session=market_session,
            event_sizing_context=event_sizing_context,
            sizing_trace=sizing_trace,
            entry_protection_plan=entry_protection_plan,
            direction_name=direction_name,
            log=log,
            submit_started_at=submit_started_at,
            fill_received_at=fill_received_at,
            parent_decision_id=parent_decision_id,
            execution_intent_id=execution_intent_id,
            position_supervisor_binding=position_supervisor_binding,
        ),
        runtime=_amended_open_success_processing_runtime(),
    )


def _amend_failure_processing_runtime() -> AmendFailureRuntime:
    ledger = _live_service()._LEDGER
    return AmendFailureRuntime(
        persist_fail_closed=_live_service()._persist_safety_fail_closed,
        record_aux_failure=live_close_settlement.record_risk_reduction_aux_failure,
        record_filled_context=lambda request: (
            _record_filled_position_open_context(**vars(request))
        ),
        update_plan_status=_live_service()._update_entry_protection_plan_status,
        ledger_available=bool(ledger),
        build_failed_payloads=_tick_build_amend_failed_ledger_payloads,
        get_risk_state=lambda: (
            live_state_get("risk", {}, clone=True) or {}
        ),
        log_composite_decision=(
            ledger.log_composite_decision if ledger else lambda **_kwargs: ""
        ),
        log_order_event=(
            ledger.log_order_event if ledger else lambda **_kwargs: None
        ),
        debug=logger.debug,
        now=time.time,
    )


def record_amend_failure_after_fill_from_live(
    *,
    attr_engine: Any,
    bridge: Any,
    broker: str,
    cfg: Any,
    bar: dict,
    tick: int,
    pid: int,
    actual_api_volume: float,
    requested_volume: float,
    base_requested_volume: float,
    fill_price: float,
    current_price: float,
    sl_price: float,
    tp_price: float,
    sl_dist: float,
    tp_dist: float,
    acct: dict,
    pos: list,
    composite: Any,
    gate_result: Any,
    risk_verdict: Any,
    market_session: dict[str, Any] | None,
    event_sizing_context: dict[str, Any],
    sizing_trace: dict[str, Any],
    status_error: str,
    ledger_action_reason: str,
    ledger_comment: str = "",
    ledger_error: str = "",
    ledger_debug_message: str = "[live] ledger amend failed event failed for pos %s: %s",
    failure_log: str = "",
    log=None,
    parent_decision_id: str = "",
    execution_intent_id: str = "",
    position_supervisor_binding: dict[str, Any] | None = None,
) -> None:
    _runtime_record_amend_failure(
        AmendFailureRequest(
            attr_engine=attr_engine,
            bridge=bridge,
            broker=broker,
            cfg=cfg,
            bar=bar,
            tick=tick,
            pid=pid,
            actual_api_volume=actual_api_volume,
            requested_volume=requested_volume,
            base_requested_volume=base_requested_volume,
            fill_price=fill_price,
            current_price=current_price,
            sl_price=sl_price,
            tp_price=tp_price,
            sl_dist=sl_dist,
            tp_dist=tp_dist,
            acct=acct,
            pos=pos,
            composite=composite,
            gate_result=gate_result,
            risk_verdict=risk_verdict,
            market_session=market_session,
            event_sizing_context=event_sizing_context,
            sizing_trace=sizing_trace,
            status_error=status_error,
            ledger_action_reason=ledger_action_reason,
            ledger_comment=ledger_comment,
            ledger_error=ledger_error,
            ledger_debug_message=ledger_debug_message,
            failure_log=failure_log,
            log=log,
            parent_decision_id=parent_decision_id,
            execution_intent_id=execution_intent_id,
            position_supervisor_binding=position_supervisor_binding,
        ),
        runtime=_amend_failure_processing_runtime(),
    )
