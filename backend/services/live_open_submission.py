"""Linearized live-open broker submission and post-fill fail-closed handling."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class OpenSubmissionRuntime:
    probe_final_admission: Callable[..., dict[str, Any]]
    admission_lock: Any
    open_trade_draining: Callable[[Any], bool]
    persist_safety_fail_closed: Callable[..., Any]
    submit_order: Callable[..., Any]
    handle_order_success: Callable[..., Any]
    record_order_failure: Callable[..., Any]
    reconcile_positions: Callable[[Any], Any]
    publish_positions: Callable[..., Any]
    append_safety_outbox: Callable[..., Any]
    finalize_nursery_reservation: Callable[[str, bool], Any]
    now: Callable[[], float]
    prepare_open_intent: Callable[..., str]


def _submit_order_with_lineage(
    submit_order: Callable[..., Any],
    bridge: Any,
    composite: Any,
    volume: float,
    *,
    decision_id: str,
    trade_id: str,
    risk_verdict: Any,
) -> Any:
    """Submit through the context-aware canonical broker callback."""
    return submit_order(
        bridge,
        composite,
        volume,
        decision_id=decision_id,
        trade_id=trade_id,
        risk_verdict=risk_verdict,
    )


def submit_open_trade_candidate(
    *,
    bridge: Any,
    attr_engine: Any,
    broker: str,
    cfg: Any,
    bar: dict[str, Any],
    tick: int,
    account: dict[str, Any],
    positions: list[Any],
    composite: Any,
    gate_result: Any,
    candidate: Any,
    current_price: float,
    log: Callable[[str], Any],
    runtime: OpenSubmissionRuntime,
    signal_decision_id: str = "",
    stop_requested: Any = None,
) -> bool:
    """Submit once under admission ownership, then finish post-fill linearly."""

    reservation_id = str(
        getattr(candidate, "nursery_reservation_id", "") or ""
    )

    def finalize_nursery(consumed: bool) -> None:
        if reservation_id:
            runtime.finalize_nursery_reservation(reservation_id, consumed)

    # PostgreSQL/session/spot probes must never hold broker admission ownership.
    final_admission = runtime.probe_final_admission(
        bridge=bridge,
        candidate=candidate,
    )
    with runtime.admission_lock:
        if runtime.open_trade_draining(stop_requested):
            log(
                f"tick {tick}: v4 open SKIP "
                "(loop_draining stage=broker_submit)"
            )
            finalize_nursery(False)
            return False
        if not bool(final_admission.get("ok")):
            blockers = tuple(final_admission.get("blockers") or ())
            failure_error = str(
                (final_admission.get("postgres") or {}).get("error")
                or (final_admission.get("spot_quote") or {}).get("error")
                or ""
            )
            runtime.persist_safety_fail_closed(
                blockers=blockers,
                source="final_open_admission",
                error=failure_error,
            )
            log(
                f"tick {tick}: v4 open SKIP (final_open_admission "
                f"blockers={','.join(str(item) for item in blockers)})"
            )
            finalize_nursery(False)
            return False
        intent_prepare_attempted = True
        decision_id = ""
        try:
            decision_id = str(
                runtime.prepare_open_intent(
                    bridge=bridge,
                    broker=broker,
                    cfg=cfg,
                    bar=bar,
                    tick=tick,
                    account=account,
                    positions=positions,
                    composite=composite,
                    gate_result=gate_result,
                    candidate=candidate,
                    current_price=current_price,
                    signal_decision_id=signal_decision_id,
                )
                or ""
            )
            if not decision_id:
                raise RuntimeError("open_intent_not_persisted")
            try:
                candidate.open_decision_id = decision_id
            except Exception:
                pass
            submit_started_at = runtime.now()
            result = _submit_order_with_lineage(
                runtime.submit_order,
                bridge,
                composite,
                float(candidate.volume),
                decision_id=decision_id,
                trade_id="",
                risk_verdict=getattr(candidate, "risk_verdict", None),
            )
            fill_received_at = runtime.now()
        except Exception as exc:
            if intent_prepare_attempted and not decision_id:
                runtime.persist_safety_fail_closed(
                    blockers=("open_intent_persist_failed",),
                    source="open_intent",
                    error=f"{type(exc).__name__}:{exc}",
                )
                finalize_nursery(False)
                log(f"tick {tick}: v4 open SKIP (open_intent_persist_failed: {exc})")
                return True
            log(
                f"tick {tick}: v4 {candidate.direction_name} "
                f"order exception: {exc}"
            )
            finalize_nursery(False)
            return True

    # Once admitted, draining cannot interrupt fill resolution, protection,
    # recovery, or audit. Process shutdown must join this owner.
    broker_open_succeeded = bool(
        result is not None and getattr(result, "success", False)
    )
    try:
        if broker_open_succeeded:
            finalize_nursery(True)
            runtime.handle_order_success(
                result=result,
                bridge=bridge,
                attr_engine=attr_engine,
                broker=broker,
                cfg=cfg,
                bar=bar,
                tick=tick,
                account=account,
                positions=positions,
                composite=composite,
                gate_result=gate_result,
                candidate=candidate,
                current_price=current_price,
                log=log,
                submit_started_at=submit_started_at,
                fill_received_at=fill_received_at,
                decision_id=decision_id,
            )
        elif result is not None:
            finalize_nursery(False)
            runtime.record_order_failure(
                result=result,
                cfg=cfg,
                bar=bar,
                account=account,
                positions=positions,
                composite=composite,
                gate_result=gate_result,
                candidate=candidate,
                current_price=current_price,
                tick=tick,
                log=log,
                decision_id=decision_id,
            )
        else:
            finalize_nursery(False)
            log(
                f"tick {tick}: v4 {candidate.direction_name} "
                "order returned no result"
            )
    except Exception as exc:
        if not broker_open_succeeded:
            finalize_nursery(False)
            log(
                f"tick {tick}: v4 {candidate.direction_name} "
                f"order exception: {exc}"
            )
            return True

        # Broker risk already exists.  Latch first, then refresh broker truth.
        failure_error = f"{type(exc).__name__}:{exc}"
        runtime.persist_safety_fail_closed(
            blockers=("confirmed_open_post_fill_processing_failed",),
            source="entry_protection_initialization",
            error=failure_error,
        )
        try:
            reconcile = runtime.reconcile_positions(bridge)
        except Exception as reconcile_exc:
            reconcile = {
                "success": False,
                "reconcile_id": "",
                "error_code": (
                    "post_fill_position_reconcile_exception:"
                    f"{type(reconcile_exc).__name__}:{reconcile_exc}"
                ),
            }
        reconcile_success = bool(
            reconcile.get("success")
            if hasattr(reconcile, "get")
            else getattr(reconcile, "success", False)
        )
        reconcile_id = str(
            (
                reconcile.get("reconcile_id")
                if hasattr(reconcile, "get")
                else getattr(reconcile, "reconcile_id", "")
            )
            or ""
        )
        if reconcile_success:
            runtime.publish_positions(reconcile, broker=broker)
        try:
            runtime.append_safety_outbox(
                event_type="confirmed_open_post_fill_processing_failed",
                payload={
                    "broker": str(broker or ""),
                    "tick": int(tick),
                    "position_id": int(
                        getattr(result, "position_id", 0) or 0
                    ),
                    "intent_id": str(
                        getattr(result, "intent_id", "") or ""
                    ),
                    "reconcile_id": reconcile_id,
                    "reconcile_success": reconcile_success,
                },
                error=failure_error,
            )
        except Exception:
            pass
        log(
            f"tick {tick}: v4 {candidate.direction_name} confirmed open "
            f"post-fill processing failed closed: {exc}"
        )
    return True


def finalize_nursery_reservation(
    reservation_id: str,
    consumed: bool,
    *,
    warning: Callable[..., Any],
) -> None:
    """Finalize the optional exploration budget without changing order truth."""

    try:
        from backend.services.nursery_exploration_budget import (
            NurseryExplorationBudgetService,
        )

        NurseryExplorationBudgetService().finalize(
            reservation_id,
            consumed=consumed,
        )
    except Exception as exc:
        warning(
            "[live] nursery exploration reservation finalize failed: %s",
            exc,
        )
