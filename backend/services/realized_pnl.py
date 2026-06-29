"""Realized PnL series built from persisted broker close records."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

from backend.core.db import get_state_conn

_DEFAULT_TZ = "Asia/Shanghai"
_VALID_SCOPES = {"today", "24h", "7d", "30d", "all"}


def _scope_window(scope: str, *, now_ts: float, tz_name: str) -> tuple[float | None, float]:
    scope = (scope or "today").strip().lower()
    if scope not in _VALID_SCOPES:
        scope = "today"
    tz = ZoneInfo(tz_name or _DEFAULT_TZ)
    now_dt = datetime.fromtimestamp(now_ts, tz)
    if scope == "today":
        start_dt = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        return start_dt.timestamp(), now_ts
    if scope == "24h":
        return now_ts - 24 * 3600, now_ts
    if scope == "7d":
        return (now_dt - timedelta(days=7)).timestamp(), now_ts
    if scope == "30d":
        return (now_dt - timedelta(days=30)).timestamp(), now_ts
    return None, now_ts


def _net_from_close_row(row) -> float:
    gross = float(row["gross_profit"] or 0.0)
    swap = float(row["swap"] or 0.0)
    close_commission = float(row["close_commission"] or 0.0)
    return gross + swap - close_commission


def _fetch_ctrader_close_points(conn, *, from_ts: float | None, to_ts: float) -> list[dict]:
    clauses = ["is_close=1", "exec_timestamp > 0", "exec_timestamp <= ?"]
    params: list[float] = [float(to_ts)]
    if from_ts is not None:
        clauses.append("exec_timestamp >= ?")
        params.append(float(from_ts))
    rows = conn.execute(
        f"""
        SELECT
            deal_id, position_id, exec_timestamp, exec_price, entry_price,
            gross_profit, swap, close_commission, balance, closed_volume
        FROM ctrader_deals
        WHERE {" AND ".join(clauses)}
        ORDER BY exec_timestamp ASC, deal_id ASC
        """,
        params,
    ).fetchall()
    points: list[dict] = []
    for row in rows:
        pnl = _net_from_close_row(row)
        points.append({
            "ts": float(row["exec_timestamp"] or 0.0),
            "position_id": int(row["position_id"] or 0),
            "deal_id": int(row["deal_id"] or 0),
            "pnl": pnl,
            "gross": float(row["gross_profit"] or 0.0),
            "swap": float(row["swap"] or 0.0),
            "commission": float(row["close_commission"] or 0.0),
            "entry_price": float(row["entry_price"] or 0.0),
            "exec_price": float(row["exec_price"] or 0.0),
            "balance": float(row["balance"] or 0.0),
            "closed_volume": float(row["closed_volume"] or 0.0),
            "source": "ctrader_deals",
        })
    return points


def _fetch_recovery_fallback_points(
    conn,
    *,
    from_ts: float | None,
    to_ts: float,
    excluded_position_ids: Iterable[int],
) -> list[dict]:
    excluded = {int(pid) for pid in excluded_position_ids if int(pid or 0) > 0}
    clauses = [
        "status LIKE 'closed%'",
        "closed_at > 0",
        "closed_at <= ?",
        "ABS(close_pnl) > 0.0000001",
    ]
    params: list[float] = [float(to_ts)]
    if from_ts is not None:
        clauses.append("closed_at >= ?")
        params.append(float(from_ts))
    rows = conn.execute(
        f"""
        SELECT position_id, symbol, direction, volume, closed_at, close_reason, close_pnl
        FROM recovery_position_state
        WHERE {" AND ".join(clauses)}
        ORDER BY closed_at ASC, position_id ASC
        """,
        params,
    ).fetchall()
    points: list[dict] = []
    for row in rows:
        position_id = int(row["position_id"] or 0)
        if position_id in excluded:
            continue
        points.append({
            "ts": float(row["closed_at"] or 0.0),
            "position_id": position_id,
            "deal_id": 0,
            "pnl": float(row["close_pnl"] or 0.0),
            "gross": float(row["close_pnl"] or 0.0),
            "swap": 0.0,
            "commission": 0.0,
            "entry_price": 0.0,
            "exec_price": 0.0,
            "balance": 0.0,
            "closed_volume": float(row["volume"] or 0.0),
            "source": "recovery_position_state",
            "close_reason": str(row["close_reason"] or ""),
            "symbol": str(row["symbol"] or ""),
            "direction": int(row["direction"] or 0),
        })
    return points


def get_realized_pnl_series(
    *,
    scope: str = "today",
    from_ts: float | None = None,
    to_ts: float | None = None,
    tz: str = _DEFAULT_TZ,
    conn_factory: Callable[[], object] = get_state_conn,
) -> dict:
    """Return close-by-close realized PnL and cumulative curve.

    Primary source is cTrader close deals. Recovery rows are used only when a
    position has no broker close deal in the same window.
    """
    now_ts = float(to_ts or time.time())
    if from_ts is None:
        from_ts, to_ts = _scope_window(scope, now_ts=now_ts, tz_name=tz)
    else:
        to_ts = now_ts
    conn = conn_factory()
    try:
        close_points = _fetch_ctrader_close_points(conn, from_ts=from_ts, to_ts=to_ts)
        broker_position_ids = {int(point["position_id"]) for point in close_points}
        fallback_points = _fetch_recovery_fallback_points(
            conn,
            from_ts=from_ts,
            to_ts=to_ts,
            excluded_position_ids=broker_position_ids,
        )
    finally:
        conn.close()

    points = sorted(close_points + fallback_points, key=lambda item: (item["ts"], item["position_id"], item["deal_id"]))
    cumulative = 0.0
    wins = 0
    losses = 0
    for point in points:
        pnl = float(point["pnl"] or 0.0)
        cumulative += pnl
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1
        point["pnl"] = round(pnl, 6)
        point["cumulative"] = round(cumulative, 6)

    return {
        "ok": True,
        "scope": scope if scope in _VALID_SCOPES else "today",
        "currency": "USD",
        "from_ts": from_ts,
        "to_ts": to_ts,
        "source": "ctrader_deals",
        "fallback_source": "recovery_position_state",
        "summary": {
            "realized_pnl": round(cumulative, 6),
            "trades": len(points),
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / len(points), 6) if points else 0.0,
        },
        "points": points,
    }
