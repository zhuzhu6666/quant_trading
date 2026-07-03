from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from backend.core.db import STATE_DB, connect_sqlite, get_state_pg_conn, is_state_db_path
from backend.services.position_supervisor import evaluate_position_supervisor
from backend.services.position_supervisor_templates import (
    CONSERVATIVE_TEMPLATE_ID,
    DEFAULT_TEMPLATE_ID,
    PROFIT_PROTECTION_TEMPLATE_ID,
    get_position_supervisor_template,
    list_position_supervisor_templates,
)


LOCAL_TZ = ZoneInfo("Asia/Shanghai")


def _use_pg(db_path: str | Path = STATE_DB) -> bool:
    return is_state_db_path(db_path)


def _connect(db_path: str | Path = STATE_DB, *, read_only: bool = False):
    conn = get_state_pg_conn(read_only=read_only) if _use_pg(db_path) else connect_sqlite(db_path, read_only=read_only)
    if not _use_pg(db_path):
        conn.row_factory = sqlite3.Row
    return conn


def _conn_is_pg(conn) -> bool:
    return conn.__class__.__module__.split(".", 1)[0] == "psycopg"


def _sql(conn, sql: str) -> str:
    return sql.replace("%", "%%").replace("?", "%s") if _conn_is_pg(conn) else sql


def _execute(conn, sql: str, params: Any = None):
    if params is None:
        return conn.execute(_sql(conn, sql))
    return conn.execute(_sql(conn, sql), params)


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
    pnl = _safe_float(real_pnl.get("net") or real_pnl.get("gross") or payload.get("pnl"))
    price_delta = close_price - entry_price
    if abs(price_delta) < 1e-9 or abs(pnl) < 1e-9:
        return 1
    return 1 if price_delta * pnl >= 0 else -1


def _position_prices(conn: sqlite3.Connection, position_id: str) -> dict[str, float]:
    row = _execute(
        conn,
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
    return _execute(
        conn,
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
    rows = _execute(
        conn,
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


def _counterfactual_summary(conn: sqlite3.Connection, *, day: str) -> dict[str, Any]:
    start_ts, end_ts = _day_bounds(day)
    try:
        rows = _execute(
            conn,
            """
            SELECT label, supervisor_event_type, COUNT(*) AS n
            FROM supervisor_counterfactual_review
            WHERE updated_at >= ? AND updated_at < ?
            GROUP BY label, supervisor_event_type
            ORDER BY n DESC
            """,
            (start_ts, end_ts),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    labels: dict[str, int] = {}
    events: dict[str, int] = {}
    for row in rows:
        label = str(row["label"] or "")
        event_type = str(row["supervisor_event_type"] or "")
        n = int(row["n"] or 0)
        labels[label] = labels.get(label, 0) + n
        events[event_type] = events.get(event_type, 0) + n
    return {
        "day": day,
        "total": sum(labels.values()),
        "labels": labels,
        "events": events,
    }


def replay_position_supervisor_templates(
    *,
    day: str = "2026-06-26",
    db_path: str | Path = STATE_DB,
    small_abs_pnl: float = 5.0,
    limit: int = 200,
) -> dict[str, Any]:
    conn = _connect(db_path, read_only=True)
    try:
        rows = _load_review_rows(conn, day=day, small_abs_pnl=small_abs_pnl, limit=limit)
        templates = list_position_supervisor_templates()
        template_summaries: dict[str, dict[str, Any]] = {}
        samples: list[dict[str, Any]] = []
        capture_failure_count = 0
        mfe_then_loss_count = 0
        capture_failure_giveback_sum = 0.0
        capture_failure_capture_sum = 0.0
        capture_failure_examples: list[dict[str, Any]] = []
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
            pnl = _safe_float(row["pnl"])
            mfe = _safe_float(row["mfe"] if row["mfe"] is not None else review_payload.get("mfe"))
            mae = _safe_float(row["mae"] if row["mae"] is not None else review_payload.get("mae"))
            giveback_ratio = _safe_float(review_payload.get("giveback_ratio"))
            profit_capture_ratio = _safe_float(review_payload.get("profit_capture_ratio"))
            if pnl < 0 and mfe > 0:
                mfe_then_loss_count += 1
                if giveback_ratio >= 0.75 and profit_capture_ratio <= 0.15:
                    capture_failure_count += 1
                    capture_failure_giveback_sum += giveback_ratio
                    capture_failure_capture_sum += profit_capture_ratio
                    if len(capture_failure_examples) < 5:
                        capture_failure_examples.append(
                            {
                                "review_id": str(row["review_id"] or ""),
                                "position_id": str(row["position_id"] or ""),
                                "pnl": pnl,
                                "mfe": mfe,
                                "giveback_ratio": giveback_ratio,
                                "profit_capture_ratio": profit_capture_ratio,
                                "close_reason": str(review_payload.get("close_reason") or ""),
                            }
                        )
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
                    "pnl": pnl,
                    "mfe": mfe,
                    "mae": mae,
                    "close_reason": str(review_payload.get("close_reason") or ""),
                    "holding_seconds": _safe_float(review_payload.get("holding_seconds")),
                    "holding_efficiency": _safe_float(review_payload.get("holding_efficiency")),
                    "profit_capture_ratio": profit_capture_ratio,
                    "giveback_ratio": giveback_ratio,
                    "template_actions": sample_actions,
                }
            )
        total = max(1, len(rows))
        for summary in template_summaries.values():
            summary["avg_confidence"] = round(float(summary.pop("confidence_sum", 0.0)) / total, 4)
        default_close = int(template_summaries.get(DEFAULT_TEMPLATE_ID, {}).get("small_loss_close_count") or 0)
        conservative_close = int(template_summaries.get(CONSERVATIVE_TEMPLATE_ID, {}).get("small_loss_close_count") or 0)
        avg_failed_giveback = capture_failure_giveback_sum / capture_failure_count if capture_failure_count else 0.0
        avg_failed_capture = capture_failure_capture_sum / capture_failure_count if capture_failure_count else 0.0
        return {
            "schema_version": "position_supervisor_replay.v1",
            "day": day,
            "sample_filter": {"abs_pnl_lte": float(small_abs_pnl), "limit": int(limit)},
            "sample_count": len(rows),
            "amend_issues": _amend_issue_count(conn, day=day),
            "templates": list(template_summaries.values()),
            "capture_failure_summary": {
                "mfe_then_loss_count": mfe_then_loss_count,
                "capture_failed_count": capture_failure_count,
                "avg_failed_giveback_ratio": round(avg_failed_giveback, 6),
                "avg_failed_profit_capture_ratio": round(avg_failed_capture, 6),
                "examples": capture_failure_examples,
            },
            "comparison": {
                "default_template_id": DEFAULT_TEMPLATE_ID,
                "candidate_template_id": CONSERVATIVE_TEMPLATE_ID,
                "profit_protection_template_id": PROFIT_PROTECTION_TEMPLATE_ID,
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
    profit_summary = next((x for x in replay["templates"] if x["template_id"] == PROFIT_PROTECTION_TEMPLATE_ID), {})
    capture_failure_summary = replay.get("capture_failure_summary") or {}
    amend_issues = replay.get("amend_issues") or {}
    conn = _connect(db_path, read_only=True)
    try:
        counterfactual_summary = _counterfactual_summary(conn, day=day)
    finally:
        conn.close()
    replay_summary = {
        "sample_count": replay.get("sample_count"),
        "comparison": replay.get("comparison"),
        "amend_issues": amend_issues,
        "capture_failure_summary": capture_failure_summary,
    }
    suggestions: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    def _generated_tpsl_template() -> dict[str, Any] | None:
        capture_failed_count = int(capture_failure_summary.get("capture_failed_count") or 0)
        avg_giveback = _safe_float(capture_failure_summary.get("avg_failed_giveback_ratio"))
        avg_capture = _safe_float(capture_failure_summary.get("avg_failed_profit_capture_ratio"))
        if capture_failed_count <= 0:
            return None
        base = get_position_supervisor_template(PROFIT_PROTECTION_TEMPLATE_ID)
        suffix = hashlib.sha1(
            f"{day}:{capture_failed_count}:{avg_giveback:.4f}:{avg_capture:.4f}".encode("utf-8")
        ).hexdigest()[:10]
        thresholds = dict(base.get("thresholds") or {})
        sl_policy = dict(base.get("sl_policy") or {})
        tp_policy = dict(base.get("tp_policy") or {})
        capture_policy = dict(base.get("capture_policy") or {})
        severity = min(1.0, max(0.0, avg_giveback))
        thresholds["giveback_tighten_threshold"] = round(max(0.16, min(0.30, 0.26 - 0.06 * severity)), 4)
        thresholds["giveback_reduce_threshold"] = round(max(0.42, min(0.62, 0.58 - 0.10 * severity)), 4)
        thresholds["profit_capture_min_threshold"] = round(max(0.36, min(0.56, 0.44 + 0.08 * (1.0 - avg_capture))), 4)
        thresholds["near_take_profit_progress"] = round(max(0.82, min(0.92, 0.90 - 0.04 * severity)), 4)
        sl_policy["profit_lock_multiplier"] = round(max(0.62, min(0.88, 0.68 + 0.14 * severity)), 4)
        sl_policy["breakeven_lock_ratio"] = round(max(0.28, min(0.45, 0.32 + 0.08 * severity)), 4)
        tp_policy["near_take_profit_action"] = "protect"
        tp_policy["extension_enabled"] = True
        tp_policy["extension_factor"] = round(max(0.12, min(0.32, 0.24 - 0.08 * severity)), 4)
        tp_policy["extension_profit_capture_min"] = round(max(0.38, min(0.58, 0.46 + 0.08 * (1.0 - avg_capture))), 4)
        capture_policy["mfe_capture_failure_threshold"] = round(max(0.16, min(0.28, 0.20 + 0.04 * severity)), 4)
        return {
            **base,
            "template_id": f"position_supervisor:auto_tpsl.{suffix}.v1",
            "template_version": f"auto_tpsl.{suffix}.v1",
            "template_role": "generated_dynamic_tpsl_capture_repair",
            "status": "candidate",
            "source": "generated_from_supervisor_learning",
            "base_template_id": PROFIT_PROTECTION_TEMPLATE_ID,
            "description": "Generated dynamic TP/SL supervisor template from MFE capture failure evidence.",
            "thresholds": thresholds,
            "sl_policy": sl_policy,
            "tp_policy": tp_policy,
            "capture_policy": capture_policy,
            "generation_evidence": {
                "day": day,
                "capture_failed_count": capture_failed_count,
                "avg_failed_giveback_ratio": round(avg_giveback, 6),
                "avg_failed_profit_capture_ratio": round(avg_capture, 6),
                "source": "position_supervisor_advisory",
            },
        }

    def _add(
        action: str,
        confidence: float,
        reason: str,
        evidence: dict[str, Any],
        *,
        target_template_id: str = CONSERVATIVE_TEMPLATE_ID,
    ) -> None:
        suggestion_id = "psv_" + hashlib.sha1(
            f"{day}:{action}:{target_template_id}:{reason}".encode("utf-8")
        ).hexdigest()[:16]
        evidence = {
            **evidence,
            "replay_summary": replay_summary,
            "counterfactual_summary": counterfactual_summary,
        }
        suggestions.append(
            {
                "suggestion_id": suggestion_id,
                "scope_type": "position_supervisor_template",
                "scope_key": target_template_id,
                "action": action,
                "confidence": round(confidence, 4),
                "reason": reason,
                "evidence": evidence,
                "status": "proposed",
                "advisory_only": True,
                "approval_path": "governor_review_then_offline_replay",
            }
        )

    capture_failed_count = int(capture_failure_summary.get("capture_failed_count") or 0)
    mfe_then_loss_count = int(capture_failure_summary.get("mfe_then_loss_count") or 0)
    if capture_failed_count >= 2 or (
        capture_failed_count >= 1 and _safe_float(capture_failure_summary.get("avg_failed_giveback_ratio")) >= 0.85
    ):
        generated_template = _generated_tpsl_template()
        if generated_template:
            _add(
                "switch_position_supervisor_template",
                min(0.86, 0.70 + 0.03 * capture_failed_count),
                "generated dynamic TP/SL template from MFE capture failure replay",
                {
                    "day": day,
                    "candidate_template_id": generated_template["template_id"],
                    "candidate_template": generated_template,
                    "base_template_id": PROFIT_PROTECTION_TEMPLATE_ID,
                    "generation_reason": "mfe_capture_failure",
                    "capture_failure_examples": capture_failure_summary.get("examples") or [],
                },
                target_template_id=generated_template["template_id"],
            )
        _add(
            "tighten_mfe_capture_protection",
            min(0.82, 0.66 + 0.03 * capture_failed_count),
            "closed losses had positive MFE but very low profit capture and high giveback",
            {
                "day": day,
                "mfe_then_loss_count": mfe_then_loss_count,
                "capture_failed_count": capture_failed_count,
                "candidate_template_id": PROFIT_PROTECTION_TEMPLATE_ID,
                "candidate_actions": profit_summary.get("actions") or {},
                "capture_failure_examples": capture_failure_summary.get("examples") or [],
            },
            target_template_id=PROFIT_PROTECTION_TEMPLATE_ID,
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
                "candidate_actions": profit_summary.get("actions") or {},
                "candidate_template_id": PROFIT_PROTECTION_TEMPLATE_ID,
            },
            target_template_id=PROFIT_PROTECTION_TEMPLATE_ID,
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
        skipped.append(
            {
                "action": "fix_stop_legality",
                "reason": "no autonomous executor; amend legality remains execution diagnostics",
                "evidence": {"day": day, "amend_issues": amend_issues},
            }
        )

    if materialize and suggestions:
        conn = _connect(db_path)
        try:
            now_ts = time.time()
            for item in suggestions:
                _execute(
                    conn,
                    """
                    INSERT INTO policy_suggestion
                    (suggestion_id, scope_type, scope_key, action, confidence, reason,
                     evidence_json, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'proposed', ?)
                    ON CONFLICT(suggestion_id) DO UPDATE SET
                        scope_type=excluded.scope_type,
                        scope_key=excluded.scope_key,
                        action=excluded.action,
                        confidence=excluded.confidence,
                        reason=excluded.reason,
                        evidence_json=excluded.evidence_json
                    """,
                    (
                        item["suggestion_id"],
                        item["scope_type"],
                        item["scope_key"],
                        item["action"],
                        float(item["confidence"]),
                        item["reason"],
                        json.dumps(item["evidence"], ensure_ascii=False),
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
            **replay_summary,
            "counterfactual_summary": counterfactual_summary,
        },
        "items": suggestions,
        "skipped": skipped,
    }
