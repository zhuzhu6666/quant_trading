"""Repair nested decision temporal contexts in decision_ledger.

The canonical market time for a decision is decision_ledger.decision_ts
(UTC epoch seconds).  Runtime audit payloads may also carry evaluated_at, but
their temporal_context.decision_ts must not drift from the ledger market time.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.core.db import STATE_DB, connect_sqlite, get_state_pg_conn, is_state_db_path
from backend.ledger.service import _normalize_decision_time_payloads


def _use_pg(db_path: str | Path) -> bool:
    return is_state_db_path(db_path)


def _sql(conn, sql: str) -> str:
    return sql.replace("?", "%s") if conn.__class__.__module__.split(".", 1)[0] == "psycopg" else sql


def _loads(raw: Any) -> Any:
    if raw is None:
        return {}
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw or "{}"))
    except Exception:
        return {}


def _dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, default=str)


def _connect(db_path: str | Path):
    if _use_pg(db_path):
        return get_state_pg_conn(read_only=False)
    conn = connect_sqlite(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def repair(*, db_path: str | Path = STATE_DB, apply: bool = False, limit: int = 0) -> dict:
    conn = _connect(db_path)
    try:
        query = """
            SELECT decision_id, timeframe, decision_ts, risk_state_json, action_json
            FROM decision_ledger
            ORDER BY created_at ASC, decision_ts ASC
        """
        params: tuple[Any, ...] = ()
        if limit and limit > 0:
            query += " LIMIT ?"
            params = (int(limit),)
        rows = conn.execute(_sql(conn, query), params).fetchall()
        checked = 0
        changed = 0
        parse_empty = 0
        samples: list[dict] = []
        for row in rows:
            item = dict(row)
            checked += 1
            risk_state = _loads(item.get("risk_state_json"))
            action_json = _loads(item.get("action_json"))
            if not risk_state and not action_json:
                parse_empty += 1
            normalized_risk, normalized_action = _normalize_decision_time_payloads(
                risk_state=risk_state,
                action_json=action_json,
                decision_ts=float(item.get("decision_ts") or 0.0),
                timeframe=str(item.get("timeframe") or ""),
            )
            next_risk = _dumps(normalized_risk)
            next_action = _dumps(normalized_action)
            if next_risk == _dumps(risk_state) and next_action == _dumps(action_json):
                continue
            changed += 1
            if len(samples) < 10:
                samples.append(
                    {
                        "decision_id": item.get("decision_id"),
                        "decision_ts": item.get("decision_ts"),
                        "timeframe": item.get("timeframe"),
                    }
                )
            if apply:
                conn.execute(
                    _sql(conn, "UPDATE decision_ledger SET risk_state_json=?, action_json=? WHERE decision_id=?"),
                    (next_risk, next_action, item.get("decision_id")),
                )
        if apply:
            conn.commit()
        return {
            "ok": True,
            "applied": bool(apply),
            "checked": checked,
            "changed": changed,
            "parse_empty": parse_empty,
            "samples": samples,
        }
    except Exception:
        if apply:
            conn.rollback()
        raise
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair decision_ledger nested temporal contexts.")
    parser.add_argument("--db", default=str(STATE_DB), help="State DB path; runtime default uses PostgreSQL state store")
    parser.add_argument("--apply", action="store_true", help="Persist fixes. Without this, only reports changes.")
    parser.add_argument("--limit", type=int, default=0, help="Optional max rows to inspect")
    args = parser.parse_args()
    result = repair(db_path=args.db, apply=args.apply, limit=args.limit)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
