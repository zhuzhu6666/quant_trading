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

from backend.core.db import STATE_DB, STATE_DB_DDL, ensure_sqlite_columns


def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(STATE_DB_DDL)
    conn.commit()
    conn.close()
    ensure_sqlite_columns(
        db_path,
        "experience_memory",
        {
            "source_table": "source_table TEXT DEFAULT ''",
            "source_id": "source_id TEXT DEFAULT ''",
            "append_source": "append_source TEXT DEFAULT ''",
            "evolution_run_id": "evolution_run_id TEXT DEFAULT ''",
        },
    )
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0] or 0) if row else 0


def _rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def _json_loads(raw: str | None, default: Any) -> Any:
    try:
        return json.loads(raw or "")
    except Exception:
        return default


def _evidence_contract_counts(conn: sqlite3.Connection, limit: int = 10000) -> dict[str, int]:
    counts = {
        "checked": 0,
        "non_matured_allows_supervised_training": 0,
        "model_ready_invalid": 0,
        "parse_errors": 0,
    }
    if not _table_exists(conn, "autonomous_learning_sample"):
        return counts
    rows = conn.execute(
        """
        SELECT label_status, integrity, features_json, label_json, trace_json, evidence_contract_json
        FROM autonomous_learning_sample
        ORDER BY updated_at DESC, created_at DESC
        LIMIT ?
        """,
        (max(1, int(limit)),),
    ).fetchall()
    for row in rows:
        counts["checked"] += 1
        try:
            contract = json.loads(row["evidence_contract_json"] or "{}")
        except Exception:
            contract = {}
            counts["parse_errors"] += 1
        allowed = set(contract.get("allowed_uses") or [])
        label_status = str(row["label_status"] or "")
        integrity = str(row["integrity"] or "")
        complete = bool(_json_loads(row["features_json"], {})) and bool(_json_loads(row["label_json"], {})) and bool(_json_loads(row["trace_json"], {}))
        if label_status != "matured" and "supervised_training" in allowed:
            counts["non_matured_allows_supervised_training"] += 1
        if bool(contract.get("model_ready")) and (
            label_status != "matured"
            or integrity == "missing"
            or not complete
            or "supervised_training" not in allowed
        ):
            counts["model_ready_invalid"] += 1
    return counts


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
            "active_recovery_positions": _scalar(conn, "SELECT COUNT(*) FROM recovery_position_state WHERE status IN ('open', 'recovered')"),
            "closed_recovery_positions_recent": _scalar(conn, "SELECT COUNT(*) FROM recovery_position_state WHERE status IN ('closed', 'closed_replayed') AND closed_at >= ?", (since,)),
        }
        evidence_contract_counts = _evidence_contract_counts(conn)
        missing_close_source_total = _scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM trade_outcome_review
            WHERE COALESCE(json_extract(review_json, '$.close_reason_source'), '') = ''
            """,
        )
        missing_review_integrity_total = _scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM trade_outcome_review
            WHERE COALESCE(json_extract(review_json, '$.attribution_integrity'), json_extract(review_json, '$.context_integrity'), '') = ''
            """,
        )
        experience_missing_source_total = _scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM experience_memory
            WHERE COALESCE(source_table, '') = ''
               OR COALESCE(source_id, '') = ''
               OR COALESCE(append_source, '') = ''
            """,
        )
        review_broker_time_mismatch_total = _scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM trade_outcome_review r
            JOIN ctrader_deals d
              ON d.deal_id = CAST(json_extract(r.review_json, '$.real_pnl.deal_id') AS INTEGER)
            WHERE COALESCE(json_extract(r.review_json, '$.real_pnl.deal_id'), '') != ''
              AND ABS(COALESCE(r.created_at, 0) - COALESCE(d.exec_timestamp, 0)) > 5.0
            """,
        )
        duplicate_review_deal_total = _scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM (
                SELECT CAST(json_extract(review_json, '$.real_pnl.deal_id') AS INTEGER) AS deal_id,
                       COUNT(*) AS n
                FROM trade_outcome_review
                WHERE COALESCE(json_extract(review_json, '$.real_pnl.deal_id'), '') != ''
                GROUP BY deal_id
                HAVING n > 1
            )
            """,
        )
        experience_event_time_mismatch_total = _scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM experience_memory e
            JOIN trade_outcome_review r
              ON e.source_table='trade_outcome_review'
             AND e.source_id = r.review_id
            WHERE ABS(COALESCE(e.created_at, 0) - COALESCE(r.created_at, 0)) > 5.0
            """,
        )
        policy_suggestion_dangling_experience_total = _scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM policy_suggestion p
            WHERE json_extract(p.evidence_json, '$.experience_id') IS NOT NULL
              AND json_extract(p.evidence_json, '$.experience_id') != ''
              AND NOT EXISTS (
                  SELECT 1
                  FROM experience_memory e
                  WHERE e.experience_id = json_extract(p.evidence_json, '$.experience_id')
              )
            """,
        )
        counts.update(
            {
                "evidence_contract_checked": evidence_contract_counts["checked"],
                "evidence_contract_non_matured_allows_supervised_training": evidence_contract_counts["non_matured_allows_supervised_training"],
                "evidence_contract_model_ready_invalid": evidence_contract_counts["model_ready_invalid"],
                "evidence_contract_parse_errors": evidence_contract_counts["parse_errors"],
                "reviews_missing_close_reason_source": missing_close_source_total,
                "reviews_missing_integrity": missing_review_integrity_total,
                "experience_missing_source": experience_missing_source_total,
                "review_broker_time_mismatch": review_broker_time_mismatch_total,
                "duplicate_review_deal": duplicate_review_deal_total,
                "experience_event_time_mismatch": experience_event_time_mismatch_total,
                "policy_suggestion_dangling_experience": policy_suggestion_dangling_experience_total,
            }
        )

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
                    WHERE is_close=1 OR closed_volume > 0
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
        active_recovery_without_entry = _rows(
            conn,
            """
            SELECT position_id, status, symbol, direction, open_price, volume, context_integrity, last_seen_at
            FROM recovery_position_state
            WHERE status IN ('open', 'recovered')
              AND COALESCE(entry_decision_id, '') = ''
            ORDER BY last_seen_at DESC
            LIMIT ?
            """,
            (int(limit),),
        )
        review_broker_time_mismatches = _rows(
            conn,
            """
            SELECT r.review_id, r.position_id,
                   json_extract(r.review_json, '$.real_pnl.deal_id') AS deal_id,
                   r.created_at AS review_created_at,
                   d.exec_timestamp AS broker_exec_timestamp,
                   ABS(COALESCE(r.created_at, 0) - COALESCE(d.exec_timestamp, 0)) AS drift_seconds
            FROM trade_outcome_review r
            JOIN ctrader_deals d
              ON d.deal_id = CAST(json_extract(r.review_json, '$.real_pnl.deal_id') AS INTEGER)
            WHERE COALESCE(json_extract(r.review_json, '$.real_pnl.deal_id'), '') != ''
              AND ABS(COALESCE(r.created_at, 0) - COALESCE(d.exec_timestamp, 0)) > 5.0
            ORDER BY drift_seconds DESC
            LIMIT ?
            """,
            (int(limit),),
        )
        duplicate_review_deals = _rows(
            conn,
            """
            SELECT CAST(json_extract(review_json, '$.real_pnl.deal_id') AS INTEGER) AS deal_id,
                   GROUP_CONCAT(review_id) AS review_ids,
                   COUNT(*) AS review_count
            FROM trade_outcome_review
            WHERE COALESCE(json_extract(review_json, '$.real_pnl.deal_id'), '') != ''
            GROUP BY deal_id
            HAVING review_count > 1
            ORDER BY review_count DESC
            LIMIT ?
            """,
            (int(limit),),
        )
        experience_event_time_mismatches = _rows(
            conn,
            """
            SELECT e.experience_id, e.trade_id, e.source_id,
                   e.created_at AS experience_created_at,
                   r.created_at AS review_created_at,
                   ABS(COALESCE(e.created_at, 0) - COALESCE(r.created_at, 0)) AS drift_seconds
            FROM experience_memory e
            JOIN trade_outcome_review r
              ON e.source_table='trade_outcome_review'
             AND e.source_id = r.review_id
            WHERE ABS(COALESCE(e.created_at, 0) - COALESCE(r.created_at, 0)) > 5.0
            ORDER BY drift_seconds DESC
            LIMIT ?
            """,
            (int(limit),),
        )

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
        if active_recovery_without_entry:
            issues.append({
                "severity": "error",
                "code": "active_recovery_without_entry",
                "message": f"{len(active_recovery_without_entry)} active recovery positions have no entry decision",
            })
        if evidence_contract_counts["non_matured_allows_supervised_training"] > 0:
            issues.append({
                "severity": "warn",
                "code": "evidence_contract_non_matured_training_allowed",
                "message": (
                    f"{evidence_contract_counts['non_matured_allows_supervised_training']} non-matured learning samples "
                    "still allow supervised_training; run repair_evidence_contracts"
                ),
            })
        if evidence_contract_counts["model_ready_invalid"] > 0 or evidence_contract_counts["parse_errors"] > 0:
            issues.append({
                "severity": "error",
                "code": "evidence_contract_invalid_model_ready",
                "message": (
                    f"{evidence_contract_counts['model_ready_invalid']} samples have invalid model_ready contract and "
                    f"{evidence_contract_counts['parse_errors']} contracts failed JSON parsing"
                ),
            })
        if missing_close_source_total > 0:
            issues.append({
                "severity": "warn",
                "code": "review_missing_close_reason_source",
                "message": f"{missing_close_source_total} trade reviews have no close_reason_source; run close source backfill",
            })
        if missing_review_integrity_total > 0:
            issues.append({
                "severity": "warn",
                "code": "review_missing_integrity",
                "message": f"{missing_review_integrity_total} trade reviews have no attribution/context integrity marker",
            })
        if experience_missing_source_total > 0:
            issues.append({
                "severity": "warn",
                "code": "experience_missing_source",
                "message": f"{experience_missing_source_total} experience_memory rows have no source_table/source_id/append_source",
            })
        if review_broker_time_mismatch_total > 0:
            issues.append({
                "severity": "error",
                "code": "review_broker_time_mismatch",
                "message": f"{review_broker_time_mismatch_total} trade reviews use a timestamp different from the broker close deal",
            })
        if duplicate_review_deal_total > 0:
            issues.append({
                "severity": "error",
                "code": "duplicate_review_deal",
                "message": f"{duplicate_review_deal_total} broker close deals have duplicate trade reviews",
            })
        if experience_event_time_mismatch_total > 0:
            issues.append({
                "severity": "warn",
                "code": "experience_event_time_mismatch",
                "message": f"{experience_event_time_mismatch_total} experience_memory rows use a timestamp different from their source review",
            })
        if policy_suggestion_dangling_experience_total > 0:
            issues.append({
                "severity": "error",
                "code": "policy_suggestion_dangling_experience",
                "message": f"{policy_suggestion_dangling_experience_total} policy suggestions reference missing experience rows",
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
                "review_broker_time_mismatches": review_broker_time_mismatches,
                "duplicate_review_deals": duplicate_review_deals,
                "experience_event_time_mismatches": experience_event_time_mismatches,
                "recent_failures": recent_failures,
                "active_recovery_without_entry": active_recovery_without_entry,
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
