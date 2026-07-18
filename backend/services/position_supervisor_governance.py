from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from backend.core.db import (
    STATE_DB,
    connect_sqlite,
    get_state_pg_conn,
    is_state_db_path,
    state_table_columns,
)
from backend.services.policy_suggestion_context import attach_policy_suggestion_agent_context
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


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _stable_supervisor_application_id(suggestion_id: str, target_template_id: str) -> str:
    digest = hashlib.sha256(
        f"{suggestion_id}|{target_template_id}".encode("utf-8")
    ).hexdigest()[:20]
    return f"psv_apply_{digest}"


def _upsert_row(
    conn,
    *,
    table: str,
    primary_key: str,
    values: Mapping[str, Any],
    immutable_columns: set[str] | None = None,
) -> None:
    columns = list(values)
    immutable = set(immutable_columns or ()) | {primary_key}
    updates = [column for column in columns if column not in immutable]
    placeholders = ", ".join("?" for _ in columns)
    update_sql = ", ".join(f"{column}=excluded.{column}" for column in updates)
    _execute(
        conn,
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT({primary_key}) DO UPDATE SET {update_sql}",
        tuple(values[column] for column in columns),
    )


def _write_supervisor_switch_domain(
    conn,
    *,
    mutation_id: str,
    application_id: str,
    suggestion_id: str,
    target_template_id: str,
    reservation_id: str,
    details: Mapping[str, Any],
    review_note: str,
    now: float,
    require_governance_eligibility: bool = True,
) -> dict[str, Any]:
    """Write application/effect/suggestion/reservation in the coordinator tx."""
    from backend.services.governance_eligibility import GOVERNANCE_ELIGIBILITY_VERSION

    details_payload = {
        **dict(details),
        "mutation_id": mutation_id,
        "commit_boundary": "governance_mutation_coordinator",
    }
    app_columns = state_table_columns(conn, "learning_application_log")
    app_values: dict[str, Any] = {
        "application_id": application_id,
        "cycle_ts": now,
        "scope_type": "position_supervisor_template",
        "scope_key": target_template_id,
        "action": "switch_position_supervisor_template",
        "bias_multiplier": 1.0,
        "old_weight": 0.0,
        "new_weight": 0.0,
        "suggestion_ids_json": _json([suggestion_id] if suggestion_id else []),
        "status": "applied",
        "details_json": _json(details_payload),
        "created_at": now,
    }
    if "mutation_id" in app_columns:
        app_values["mutation_id"] = mutation_id
    if "governance_eligibility_version" in app_columns:
        app_values["governance_eligibility_version"] = GOVERNANCE_ELIGIBILITY_VERSION
    _upsert_row(
        conn,
        table="learning_application_log",
        primary_key="application_id",
        values=app_values,
        immutable_columns={"created_at"},
    )

    effect_columns = state_table_columns(conn, "learning_application_effect")
    effect_values: dict[str, Any] = {
        "application_id": application_id,
        "scope_type": "position_supervisor_template",
        "scope_key": target_template_id,
        "action": "switch_position_supervisor_template",
        "status": "observing",
        "decision_json": _json(details_payload),
        "updated_at": now,
        "created_at": now,
    }
    if "mutation_id" in effect_columns:
        effect_values["mutation_id"] = mutation_id
    if "governance_eligibility_version" in effect_columns:
        effect_values["governance_eligibility_version"] = GOVERNANCE_ELIGIBILITY_VERSION
    _upsert_row(
        conn,
        table="learning_application_effect",
        primary_key="application_id",
        values=effect_values,
        immutable_columns={"created_at"},
    )

    if reservation_id:
        reservation_columns = state_table_columns(conn, "learning_experiment_reservation")
        assignments = ["status='consumed'", "application_id=?", "updated_at=?"]
        params: list[Any] = [application_id, now]
        if "mutation_id" in reservation_columns:
            assignments.append("mutation_id=?")
            params.append(mutation_id)
        params.extend([reservation_id])
        reservation_update = _execute(
            conn,
            "UPDATE learning_experiment_reservation SET "
            + ", ".join(assignments)
            + " WHERE reservation_id=? AND status='reserved'",
            tuple(params),
        )
        if int(reservation_update.rowcount or 0) != 1:
            existing = _execute(
                conn,
                "SELECT status, application_id FROM learning_experiment_reservation WHERE reservation_id=?",
                (reservation_id,),
            ).fetchone()
            if not existing or str(existing["status"] or "") != "consumed" or str(
                existing["application_id"] or ""
            ) != application_id:
                raise RuntimeError("supervisor_reservation_not_reserved")

    if suggestion_id:
        suggestion_columns = state_table_columns(conn, "policy_suggestion")
        assignments = [
            "status='applied'",
            "reviewed_at=CASE WHEN reviewed_at > 0 THEN reviewed_at ELSE ? END",
            "review_note=?",
        ]
        params = [now, review_note]
        if "applied_mutation_id" in suggestion_columns:
            assignments.append("applied_mutation_id=?")
            params.append(mutation_id)
        params.append(suggestion_id)
        eligibility_predicate = ""
        if require_governance_eligibility:
            eligibility_predicate = (
                " AND governance_eligible=1"
                " AND governance_eligibility_version=?"
                " AND governance_eligibility_fingerprint<>''"
            )
            params.append(GOVERNANCE_ELIGIBILITY_VERSION)
        suggestion_update = _execute(
            conn,
            "UPDATE policy_suggestion SET "
            + ", ".join(assignments)
            + " WHERE suggestion_id=? AND status IN ('approved', 'applied')"
            + eligibility_predicate,
            tuple(params),
        )
        if int(suggestion_update.rowcount or 0) != 1:
            raise RuntimeError("supervisor_suggestion_not_approved")
    return {
        "application_id": application_id,
        "reservation_id": reservation_id,
        "suggestion_id": suggestion_id,
        "mutation_id": mutation_id,
    }


def _write_supervisor_rollback_domain(
    conn,
    *,
    mutation_id: str,
    application_id: str,
    rollback: Mapping[str, Any],
    now: float,
) -> dict[str, Any]:
    details_row = _execute(
        conn,
        "SELECT details_json FROM learning_application_log WHERE application_id=?",
        (application_id,),
    ).fetchone()
    previous_details = _loads(details_row["details_json"], {}) if details_row else {}
    rollback_payload = {
        **dict(rollback),
        "mutation_id": mutation_id,
        "commit_boundary": "governance_mutation_coordinator",
    }
    app_columns = state_table_columns(conn, "learning_application_log")
    assignments = ["status='rolled_back'", "details_json=?"]
    params: list[Any] = [_json({**previous_details, "rollback": rollback_payload})]
    if "mutation_id" in app_columns:
        assignments.append("mutation_id=?")
        params.append(mutation_id)
    params.append(application_id)
    application_update = _execute(
        conn,
        "UPDATE learning_application_log SET "
        + ", ".join(assignments)
        + " WHERE application_id=?",
        tuple(params),
    )
    if int(application_update.rowcount or 0) != 1:
        raise RuntimeError("supervisor_rollback_application_missing")
    effect_columns = state_table_columns(conn, "learning_application_effect")
    assignments = ["status='rolled_back'", "decision_json=?", "updated_at=?"]
    params = [_json(rollback_payload), now]
    if "mutation_id" in effect_columns:
        assignments.append("mutation_id=?")
        params.append(mutation_id)
    params.append(application_id)
    effect_update = _execute(
        conn,
        "UPDATE learning_application_effect SET "
        + ", ".join(assignments)
        + " WHERE application_id=?",
        tuple(params),
    )
    if int(effect_update.rowcount or 0) != 1:
        raise RuntimeError("supervisor_rollback_effect_missing")
    return {"application_id": application_id, "mutation_id": mutation_id}


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
            SELECT label, supervisor_event_type, evidence_json
            FROM supervisor_counterfactual_review
            WHERE close_ts >= ? AND close_ts < ?
            """,
            (start_ts, end_ts),
        ).fetchall()
    except Exception:
        rows = []
    labels: dict[str, int] = {}
    events: dict[str, int] = {}
    for row in rows:
        label = str(row["label"] or "")
        event_type = str(row["supervisor_event_type"] or "")
        evidence = row["evidence_json"] or {}
        if isinstance(evidence, str):
            try:
                evidence = json.loads(evidence or "{}")
            except Exception:
                evidence = {}
        if not bool(((evidence or {}).get("maturity") or {}).get("governance_eligible")):
            continue
        labels[label] = labels.get(label, 0) + 1
        events[event_type] = events.get(event_type, 0) + 1
    return {
        "day": day,
        "total": sum(labels.values()),
        "labels": labels,
        "events": events,
    }


def materialize_position_supervisor_candidate_observations(
    *,
    db_path: str | Path = STATE_DB,
    limit: int = 500,
    run_id: str = "",
) -> dict[str, Any]:
    """Replay approved supervisor candidates in the non-execution learning plane.

    Approved suggestions are observations, never live controls.  This helper
    reconstructs a closed-position context from the outcome review, evaluates
    the candidate without a broker dependency, and persists an immutable
    ``learning_shadow`` trace.  The trace is deliberately marked recovered and
    non-authoritative so it cannot be mistaken for a broker mutation or a live
    execution trace.
    """
    bounded_limit = max(1, min(int(limit), 5000))
    now = time.time()
    conn = _connect(db_path)
    inserted = 0
    existing = 0
    evaluated = 0
    skipped: list[dict[str, Any]] = []
    candidate_summaries: list[dict[str, Any]] = []
    try:
        if not state_table_columns(conn, "policy_suggestion"):
            return {
                "schema_version": "position_supervisor_candidate_observation.v1",
                "status": "unavailable",
                "reason": "policy_suggestion_missing",
                "inserted": 0,
                "existing": 0,
                "evaluated": 0,
                "candidates": [],
                "skipped": [],
            }
        candidates = _execute(
            conn,
            """
            SELECT suggestion_id, scope_key, created_at
            FROM policy_suggestion
            WHERE scope_type='position_supervisor_template'
              AND status='approved'
            ORDER BY reviewed_at DESC, created_at DESC
            LIMIT ?
            """,
            (min(bounded_limit, 100),),
        ).fetchall()
        remaining = bounded_limit
        for candidate_row in candidates:
            if remaining <= 0:
                break
            candidate = dict(candidate_row)
            suggestion_id = str(candidate.get("suggestion_id") or "")
            template_id = str(candidate.get("scope_key") or "")
            candidate_created_at = _safe_float(candidate.get("created_at"))
            template = get_position_supervisor_template(template_id, db_path=db_path)
            if str(template.get("template_id") or "") != template_id:
                skipped.append(
                    {
                        "suggestion_id": suggestion_id,
                        "template_id": template_id,
                        "reason": "candidate_template_unavailable",
                    }
                )
                continue
            rows = _execute(
                conn,
                """
                SELECT cf.counterfactual_id, cf.close_ts,
                       cf.evidence_json AS counterfactual_evidence_json,
                       tr.review_id, tr.trade_id, tr.position_id,
                       tr.entry_decision_id, tr.exit_decision_id,
                       tr.pnl, tr.mae, tr.mfe, tr.outcome_label,
                       tr.failure_tags_json, tr.summary_text,
                       tr.review_json, tr.created_at
                FROM supervisor_counterfactual_review cf
                JOIN trade_outcome_review tr ON tr.position_id=cf.position_id
                WHERE cf.close_ts>=?
                ORDER BY cf.close_ts ASC, tr.created_at DESC
                LIMIT ?
                """,
                (candidate_created_at, min(remaining * 4, 5000)),
            ).fetchall()
            seen_positions: set[str] = set()
            candidate_inserted = 0
            candidate_existing = 0
            candidate_evaluated = 0
            for row in rows:
                item = dict(row)
                position_id = str(item.get("position_id") or "")
                if not position_id or position_id in seen_positions:
                    continue
                seen_positions.add(position_id)
                counterfactual_evidence = _loads(
                    item.get("counterfactual_evidence_json"), {}
                )
                maturity = dict((counterfactual_evidence or {}).get("maturity") or {})
                if (
                    not bool(maturity.get("governance_eligible"))
                    or bool((counterfactual_evidence or {}).get("evidence_invalidated"))
                ):
                    continue
                close_ts = _safe_float(item.get("close_ts"))
                trace_id = "psvobs_" + hashlib.sha256(
                    f"{suggestion_id}|{position_id}|{close_ts:.6f}".encode("utf-8")
                ).hexdigest()
                already_materialized = _execute(
                    conn,
                    "SELECT 1 FROM position_supervisor_trace WHERE trace_id=?",
                    (trace_id,),
                ).fetchone()
                if already_materialized:
                    existing += 1
                    candidate_existing += 1
                    remaining -= 1
                    if remaining <= 0:
                        break
                    continue
                context = _review_to_supervisor_context(conn, row)
                context["position_supervisor_template"] = template
                verdict = evaluate_position_supervisor(context)
                verdict_evidence = dict(verdict.get("evidence") or {})
                verdict_evidence.update(
                    {
                        "candidate_suggestion_id": suggestion_id,
                        "counterfactual_id": str(item.get("counterfactual_id") or ""),
                        "non_authoritative": True,
                        "observation_source": "learning_worker_closed_position_replay",
                        "lineage_state": "verified_recovered",
                    }
                )
                verdict = {**dict(verdict), "evidence": verdict_evidence}
                observation_contract = {
                    "schema_version": "position_supervisor_candidate_observation.v1",
                    "candidate_suggestion_id": suggestion_id,
                    "counterfactual_id": str(item.get("counterfactual_id") or ""),
                    "source": "learning_worker",
                    "non_authoritative": True,
                    "broker_mutation_allowed": False,
                    "lineage_state": "verified_recovered",
                    "governance_eligible_counterfactual": True,
                }
                cursor = _execute(
                    conn,
                    """
                    INSERT INTO position_supervisor_trace
                    (trace_id, decision_id, position_id, trade_id, symbol, timeframe,
                     tick, event_ts, action, summary_reason, confidence, template_id,
                     template_version, stage, outcome, risk_action, risk_allowed,
                     risk_reason, execution_status, execution_reason, context_json,
                     verdict_json, risk_verdict_json, execution_json, trace_integrity,
                     config_version, config_hash, evolution_run_id, created_at)
                    VALUES (?, ?, ?, ?, '', '', 0, ?, ?, ?, ?, ?, ?,
                            'learning_shadow', 'shadow', '', 0, '',
                            'observation_only', ?,
                            ?, ?, '{}', ?, 'recovered', 0, '', ?, ?)
                    ON CONFLICT(trace_id) DO NOTHING
                    """,
                    (
                        trace_id,
                        str(item.get("exit_decision_id") or ""),
                        position_id,
                        str(item.get("trade_id") or position_id),
                        close_ts,
                        str(verdict.get("action") or "hold"),
                        str(verdict.get("summary_reason") or ""),
                        _safe_float(verdict.get("confidence")),
                        template_id,
                        str(template.get("template_version") or ""),
                        f"learning_worker_candidate_replay:{suggestion_id}",
                        _json({**context, "observation_contract": observation_contract}),
                        _json(verdict),
                        _json(
                            {
                                "execution_class": "shadow",
                                "is_real_execution": False,
                                "broker_mutation_attempted": False,
                                "observation_contract": observation_contract,
                            }
                        ),
                        str(run_id or ""),
                        now,
                    ),
                )
                was_inserted = int(cursor.rowcount or 0) == 1
                inserted += int(was_inserted)
                existing += int(not was_inserted)
                evaluated += 1
                candidate_inserted += int(was_inserted)
                candidate_existing += int(not was_inserted)
                candidate_evaluated += 1
                remaining -= 1
                if remaining <= 0:
                    break
            candidate_summaries.append(
                {
                    "suggestion_id": suggestion_id,
                    "template_id": template_id,
                    "evaluated": candidate_evaluated,
                    "inserted": candidate_inserted,
                    "existing": candidate_existing,
                }
            )
        conn.commit()
        return {
            "schema_version": "position_supervisor_candidate_observation.v1",
            "status": "completed",
            "authority": "learning_observation_only",
            "broker_mutation_allowed": False,
            "inserted": inserted,
            "existing": existing,
            "evaluated": evaluated,
            "candidates": candidate_summaries,
            "skipped": skipped,
        }
    finally:
        conn.close()


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
            (
                f"v2:{day}:{capture_failed_count}:{avg_giveback:.4f}:{avg_capture:.4f}:"
                f"{int(counterfactual_summary.get('total') or 0)}"
            ).encode("utf-8")
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
                "evidence": attach_policy_suggestion_agent_context(
                    {
                        **evidence,
                        "schema_version": "position_supervisor_advisory_evidence.v1",
                        "advisory_only": True,
                    },
                    source_agent="autonomous_learning",
                    scope_type="position_supervisor_template",
                    action=action,
                    requested_writes=["policy_suggestion"],
                    status="proposed",
                    impact_level="medium",
                    db_path=db_path,
                ),
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


class PositionSupervisorGovernanceMutationService:
    """Single typed mutation boundary for supervisor-template controls.

    Evidence selection and RiskPolicy approval stay with the caller.  This
    service owns only the atomic commit of the runtime target and its durable
    application/effect/suggestion projections.
    """

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = db_path

    @staticmethod
    def _committed(mutation: Mapping[str, Any]) -> bool:
        return bool(mutation.get("ok")) or str(mutation.get("status") or "") in {
            "applied",
            "committed",
            "committed_projection_degraded",
        }

    def switch_template(
        self,
        *,
        suggestion_id: str,
        previous_template_id: str,
        target_template_id: str,
        actor: str,
        source: str,
        run_id: str,
        reason: str,
        evidence: Mapping[str, Any],
        risk_verdict: Mapping[str, Any],
        reservation_id: str = "",
        application_id: str = "",
        application_details: Mapping[str, Any] | None = None,
        v16_command_id: str = "",
        v16_claim_token: str = "",
    ) -> dict[str, Any]:
        from backend.services.governance_control_plans import (
            PositionSupervisorTemplatePlan,
            governance_coordinator_mode,
        )

        now = time.time()
        application_id = application_id or _stable_supervisor_application_id(
            suggestion_id, target_template_id
        )
        details = {
            "schema_version": "position_supervisor_template_switch.v2",
            "suggestion_id": suggestion_id,
            "previous_template_id": previous_template_id,
            "target_template_id": target_template_id,
            "risk_verdict": dict(risk_verdict),
            "evidence": dict(evidence),
            "experiment_reservation_id": reservation_id,
            **dict(application_details or {}),
        }
        plan = PositionSupervisorTemplatePlan(
            patch={"position_supervisor_template_id": target_template_id},
            source=source,
            actor=actor,
            action="switch_position_supervisor_template",
            run_id=run_id,
            reason=reason,
            scope_type="supervisor_template",
            scope_key="position_supervisor",
            target_agent="position_supervisor_governance",
            previous_template_id=previous_template_id,
            target_template_id=target_template_id,
            suggestion_id=suggestion_id,
            application_id=application_id,
            reservation_id=reservation_id,
            rollback={"position_supervisor_template_id": previous_template_id},
            evidence_refs={
                "suggestion_id": suggestion_id,
                "risk_verdict": dict(risk_verdict),
                "evidence": dict(evidence),
                "previous_template_id": previous_template_id,
                "target_template_id": target_template_id,
            },
            idempotency_key=(
                f"position-supervisor-switch:v2:{suggestion_id or run_id}:"
                f"{previous_template_id}:{target_template_id}"
            ),
            v16_command_id=v16_command_id,
            v16_claim_token=v16_claim_token,
        )

        mode = governance_coordinator_mode()

        def writer(conn, mutation_id: str, _effective_config) -> Mapping[str, Any]:
            return _write_supervisor_switch_domain(
                conn,
                mutation_id=mutation_id,
                application_id=application_id,
                suggestion_id=suggestion_id,
                target_template_id=target_template_id,
                reservation_id=reservation_id,
                details=details,
                review_note=reason,
                now=now,
                require_governance_eligibility=mode != "off",
            )

        mutation = plan.execute(
            self.db_path,
            transaction_writer=writer if mode != "off" else None,
        )
        committed = self._committed(mutation)
        if committed and mode == "off":
            conn = _connect(self.db_path)
            try:
                writer(conn, str(mutation.get("mutation_id") or ""), None)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        elif not committed and reservation_id:
            from backend.services.learning_experiment_admission import (
                LearningExperimentAdmissionService,
            )

            LearningExperimentAdmissionService(self.db_path).release_reservations(
                [reservation_id]
            )
        return {
            "ok": committed,
            "committed": committed,
            "projection_ready": bool(mutation.get("ok")),
            "application_id": application_id,
            "suggestion_id": suggestion_id,
            "previous_template_id": previous_template_id,
            "target_template_id": target_template_id,
            "mutation": mutation,
            "mutation_id": str(mutation.get("mutation_id") or ""),
            "coordinator_mode": mode,
        }

    def rollback_template(
        self,
        *,
        application_id: str,
        current_template_id: str,
        previous_template_id: str,
        actor: str,
        source: str,
        run_id: str,
        reason: str,
        evidence: Mapping[str, Any],
        rollback_details: Mapping[str, Any],
        v16_command_id: str = "",
    ) -> dict[str, Any]:
        from backend.services.governance_control_plans import (
            PositionSupervisorTemplatePlan,
            governance_coordinator_mode,
        )

        now = time.time()
        plan = PositionSupervisorTemplatePlan(
            patch={"position_supervisor_template_id": previous_template_id},
            source=source,
            actor=actor,
            action="rollback_position_supervisor_template",
            run_id=run_id,
            reason=reason,
            scope_type="supervisor_template",
            scope_key="position_supervisor",
            target_agent="position_supervisor_governance",
            previous_template_id=current_template_id,
            target_template_id=previous_template_id,
            application_id=application_id,
            rollback={"position_supervisor_template_id": current_template_id},
            evidence_refs={
                "application_id": application_id,
                "current_template_id": current_template_id,
                "previous_template_id": previous_template_id,
                **dict(evidence),
            },
            idempotency_key=(
                f"position-supervisor-rollback:v2:{application_id}:"
                f"{current_template_id}:{previous_template_id}"
            ),
            v16_command_id=v16_command_id,
        )

        def writer(conn, mutation_id: str, _effective_config) -> Mapping[str, Any]:
            return _write_supervisor_rollback_domain(
                conn,
                mutation_id=mutation_id,
                application_id=application_id,
                rollback=rollback_details,
                now=now,
            )

        mode = governance_coordinator_mode()
        if mode == "off":
            # Legacy compatibility only.  dual/enforce always use the typed
            # plan and coordinator-derived risk classification.
            from backend.services.runtime_config_mutation import (
                RuntimeConfigMutationService,
            )

            mutation = RuntimeConfigMutationService(self.db_path).apply_patch(
                dict(plan.patch),
                source=plan.source,
                run_id=plan.run_id,
                actor=plan.actor,
                action=plan.action,
                reason=plan.reason,
                v16_command_id=plan.v16_command_id,
                v16_target_agent=plan.target_agent,
                v16_scope_type=plan.scope_type,
                v16_scope_key=plan.scope_key,
                v16_action=plan.action,
                risk_reduction=True,
            )
        else:
            mutation = plan.execute(self.db_path, transaction_writer=writer)
        committed = self._committed(mutation)
        if committed and mode == "off":
            conn = _connect(self.db_path)
            try:
                writer(conn, str(mutation.get("mutation_id") or ""), None)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        return {
            "ok": committed,
            "committed": committed,
            "projection_ready": bool(mutation.get("ok")),
            "application_id": application_id,
            "previous_template_id": previous_template_id,
            "rolled_back_from": current_template_id,
            "mutation": mutation,
            "mutation_id": str(mutation.get("mutation_id") or ""),
            "coordinator_mode": mode,
        }
