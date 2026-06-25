"""Phase A closed-loop health check.

Checks whether live decisions, broker lifecycle, reviews, learning memory, and
application effects are connected well enough for demo-account stabilization.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.core.db import STATE_DB, STATE_DB_DDL


def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(STATE_DB_DDL)
    return conn


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0] or 0) if row else 0


def _rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _json_loads(raw: str | None, default: Any) -> Any:
    try:
        return json.loads(raw or "")
    except Exception:
        return default


def run_check(*, db_path: str | Path = STATE_DB, hours: float = 24.0, limit: int = 20) -> dict:
    since = time.time() - float(hours) * 3600.0
    conn = _connect(db_path)
    try:
        counts = {
            "decision_total": _scalar(conn, "SELECT COUNT(*) FROM decision_ledger"),
            "decision_recent": _scalar(conn, "SELECT COUNT(*) FROM decision_ledger WHERE decision_ts >= ?", (since,)),
            "signal_recent": _scalar(conn, "SELECT COUNT(*) FROM decision_ledger WHERE event_type='signal' AND decision_ts >= ?", (since,)),
            "open_recent": _scalar(conn, "SELECT COUNT(*) FROM decision_ledger WHERE event_type='open' AND decision_ts >= ?", (since,)),
            "close_recent": _scalar(conn, "SELECT COUNT(*) FROM decision_ledger WHERE event_type='close' AND decision_ts >= ?", (since,)),
            "skip_recent": _scalar(conn, "SELECT COUNT(*) FROM decision_ledger WHERE event_type='skip' AND decision_ts >= ?", (since,)),
            "order_failed_recent": _scalar(conn, "SELECT COUNT(*) FROM decision_ledger WHERE event_type='order_failed' AND decision_ts >= ?", (since,)),
            "amend_failed_recent": _scalar(conn, "SELECT COUNT(*) FROM decision_ledger WHERE event_type='amend_failed' AND decision_ts >= ?", (since,)),
            "reviews_total": _scalar(conn, "SELECT COUNT(*) FROM trade_outcome_review"),
            "reviews_recent": _scalar(conn, "SELECT COUNT(*) FROM trade_outcome_review WHERE created_at >= ?", (since,)),
            "experience_total": _scalar(conn, "SELECT COUNT(*) FROM experience_memory"),
            "suggestions_total": _scalar(conn, "SELECT COUNT(*) FROM policy_suggestion"),
            "applications_total": _scalar(conn, "SELECT COUNT(*) FROM learning_application_log"),
            "effects_total": _scalar(conn, "SELECT COUNT(*) FROM learning_application_effect"),
            "open_recovery_positions": _scalar(conn, "SELECT COUNT(*) FROM recovery_position_state WHERE status='open'"),
            "closed_recovery_positions_recent": _scalar(conn, "SELECT COUNT(*) FROM recovery_position_state WHERE status='closed' AND closed_at >= ?", (since,)),
        }

        missing_review = _rows(
            conn,
            """
            SELECT d.position_id, d.trade_id, d.decision_ts, d.action_reason
            FROM decision_ledger d
            LEFT JOIN trade_outcome_review r ON r.position_id = d.position_id
            WHERE d.event_type='close'
              AND d.decision_ts >= ?
              AND d.position_id != ''
              AND r.position_id IS NULL
            ORDER BY d.decision_ts DESC
            LIMIT ?
            """,
            (since, int(limit)),
        )
        closed_without_open = _rows(
            conn,
            """
            SELECT c.position_id, c.trade_id, c.decision_ts, c.action_reason
            FROM decision_ledger c
            LEFT JOIN decision_ledger o
              ON o.position_id = c.position_id AND o.event_type='open'
            WHERE c.event_type='close'
              AND c.decision_ts >= ?
              AND c.position_id != ''
              AND o.decision_id IS NULL
            ORDER BY c.decision_ts DESC
            LIMIT ?
            """,
            (since, int(limit)),
        )
        broker_closes_without_review = _rows(
            conn,
            """
            WITH closes AS (
                SELECT position_id, MAX(exec_timestamp) AS close_ts
                FROM ctrader_deals
                WHERE is_close=1
                GROUP BY position_id
            )
            SELECT c.position_id, c.close_ts
            FROM closes c
            LEFT JOIN trade_outcome_review r
              ON CAST(r.position_id AS INTEGER) = c.position_id
            WHERE c.close_ts >= ?
              AND r.position_id IS NULL
            ORDER BY c.close_ts DESC
            LIMIT ?
            """,
            (since, int(limit)),
        )
        recent_failures = _rows(
            conn,
            """
            SELECT decision_id, position_id, event_type, decision_ts, action_reason, action_json
            FROM decision_ledger
            WHERE event_type IN ('skip', 'order_failed', 'amend_failed')
              AND decision_ts >= ?
            ORDER BY decision_ts DESC
            LIMIT ?
            """,
            (since, int(limit)),
        )
        for row in recent_failures:
            row["action"] = _json_loads(row.pop("action_json", "{}"), {})

        issues = []
        if counts["amend_failed_recent"] > 0:
            issues.append({
                "severity": "warn",
                "code": "recent_amend_failed",
                "message": f"{counts['amend_failed_recent']} recent SL/TP amend failures need broker/log review",
            })
        if missing_review:
            issues.append({
                "severity": "warn",
                "code": "close_without_review",
                "message": f"{len(missing_review)} recent close decisions have no trade review",
            })
        if closed_without_open:
            issues.append({
                "severity": "error",
                "code": "close_without_open",
                "message": f"{len(closed_without_open)} recent close decisions have no open ledger entry",
            })
        if broker_closes_without_review:
            issues.append({
                "severity": "warn",
                "code": "broker_close_without_review",
                "message": f"{len(broker_closes_without_review)} recent broker close deals have no review",
            })

        status = "healthy"
        if any(item["severity"] == "error" for item in issues):
            status = "blocked"
        elif issues:
            status = "needs_review"

        return {
            "ok": status != "blocked",
            "status": status,
            "db_path": str(db_path),
            "window_hours": hours,
            "counts": counts,
            "issues": issues,
            "samples": {
                "missing_review": missing_review,
                "closed_without_open": closed_without_open,
                "broker_closes_without_review": broker_closes_without_review,
                "recent_failures": recent_failures,
            },
        }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Phase A closed-loop health checks.")
    parser.add_argument("--db", default=str(STATE_DB), help="Path to state.db")
    parser.add_argument("--hours", type=float, default=24.0, help="Lookback window in hours")
    parser.add_argument("--limit", type=int, default=20, help="Maximum sample rows per issue")
    args = parser.parse_args()
    result = run_check(db_path=args.db, hours=args.hours, limit=args.limit)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
