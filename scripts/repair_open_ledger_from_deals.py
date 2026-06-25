"""Repair missing open ledger rows from cTrader deal history.

This is a Phase A stabilization utility for historical gaps where a position
was filled but the old live loop failed before persisting the open context.
It writes minimal, explicitly partial open evidence. It does not fabricate
factor snapshots.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.core.db import STATE_DB, STATE_DB_DDL


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(STATE_DB_DDL)
    return conn


def _dump(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, default=str)


def _missing_open_rows(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT
                c.position_id,
                MIN(c.decision_ts) AS first_close_decision_ts,
                MAX(c.trade_id) AS trade_id,
                MAX(c.symbol) AS symbol,
                MAX(c.timeframe) AS timeframe,
                MAX(c.action_reason) AS close_reason
            FROM decision_ledger c
            LEFT JOIN decision_ledger o
              ON o.position_id = c.position_id AND o.event_type='open'
            WHERE c.event_type='close'
              AND c.position_id != ''
              AND o.decision_id IS NULL
            GROUP BY c.position_id
            ORDER BY first_close_decision_ts DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    )


def _deal_context(conn: sqlite3.Connection, position_id: int) -> dict:
    open_deal = conn.execute(
        """
        SELECT *
        FROM ctrader_deals
        WHERE position_id=? AND is_close=0
        ORDER BY exec_timestamp ASC
        LIMIT 1
        """,
        (position_id,),
    ).fetchone()
    close_deal = conn.execute(
        """
        SELECT *
        FROM ctrader_deals
        WHERE position_id=? AND is_close=1
        ORDER BY exec_timestamp DESC
        LIMIT 1
        """,
        (position_id,),
    ).fetchone()
    recovery = conn.execute(
        "SELECT * FROM recovery_position_state WHERE position_id=?",
        (position_id,),
    ).fetchone()
    return {
        "open_deal": dict(open_deal) if open_deal else {},
        "close_deal": dict(close_deal) if close_deal else {},
        "recovery": dict(recovery) if recovery else {},
    }


def _price_from_context(ctx: dict) -> float:
    recovery = ctx.get("recovery") or {}
    close_deal = ctx.get("close_deal") or {}
    open_deal = ctx.get("open_deal") or {}
    for value in (
        recovery.get("open_price"),
        close_deal.get("entry_price"),
        open_deal.get("entry_price"),
        open_deal.get("exec_price"),
    ):
        try:
            price = float(value or 0.0)
        except Exception:
            continue
        if price > 100.0:
            return price
    return 0.0


def _direction_from_context(ctx: dict) -> int:
    recovery = ctx.get("recovery") or {}
    if int(recovery.get("direction") or 0) != 0:
        return int(recovery.get("direction") or 0)
    side = str((ctx.get("open_deal") or {}).get("trade_side") or "").lower()
    if "buy" in side:
        return 1
    if "sell" in side:
        return -1
    return 0


def repair(*, db_path: str | Path = STATE_DB, limit: int = 50, apply: bool = False) -> dict:
    conn = _connect(db_path)
    repaired = []
    skipped = []
    try:
        rows = _missing_open_rows(conn, limit)
        now = time.time()
        for row in rows:
            position_id = int(row["position_id"])
            ctx = _deal_context(conn, position_id)
            open_deal = ctx.get("open_deal") or {}
            close_deal = ctx.get("close_deal") or {}
            recovery = ctx.get("recovery") or {}
            decision_ts = float(
                open_deal.get("exec_timestamp")
                or recovery.get("first_seen_at")
                or row["first_close_decision_ts"]
                or now
            )
            price = _price_from_context(ctx)
            volume = float(
                recovery.get("volume")
                or open_deal.get("filled_volume")
                or open_deal.get("volume")
                or close_deal.get("closed_volume")
                or 0.0
            )
            direction = _direction_from_context(ctx)
            if not open_deal and not recovery:
                skipped.append({"position_id": position_id, "reason": "no_open_deal_or_recovery"})
                continue

            decision_id = _new_id("dec_repair")
            trade_id = str(row["trade_id"] or position_id)
            payload = {
                "position_id": position_id,
                "source": "repair_open_ledger_from_deals",
                "context_integrity": "partial",
                "direction": direction,
                "price": price,
                "volume": volume,
                "open_deal_id": open_deal.get("deal_id"),
                "close_deal_id": close_deal.get("deal_id"),
                "repair_created_at": now,
                "note": "minimal historical open ledger repair; no factor snapshots fabricated",
            }
            repaired.append({"position_id": position_id, "decision_id": decision_id, "price": price, "volume": volume})
            if not apply:
                continue

            conn.execute(
                """
                INSERT INTO decision_ledger
                (decision_id, trade_id, position_id, event_type, symbol, timeframe,
                 decision_ts, portfolio_state_json, risk_state_json, action_score,
                 action_reason, action_json, created_at)
                VALUES (?, ?, ?, 'open', ?, ?, ?, '{}', '{}', 0.0,
                        'historical_open_repair', ?, ?)
                """,
                (
                    decision_id,
                    trade_id,
                    str(position_id),
                    str(row["symbol"] or recovery.get("symbol") or "XAUUSD+"),
                    str(row["timeframe"] or ""),
                    decision_ts,
                    _dump(payload),
                    now,
                ),
            )
            for event_type, status in (("submitted", "submitted"), ("filled", "filled")):
                conn.execute(
                    """
                    INSERT INTO order_lifecycle_event
                    (event_id, decision_id, trade_id, order_id, broker_order_id,
                     event_type, event_ts, price, volume, status, details_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _new_id("ordevt_repair"),
                        decision_id,
                        trade_id,
                        str(position_id),
                        str(position_id),
                        event_type,
                        decision_ts,
                        price,
                        volume,
                        status,
                        _dump(payload),
                    ),
                )
            conn.execute(
                """
                INSERT INTO position_lifecycle_event
                (event_id, position_id, trade_id, symbol, event_type, event_ts,
                 net_volume, avg_price, details_json)
                VALUES (?, ?, ?, ?, 'opened', ?, ?, ?, ?)
                """,
                (
                    _new_id("posevt_repair"),
                    str(position_id),
                    trade_id,
                    str(row["symbol"] or recovery.get("symbol") or "XAUUSD+"),
                    decision_ts,
                    volume,
                    price,
                    _dump(payload),
                ),
            )
            conn.execute(
                """
                UPDATE recovery_position_state
                SET entry_decision_id=CASE
                        WHEN entry_decision_id='' THEN ?
                        ELSE entry_decision_id
                    END,
                    context_integrity=CASE
                        WHEN context_integrity='' THEN 'partial'
                        ELSE context_integrity
                    END
                WHERE position_id=?
                """,
                (decision_id, position_id),
            )
        if apply:
            conn.commit()
        return {
            "applied": bool(apply),
            "candidate_count": len(rows),
            "repaired": repaired,
            "skipped": skipped,
        }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair missing open ledger rows from cTrader deals.")
    parser.add_argument("--db", default=str(STATE_DB), help="Path to state.db")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--apply", action="store_true", help="Write repairs. Omit for dry-run.")
    args = parser.parse_args()
    result = repair(db_path=args.db, limit=args.limit, apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
