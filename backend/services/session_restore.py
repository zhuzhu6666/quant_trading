"""Pure deals-first reconstruction for the live UTC risk session.

The broker account balance and completed-position deal stream are the inputs.
``runtime_kv`` is deliberately absent from this module: a persisted snapshot
may be displayed as a degraded cache, but it must never influence an
authoritative reconstruction.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any


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
