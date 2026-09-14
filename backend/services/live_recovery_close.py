"""Recovered-close replay and broker-missing position retirement."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from backend.services.review_contract import (
    broker_stop_hit_evidence,
    trusted_broker_close_price,
)


_SUPERVISOR_APPLIED_CLOSE_WINDOW_SECONDS = 600.0

_RESERVED_RECOVERY_REASONS = frozenset(
    {"broker_close", "restart_replay", "chain_broken", "broker_position_not_found"}
)


def _supervisor_applied_close_evidence(
    real_pnl: dict[str, Any] | None,
    position_state: dict[str, Any],
) -> dict[str, Any] | None:
    """Cite a complete supervisor-executed close chain, or return None.

    A durable supervisor record alone proves nothing (a protective order may
    have fired first, or the decision may never have executed).  The chain is
    complete only when three durable facts line up for this position: the
    supervisor applied a close, the broker fill landed after that decision
    and inside a short execution window, and the fill did not match the
    protective order (checked by the caller first).  Only then may the
    recovery path reuse the supervisor vocabulary instead of chain_broken.
    """
    state = position_state if isinstance(position_state, Mapping) else {}
    meta = state.get("recovery_meta")
    meta = meta if isinstance(meta, Mapping) else {}
    if str(meta.get("last_supervisor_applied_action") or "") != "close":
        return None
    if (
        str(
            meta.get("latest_supervisor_source")
            or meta.get("last_supervisor_applied_source")
            or ""
        )
        != "position_supervisor"
    ):
        return None
    reason = str(
        meta.get("last_supervisor_reason") or meta.get("pending_close_reason") or ""
    )
    if not reason or reason in _RESERVED_RECOVERY_REASONS:
        return None
    latest = meta.get("latest_supervisor")
    latest = latest if isinstance(latest, Mapping) else {}
    pnl = real_pnl if isinstance(real_pnl, Mapping) else {}
    try:
        decision_ts = float(latest.get("decision_ts") or 0.0)
        fill_ts = float(pnl.get("exec_timestamp") or 0.0)
        applied_ts = float(meta.get("last_supervisor_applied_ts") or 0.0)
    except (TypeError, ValueError):
        return None
    if decision_ts <= 0 or fill_ts <= 0 or applied_ts <= 0:
        return None
    if not (
        decision_ts
        <= fill_ts
        <= decision_ts + _SUPERVISOR_APPLIED_CLOSE_WINDOW_SECONDS
    ):
        return None
    return {
        "supervisor_applied_close": True,
        "reason": reason,
        "decision_ts": decision_ts,
        "fill_ts": fill_ts,
        "applied_ts": applied_ts,
        "window_seconds": _SUPERVISOR_APPLIED_CLOSE_WINDOW_SECONDS,
    }


def _resolve_replayed_close_reason(
    real_pnl: dict[str, Any] | None,
    position_state: dict[str, Any],
) -> tuple[str, str, dict[str, Any]]:
    """L0-0R attribution for recovery-replayed closes, with one honest upgrade.

    A fill matching the durable protective order is ``broker_close``.  A fill
    covered by a complete supervisor-executed chain (applied close + fill
    after the decision inside the execution window) reuses the supervisor
    reason.  Anything else stays ``chain_broken``: recovery never guesses
    from a bare supervisor record.
    """
    evidence = broker_stop_hit_evidence(
        real_pnl=real_pnl, position_state=position_state
    )
    if evidence.get("matched"):
        return "broker_close", "external_broker_close", evidence
    applied = _supervisor_applied_close_evidence(real_pnl, position_state)
    if applied is not None:
        return str(applied["reason"]), "supervisor_applied_close", applied
    return "chain_broken", "restart_replay", evidence


@dataclass(frozen=True)
class RecoveredCloseReplayRuntime:
    authoritative_close_pnl: Callable[[dict[str, Any] | None], bool]
    defer_close: Callable[..., Any]
    build_payloads: Callable[..., dict[str, Any]]
    mark_recovery_closed: Callable[..., None]
    release_close_latch: Callable[[int, dict[str, Any] | None], Any]
    get_risk_state: Callable[[], dict[str, Any]]
    now: Callable[[], float]
    partial_context: str
    ledger: Any = None
    trade_reviewer: Any = None
    experience_builder: Any = None
    policy_suggester: Any = None
    attr_engine: Any = None
    debug: Callable[..., Any] = lambda *_args, **_kwargs: None


@dataclass(frozen=True)
class MissingPositionRetirementRuntime:
    read_positions: Callable[[Any], list[Any]]
    normalize_position: Callable[[Any], dict[str, Any]]
    load_recovery_position: Callable[[int], dict[str, Any]]
    open_prices: Mapping[int, float]
    get_state_connection: Callable[[], Any]
    sync_close_deals_batch: Callable[..., dict[int, Any]]
    authoritative_close_pnl: Callable[[dict[str, Any] | None], bool]
    defer_close: Callable[..., Any]
    replay_close: Callable[..., bool]
    mark_recovery_closed: Callable[..., None]
    remove_live_position_state: Callable[[int], None]
    now: Callable[[], float]
    replay_lookback_seconds: int
    partial_context: str
    debug: Callable[..., Any] = lambda *_args, **_kwargs: None


def replay_recovered_close(
    *,
    broker: str,
    position_id: int,
    position_state: dict[str, Any],
    real_pnl: dict[str, Any] | None,
    strategy_name: str,
    runtime: RecoveredCloseReplayRuntime,
) -> bool:
    """Commit recovery projection before releasing its durable deal cursor."""

    if not runtime.authoritative_close_pnl(real_pnl):
        runtime.defer_close(
            int(position_id),
            broker=broker,
            tick=0,
            reason="restart_replay_close_deal_unavailable",
        )
        return False

    # Why did this position close?  Reconciliation only knows that it is gone.
    # L0-0R: a protective-fill match is broker_close; a complete
    # supervisor-executed chain reuses the supervisor reason; everything else
    # stays chain_broken.
    resolved_close_reason, resolved_close_reason_source, sl_hit_evidence = (
        _resolve_replayed_close_reason(real_pnl, position_state)
    )
    payloads = runtime.build_payloads(
        position_id=position_id,
        position_state=position_state,
        real_pnl=real_pnl,
        strategy_name=strategy_name,
        now_ts=runtime.now(),
        context_integrity_default=runtime.partial_context,
        sl_hit_evidence=sl_hit_evidence,
        resolved_close_reason=resolved_close_reason,
        close_reason_source=resolved_close_reason_source,
        recovery_observation_reason="position_missing_after_recovery_reconcile",
        attr_engine=runtime.attr_engine,
    )
    total_pnl = float(payloads["total_pnl"])
    close_ts = float(payloads["close_ts"])
    close_price = trusted_broker_close_price(real_pnl)

    # Session risk is rebuilt from deals.  This projection is recovery/audit
    # state only and must commit before its original pre-fetch cursor is freed.
    runtime.mark_recovery_closed(
        position_id,
        close_reason=resolved_close_reason,
        close_pnl=total_pnl,
        closed_at=close_ts,
        meta=payloads["recovery_meta"],
        attribution_integrity=str(
            (payloads.get("review") or {}).get("attribution_integrity") or ""
        ),
    )
    runtime.release_close_latch(int(position_id), real_pnl)

    exit_decision_id = ""
    if runtime.ledger and close_price is not None:
        try:
            decision = dict(payloads["decision"])
            exit_decision_id = runtime.ledger.log_decision(
                event_type=decision["event_type"],
                symbol=decision["symbol"],
                timeframe=decision["timeframe"],
                trade_id=decision["trade_id"],
                position_id=decision["position_id"],
                decision_ts=decision["decision_ts"],
                portfolio_state=decision["portfolio_state"],
                risk_state=runtime.get_risk_state(),
                action_score=decision["action_score"],
                action_reason=decision["action_reason"],
                action_json=decision["action_json"],
            )
            runtime.ledger.log_position_event(
                decision_id=exit_decision_id,
                **payloads["position_event"],
            )
        except Exception as exc:
            runtime.debug(
                "[live] replay close ledger failed for pos %s: %s",
                position_id,
                exc,
            )

    if (
        close_price is not None
        and
        runtime.trade_reviewer
        and runtime.experience_builder
        and runtime.policy_suggester
    ):
        try:
            review_payload = payloads["review"]
            review = runtime.trade_reviewer.review_closed_trade(
                position_id=review_payload["position_id"],
                pnl=review_payload["pnl"],
                close_price=review_payload["close_price"],
                close_ts=review_payload["close_ts"],
                contributions=review_payload["contributions"],
                exit_decision_id=exit_decision_id,
                real_pnl=review_payload["real_pnl"],
                close_reason=review_payload["close_reason"],
                close_reason_source=str(review_payload.get("close_reason_source") or ""),
                context_integrity=review_payload["context_integrity"],
                attribution_integrity=str(
                    review_payload.get("attribution_integrity")
                    or ("full" if review_payload["contributions"] else "missing")
                ),
            )
            if review.get("accepted", True):
                experience = runtime.experience_builder.build_from_review(review)
                runtime.policy_suggester.suggest_from_experience(experience)
        except Exception as exc:
            runtime.debug(
                "[live] replay close learning failed for pos %s: %s",
                position_id,
                exc,
            )
    return True


def retire_broker_missing_position(
    bridge: Any,
    position_id: int,
    *,
    broker: str,
    strategy_name: str,
    reason: str,
    runtime: MissingPositionRetirementRuntime,
    log: Callable[[str], Any] | None = None,
) -> bool:
    """Retire only after fresh absence and an authoritative complete close deal."""

    pid = int(position_id)
    try:
        live_positions = runtime.read_positions(bridge)
    except Exception as exc:
        runtime.debug(
            "[live] missing-position confirm failed for pos %s: %s",
            pid,
            exc,
        )
        return False
    live_ids = {
        int(item["position_id"])
        for item in (
            runtime.normalize_position(position) for position in live_positions
        )
        if int(item["position_id"]) > 0
    }
    if pid in live_ids:
        return False

    position_state = dict(runtime.load_recovery_position(pid) or {})
    if not position_state:
        position_state = {
            "position_id": pid,
            "broker": broker,
            "symbol": "XAUUSD+",
            "open_price": float(runtime.open_prices.get(pid, 0.0) or 0.0),
            "close_pnl": 0.0,
            "context_integrity": runtime.partial_context,
        }

    real_pnl = None
    try:
        conn = runtime.get_state_connection()
        try:
            last_seen_at = float(
                position_state.get("last_seen_at") or runtime.now()
            )
            real_pnl = runtime.sync_close_deals_batch(
                bridge,
                conn,
                {pid},
                from_ts=int(
                    max(0.0, last_seen_at - runtime.replay_lookback_seconds)
                ),
                max_rows=200,
                min_exec_timestamp_by_position={
                    pid: max(
                        0.0,
                        float(position_state.get("last_seen_at") or 0.0) - 5.0,
                    )
                },
                required_closed_volume_delta_by_position={
                    pid: float(position_state.get("volume") or 0.0)
                },
                baseline_close_cursor_by_position={
                    pid: {
                        "baseline_cursor_available": True,
                        "baseline_deal_ids": [],
                        "baseline_closed_volume": 0.0,
                    }
                },
            ).get(pid)
        finally:
            conn.close()
    except Exception as exc:
        runtime.debug(
            "[live] missing-position deal sync failed for pos %s: %s",
            pid,
            exc,
        )

    if not runtime.authoritative_close_pnl(real_pnl):
        runtime.defer_close(
            pid,
            broker=broker,
            tick=0,
            reason="broker_position_missing_close_deal_unavailable",
        )
        if log:
            log(
                "broker missing position pending authoritative close deal "
                f"pos={pid}: {reason}"
            )
        return False

    if not runtime.replay_close(
        broker=broker,
        position_id=pid,
        position_state=position_state,
        real_pnl=real_pnl,
        strategy_name=strategy_name,
    ):
        return False

    now = runtime.now()
    trade_close_reason, trade_close_reason_source, _sl_hit_evidence = (
        _resolve_replayed_close_reason(real_pnl, position_state)
    )
    runtime.mark_recovery_closed(
        pid,
        close_reason=trade_close_reason,
        close_pnl=float(
            (real_pnl or {}).get(
                "net",
                position_state.get("close_pnl", 0.0),
            )
            or 0.0
        ),
        closed_at=float(
            (real_pnl or {}).get("exec_timestamp", now) or now
        ),
        meta={
            "broker_position_not_found": True,
            "failure_reason": reason,
            "retired_at": now,
            "recovery_observation_reason": "broker_position_not_found",
            "trade_close_reason": trade_close_reason,
            "trade_close_reason_source": trade_close_reason_source,
        },
    )
    runtime.remove_live_position_state(pid)
    if log:
        log(f"broker missing position retired pos={pid}: {reason}")
    return True
