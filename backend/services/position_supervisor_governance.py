from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from backend.core.db import STATE_DB, connect_sqlite
from backend.services.position_supervisor import evaluate_position_supervisor
from backend.services.position_supervisor_templates import (
    CONSERVATIVE_TEMPLATE_ID,
    DEFAULT_TEMPLATE_ID,
    get_position_supervisor_template,
    list_position_supervisor_templates,
)


LOCAL_TZ = ZoneInfo("Asia/Shanghai")


def _loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return float(default)


def _day_bounds(day: str) -> tuple[float, float]:
    base = datetime.fromisoformat(str(day or "").strip()).replace(tzinfo=LOCAL_TZ)
    end = base + timedelta(days=1)
    return base.timestamp(), end.timestamp()


def _direction_from_review(payload: dict[str, Any]) -> int:
    real_pnl = payload.get("real_pnl") or {}
    entry_price = _safe_float(real_pnl.get("entry_price") or payload.get("entry_price"))
    close_price = _safe_float(payload.get("close_price") or real_pnl.get("exec_price"))
    pnl = _safe_float(real_pnl.get("gross") or real_pnl.get("net") or payload.get("pnl"))
    price_delta = close_price - entry_price
    if abs(price_delta) < 1e-9 or abs(pnl) < 1e-9:
        return 1
    return 1 if price_delta * pnl >= 0 else -1


def _position_prices(conn: sqlite3.Connection, position_id: str) -> dict[str, float]:
    row = conn.execute(
        """
        SELECT details_json
        FROM position_lifecycle_event
        WHERE position_id=? AND event_type='opened'
        ORDER BY event_ts ASC
        LIMIT 1
        """,
        (str(position_id),),
    ).fetchone()
    details = _loads(row["details_json"], {}) if row else {}
    return {
        "sl": _safe_float(details.get("sl")),
        "tp": _safe_float(details.get("tp")),
    }


def _review_to_supervisor_context(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    payload = _loads(row["review_json"], {})
    real_pnl = payload.get("real_pnl") or {}
    position_id = str(row["position_id"] or payload.get("position_id") or "")
    prices = _position_prices(conn, position_id)
    entry_price = _safe_float(real_pnl.get("entry_price") or payload.get("entry_price"))
    current_price = _safe_float(payload.get("close_price") or real_pnl.get("exec_price"))
    return {
        "position": {
            "position_id": position_id,
            "direction": _direction_from_review(payload),
            "entry_price": entry_price,
            "current_price": current_price,
            "volume": 100.0,
            "unrealized_pnl": _safe_float(row["pnl"]),
            "sl": prices["sl"],
            "tp": prices["tp"],
        },
        "risk": {
            "max_holding_seconds": 0.0,
            "holding_timeout_ratio": 0.0,
            "mfe": _safe_float(row["mfe"] if row["mfe"] is not None else payload.get("mfe")),
            "mae": _safe_float(row["mae"] if row["mae"] is not None else payload.get("mae")),
            "giveback_ratio": _safe_float(payload.get("giveback_ratio")),
            "profit_capture_ratio": _safe_float(payload.get("profit_capture_ratio")),
            "time_in_profit": _safe_float(payload.get("time_in_profit") or payload.get("time_in_profit_seconds")),
            "holding_efficiency": _safe_float(payload.get("holding_efficiency")),
            "time_decay_score": _safe_float(payload.get("time_decay_score")),
            "thesis_status": str(payload.get("thesis_status_at_exit") or payload.get("thesis_status") or "intact"),
            "regime_shift": str(payload.get("regime_shift_at_exit") or payload.get("regime_shift") or "none"),
        },
        "temporal_context": {
            "decision_ts": _safe_float(payload.get("close_ts") or row["created_at"]),
            "holding_seconds": _safe_float(payload.get("holding_seconds")),
        },
        "market_space_context": {
            "distance_to_sl": abs(current_price - prices["sl"]) if current_price > 0 and prices["sl"] > 0 else 0.0,
            "distance_to_tp": abs(prices["tp"] - current_price) if current_price > 0 and prices["tp"] > 0 else 0.0,
        },
        "entry_context": {},
        "runtime": {},
    }


def _load_review_rows(
    conn: sqlite3.Connection,
    *,
    day: str,
    small_abs_pnl: float,
    limit: int,
) -> list[sqlite3.Row]:
    start_ts, end_ts = _day_bounds(day)
    return conn.execute(
        """
        SELECT review_id, trade_id, position_id, entry_decision_id, exit_decision_id,
               pnl, mae, mfe, outcome_label, failure_tags_json, summary_text,
               review_json, created_at
        FROM trade_outcome_review
        WHERE created_at >= ? AND created_at < ?
          AND ABS(COALESCE(pnl, 0.0)) <= ?
        ORDER BY created_at ASC
        LIMIT ?
        """,
        (start_ts, end_ts, float(small_abs_pnl), int(limit)),
    ).fetchall()


def _amend_issue_count(conn: sqlite3.Connection, *, day: str) -> dict[str, int]:
    start_ts, end_ts = _day_bounds(day)
    rows = conn.execute(
        """
        SELECT event_type, COUNT(*) AS n
        FROM position_lifecycle_event
        WHERE event_ts >= ? AND event_ts < ?
          AND event_type IN ('amend_failed', 'amend_skipped')
        GROUP BY event_type
        """,
        (start_ts, end_ts),
    ).fetchall()
    return {str(row["event_type"]): int(row["n"] or 0) for row in rows}


def replay_position_supervisor_templates(
    *,
    day: str = "2026-06-26",
    db_path: str | Path = STATE_DB,
    small_abs_pnl: float = 5.0,
    limit: int = 200,
) -> dict[str, Any]:
    conn = connect_sqlite(db_path, read_only=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = _load_review_rows(conn, day=day, small_abs_pnl=small_abs_pnl, limit=limit)
        templates = list_position_supervisor_templates()
        template_summaries: dict[str, dict[str, Any]] = {}
        samples: list[dict[str, Any]] = []
        for template in templates:
            template_id = str(template.get("template_id") or "")
            template_summaries[template_id] = {
                "template_id": template_id,
                "template_version": str(template.get("template_version") or ""),
                "template_role": str(template.get("template_role") or ""),
                "actions": {"hold": 0, "tighten": 0, "reduce": 0, "close": 0},
                "small_loss_close_count": 0,
                "thesis_broken_close_count": 0,
                "avg_confidence": 0.0,
                "confidence_sum": 0.0,
            }
        for row in rows:
            context = _review_to_supervisor_context(conn, row)
            review_payload = _loads(row["review_json"], {})
            sample_actions: dict[str, Any] = {}
            for template in templates:
                template_id = str(template.get("template_id") or "")
                replay_context = {**context, "position_supervisor_template": template}
                verdict = evaluate_position_supervisor(replay_context)
                action = str(verdict.get("action") or "hold")
                summary = template_summaries[template_id]
                summary["actions"][action] = int(summary["actions"].get(action, 0)) + 1
                summary["confidence_sum"] += _safe_float(verdict.get("confidence"))
                if action == "close" and abs(_safe_float(row["pnl"])) <= small_abs_pnl:
                    summary["small_loss_close_count"] += 1
                if action == "close" and str(verdict.get("summary_reason") or "") == "thesis_broken":
                    summary["thesis_broken_close_count"] += 1
                sample_actions[template_id] = {
                    "action": action,
                    "summary_reason": str(verdict.get("summary_reason") or ""),
                    "confidence": _safe_float(verdict.get("confidence")),
                    "trigger_tags": list((verdict.get("evidence") or {}).get("trigger_tags") or []),
                }
            samples.append(
                {
                    "review_id": str(row["review_id"] or ""),
                    "position_id": str(row["position_id"] or ""),
                    "pnl": _safe_float(row["pnl"]),
                    "mfe": _safe_float(row["mfe"]),
                    "mae": _safe_float(row["mae"]),
                    "close_reason": str(review_payload.get("close_reason") or ""),
                    "holding_seconds": _safe_float(review_payload.get("holding_seconds")),
                    "holding_efficiency": _safe_float(review_payload.get("holding_efficiency")),
                    "profit_capture_ratio": _safe_float(review_payload.get("profit_capture_ratio")),
                    "template_actions": sample_actions,
                }
            )
        total = max(1, len(rows))
        for summary in template_summaries.values():
            summary["avg_confidence"] = round(float(summary.pop("confidence_sum", 0.0)) / total, 4)
        default_close = int(template_summaries.get(DEFAULT_TEMPLATE_ID, {}).get("small_loss_close_count") or 0)
        conservative_close = int(template_summaries.get(CONSERVATIVE_TEMPLATE_ID, {}).get("small_loss_close_count") or 0)
        return {
            "schema_version": "position_supervisor_replay.v1",
            "day": day,
            "sample_filter": {"abs_pnl_lte": float(small_abs_pnl), "limit": int(limit)},
            "sample_count": len(rows),
            "amend_issues": _amend_issue_count(conn, day=day),
            "templates": list(template_summaries.values()),
            "comparison": {
                "default_template_id": DEFAULT_TEMPLATE_ID,
                "candidate_template_id": CONSERVATIVE_TEMPLATE_ID,
                "small_loss_close_delta": conservative_close - default_close,
                "small_loss_closes_reduced": max(0, default_close - conservative_close),
            },
            "samples": samples,
        }
    finally:
        conn.close()


def build_position_supervisor_advisories(
    *,
    day: str = "2026-06-26",
    db_path: str | Path = STATE_DB,
    materialize: bool = False,
) -> dict[str, Any]:
    replay = replay_position_supervisor_templates(day=day, db_path=db_path)
    default_summary = next((x for x in replay["templates"] if x["template_id"] == DEFAULT_TEMPLATE_ID), {})
    candidate_summary = next((x for x in replay["templates"] if x["template_id"] == CONSERVATIVE_TEMPLATE_ID), {})
    amend_issues = replay.get("amend_issues") or {}
    suggestions: list[dict[str, Any]] = []

    def _add(action: str, confidence: float, reason: str, evidence: dict[str, Any]) -> None:
        suggestion_id = "psv_" + hashlib.sha1(f"{day}:{action}:{reason}".encode("utf-8")).hexdigest()[:16]
        suggestions.append(
            {
                "suggestion_id": suggestion_id,
                "scope_type": "position_supervisor_template",
                "scope_key": CONSERVATIVE_TEMPLATE_ID if action != "fix_stop_legality" else "supervisor_tighten_sltp",
                "action": action,
                "confidence": round(confidence, 4),
                "reason": reason,
                "evidence": evidence,
                "status": "proposed",
                "advisory_only": True,
                "approval_path": "governor_review_then_offline_replay",
            }
        )

    if int(replay["comparison"].get("small_loss_closes_reduced") or 0) > 0:
        _add(
            "relax_thesis_break",
            0.76,
            "conservative supervisor template reduces small-loss full exits in offline replay",
            {
                "day": day,
                "default_small_loss_close_count": default_summary.get("small_loss_close_count"),
                "candidate_small_loss_close_count": candidate_summary.get("small_loss_close_count"),
                "candidate_template_id": CONSERVATIVE_TEMPLATE_ID,
            },
        )
    if int(default_summary.get("actions", {}).get("tighten", 0) or 0) > 0:
        _add(
            "tighten_profit_protection",
            0.68,
            "historical samples show frequent tighten/reduce pressure before exits",
            {
                "day": day,
                "default_actions": default_summary.get("actions") or {},
                "candidate_actions": candidate_summary.get("actions") or {},
            },
        )
    if int(default_summary.get("thesis_broken_close_count") or 0) >= 3:
        _add(
            "increase_min_hold_window",
            0.64,
            "multiple thesis-broken exits are small and early enough to require a minimum evidence window",
            {
                "day": day,
                "thesis_broken_close_count": default_summary.get("thesis_broken_close_count"),
                "candidate_min_thesis_break_seconds": get_position_supervisor_template(CONSERVATIVE_TEMPLATE_ID).get("thresholds", {}).get("min_thesis_break_seconds"),
            },
        )
    if int(amend_issues.get("amend_failed", 0) or 0) > 0 or int(amend_issues.get("amend_skipped", 0) or 0) > 0:
        _add(
            "fix_stop_legality",
            0.82,
            "supervisor protection amendments had broker-side skip/failure evidence",
            {"day": day, "amend_issues": amend_issues},
        )

    if materialize and suggestions:
        conn = connect_sqlite(db_path)
        try:
            now_ts = time.time()
            for item in suggestions:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO policy_suggestion
                    (suggestion_id, scope_type, scope_key, action, confidence, reason,
                     evidence_json, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(
                        (SELECT status FROM policy_suggestion WHERE suggestion_id=?),
                        'proposed'
                    ), COALESCE(
                        (SELECT created_at FROM policy_suggestion WHERE suggestion_id=?),
                        ?
                    ))
                    """,
                    (
                        item["suggestion_id"],
                        item["scope_type"],
                        item["scope_key"],
                        item["action"],
                        float(item["confidence"]),
                        item["reason"],
                        json.dumps(item["evidence"], ensure_ascii=False),
                        item["suggestion_id"],
                        item["suggestion_id"],
                        now_ts,
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    return {
        "schema_version": "position_supervisor_advisory.v1",
        "day": day,
        "advisory_only": True,
        "materialized": bool(materialize),
        "replay_summary": {
            "sample_count": replay.get("sample_count"),
            "comparison": replay.get("comparison"),
            "amend_issues": replay.get("amend_issues"),
        },
        "items": suggestions,
    }
