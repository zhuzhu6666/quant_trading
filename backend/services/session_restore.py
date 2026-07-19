"""Pure deals-first reconstruction for the live UTC risk session.

The broker account balance and completed-position deal stream are the inputs.
``runtime_kv`` is deliberately absent from this module: a persisted snapshot
may be displayed as a degraded cache, but it must never influence an
authoritative reconstruction.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class PartialCloseSessionFactRuntime:
    get_state_connection: Callable[[], Any]
    fetch_deals_since_result: Callable[..., Any]
    store_deals: Callable[[Any, list[Any]], Any]
    find_close_deal: Callable[[Any, int], dict[str, Any] | None]
    authoritative_close_pnl: Callable[[Any], bool]
    defer_close: Callable[..., Any]
    record_aux_failure: Callable[..., Any]
    release_close_latch: Callable[[int, dict[str, Any]], Any]
    update_live_state: Callable[..., Any]
    no_new_risk_latch_status: Callable[..., dict[str, Any]]
    open_api_volumes: dict[int, float]
    now: Callable[[], float]


def sync_partial_close_session_fact(
    bridge: Any,
    *,
    broker: str,
    position_id: int,
    close_ts: float,
    volume: float,
    tick: int,
    runtime: PartialCloseSessionFactRuntime,
    deal_cursor: dict[str, Any] | None = None,
) -> bool:
    """Ingest a confirmed partial-close deal without rewriting broker success."""

    pid = int(position_id or 0)
    cursor = dict(deal_cursor or {})
    baseline_cursor_available = bool(
        cursor.get("baseline_cursor_available", False)
    )
    before_deal_ids = {
        int(item)
        for item in list(cursor.get("baseline_deal_ids") or [])
        if int(item or 0) > 0
    }
    before_closed_volume = float(
        cursor.get("baseline_closed_volume") or 0.0
    )
    try:
        conn = runtime.get_state_connection()
        try:
            fetch_result = runtime.fetch_deals_since_result(
                bridge,
                from_ts=int(
                    max(0.0, float(close_ts or runtime.now()) - 60.0)
                ),
                max_rows=200,
            )
            if not fetch_result.success:
                raise RuntimeError(
                    "partial_close_deal_fetch_failed:"
                    f"{fetch_result.error_code}:"
                    f"{fetch_result.error_message}"
                )
            if fetch_result.empty:
                raise RuntimeError("partial_close_deal_fetch_valid_empty")
            runtime.store_deals(conn, list(fetch_result.deals))
            after = runtime.find_close_deal(conn, pid) or {}
        finally:
            conn.close()
        after_deal_ids = {
            int(item)
            for item in list(after.get("deal_ids") or [])
            if int(item or 0) > 0
        }
        new_deal_ids = after_deal_ids - before_deal_ids
        closed_volume_delta = max(
            0.0,
            float(after.get("closed_volume") or 0.0) - before_closed_volume,
        )
        payload = {
            "gross": float(after.get("gross_profit") or 0.0),
            "swap": float(after.get("swap") or 0.0),
            "commission": float(after.get("close_commission") or 0.0),
            "net": float(after.get("gross_profit") or 0.0)
            + float(after.get("swap") or 0.0)
            + float(after.get("close_commission") or 0.0),
            "exec_timestamp": float(after.get("exec_timestamp") or 0.0),
            "closed_volume": float(after.get("closed_volume") or 0.0),
            "deal_id": after.get("deal_id"),
            "deal_ids": sorted(after_deal_ids),
            "close_deals_count": int(after.get("close_deals_count") or 0),
            "source": "ctrader_deals",
        }
        if (
            not baseline_cursor_available
            or not runtime.authoritative_close_pnl(payload)
            or not new_deal_ids
            or closed_volume_delta + 1e-9 < max(0.0, float(volume or 0.0))
        ):
            raise RuntimeError(
                "partial_close_deal_unavailable_or_incomplete:"
                f"new_deals={sorted(new_deal_ids)}:"
                f"closed_volume_delta={closed_volume_delta}"
            )
    except Exception as exc:
        runtime.defer_close(
            pid,
            broker=broker,
            tick=tick,
            reason=f"partial_close_deal_unavailable:{type(exc).__name__}",
            recovery_evidence={
                "pending_kind": "partial_close",
                "baseline_cursor_available": baseline_cursor_available,
                "baseline_deal_ids": sorted(before_deal_ids),
                "baseline_closed_volume": float(before_closed_volume),
                "required_closed_volume_delta": float(volume or 0.0),
                "expected_position_volume": float(
                    runtime.open_api_volumes.get(pid, 0.0) or 0.0
                ),
                "close_requested_at": float(close_ts or 0.0),
                "cursor_captured_at": float(cursor.get("captured_at") or 0.0),
                "cursor_error": str(cursor.get("error") or ""),
            },
        )
        runtime.record_aux_failure(
            "partial_close_session_fact_unavailable",
            position_id=pid,
            action="reduce_position",
            error=exc,
            payload={"requested_close_volume": float(volume or 0.0)},
        )
        return False

    runtime.release_close_latch(pid, payload)
    runtime.update_live_state(
        session_state_status="unavailable",
        session_state_source="partial_close_deal_projection_pending",
        session_risk_blockers=[f"partial_close_projection_pending:{pid}"],
        session_observed_at=0.0,
        accepting_new_risk=False,
        no_new_risk_latch=runtime.no_new_risk_latch_status(fail_closed=True),
    )
    return True


def session_trade_window(
    trade_date: str,
    timezone_name: str = "UTC",
) -> tuple[float, float]:
    """Return a natural-day window in the requested IANA timezone."""

    tz = timezone.utc if timezone_name == "UTC" else ZoneInfo(timezone_name)
    day_start = datetime.strptime(str(trade_date), "%Y-%m-%d").replace(tzinfo=tz)
    return day_start.timestamp(), (day_start + timedelta(days=1)).timestamp()


def load_authoritative_session_deal_facts(
    trade_date: str,
    timezone_name: str = "UTC",
    *,
    broker_open_position_ids: set[int] | None,
    confirmed_closed_position_ids: set[int] | None = None,
    connection_factory: Callable[[], Any],
    execute: Callable[[Any, str, tuple[Any, ...]], Any],
    warning: Callable[..., Any] | None = None,
) -> dict[str, Any] | None:
    """Read deals-first session facts through an injected state-store boundary.

    The module owns the SQL and lifecycle completeness proof, while the live
    façade supplies only the PostgreSQL connection and placeholder adapter.
    A missing fresh broker-open set or any ambiguous broker-missing position
    keeps the session unavailable instead of manufacturing a zero-risk day.
    """

    warn = warning or (lambda *_args, **_kwargs: None)
    if broker_open_position_ids is None:
        warn(
            "[live] authoritative session trade rebuild requires fresh broker-open position IDs"
        )
        return None
    try:
        window_start, window_end = session_trade_window(trade_date, timezone_name)
        conn = connection_factory()
        try:
            completed_rows = execute(
                conn,
                """
                WITH final_close AS (
                    SELECT position_id, MAX(exec_timestamp) AS final_close_ts
                    FROM ctrader_deals
                    WHERE position_id > 0
                      AND (is_close=1 OR closed_volume > 0)
                      AND exec_timestamp > 0
                    GROUP BY position_id
                    HAVING MAX(exec_timestamp) >= ?
                       AND MAX(exec_timestamp) < ?
                )
                SELECT d.position_id,
                       SUM(COALESCE(d.gross_profit, 0.0)) AS gross_profit,
                       SUM(COALESCE(d.swap, 0.0)) AS swap,
                       SUM(COALESCE(d.close_commission, 0.0)) AS close_commission,
                       SUM(COALESCE(d.gross_profit, 0.0)
                           + COALESCE(d.swap, 0.0)
                           + COALESCE(d.close_commission, 0.0)) AS net,
                       MAX(d.exec_timestamp) AS exec_timestamp,
                       COUNT(*) AS close_deals_count
                FROM ctrader_deals d
                JOIN final_close f ON f.position_id = d.position_id
                WHERE d.is_close=1 OR d.closed_volume > 0
                GROUP BY d.position_id
                ORDER BY exec_timestamp ASC, d.position_id ASC
                """,
                (window_start, window_end),
            ).fetchall()
            realized_rows = execute(
                conn,
                """
                SELECT deal_id, position_id,
                       COALESCE(gross_profit, 0.0) AS gross_profit,
                       COALESCE(swap, 0.0) AS swap,
                       COALESCE(close_commission, 0.0) AS close_commission,
                       COALESCE(gross_profit, 0.0)
                           + COALESCE(swap, 0.0)
                           + COALESCE(close_commission, 0.0) AS net,
                       exec_timestamp,
                       COALESCE(closed_volume, 0.0) AS closed_volume
                FROM ctrader_deals
                WHERE position_id > 0
                  AND (is_close=1 OR closed_volume > 0)
                  AND exec_timestamp >= ?
                  AND exec_timestamp < ?
                ORDER BY exec_timestamp ASC, deal_id ASC
                """,
                (window_start, window_end),
            ).fetchall()
            open_position_ids = {
                int(position_id)
                for position_id in broker_open_position_ids
                if int(position_id or 0) > 0
            }
            confirmed_closed_ids = {
                int(position_id)
                for position_id in (confirmed_closed_position_ids or set())
                if int(position_id or 0) > 0
            }
            completed_position_ids = {
                int(row["position_id"] or 0)
                for row in completed_rows
                if int(row["position_id"] or 0) > 0
            }
            active_rows = execute(
                conn,
                """
                SELECT position_id, last_seen_at
                FROM recovery_position_state
                WHERE broker=? AND status IN ('open', 'recovered')
                """,
                ("ctrader",),
            ).fetchall()
            tracked_active_ids = {
                int(row["position_id"] or 0)
                for row in active_rows
                if int(row["position_id"] or 0) > 0
            }
            unresolved_close_ids = sorted(
                pid
                for pid in tracked_active_ids - open_position_ids
                if (
                    pid not in completed_position_ids
                    or pid not in confirmed_closed_ids
                )
            )
            if unresolved_close_ids:
                warn(
                    "[live] session risk unavailable; broker-missing positions lack close deals: %s",
                    unresolved_close_ids,
                )
                return None
            completed_position_trades = [
                {
                    "position_id": int(row["position_id"] or 0),
                    "gross": float(row["gross_profit"] or 0.0),
                    "swap": float(row["swap"] or 0.0),
                    "commission": float(row["close_commission"] or 0.0),
                    "net": float(row["net"] or 0.0),
                    "exec_timestamp": float(row["exec_timestamp"] or 0.0),
                    "close_deals_count": int(row["close_deals_count"] or 0),
                }
                for row in completed_rows
                if int(row["position_id"] or 0) not in open_position_ids
            ]
            realized_close_legs = [
                {
                    "deal_id": int(row["deal_id"] or 0),
                    "position_id": int(row["position_id"] or 0),
                    "gross": float(row["gross_profit"] or 0.0),
                    "swap": float(row["swap"] or 0.0),
                    "commission": float(row["close_commission"] or 0.0),
                    "net": float(row["net"] or 0.0),
                    "exec_timestamp": float(row["exec_timestamp"] or 0.0),
                    "closed_volume": float(row["closed_volume"] or 0.0),
                }
                for row in realized_rows
            ]
            return {
                "schema_version": "live_session_deal_facts.v2",
                "trade_date": str(trade_date),
                "timezone": str(timezone_name),
                "window_start": float(window_start),
                "window_end": float(window_end),
                "broker_open_position_ids": sorted(open_position_ids),
                "confirmed_closed_position_ids": sorted(confirmed_closed_ids),
                "completed_position_trades": completed_position_trades,
                "realized_close_legs": realized_close_legs,
            }
        finally:
            conn.close()
    except Exception as exc:
        warn(
            "[live] authoritative session trade rebuild failed for %s: %s",
            trade_date,
            exc,
        )
        return None


def authoritative_close_pnl(real_pnl: Any) -> bool:
    """Return whether a close PnL payload is backed by a concrete deal.

    Position disappearance proves that broker exposure ended, but it does not
    prove realized PnL.  Estimates, factor attribution, and an implicit zero
    must never advance the session-risk authority boundary.
    """

    if not isinstance(real_pnl, Mapping) or real_pnl.get("net") is None:
        return False
    try:
        net = float(real_pnl.get("net"))
        exec_timestamp = float(real_pnl.get("exec_timestamp") or 0.0)
        close_deals_count = int(real_pnl.get("close_deals_count") or 0)
    except (TypeError, ValueError, OverflowError):
        return False
    if not math.isfinite(net) or not math.isfinite(exec_timestamp) or exec_timestamp <= 0.0:
        return False
    source = str(real_pnl.get("source") or "").strip().lower()
    deal_ids = list(real_pnl.get("deal_ids") or [])
    deal_id = real_pnl.get("deal_id")
    return bool(
        source == "ctrader_deals"
        or close_deals_count > 0
        or deal_ids
        or deal_id not in (None, "", 0, "0")
    )


def _finite_float(value: Any, *, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}_invalid") from exc
    if not math.isfinite(result):
        raise ValueError(f"{field}_invalid")
    return result


def derive_session_start_balance(
    *,
    current_balance: Any,
    completed_position_trades: Sequence[Mapping[str, Any]],
    realized_close_legs: Sequence[Mapping[str, Any]] | None = None,
) -> float:
    """Derive the UTC-day opening balance from fresh broker facts.

    Realized balance changes happen per close leg, including partial closes on
    positions that remain open.  Completed-position aggregates are retained as
    the compatibility fallback, but must not hide those already-realized legs.
    """

    balance = _finite_float(current_balance, field="current_balance")
    realized_rows = (
        completed_position_trades
        if realized_close_legs is None
        else realized_close_legs
    )
    realized_pnl = sum(
        _finite_float(trade.get("net", 0.0), field="trade_net")
        for trade in realized_rows
    )
    start_balance = balance - realized_pnl
    if balance <= 0.0 or start_balance <= 0.0:
        raise ValueError("session_start_balance_unavailable")
    return start_balance


def rebuild_session_risk_projection(
    *,
    trade_date: str,
    completed_position_trades: Sequence[Mapping[str, Any]],
    session_start_balance: Any,
    max_consecutive_losses: int,
    max_daily_loss_pct: float,
    realized_close_legs: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Rebuild PnL, equity path, peak, drawdown and circuit deterministically.

    Input rows represent completed positions (not individual partial-close
    legs).  Rows are sorted here as a defensive boundary so database return
    order can never change loss streaks or the reconstructed equity path.
    """

    if not str(trade_date or ""):
        raise ValueError("trade_date_required")
    start_balance = _finite_float(
        session_start_balance,
        field="session_start_balance",
    )
    if start_balance <= 0.0:
        raise ValueError("session_start_balance_unavailable")

    normalized: list[dict[str, Any]] = []
    for sequence, trade in enumerate(completed_position_trades):
        if not isinstance(trade, Mapping):
            raise ValueError("completed_position_trade_invalid")
        exec_timestamp = _finite_float(
            trade.get("exec_timestamp", 0.0),
            field="trade_exec_timestamp",
        )
        if exec_timestamp < 0.0:
            raise ValueError("trade_exec_timestamp_invalid")
        try:
            position_id = int(trade.get("position_id", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("trade_position_id_invalid") from exc
        normalized.append(
            {
                "position_id": position_id,
                "net": _finite_float(trade.get("net", 0.0), field="trade_net"),
                "exec_timestamp": exec_timestamp,
                "sequence": sequence,
            }
        )
    normalized.sort(
        key=lambda item: (
            item["exec_timestamp"],
            item["position_id"],
            item["sequence"],
        )
    )

    # Session PnL and drawdown follow every realized close leg.  Trade count,
    # win/loss and consecutive-loss semantics remain position-lifecycle based,
    # so an open position's partial close cannot pretend that a trade ended.
    realized_source = (
        normalized
        if realized_close_legs is None
        else realized_close_legs
    )
    normalized_realized: list[dict[str, Any]] = []
    for sequence, leg in enumerate(realized_source):
        if not isinstance(leg, Mapping):
            raise ValueError("realized_close_leg_invalid")
        try:
            deal_id = int(leg.get("deal_id", 0) or 0)
            position_id = int(leg.get("position_id", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("realized_close_leg_identity_invalid") from exc
        normalized_realized.append(
            {
                "deal_id": deal_id,
                "position_id": position_id,
                "net": _finite_float(
                    leg.get("net", 0.0),
                    field="realized_close_leg_net",
                ),
                "exec_timestamp": _finite_float(
                    leg.get("exec_timestamp", 0.0),
                    field="realized_close_leg_exec_timestamp",
                ),
                "sequence": sequence,
            }
        )
    normalized_realized.sort(
        key=lambda item: (
            item["exec_timestamp"],
            item["deal_id"],
            item["position_id"],
            item["sequence"],
        )
    )

    all_trade_pnls = [float(item["net"]) for item in normalized]
    realized_pnls = [float(item["net"]) for item in normalized_realized]
    session_pnl = sum(realized_pnls)
    equity_path = [start_balance]
    running_equity = start_balance
    peak_equity = start_balance
    max_drawdown_pct = 0.0
    for pnl in realized_pnls:
        running_equity += pnl
        equity_path.append(running_equity)
        peak_equity = max(peak_equity, running_equity)
        if peak_equity > 0.0:
            max_drawdown_pct = max(
                max_drawdown_pct,
                max(0.0, peak_equity - running_equity) / peak_equity * 100.0,
            )

    consecutive_loss = 0
    for pnl in reversed(all_trade_pnls):
        if pnl < 0.0:
            consecutive_loss += 1
        elif pnl > 0.0:
            break

    circuit_breaker = False
    circuit_reason = ""
    consecutive_limit = int(max_consecutive_losses)
    drawdown_limit = float(max_daily_loss_pct)
    if consecutive_limit > 0 and consecutive_loss >= consecutive_limit:
        circuit_breaker = True
        circuit_reason = f"consecutive losses {consecutive_loss}"
    elif drawdown_limit > 0.0 and max_drawdown_pct >= drawdown_limit:
        circuit_breaker = True
        circuit_reason = f"daily drawdown {max_drawdown_pct:.1f}%"

    return {
        "trade_date": str(trade_date),
        "session_pnl": session_pnl,
        "session_trades": len(all_trade_pnls),
        "session_winning": sum(1 for pnl in all_trade_pnls if pnl > 0.0),
        "session_losing": sum(1 for pnl in all_trade_pnls if pnl < 0.0),
        "session_trade_pnls": all_trade_pnls[-200:],
        "session_realized_pnl_legs": realized_pnls[-500:],
        "session_realized_legs": len(realized_pnls),
        "session_consecutive_loss": consecutive_loss,
        "session_max_drawdown_pct": max_drawdown_pct,
        "session_peak_equity": peak_equity,
        "session_start_balance": start_balance,
        "session_last_trade_ts": (
            float(normalized_realized[-1]["exec_timestamp"])
            if normalized_realized
            else 0.0
        ),
        "circuit_breaker": circuit_breaker,
        "circuit_reason": circuit_reason,
        "trade_equity_history": equity_path[-500:],
    }


def build_authoritative_session_state(
    *,
    trade_date: str,
    completed_position_trades: Sequence[Mapping[str, Any]],
    realized_close_legs: Sequence[Mapping[str, Any]] | None,
    current_balance: Any,
    max_consecutive_losses: int,
    max_daily_loss_pct: float,
) -> dict[str, Any]:
    """Build the complete deals-first live session projection from explicit facts."""

    trades = [dict(item) for item in completed_position_trades]
    realized_legs = (
        None
        if realized_close_legs is None
        else [dict(item) for item in realized_close_legs]
    )
    start_balance = derive_session_start_balance(
        current_balance=current_balance,
        completed_position_trades=trades,
        realized_close_legs=realized_legs,
    )
    projection = rebuild_session_risk_projection(
        trade_date=trade_date,
        completed_position_trades=trades,
        session_start_balance=start_balance,
        max_consecutive_losses=max_consecutive_losses,
        max_daily_loss_pct=max_daily_loss_pct,
        realized_close_legs=realized_legs,
    )
    return {
        **projection,
        "session_state_source": "ctrader_deals.final_close_rebuild.v1",
        "session_recorded_position_ids": sorted(
            {
                int(item.get("position_id") or 0)
                for item in trades
                if int(item.get("position_id") or 0) > 0
            }
        ),
    }


def resolve_session_restore(
    *,
    trade_date: str,
    raw_cache: Mapping[str, Any] | None,
    authoritative_facts: Mapping[str, Any] | None,
    current_balance: Any,
    max_consecutive_losses: int,
    max_daily_loss_pct: float,
    observed_at: float,
) -> dict[str, Any]:
    """Resolve authoritative, degraded-cache, and unavailable restore states.

    This function is deliberately side-effect free. Callers own PostgreSQL
    reads, runtime publication, cache healing, and circuit evaluation.
    """

    authoritative_error: str | None = None
    if authoritative_facts is not None:
        try:
            state = build_authoritative_session_state(
                trade_date=trade_date,
                completed_position_trades=list(
                    authoritative_facts.get("completed_position_trades") or []
                ),
                realized_close_legs=(
                    list(authoritative_facts.get("realized_close_legs") or [])
                    if "realized_close_legs" in authoritative_facts
                    else None
                ),
                current_balance=current_balance,
                max_consecutive_losses=max_consecutive_losses,
                max_daily_loss_pct=max_daily_loss_pct,
            )
        except (TypeError, ValueError, OverflowError) as exc:
            authoritative_error = f"{type(exc).__name__}:{exc}"
        else:
            return {
                "restored": True,
                "authoritative": True,
                "authoritative_error": None,
                "state": {
                    **state,
                    "session_state_status": "available",
                    "session_observed_at": float(observed_at),
                    "session_risk_blockers": [],
                },
            }

    degraded = parse_degraded_session_cache(raw_cache, trade_date=trade_date)
    if not degraded:
        return {
            "restored": False,
            "authoritative": False,
            "authoritative_error": authoritative_error,
            "state": {
                "session_state_status": "unavailable",
                "session_state_source": "unavailable",
                "accepting_new_risk": False,
            },
        }
    return {
        "restored": True,
        "authoritative": False,
        "authoritative_error": authoritative_error,
        "state": {
            **degraded,
            "session_state_source": "runtime_legacy_snapshot",
            "session_state_status": "degraded_cache",
            "accepting_new_risk": False,
        },
    }


def parse_degraded_session_cache(
    raw_state: Any,
    *,
    trade_date: str,
) -> dict[str, Any] | None:
    """Validate a same-day compatibility snapshot for display/protection only."""

    if not isinstance(raw_state, Mapping):
        return None
    if str(raw_state.get("trade_date") or "") != str(trade_date):
        return None
    required = ("session_pnl", "session_trades", "session_start_balance")
    if any(field not in raw_state for field in required):
        return None
    try:
        trade_pnls_raw = raw_state.get("session_trade_pnls") or []
        equity_history_raw = raw_state.get("trade_equity_history") or []
        if not isinstance(trade_pnls_raw, list) or not isinstance(
            equity_history_raw,
            list,
        ):
            return None
        trade_pnls = [
            _finite_float(value, field="cache_trade_pnl")
            for value in trade_pnls_raw
        ][-200:]
        equity_history = [
            _finite_float(value, field="cache_trade_equity")
            for value in equity_history_raw
        ][-500:]
        counters = {
            field: int(raw_state.get(field, 0) or 0)
            for field in (
                "session_trades",
                "session_winning",
                "session_losing",
                "session_consecutive_loss",
            )
        }
        if any(value < 0 for value in counters.values()):
            return None
        session_observed_at = _finite_float(
            raw_state.get(
                "session_observed_at",
                raw_state.get("updated_at", 0.0),
            ),
            field="cache_session_observed_at",
        )
        if session_observed_at < 0.0:
            return None
        return {
            "session_pnl": _finite_float(
                raw_state.get("session_pnl"),
                field="cache_session_pnl",
            ),
            **counters,
            "session_trade_pnls": trade_pnls,
            "session_max_drawdown_pct": _finite_float(
                raw_state.get("session_max_drawdown_pct", 0.0),
                field="cache_session_max_drawdown_pct",
            ),
            "session_peak_equity": _finite_float(
                raw_state.get("session_peak_equity", 0.0),
                field="cache_session_peak_equity",
            ),
            "session_start_balance": _finite_float(
                raw_state.get("session_start_balance"),
                field="cache_session_start_balance",
            ),
            "session_last_trade_ts": _finite_float(
                raw_state.get("session_last_trade_ts", 0.0),
                field="cache_session_last_trade_ts",
            ),
            # Compatibility snapshots retain their original observation
            # time.  Loading a cache never makes the risk fact fresh.
            "session_observed_at": session_observed_at,
            "circuit_breaker": bool(raw_state.get("circuit_breaker", False)),
            "circuit_reason": str(raw_state.get("circuit_reason") or ""),
            "trade_equity_history": equity_history,
        }
    except (TypeError, ValueError, OverflowError):
        return None
