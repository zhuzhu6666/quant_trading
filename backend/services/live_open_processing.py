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
            trade_attribution_payload=trade_attribution_payload,
            learning_context=recovery_learning_context,
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


@dataclass(frozen=True)
class AmendedOpenSuccessRuntime:
    mark_local_state: Callable[..., Any]
    record_execution_quality: Callable[..., Any]
    record_attribution: Callable[..., Any]
    build_learning_context: Callable[..., dict[str, Any]]
    log_ledger: Callable[..., str]
    upsert_recovery: Callable[..., Any]
    write_decision_log: Callable[..., Any]


def record_amended_open_success_context(
    request: AmendedOpenSuccessRequest,
    *,
    runtime: AmendedOpenSuccessRuntime,
) -> None:
    """Publish confirmed protection context in the legacy-compatible order."""

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
            entry_protection_plan=request.entry_protection_plan,
            trade_attr=trade_attribution,
            event_sizing_context=request.event_sizing_context,
            sizing_trace=request.sizing_trace,
            learning_context=learning_context,
        )
        runtime.write_decision_log(
            bar=request.bar,
            composite=request.composite,
            pid=request.pid,
            actual_api_volume=request.actual_api_volume,
            requested_volume=request.requested_volume,
            base_requested_volume=request.base_requested_volume,
            event_sizing_context=request.event_sizing_context,
            sizing_trace=request.sizing_trace,
            current_price=request.current_price,
            sl_price=request.sl_price,
            tp_price=request.tp_price,
            tick=request.tick,
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
        decision_id = runtime.log_composite_decision(
            **payloads["decision"]
        )
        runtime.log_order_event(
            decision_id=decision_id,
            **payloads["order_event"],
        )
    except Exception as exc:
        runtime.debug(
            request.ledger_debug_message,
            request.pid,
            exc,
        )
