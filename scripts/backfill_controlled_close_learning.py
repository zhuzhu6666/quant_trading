"""Controlled close-ledger and learning backfill for known failed closes.

This utility repairs a narrow class of runtime gaps where broker close facts
exist in cTrader deals, but the live close ledger/review path failed before
writing PostgreSQL state. It does not create factor snapshots, policy
suggestions, runtime overlay updates, or trading actions.
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

from backend.core.db import STATE_DB, STATE_DB_DDL, connect_sqlite, get_state_pg_conn, is_state_db_path
from backend.services import learning_backfill
from backend.services.trade_lesson_memory import upsert_trade_lesson_memory
from backend.services.live_position_lifecycle import classify_close_source_from_evidence

BACKFILL_SOURCE = "controlled_close_learning_backfill.v1"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _use_pg(db_path: str | Path) -> bool:
    return is_state_db_path(db_path)


def _sql(conn, sql: str) -> str:
    return sql.replace("?", "%s") if conn.__class__.__module__.split(".", 1)[0] == "psycopg" else sql


def _execute(conn, sql: str, params: tuple | list | None = None):
    if params is None:
        return conn.execute(_sql(conn, sql))
    return conn.execute(_sql(conn, sql), tuple(params))


def _connect(db_path: str | Path):
    conn = get_state_pg_conn() if _use_pg(db_path) else connect_sqlite(db_path)
    if not _use_pg(db_path):
        conn.row_factory = sqlite3.Row
        conn.executescript(STATE_DB_DDL)
    return conn


def _dump(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, default=str)


def _loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except Exception:
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return float(default)


def _normalize_deal_price(*, entry_price: float, exec_price: float) -> float:
    entry = _safe_float(entry_price)
    price = _safe_float(exec_price)
    if entry > 0 and price > 0:
        ratio = entry / price
        if 50.0 <= ratio <= 150.0:
            return round(price * 100.0, 6)
    return price


def fetch_backfill_rows(conn, position_ids: list[int]) -> list[dict[str, Any]]:
    placeholders = ", ".join(["?"] * len(position_ids))
    rows = _execute(
        conn,
        f"""
        WITH close_positions AS (
            SELECT
                position_id,
                MAX(CASE WHEN is_close = 1 THEN exec_timestamp ELSE 0 END) AS close_ts,
                SUM(CASE WHEN is_close = 1 THEN COALESCE(gross_profit, 0) + COALESCE(swap, 0) + COALESCE(close_commission, 0) ELSE 0 END) AS net_pnl,
                MAX(CASE WHEN is_close = 1 THEN entry_price ELSE 0 END) AS entry_price,
                MAX(CASE WHEN is_close = 1 THEN exec_price ELSE 0 END) AS exec_price,
                MAX(CASE WHEN is_close = 1 THEN balance ELSE 0 END) AS balance,
                MAX(CASE WHEN is_close = 1 THEN deal_id ELSE 0 END) AS deal_id,
                SUM(CASE WHEN is_close = 1 THEN COALESCE(close_commission, 0) ELSE 0 END) AS close_commission,
                SUM(CASE WHEN is_close = 1 THEN COALESCE(gross_profit, 0) ELSE 0 END) AS gross_profit,
                SUM(CASE WHEN is_close = 1 THEN COALESCE(swap, 0) ELSE 0 END) AS swap,
                MIN(CASE WHEN is_close = 0 THEN exec_timestamp END) AS broker_entry_ts,
                MAX(CASE WHEN is_close = 0 THEN exec_price END) AS broker_entry_price
            FROM ctrader_deals
            WHERE position_id IN ({placeholders})
            GROUP BY position_id
            HAVING MAX(CASE WHEN is_close = 1 THEN 1 ELSE 0 END) = 1
        ),
        open_decisions AS (
            SELECT *
            FROM decision_ledger
            WHERE event_type = 'open'
        ),
        supervisor_decisions AS (
            SELECT *
            FROM decision_ledger
            WHERE event_type IN ('supervisor_close', 'supervisor_reduce', 'supervisor_tighten')
        )
        SELECT
            c.position_id,
            c.close_ts,
            c.net_pnl,
            c.entry_price,
            c.exec_price,
            c.balance,
            c.deal_id,
            c.close_commission,
            c.gross_profit,
            c.swap,
            d.decision_id AS entry_decision_id,
            d.trade_id,
            d.regime_id,
            d.action_score AS entry_score,
            d.decision_ts AS entry_ts,
            d.symbol,
            d.timeframe,
            c.broker_entry_ts,
            c.broker_entry_price,
            s.decision_id AS supervisor_decision_id,
            s.event_type AS supervisor_event_type,
            s.action_score AS supervisor_action_score,
            s.action_reason AS supervisor_action_reason,
            s.action_json AS supervisor_action_json,
            s.decision_ts AS supervisor_decision_ts,
            (SELECT COUNT(*) FROM decision_ledger x WHERE x.position_id = CAST(c.position_id AS TEXT) AND x.event_type = 'close') AS existing_close_ledger,
            (SELECT COUNT(*) FROM position_lifecycle_event x WHERE x.position_id = CAST(c.position_id AS TEXT) AND x.event_type = 'closed') AS existing_closed_event,
            (SELECT COUNT(*) FROM trade_outcome_review x WHERE x.position_id = CAST(c.position_id AS TEXT)) AS existing_review
        FROM close_positions c
        LEFT JOIN open_decisions d
          ON d.position_id = CAST(c.position_id AS TEXT)
        LEFT JOIN supervisor_decisions s
          ON s.position_id = CAST(c.position_id AS TEXT)
         AND s.decision_ts = (
             SELECT MAX(s2.decision_ts)
             FROM supervisor_decisions s2
             WHERE s2.position_id = CAST(c.position_id AS TEXT)
               AND s2.decision_ts <= c.close_ts + 120
         )
        ORDER BY c.close_ts ASC
        """,
        position_ids,
    ).fetchall()
    return [dict(row) for row in rows]


def _supervisor_evidence(row: dict[str, Any]) -> dict[str, Any]:
    event_type = str(row.get("supervisor_event_type") or "")
    if not event_type:
        return {}
    action_json = _loads(row.get("supervisor_action_json"), {})
    verdict = action_json.get("supervisor_verdict") if isinstance(action_json, dict) else {}
    evidence = dict(verdict or {}) if isinstance(verdict, dict) else {}
    evidence.update(
        {
            "decision_id": str(row.get("supervisor_decision_id") or ""),
            "event_type": event_type,
            "decision_ts": _safe_float(row.get("supervisor_decision_ts")),
            "action_reason": str(row.get("supervisor_action_reason") or ""),
            "action_score": _safe_float(row.get("supervisor_action_score")),
            "source": "decision_ledger",
        }
    )
    return evidence


def _close_reason(row: dict[str, Any]) -> str:
    supervisor_reason = str(row.get("supervisor_action_reason") or "")
    if str(row.get("supervisor_event_type") or "") == "supervisor_close" and supervisor_reason:
        return supervisor_reason
    return "broker_close"


def _real_pnl(row: dict[str, Any], close_price: float) -> dict[str, Any]:
    raw_exec_price = _safe_float(row.get("exec_price"))
    return {
        "gross": _safe_float(row.get("gross_profit")),
        "swap": _safe_float(row.get("swap")),
        "commission": _safe_float(row.get("close_commission")),
        "net": _safe_float(row.get("net_pnl")),
        "entry_price": _safe_float(row.get("entry_price")),
        "exec_price": close_price,
        "raw_exec_price": raw_exec_price,
        "balance": _safe_float(row.get("balance")),
        "deal_id": int(row.get("deal_id") or 0),
        "exec_timestamp": _safe_float(row.get("close_ts")),
    }


def _amend_review_record(record: dict[str, Any], row: dict[str, Any], *, exit_decision_id: str, close_price: float) -> dict[str, Any]:
    review_json = _loads(record.get("review_json"), {})
    close_reason = _close_reason(row)
    close_source = classify_close_source_from_evidence(
        close_reason=close_reason,
        evidence=_supervisor_evidence(row),
    )
    review_json.update(
        {
            "exit_decision_id": exit_decision_id,
            "close_price": close_price,
            "real_pnl": _real_pnl(row, close_price),
            "close_reason": close_reason,
            "close_reason_source": close_source["close_reason_source"],
            "inferred_close_supervisor": close_source["inferred_close_supervisor"],
            "attribution_integrity": "missing",
            "factor_contributions": {},
            "backfill_source": BACKFILL_SOURCE,
        }
    )
    failure_tags = _loads(record.get("failure_tags_json"), [])
    if not isinstance(failure_tags, list):
        failure_tags = []
    for tag in ("attribution_missing",):
        if tag not in failure_tags:
            failure_tags.append(tag)
    if close_source["inferred_close_supervisor"] and "supervisor_entry_feedback" not in failure_tags:
        failure_tags.append("supervisor_entry_feedback")
    record["exit_decision_id"] = exit_decision_id
    record["failure_tags_json"] = _dump(failure_tags)
    record["review_json"] = _dump(review_json)
    return record


def _insert_close_ledger(conn, row: dict[str, Any], *, close_price: float, now: float) -> str:
    decision_id = _new_id("dec_backfill")
    position_id = str(row["position_id"])
    trade_id = str(row.get("trade_id") or position_id)
    close_reason = _close_reason(row)
    close_source = classify_close_source_from_evidence(
        close_reason=close_reason,
        evidence=_supervisor_evidence(row),
    )
    pnl = _safe_float(row.get("net_pnl"))
    real_pnl = _real_pnl(row, close_price)
    payload = {
        "position_id": int(row["position_id"]),
        "pnl": round(pnl, 6),
        "price": close_price,
        "close_reason": close_reason,
        "close_reason_source": close_source["close_reason_source"],
        "inferred_close_supervisor": close_source["inferred_close_supervisor"],
        "attribution_integrity": "missing",
        "factor_contributions": {},
        "real_pnl": real_pnl,
        "backfill_source": BACKFILL_SOURCE,
        "note": "historical close fact repair; no factor snapshots fabricated",
    }
    _execute(
        conn,
        """
        INSERT INTO decision_ledger
        (decision_id, trade_id, position_id, event_type, symbol, timeframe,
         decision_ts, regime_id, portfolio_state_json, risk_state_json,
         action_score, action_reason, action_json, created_at)
        VALUES (?, ?, ?, 'close', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            decision_id,
            trade_id,
            position_id,
            str(row.get("symbol") or "XAUUSD+"),
            str(row.get("timeframe") or "M5"),
            _safe_float(row.get("close_ts")),
            str(row.get("regime_id") or ""),
            _dump({"balance": row.get("balance"), "equity": row.get("balance")}),
            _dump(
                {
                    "backfill": True,
                    "source": BACKFILL_SOURCE,
                    "risk_policy_verdict": {
                        "available": False,
                        "reason": "historical_close_fact_repair_no_new_risk_action",
                    },
                }
            ),
            pnl,
            close_reason,
            _dump(payload),
            now,
        ),
    )
    return decision_id


def _insert_closed_event(conn, row: dict[str, Any], *, close_price: float) -> str:
    event_id = _new_id("posevt_backfill")
    position_id = str(row["position_id"])
    trade_id = str(row.get("trade_id") or position_id)
    details = {
        "real_pnl": _real_pnl(row, close_price),
        "close_reason": _close_reason(row),
        "factor_contributions": {},
        "attribution_integrity": "missing",
        "backfill_source": BACKFILL_SOURCE,
    }
    _execute(
        conn,
        """
        INSERT INTO position_lifecycle_event
        (event_id, position_id, trade_id, symbol, event_type, event_ts,
         net_volume, avg_price, realized_pnl, details_json)
        VALUES (?, ?, ?, ?, 'closed', ?, 0.0, ?, ?, ?)
        """,
        (
            event_id,
            position_id,
            trade_id,
            str(row.get("symbol") or "XAUUSD+"),
            _safe_float(row.get("close_ts")),
            close_price,
            _safe_float(row.get("net_pnl")),
            _dump(details),
        ),
    )
    return event_id


def _insert_experience(conn, review: dict[str, Any]) -> str:
    return str(upsert_trade_lesson_memory(conn, review)["experience_id"])


def _planned_action(row: dict[str, Any]) -> dict[str, Any]:
    close_price = _normalize_deal_price(entry_price=row.get("entry_price"), exec_price=row.get("exec_price"))
    return {
        "position_id": str(row["position_id"]),
        "pnl": round(_safe_float(row.get("net_pnl")), 6),
        "close_ts": _safe_float(row.get("close_ts")),
        "close_price": close_price,
        "has_open_ledger": bool(row.get("entry_decision_id")),
        "close_reason": _close_reason(row),
        "close_reason_source": classify_close_source_from_evidence(
            close_reason=_close_reason(row),
            evidence=_supervisor_evidence(row),
        )["close_reason_source"],
        "will_insert_close_ledger": int(row.get("existing_close_ledger") or 0) == 0,
        "will_insert_closed_event": int(row.get("existing_closed_event") or 0) == 0,
        "will_insert_review": int(row.get("existing_review") or 0) == 0,
        "will_insert_experience": int(row.get("existing_review") or 0) == 0,
    }


def run_backfill(*, position_ids: list[int], apply: bool, db_path: str | Path = STATE_DB) -> dict[str, Any]:
    conn = _connect(db_path)
    try:
        rows = fetch_backfill_rows(conn, position_ids)
        found = {int(row["position_id"]) for row in rows}
        missing = [str(pid) for pid in position_ids if int(pid) not in found]
        if missing:
            raise RuntimeError(f"missing broker close facts for positions: {', '.join(missing)}")
        if any(not row.get("entry_decision_id") for row in rows):
            bad = [str(row["position_id"]) for row in rows if not row.get("entry_decision_id")]
            raise RuntimeError(f"missing open decision ledger for positions: {', '.join(bad)}")
        plan = [_planned_action(row) for row in rows]
        result: dict[str, Any] = {"apply": bool(apply), "source": BACKFILL_SOURCE, "planned": plan, "applied": []}
        if not apply:
            conn.rollback()
            return result

        now = time.time()
        for row in rows:
            close_price = _normalize_deal_price(entry_price=row.get("entry_price"), exec_price=row.get("exec_price"))
            exit_decision_id = ""
            closed_event_id = ""
            review_id = ""
            experience_id = ""
            if int(row.get("existing_close_ledger") or 0) == 0:
                exit_decision_id = _insert_close_ledger(conn, row, close_price=close_price, now=now)
            if int(row.get("existing_closed_event") or 0) == 0:
                closed_event_id = _insert_closed_event(conn, row, close_price=close_price)
            if int(row.get("existing_review") or 0) == 0:
                record = learning_backfill.build_review_record(row)
                record = _amend_review_record(record, row, exit_decision_id=exit_decision_id, close_price=close_price)
                learning_backfill.insert_review(conn, record)
                review_id = str(record["review_id"])
                experience_id = _insert_experience(conn, record)
            result["applied"].append(
                {
                    "position_id": str(row["position_id"]),
                    "exit_decision_id": exit_decision_id,
                    "closed_event_id": closed_event_id,
                    "review_id": review_id,
                    "experience_id": experience_id,
                }
            )
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _parse_position_ids(values: list[str]) -> list[int]:
    ids: list[int] = []
    for value in values:
        for part in str(value).split(","):
            part = part.strip()
            if part:
                ids.append(int(part))
    if not ids:
        raise SystemExit("at least one --position-id is required")
    return ids


def main() -> None:
    parser = argparse.ArgumentParser(description="Controlled backfill for failed close ledger/review learning facts.")
    parser.add_argument("--position-id", action="append", default=[], help="Position id to repair; may be repeated or comma-separated.")
    parser.add_argument("--apply", action="store_true", help="Write repairs. Omit for dry-run.")
    parser.add_argument("--db-path", default=str(STATE_DB), help="State DB path; default uses PostgreSQL state when configured.")
    args = parser.parse_args()
    result = run_backfill(
        position_ids=_parse_position_ids(args.position_id),
        apply=bool(args.apply),
        db_path=args.db_path,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
