"""Reconcile stale open rows in data/trades.duckdb.

The attribution ledger can miss a close write when the in-memory attribution
context is absent during live-loop recovery. This script uses independent
runtime evidence from state.db to mark those stale `open` rows as `closed`.

Default mode is read-only dry-run. Use --apply to update trades.duckdb.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.core.db import DUCKDB_TRADES, connect_duckdb, get_state_pg_conn
from backend.services.state_payload_archive import load_json_payload


@dataclass
class ReconcileRow:
    position_id: int
    status: str
    action: str
    evidence: list[str]
    reason: str
    open_ts: float
    open_price: float
    direction: int
    volume: float
    close_ts: float | None = None
    close_price: float | None = None
    trade_pnl: float | None = None
    pnl_pct: float | None = None
    review_pnl: float | None = None
    recovery_status: str | None = None
    recovery_closed_at: float | None = None
    recovery_pnl: float | None = None
    close_deal_id: int | None = None


def _rowdict(row: sqlite3.Row | None) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def _price_to_trade_scale(price: Any, *, symbol: str = "XAUUSD+") -> float | None:
    try:
        value = float(price or 0.0)
    except Exception:
        return None
    if value <= 0:
        return None
    if symbol.upper().startswith("XAU") and value < 100.0:
        return value * 100.0
    return value


def _first_float(*values: Any) -> float | None:
    for value in values:
        try:
            parsed = float(value)
        except Exception:
            continue
        if parsed > 0:
            return parsed
    return None


def _review_payload(review: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(str(review.get("review_json") or "{}"))
    except Exception:
        return {}


def _open_trade_rows(limit: int | None = None) -> list[dict[str, Any]]:
    con = connect_duckdb(DUCKDB_TRADES, read_only=True)
    try:
        query = """
            SELECT position_id, symbol, direction, volume, open_ts, open_price,
                   close_ts, close_price, status
            FROM trades
            WHERE status='open'
            ORDER BY open_ts DESC
        """
        params: list[Any] = []
        if limit is not None and limit > 0:
            query += " LIMIT ?"
            params.append(int(limit))
        cols = [d[0] for d in con.execute(query, params).description]
        return [dict(zip(cols, row)) for row in con.fetchall()]
    finally:
        con.close()


def _state_sql(sql: str) -> str:
    return sql.replace("?", "%s")


def _state_execute(conn, sql: str, params: tuple | list | None = None):
    if params is None:
        return conn.execute(_state_sql(sql))
    return conn.execute(_state_sql(sql), tuple(params))


def _state_context(conn, position_id: int) -> dict[str, Any]:
    close_deal = _state_execute(
        conn,
        """
        SELECT *
        FROM ctrader_deals
        WHERE position_id=? AND is_close=1
        ORDER BY exec_timestamp DESC, deal_id DESC
        LIMIT 1
        """,
        (position_id,),
    ).fetchone()
    open_deal = _state_execute(
        conn,
        """
        SELECT *
        FROM ctrader_deals
        WHERE position_id=? AND is_close=0
        ORDER BY exec_timestamp ASC, deal_id ASC
        LIMIT 1
        """,
        (position_id,),
    ).fetchone()
    review = _state_execute(
        conn,
        """
        SELECT *
        FROM trade_outcome_review
        WHERE position_id=? OR trade_id=?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (str(position_id), str(position_id)),
    ).fetchone()
    recovery = _state_execute(
        conn,
        """
        SELECT *
        FROM recovery_position_state
        WHERE position_id=?
        """,
        (str(position_id),),
    ).fetchone()
    review_data = _rowdict(review)
    if review_data:
        review_data["review_json"] = load_json_payload(
            conn,
            source_table="trade_outcome_review",
            source_id=str(review_data.get("review_id") or ""),
            inline_json=review_data.get("review_json", "{}"),
            archive_hash=review_data.get("review_archive_hash", ""),
            default={},
        )
    return {
        "close_deal": _rowdict(close_deal),
        "open_deal": _rowdict(open_deal),
        "review": review_data,
        "recovery": _rowdict(recovery),
    }


def _classify(trade: dict[str, Any], ctx: dict[str, Any], *, min_evidence: int) -> ReconcileRow:
    position_id = int(trade["position_id"])
    symbol = str(trade.get("symbol") or "XAUUSD+")
    open_price = float(trade.get("open_price") or 0.0)
    direction = int(trade.get("direction") or 0)
    volume = float(trade.get("volume") or 0.0)
    open_ts = float(trade.get("open_ts") or 0.0)

    close_deal = ctx["close_deal"]
    review = ctx["review"]
    recovery = ctx["recovery"]
    review_json = _review_payload(review)
    real_pnl = review_json.get("real_pnl") if isinstance(review_json.get("real_pnl"), dict) else {}

    evidence: list[str] = []
    if close_deal:
        evidence.append("ctrader_close_deal")
    if review:
        evidence.append("trade_outcome_review")
    recovery_status = str(recovery.get("status") or "") if recovery else ""
    if recovery_status.startswith("closed"):
        evidence.append(f"recovery_{recovery_status}")

    close_ts = _first_float(
        close_deal.get("exec_timestamp"),
        review_json.get("close_ts"),
        review.get("created_at"),
        recovery.get("closed_at"),
    )
    close_price = _price_to_trade_scale(
        close_deal.get("exec_price")
        or real_pnl.get("exec_price")
        or review_json.get("close_price"),
        symbol=symbol,
    )
    close_deal_id = None
    try:
        close_deal_id = int(close_deal.get("deal_id")) if close_deal.get("deal_id") else None
    except Exception:
        close_deal_id = None

    review_pnl = None
    try:
        review_pnl = float(review.get("pnl")) if review and review.get("pnl") is not None else None
    except Exception:
        review_pnl = None
    recovery_pnl = None
    try:
        recovery_pnl = float(recovery.get("close_pnl")) if recovery and recovery.get("close_pnl") is not None else None
    except Exception:
        recovery_pnl = None

    if len(evidence) < min_evidence:
        if recovery_status == "open":
            action = "keep_open"
            reason = "recovery_position_state still reports open"
        else:
            action = "uncertain"
            reason = "not enough independent close evidence"
        return ReconcileRow(
            position_id=position_id,
            status="open",
            action=action,
            evidence=evidence,
            reason=reason,
            open_ts=open_ts,
            open_price=open_price,
            direction=direction,
            volume=volume,
            review_pnl=review_pnl,
            recovery_status=recovery_status or None,
            recovery_closed_at=_first_float(recovery.get("closed_at")),
            recovery_pnl=recovery_pnl,
            close_deal_id=close_deal_id,
        )

    trade_pnl = None
    pnl_pct = None
    if close_price is not None and open_price > 0 and direction:
        trade_pnl = round((close_price - open_price) * direction * volume, 6)
        pnl_pct = round(trade_pnl / open_price * 100.0, 6)

    if close_ts is None or close_price is None:
        action = "uncertain"
        reason = "closed evidence exists but close_ts or close_price is missing"
    else:
        action = "mark_closed"
        reason = "closed in state.db but still open in trades.duckdb"

    return ReconcileRow(
        position_id=position_id,
        status="open",
        action=action,
        evidence=evidence,
        reason=reason,
        open_ts=open_ts,
        open_price=open_price,
        direction=direction,
        volume=volume,
        close_ts=close_ts,
        close_price=close_price,
        trade_pnl=trade_pnl,
        pnl_pct=pnl_pct,
        review_pnl=review_pnl,
        recovery_status=recovery_status or None,
        recovery_closed_at=_first_float(recovery.get("closed_at")),
        recovery_pnl=recovery_pnl,
        close_deal_id=close_deal_id,
    )


def analyze(*, limit: int | None = None, min_evidence: int = 1) -> list[ReconcileRow]:
    state = get_state_pg_conn(read_only=True)
    try:
        return [
            _classify(trade, _state_context(state, int(trade["position_id"])), min_evidence=min_evidence)
            for trade in _open_trade_rows(limit)
        ]
    finally:
        state.close()


def apply_updates(rows: list[ReconcileRow]) -> int:
    targets = [row for row in rows if row.action == "mark_closed"]
    if not targets:
        return 0

    con = connect_duckdb(DUCKDB_TRADES)
    try:
        con.execute("BEGIN TRANSACTION")
        next_exec_id = int(con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM trade_executions").fetchone()[0])
        now = time.time()
        changed = 0
        for row in targets:
            con.execute(
                """
                UPDATE trades
                SET close_ts=?,
                    close_price=?,
                    close_reason=?,
                    trade_pnl=?,
                    pnl_pct=?,
                    status='closed',
                    updated_at=?
                WHERE position_id=?
                  AND status='open'
                  AND close_ts IS NULL
                """,
                [
                    row.close_ts,
                    row.close_price,
                    "reconciled_from_state_db",
                    row.trade_pnl,
                    row.pnl_pct,
                    now,
                    row.position_id,
                ],
            )
            exists = con.execute(
                """
                SELECT COUNT(*)
                FROM trade_executions
                WHERE trade_id=? AND exec_type='close'
                """,
                [row.position_id],
            ).fetchone()[0]
            if int(exists or 0) == 0:
                con.execute(
                    """
                    INSERT INTO trade_executions
                    (id, trade_id, exec_ts, exec_type, price, volume, reason)
                    VALUES (?, ?, ?, 'close', ?, ?, 'reconciled_from_state_db')
                    """,
                    [next_exec_id, row.position_id, row.close_ts, row.close_price, row.volume],
                )
                next_exec_id += 1
            changed += 1
        con.execute("COMMIT")
        return changed
    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        con.close()


def _summary(rows: list[ReconcileRow]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.action] = counts.get(row.action, 0) + 1
    return {
        "total_open_rows": len(rows),
        "counts": counts,
        "mark_closed_position_ids": [row.position_id for row in rows if row.action == "mark_closed"],
        "keep_open_position_ids": [row.position_id for row in rows if row.action == "keep_open"],
        "uncertain_position_ids": [row.position_id for row in rows if row.action == "uncertain"],
    }


def _print_human(rows: list[ReconcileRow], *, applied: int | None) -> None:
    summary = _summary(rows)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if applied is not None:
        print(f"applied_updates={applied}")

    for action in ("mark_closed", "keep_open", "uncertain"):
        subset = [row for row in rows if row.action == action]
        if not subset:
            continue
        print(f"\n[{action}] {len(subset)}")
        for row in subset[:80]:
            evidence = ",".join(row.evidence) if row.evidence else "-"
            print(
                f"{row.position_id} evidence={evidence} "
                f"open={row.open_price:.3f} close={row.close_price or 0:.3f} "
                f"pnl={row.trade_pnl if row.trade_pnl is not None else 'n/a'} "
                f"review_pnl={row.review_pnl if row.review_pnl is not None else 'n/a'} "
                f"reason={row.reason}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="update trades.duckdb instead of dry-run")
    parser.add_argument("--limit", type=int, default=0, help="limit open rows scanned; default scans all")
    parser.add_argument("--min-evidence", type=int, default=1, help="minimum close evidence sources required")
    parser.add_argument("--report", type=Path, help="optional JSON report path")
    parser.add_argument("--json", action="store_true", help="print full JSON rows")
    args = parser.parse_args()

    rows = analyze(limit=args.limit or None, min_evidence=max(1, int(args.min_evidence)))
    applied = apply_updates(rows) if args.apply else None

    payload = {
        "generated_at": time.time(),
        "apply": bool(args.apply),
        "applied_updates": applied,
        "summary": _summary(rows),
        "rows": [asdict(row) for row in rows],
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_human(rows, applied=applied)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
