"""Post-close attribution, recovery projection, and session-risk rebuild cycle."""
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ClosedPositionCycleRuntime:
    authoritative_close_pnl: Callable[[dict[str, Any] | None], bool]
    defer_close: Callable[..., Any]
    update_live_state: Callable[..., None]
    collect_attribution: Callable[..., dict[str, Any]]
    write_close_decision_log: Callable[..., Any]
    lookup_context_integrity: Callable[[int, str], str]
    log_closed_position_ledger: Callable[..., tuple[str, str]]
    run_closed_position_learning: Callable[..., Any]
    cleanup_closed_position: Callable[..., Any]
    record_aux_failure: Callable[..., Any]
    mark_recovery_closed: Callable[..., None]
    reconcile_account: Callable[[Any], Any]
    reconcile_value: Callable[[Any, str, Any], Any]
    restore_session_state: Callable[..., bool]
    release_close_latch: Callable[[int, dict[str, Any]], Any]
    trade_date: Callable[[], str]
    now: Callable[[], float]
    full_context: str


def handle_closed_positions_after_tick(
    *,
    closed_pids: set[int],
    real_pnls: dict[int, dict[str, Any]],
    attr_engine: Any,
    current_price: float,
    bar: dict[str, Any],
    cfg: Any,
    account: dict[str, Any],
    broker: str,
    tick: int,
    log: Callable[[str], Any],
    runtime: ClosedPositionCycleRuntime,
    broker_open_position_ids: set[int] | None = None,
    bridge: Any | None = None,
    close_deal_cursors: dict[int, dict[str, Any]] | None = None,
) -> None:
    """Fail closed until every confirmed close is projected into session risk."""

    confirmed_close_ids: set[int] = set()
    recovery_projected_ids: set[int] = set()
    for position_id in closed_pids:
        pid = int(position_id)
        real_pnl = real_pnls.get(pid)
        try:
            if not runtime.authoritative_close_pnl(real_pnl):
                cursor = dict((close_deal_cursors or {}).get(pid) or {})
                runtime.defer_close(
                    pid,
                    broker=broker,
                    tick=tick,
                    recovery_evidence=(
                        {"pending_kind": "final_close", **cursor}
                        if cursor
                        else None
                    ),
                )
                log(
                    f"tick {tick}: close pos={pid} deferred until authoritative "
                    "cTrader close deal is available"
                )
                continue

            confirmed_close_ids.add(pid)
            # The old risk projection excludes newly realized broker facts.
            # Block admission before all auxiliary attribution/audit work.
            runtime.update_live_state(
                session_state_status="unavailable",
                session_state_source="post_close_projection_pending",
                session_risk_blockers=[
                    f"post_close_projection_pending:{item}"
                    for item in sorted(confirmed_close_ids)
                ],
                session_observed_at=0.0,
                accepting_new_risk=False,
            )
            close_payload = runtime.collect_attribution(
                cpid=pid,
                real_pnl=real_pnl,
                attr_engine=attr_engine,
                current_price=current_price,
                tick=tick,
                log=log,
            )
            total_pnl = float(close_payload["total_pnl"])
            close_ts = float(close_payload["close_ts"])
            close_reason = str(close_payload["close_reason"])
            close_source = close_payload["close_source"]
            close_verdict = close_payload["close_verdict"]
            attribution_integrity = str(
                close_payload["attribution_integrity"]
            )
            factor_contributions = close_payload["factor_contributions"]
            runtime.write_close_decision_log(
                cpid=pid,
                bar=bar,
                total_pnl=total_pnl,
                current_price=current_price,
                tick=tick,
            )
            context_integrity = runtime.lookup_context_integrity(
                pid,
                runtime.full_context,
            )
            exit_decision_id, context_integrity = (
                runtime.log_closed_position_ledger(
                    cpid=pid,
                    broker=broker,
                    close_ts=close_ts,
                    current_price=current_price,
                    real_pnl=real_pnl,
                    close_reason=close_reason,
                    context_integrity=context_integrity,
                    cfg=cfg,
                    bar=bar,
                    acct=account,
                    total_pnl=total_pnl,
                    tick=tick,
                    close_source=close_source,
                    attribution_integrity=attribution_integrity,
                    close_verdict=close_verdict,
                    factor_contributions=factor_contributions,
                )
            )
            runtime.run_closed_position_learning(
                cpid=pid,
                total_pnl=total_pnl,
                current_price=current_price,
                close_ts=close_ts,
                factor_contributions=factor_contributions,
                exit_decision_id=exit_decision_id,
                real_pnl=real_pnl,
                close_reason=close_reason,
                context_integrity=context_integrity,
                attribution_integrity=attribution_integrity,
                close_source=close_source,
            )
            projection_ready = runtime.cleanup_closed_position(
                cpid=pid,
                close_reason=close_reason,
                total_pnl=total_pnl,
                close_ts=close_ts,
                real_pnl=real_pnl,
                factor_contributions=factor_contributions,
            )
            if projection_ready is not False:
                recovery_projected_ids.add(pid)
        except Exception as exc:
            log(f"tick {tick}: attribution close pos={pid} error: {exc}")
            if pid not in confirmed_close_ids:
                continue
            runtime.record_aux_failure(
                "post_close_auxiliary_processing_failed",
                position_id=pid,
                action="close_position",
                error=exc,
            )
            try:
                runtime.mark_recovery_closed(
                    pid,
                    close_reason="broker_close_auxiliary_deferred",
                    close_pnl=float((real_pnl or {}).get("net") or 0.0),
                    closed_at=float(
                        (real_pnl or {}).get("exec_timestamp")
                        or runtime.now()
                    ),
                    meta={
                        "real_pnl": real_pnl or {},
                        "auxiliary_processing_error": (
                            f"{type(exc).__name__}:{exc}"
                        ),
                    },
                )
                recovery_projected_ids.add(pid)
            except Exception as recovery_exc:
                runtime.record_aux_failure(
                    "post_close_recovery_projection_failed",
                    position_id=pid,
                    action="close_position",
                    error=recovery_exc,
                )

    if not confirmed_close_ids:
        return

    account_reconcile = (
        runtime.reconcile_account(bridge)
        if broker_open_position_ids is not None
        else None
    )
    account_value = (
        runtime.reconcile_value(account_reconcile, "account", None)
        if account_reconcile is not None
        else None
    )
    account_ready = account_value is not None
    if account_ready:
        account_payload = (
            asdict(account_value)
            if is_dataclass(account_value)
            else dict(account_value)
        )
        account_payload.update({"ok": True, "broker": broker})
        runtime.update_live_state(
            account=account_payload,
            account_reconciled=copy.deepcopy(account_payload),
            account_updated_at=float(
                runtime.reconcile_value(
                    account_reconcile,
                    "observed_at",
                    0.0,
                )
                or 0.0
            ),
            account_reconcile_id=str(
                runtime.reconcile_value(
                    account_reconcile,
                    "reconcile_id",
                    "",
                )
                or ""
            ),
            account_reconcile_failed_at=None,
            account_reconcile_error=None,
        )
    else:
        runtime.update_live_state(
            account_reconcile_failed_at=runtime.now(),
            account_reconcile_error="post_close_account_reconcile_failed",
        )

    restored = bool(
        account_ready
        and broker_open_position_ids is not None
        and runtime.restore_session_state(
            runtime.trade_date(),
            broker_open_position_ids={
                int(position_id)
                for position_id in broker_open_position_ids
                if int(position_id or 0) > 0
            },
            confirmed_closed_position_ids=set(confirmed_close_ids),
        )
    )
    pending_projection_ids = set(confirmed_close_ids)
    if restored:
        for position_id in sorted(
            confirmed_close_ids & recovery_projected_ids
        ):
            runtime.release_close_latch(
                position_id,
                real_pnls[position_id],
            )
        pending_projection_ids -= recovery_projected_ids

    if not pending_projection_ids:
        return

    for position_id in sorted(pending_projection_ids):
        cursor = dict((close_deal_cursors or {}).get(position_id) or {})
        runtime.defer_close(
            position_id,
            broker=broker,
            tick=tick,
            reason="post_close_session_projection_unavailable",
            recovery_evidence={
                "pending_kind": "final_close",
                **cursor,
                "confirmed_deal_ids": list(
                    real_pnls[position_id].get("deal_ids") or []
                ),
            },
        )
    runtime.update_live_state(
        session_state_status="unavailable",
        session_state_source="post_close_projection_unavailable",
        session_risk_blockers=[
            f"post_close_projection_pending:{position_id}"
            for position_id in sorted(pending_projection_ids)
        ],
        session_observed_at=0.0,
        accepting_new_risk=False,
    )
    runtime.record_aux_failure(
        "post_close_session_projection_unavailable",
        action="close_position",
        error="authoritative_session_restore_unavailable",
        payload={
            "position_ids": sorted(pending_projection_ids),
            "session_projection_restored": restored,
        },
    )
