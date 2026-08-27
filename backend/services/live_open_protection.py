"""Broker-confirmed open protection attachment state machine."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class OpenProtectionRequest:
    bridge: Any
    attr_engine: Any
    broker: str
    cfg: Any
    bar: dict[str, Any]
    tick: int
    position_id: int
    actual_api_volume: float
    requested_volume: float
    base_requested_volume: float
    fill_price: float
    current_price: float
    sl_price: float
    tp_price: float
    sl_dist: float
    tp_dist: float
    account: dict[str, Any]
    positions: list[Any]
    composite: Any
    gate_result: Any
    candidate: Any
    entry_protection_plan: dict[str, Any]
    log: Callable[[str], Any]
    submit_started_at: float | None = None
    fill_received_at: float | None = None


@dataclass(frozen=True)
class OpenProtectionRuntime:
    amend_position: Callable[..., Any]
    reconcile_positions: Callable[[Any], Any]
    verify_projection: Callable[..., dict[str, Any]]
    publish_projection: Callable[..., Any]
    release_pending_latch: Callable[..., Any]
    record_success: Callable[..., Any]
    record_failure: Callable[..., Any]
    record_aux_failure: Callable[..., Any]


def _failure_context(
    request: OpenProtectionRequest,
    *,
    market_session: dict[str, Any] | None,
) -> dict[str, Any]:
    candidate = request.candidate
    return {
        "attr_engine": request.attr_engine,
        "bridge": request.bridge,
        "broker": request.broker,
        "cfg": request.cfg,
        "bar": request.bar,
        "tick": request.tick,
        "pid": request.position_id,
        "actual_api_volume": request.actual_api_volume,
        "requested_volume": request.requested_volume,
        "base_requested_volume": request.base_requested_volume,
        "fill_price": request.fill_price,
        "current_price": request.current_price,
        "sl_price": request.sl_price,
        "tp_price": request.tp_price,
        "acct": request.account,
        "pos": request.positions,
        "composite": request.composite,
        "gate_result": request.gate_result,
        "risk_verdict": candidate.risk_verdict,
        "market_session": market_session,
        "event_sizing_context": candidate.event_sizing_context,
        "sizing_trace": candidate.sizing_trace,
        "sl_dist": request.sl_dist,
        "tp_dist": request.tp_dist,
        "log": request.log,
        "parent_decision_id": str(
            getattr(candidate, "open_decision_id", "") or ""
        ),
        "execution_intent_id": str(
            getattr(candidate, "execution_intent_id", "") or ""
        ),
        "position_supervisor_binding": dict(
            getattr(candidate, "position_supervisor_binding", {}) or {}
        ),
    }


def _success_context(request: OpenProtectionRequest) -> dict[str, Any]:
    candidate = request.candidate
    context = _failure_context(
        request,
        market_session=candidate.market_session,
    )
    context.update(
        {
            "entry_protection_plan": request.entry_protection_plan,
            "direction_name": candidate.direction_name,
            "submit_started_at": request.submit_started_at,
            "fill_received_at": request.fill_received_at,
        }
    )
    return context


def attach_open_trade_protection(
    request: OpenProtectionRequest,
    *,
    runtime: OpenProtectionRuntime,
) -> None:
    """Amend SL/TP and release new risk only after fresh broker proof."""

    candidate = request.candidate
    try:
        amend_result = runtime.amend_position(
            bridge=request.bridge,
            position_id=request.position_id,
            sl=request.sl_price,
            tp=request.tp_price,
        )
        if getattr(amend_result, "success", False):
            projection = runtime.reconcile_positions(request.bridge)
            precision = int(
                (
                    getattr(request.bridge, "_symbol_meta", None)
                    or {}
                ).get("digits", 2)
                or 2
            )
            verification = runtime.verify_projection(
                projection,
                position_id=request.position_id,
                expected_stop_loss=request.sl_price,
                expected_take_profit=request.tp_price,
                precision=precision,
            )
            if bool(verification.get("ok")):
                runtime.publish_projection(
                    projection,
                    broker=request.broker,
                )
                runtime.release_pending_latch(
                    request.position_id,
                    reconcile=projection,
                    expected_stop_loss=request.sl_price,
                    expected_take_profit=request.tp_price,
                )
                runtime.record_success(**_success_context(request))
                return

            projection_reason = str(
                verification.get("reason")
                or "position_reconcile_failed"
            )
            failure_reason = (
                "entry_protection_projection_unverified:"
                f"{projection_reason}"
            )
            runtime.record_aux_failure(
                "entry_protection_projection_unverified",
                position_id=int(request.position_id),
                action="amend_position_sltp",
                error=failure_reason,
                payload={"verification": verification},
            )
            runtime.record_failure(
                **_failure_context(
                    request,
                    market_session=candidate.market_session,
                ),
                status_error=failure_reason,
                ledger_action_reason=failure_reason,
                ledger_comment=str(
                    getattr(amend_result, "comment", "") or ""
                ),
                failure_log=(
                    f"tick {request.tick}: v4 "
                    f"{candidate.direction_name} AMEND UNVERIFIED "
                    f"pos={request.position_id}: {failure_reason}"
                ),
            )
            return

        failure_reason = str(
            getattr(amend_result, "comment", "")
            or getattr(amend_result, "error", "")
            or "amend_failed"
        )
        runtime.record_failure(
            **_failure_context(
                request,
                market_session=candidate.market_session,
            ),
            status_error=failure_reason,
            ledger_action_reason=str(
                getattr(amend_result, "comment", "amend_failed")
                or "amend_failed"
            ),
            ledger_comment=str(
                getattr(amend_result, "comment", "") or ""
            ),
            failure_log=(
                f"tick {request.tick}: v4 "
                f"{candidate.direction_name} AMEND FAILED "
                f"pos={request.position_id}: {failure_reason}"
            ),
        )
    except Exception as exc:
        runtime.record_failure(
            **_failure_context(request, market_session=None),
            status_error=(
                f"amend_exception:{type(exc).__name__}:"
                f"{str(exc)[:220]}"
            ),
            ledger_action_reason=f"amend_exception:{type(exc).__name__}",
            ledger_error=str(exc)[:300],
            ledger_debug_message=(
                "[live] ledger amend exception event failed for pos %s: %s"
            ),
            failure_log=(
                f"tick {request.tick}: v4 "
                f"{candidate.direction_name} amend exception: {exc}"
            ),
        )
