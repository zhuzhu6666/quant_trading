"""Closed-position attribution, audit, learning, and local cleanup services."""
from __future__ import annotations

from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
import time
from typing import Any

from backend.services.review_contract import trusted_broker_close_price


@dataclass(frozen=True)
class ClosedPositionProcessingRuntime:
    consume_close_reason: Callable[[int, str], str]
    consume_close_verdict: Callable[[int, str], dict[str, Any]]
    classify_close_source: Callable[..., Any]
    select_close_total_pnl: Callable[..., float]
    open_api_volumes: MutableMapping[int, float]
    decision_log: Any
    decision_log_run_id: str
    safe_decision_log: Callable[..., Any]
    build_close_decision_audit_meta: Callable[..., dict[str, Any]]
    json_dumps: Callable[..., str]
    ledger: Any
    ensure_open_ledger: Callable[..., str]
    lookup_context_integrity: Callable[[int, str], str]
    build_close_ledger_payloads: Callable[..., dict[str, Any]]
    get_session_pnl: Callable[[], float]
    risk_state_with_verdict: Callable[[dict[str, Any]], dict[str, Any]]
    trade_reviewer: Any
    experience_builder: Any
    policy_suggester: Any
    build_trade_review_payload: Callable[..., dict[str, Any]]
    mark_recovery_closed: Callable[..., None]
    trailing_state: MutableMapping[int, Any]
    entry_scores: MutableMapping[int, Any]
    entry_decisions: MutableMapping[int, Any]
    pending_open_attach_until: MutableMapping[int, Any]
    now: Callable[[], float]
    debug: Callable[..., Any]
    info: Callable[..., Any]
    exception: Callable[..., Any]


def collect_closed_position_attribution(
    *,
    position_id: int,
    real_pnl: dict[str, Any] | None,
    attr_engine: Any,
    tick: int,
    log: Callable[[str], Any],
    runtime: ClosedPositionProcessingRuntime,
) -> dict[str, Any]:
    pid = int(position_id)
    close_reason = runtime.consume_close_reason(pid, "broker_close")
    close_verdict = runtime.consume_close_verdict(pid, close_reason)
    close_ts = float((real_pnl or {}).get("exec_timestamp") or runtime.now())
    attribution_integrity = (
        attr_engine.open_integrity(pid)
        if attr_engine is not None and hasattr(attr_engine, "open_integrity")
        else "missing"
    )
    close_price = trusted_broker_close_price(real_pnl)
    if close_price is None:
        if attr_engine is not None and hasattr(attr_engine, "discard_open"):
            attr_engine.discard_open(pid)
        contributions = {}
        attribution_integrity = "missing"
    else:
        contributions = attr_engine.record_close(
            pid,
            close_price=close_price,
            close_ts=close_ts,
            real_pnl=real_pnl,
        )
    if not contributions:
        attribution_integrity = "missing"
    close_source = runtime.classify_close_source(
        pid,
        close_reason,
        close_ts,
    )
    total_pnl = runtime.select_close_total_pnl(
        real_pnl=real_pnl,
        factor_contributions=contributions,
        fallback_pnl=0.0,
    )
    log(
        f"tick {tick}: attribution close pos={pid} "
        f"pnl={total_pnl:.2f} factors={len(contributions)}"
    )
    runtime.open_api_volumes.pop(pid, None)
    return {
        "close_reason": close_reason,
        "close_verdict": close_verdict,
        "close_ts": close_ts,
        "attribution_integrity": attribution_integrity,
        "factor_contributions": contributions,
        "close_source": close_source,
        "close_price": close_price,
        "total_pnl": total_pnl,
    }


def write_close_decision_log(
    *,
    position_id: int,
    bar: dict[str, Any],
    total_pnl: float,
    current_price: float,
    tick: int,
    runtime: ClosedPositionProcessingRuntime,
) -> None:
    if not runtime.decision_log:
        return
    bar_ts = bar.get("time", 0)
    bar_date = time.strftime("%Y-%m-%d", time.gmtime(bar_ts)) if bar_ts else ""
    runtime.safe_decision_log(
        runtime.decision_log,
        run_id=runtime.decision_log_run_id,
        ts=bar_ts or runtime.now(),
        bar_date=bar_date,
        decision_type="close",
        strategy="factor_v4",
        direction=0,
        confidence=round(total_pnl, 2),
        decision="closed",
        meta=runtime.json_dumps(
            runtime.build_close_decision_audit_meta(
                position_id=int(position_id),
                total_pnl=float(total_pnl),
                current_price=float(current_price),
                tick=tick,
            ),
            ensure_ascii=False,
        ),
    )


def log_closed_position_ledger(
    *,
    position_id: int,
    broker: str,
    close_ts: float,
    current_price: float,
    real_pnl: dict[str, Any] | None,
    close_reason: str,
    context_integrity: str,
    cfg: Any,
    bar: dict[str, Any],
    account: dict[str, Any],
    total_pnl: float,
    tick: int,
    close_source: dict[str, Any] | str | None,
    attribution_integrity: str,
    close_verdict: dict[str, Any],
    factor_contributions: dict[str, Any],
    runtime: ClosedPositionProcessingRuntime,
) -> tuple[str, str]:
    if not runtime.ledger:
        return "", context_integrity
    pid = int(position_id)
    try:
        repaired_entry_decision_id = runtime.ensure_open_ledger(
            pid,
            broker=broker,
            close_ts=close_ts,
            close_price=float(current_price),
            real_pnl=real_pnl,
            close_reason=close_reason,
        )
        if repaired_entry_decision_id:
            context_integrity = runtime.lookup_context_integrity(
                pid,
                context_integrity,
            )
        payloads = runtime.build_close_ledger_payloads(
            position_id=pid,
            timeframe=str(getattr(cfg, "timeframe", "") or ""),
            decision_ts=bar.get("time", close_ts),
            close_ts=close_ts,
            account=account,
            session_pnl=runtime.get_session_pnl(),
            risk_state=runtime.risk_state_with_verdict(close_verdict),
            total_pnl=float(total_pnl),
            current_price=float(current_price),
            tick=tick,
            close_reason=close_reason,
            close_source=close_source,
            attribution_integrity=attribution_integrity,
            close_verdict=close_verdict,
            factor_contributions=factor_contributions,
            real_pnl=real_pnl,
        )
        exit_decision_id = runtime.ledger.log_decision(**payloads["decision"])
        runtime.ledger.log_position_event(**payloads["position_event"])
        return exit_decision_id, context_integrity
    except Exception:
        runtime.exception("[live] ledger close failed for pos {}", pid)
        return "", context_integrity


def run_closed_position_learning(
    *,
    position_id: int,
    total_pnl: float,
    current_price: float,
    close_ts: float,
    factor_contributions: dict[str, Any],
    exit_decision_id: str,
    real_pnl: dict[str, Any] | None,
    close_reason: str,
    context_integrity: str,
    attribution_integrity: str,
    close_source: dict[str, Any] | str | None,
    runtime: ClosedPositionProcessingRuntime,
) -> None:
    if not (
        runtime.trade_reviewer
        and runtime.experience_builder
        and runtime.policy_suggester
    ):
        return
    pid = int(position_id)
    try:
        review = runtime.trade_reviewer.review_closed_trade(
            **runtime.build_trade_review_payload(
                position_id=pid,
                total_pnl=float(total_pnl),
                current_price=float(current_price),
                close_ts=close_ts,
                factor_contributions=factor_contributions,
                exit_decision_id=exit_decision_id,
                real_pnl=real_pnl,
                close_reason=close_reason,
                context_integrity=context_integrity,
                attribution_integrity=attribution_integrity,
                close_source=close_source,
            )
        )
        if review.get("accepted", True):
            experience = runtime.experience_builder.build_from_review(review)
            runtime.policy_suggester.suggest_from_experience(experience)
        else:
            runtime.info(
                "[live] skipped unverified trade review for pos %s: %s",
                pid,
                review.get("skip_reason", "unknown"),
            )
    except Exception:
        runtime.exception("[live] post-trade learning failed for pos {}", pid)


def cleanup_closed_position(
    *,
    position_id: int,
    close_reason: str,
    total_pnl: float,
    close_ts: float,
    real_pnl: dict[str, Any] | None,
    factor_contributions: dict[str, Any],
    runtime: ClosedPositionProcessingRuntime,
) -> bool:
    pid = int(position_id)
    projection_ready = True
    try:
        runtime.mark_recovery_closed(
            pid,
            close_reason=close_reason,
            close_pnl=float(total_pnl),
            closed_at=close_ts,
            meta={
                "real_pnl": real_pnl or {},
                "factor_contributions": factor_contributions or {},
            },
        )
    except Exception as exc:
        projection_ready = False
        runtime.debug(
            "[live] recovery close persist failed for pos %s: %s",
            pid,
            exc,
        )
    runtime.trailing_state.pop(pid, None)
    runtime.entry_scores.pop(pid, None)
    runtime.entry_decisions.pop(pid, None)
    runtime.pending_open_attach_until.pop(pid, None)
    return projection_ready
