"""Recovered-close replay and broker-missing position retirement."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from backend.services.review_contract import trusted_broker_close_price

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

    payloads = runtime.build_payloads(
        position_id=position_id,
        position_state=position_state,
        real_pnl=real_pnl,
        strategy_name=strategy_name,
        now_ts=runtime.now(),
        context_integrity_default=runtime.partial_context,
    )
    total_pnl = float(payloads["total_pnl"])
    close_ts = float(payloads["close_ts"])
    close_price = trusted_broker_close_price(real_pnl)

    # Session risk is rebuilt from deals.  This projection is recovery/audit
    # state only and must commit before its original pre-fetch cursor is freed.
    runtime.mark_recovery_closed(
        position_id,
        close_reason="restart_replay",
        close_pnl=total_pnl,
        closed_at=close_ts,
        meta=payloads["recovery_meta"],
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
            runtime.ledger.log_position_event(**payloads["position_event"])
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
    runtime.mark_recovery_closed(
        pid,
        close_reason="broker_position_not_found",
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
        },
    )
    runtime.remove_live_position_state(pid)
    if log:
        log(f"broker missing position retired pos={pid}: {reason}")
    return True
