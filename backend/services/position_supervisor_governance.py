from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from copy import deepcopy
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
from backend.core.db_helpers import conn_is_pg as _conn_is_pg, execute as _execute, pg_sql as _sql
from backend.services.brain_governance_candidates import sync_candidate_suggestion_lifecycle
from backend.services.canonical_v2_reader import canonical_ready, iter_fact_events, iter_review_rows, review_row
from backend.services.fact_envelope import observed_epoch
from backend.services.governance_eligibility import GOVERNANCE_ELIGIBILITY_VERSION
from backend.services.learning_application_store import (
    LearningApplicationStore,
    store_for_conn,
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
from backend.services.review_contract import review_has_system_contamination
from backend.services.state_payload_archive import (
    archive_json_payload,
    load_json_payload,
    supervisor_trace_archive_text,
)
from backend.services.supervisor_payload_contract import (
    compact_supervisor_mapping,
)


LOCAL_TZ = ZoneInfo("Asia/Shanghai")


def _use_pg(db_path: str | Path = STATE_DB) -> bool:
    return is_state_db_path(db_path)


def _connect(db_path: str | Path = STATE_DB, *, read_only: bool = False):
    conn = get_state_pg_conn(read_only=read_only) if _use_pg(db_path) else connect_sqlite(db_path, read_only=read_only)
    if not _use_pg(db_path):
        conn.row_factory = sqlite3.Row
    return conn


def _review_archive_select(conn: Any, *, alias: str = "", output: str = "review_archive_hash") -> str:
    try:
        has_archive = "review_archive_hash" in state_table_columns(conn, "trade_outcome_review")
    except Exception:
        has_archive = False
    if not has_archive:
        return ""
    prefix = f"{alias}." if alias else ""
    return f", {prefix}review_archive_hash AS {output}"


def _legacy_review_table_exists(conn) -> bool:
    try:
        return "review_id" in state_table_columns(conn, "trade_outcome_review")
    except Exception:
        return False


def _attach_reviews(conn, rows: list[Any]) -> list[dict[str, Any]]:
    """Attach canonical/legacy review facts to counterfactual rows (inner-join semantics)."""
    combined: list[dict[str, Any]] = []
    for row in rows:
        mapping = dict(row) if hasattr(row, "keys") else {}
        review_id = str(mapping.get("review_id") or "")
        review = review_row(conn, review_id) if review_id else None
        if review is None:
            continue
        combined.append(
            {
                **mapping,
                **review,
                "source_review_id": review_id,
                "source_review_json": review.get("review_json") or {},
                "source_review_archive_hash": "",
            }
        )
    return combined


def _review_payload(
    conn: Any,
    row: Any,
    *,
    source_id_key: str = "review_id",
    inline_key: str = "review_json",
    archive_key: str = "review_archive_hash",
) -> dict[str, Any]:
    payload = load_json_payload(
        conn,
        source_table="trade_outcome_review",
        source_id=str(row[source_id_key] or ""),
        inline_json=row[inline_key],
        archive_hash=row[archive_key] if archive_key in row.keys() else "",
        default={},
    )
    return payload if isinstance(payload, dict) else {}


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


def list_position_supervisor_canary_candidates(
    conn: Any,
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List approved candidates and the currently active applied candidate.

    ``approved`` rows are still needed for pre-application shadow replay.  Once
    a supervisor mutation is committed, the suggestion transitions to
    ``applied``; the active effect and mutation binding then become the only
    safe way to keep observing that same cohort.  Historical applied effects,
    rolled-back effects, and ineffective effects must not reopen a canary.
    """
    bounded_limit = max(1, min(int(limit), 100))
    suggestion_columns = state_table_columns(conn, "policy_suggestion")
    required_suggestion_columns = {
        "suggestion_id",
        "scope_type",
        "scope_key",
        "status",
        "created_at",
    }
    if not required_suggestion_columns.issubset(suggestion_columns):
        return []

    approved_rows = _execute(
        conn,
        """
        SELECT suggestion_id, scope_key, status, created_at
        FROM policy_suggestion
        WHERE scope_type='position_supervisor_template'
          AND status='approved'
        ORDER BY created_at DESC
        """,
    ).fetchall()
    candidates: list[dict[str, Any]] = [
        {
            "suggestion_id": str(r["suggestion_id"] or ""),
            "scope_key": str(r["scope_key"] or ""),
            "status": str(r["status"] or ""),
            "created_at": float(r["created_at"] or 0.0),
            "_lifecycle_ts": float(r["created_at"] or 0.0),
        }
        for r in approved_rows
    ]
    # Applied suggestions bound to a still-active effect (prepared/observing/
    # mixed) stay observable through their effect's lifecycle timestamp so a
    # historical/ineffective effect cannot reopen a canary.  The lean store
    # keeps effect status/scope_key/mutation_id inside effect_json.
    store = store_for_conn(conn)
    if store is not None and "applied_mutation_id" in suggestion_columns:
        applied_rows = _execute(
            conn,
            """
            SELECT suggestion_id, scope_key, status, created_at, applied_mutation_id
            FROM policy_suggestion
            WHERE scope_type='position_supervisor_template'
              AND status='applied'
              AND applied_mutation_id<>''
            """,
        ).fetchall()
        applied_by_mutation = {
            str(r["applied_mutation_id"] or ""): dict(r) for r in applied_rows
        }
        for eff in store.iter_effects(scope_type="position_supervisor_template"):
            if str(eff.get("status") or "") not in ("prepared", "observing", "mixed"):
                continue
            applied = applied_by_mutation.get(str(eff.get("mutation_id") or ""))
            if applied is None:
                continue
            if str(applied.get("scope_key") or "") != str(eff.get("scope_key") or ""):
                continue
            candidates.append(
                {
                    "suggestion_id": str(applied.get("suggestion_id") or ""),
                    "scope_key": str(applied.get("scope_key") or ""),
                    "status": str(applied.get("status") or ""),
                    "created_at": float(applied.get("created_at") or 0.0),
                    "_lifecycle_ts": float(eff.get("created_at") or 0.0),
                }
            )
    candidates.sort(key=lambda item: (item["_lifecycle_ts"], item["created_at"]), reverse=True)
    return [
        {
            "suggestion_id": item["suggestion_id"],
            "scope_key": item["scope_key"],
            "status": item["status"],
            "created_at": item["created_at"],
        }
        for item in candidates[:bounded_limit]
    ]


_SUPERVISOR_TEMPLATE_CONTROL_SECTIONS = (
    "thresholds",
    "sl_policy",
    "tp_policy",
    "capture_policy",
    "learning_bounds",
)


def _nested_value(payload: Mapping[str, Any], path: str) -> tuple[bool, Any]:
    current: Any = payload
    for part in str(path or "").split("."):
        if not part or not isinstance(current, Mapping) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _flatten_template_controls(
    payload: Mapping[str, Any],
    *,
    prefix: str = "",
) -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    for key, value in payload.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, Mapping):
            flattened.update(_flatten_template_controls(value, prefix=path))
        else:
            flattened[path] = value
    return flattened


def _single_control_candidate_contract(
    candidate_template: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Validate a generated supervisor template as one scalar patch.

    The runtime template snapshot is intentionally complete so it can be
    restored after a restart.  The evidence contract, however, must prove
    that only one control differs from the named base template.
    """

    candidate = dict(candidate_template or {})
    base_template_id = str(
        candidate.get("base_template_id")
        or candidate.get("base_template")
        or ""
    )
    patch = dict(candidate.get("candidate_patch") or {})
    path = str(patch.get("path") or "")
    regime_stratum = str(patch.get("regime_stratum") or "")
    if not base_template_id or not path or not regime_stratum:
        return {"ok": False, "reason": "missing_base_template_patch_or_regime"}
    if path.split(".", 1)[0] not in _SUPERVISOR_TEMPLATE_CONTROL_SECTIONS:
        return {"ok": False, "reason": "candidate_patch_outside_control_sections"}
    base = get_position_supervisor_template(base_template_id)
    base_exists, base_value = _nested_value(base, path)
    candidate_exists, candidate_value = _nested_value(candidate, path)
    if not base_exists or not candidate_exists:
        return {"ok": False, "reason": "candidate_patch_path_missing"}
    if patch.get("base_value") != base_value:
        return {"ok": False, "reason": "candidate_patch_base_value_mismatch"}
    if patch.get("candidate_value") != candidate_value:
        return {"ok": False, "reason": "candidate_patch_value_mismatch"}
    base_controls: dict[str, Any] = {}
    candidate_controls: dict[str, Any] = {}
    for section in _SUPERVISOR_TEMPLATE_CONTROL_SECTIONS:
        base_controls.update(
            _flatten_template_controls(
                dict(base.get(section) or {}),
                prefix=section,
            )
        )
        candidate_controls.update(
            _flatten_template_controls(
                dict(candidate.get(section) or {}),
                prefix=section,
            )
        )
    changed = sorted(
        key
        for key in set(base_controls) | set(candidate_controls)
        if base_controls.get(key) != candidate_controls.get(key)
    )
    if changed != [path]:
        return {
            "ok": False,
            "reason": "candidate_template_changes_multiple_controls",
            "changed_controls": changed,
        }
    return {
        "ok": True,
        "base_template_id": base_template_id,
        "candidate_patch": patch,
        "changed_controls": changed,
    }


def _build_single_control_candidate_template(
    *,
    day: str,
    action: str,
    base_template_id: str,
    control_path: str,
    candidate_value: Any,
    regime_stratum: str,
    generation_reason: str,
) -> dict[str, Any] | None:
    base = get_position_supervisor_template(base_template_id)
    exists, base_value = _nested_value(base, control_path)
    if not exists:
        return None
    candidate = deepcopy(base)
    target: Any = candidate
    parts = str(control_path or "").split(".")
    for part in parts[:-1]:
        if not isinstance(target, dict) or part not in target:
            return None
        target = target[part]
    if not isinstance(target, dict) or not parts[-1]:
        return None
    target[parts[-1]] = deepcopy(candidate_value)
    suffix = hashlib.sha1(
        _json(
            {
                "day": day,
                "action": action,
                "base_template_id": base_template_id,
                "control_path": control_path,
                "candidate_value": candidate_value,
                "regime_stratum": regime_stratum,
            }
        ).encode("utf-8")
    ).hexdigest()[:10]
    candidate_id = f"position_supervisor:auto_{action}.{suffix}.v1"
    candidate["template_id"] = candidate_id
    candidate["template_version"] = f"auto_{action}.{suffix}.v1"
    candidate["template_role"] = "generated_single_control_candidate"
    candidate["status"] = "candidate"
    candidate["source"] = "generated_from_supervisor_learning"
    candidate["base_template_id"] = base_template_id
    candidate["candidate_patch"] = {
        "path": control_path,
        "base_value": deepcopy(base_value),
        "candidate_value": deepcopy(candidate_value),
        "regime_stratum": regime_stratum,
    }
    candidate["generation_context"] = {
        "schema_version": "position_supervisor_candidate_generation.v1",
        "day": day,
        "action": action,
        "control": control_path,
        "regime_stratum": regime_stratum,
        "base_template_id": base_template_id,
        "reason": generation_reason,
        "source": "position_supervisor_advisory",
    }
    return candidate


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
    # Lean convergence: fold every former wide column into details_json and
    # write only the 7 lean log columns on the coordinator-owned conn (the
    # store cannot write inside the open SQLite transaction).
    app_details = {
        **dict(details),
        "mutation_id": mutation_id,
        "commit_boundary": "governance_mutation_coordinator",
        "scope_type": "position_supervisor_template",
        "scope_key": target_template_id,
        "action": "switch_position_supervisor_template",
        "bias_multiplier": 1.0,
        "old_weight": 0.0,
        "new_weight": 0.0,
        "suggestion_ids": [suggestion_id] if suggestion_id else [],
        "governance_eligibility_version": str(GOVERNANCE_ELIGIBILITY_VERSION or ""),
        "application_state": {
            "status": "applied",
            "prepared_at": now,
            "applied_at": now,
            "updated_at": now,
            "atomic_commit": True,
        },
    }
    _upsert_row(
        conn,
        table="learning_application_log",
        primary_key="application_id",
        values={
            "application_id": application_id,
            "run_id": str(details.get("run_id") or ""),
            "source": str(details.get("source") or ""),
            "status": "applied",
            "details_json": _json(app_details),
            "created_at": now,
            "updated_at": now,
        },
        immutable_columns={"created_at"},
    )

    effect_payload = {
        "scope_type": "position_supervisor_template",
        "scope_key": target_template_id,
        "action": "switch_position_supervisor_template",
        "status": "observing",
        "observed_trade_count": 0,
        "baseline_trade_count": 0,
        "post_avg_reward": 0.0,
        "baseline_avg_reward": 0.0,
        "delta_avg_reward": None,
        "post_win_rate": 0.0,
        "baseline_win_rate": 0.0,
        "decision": details_payload,
        "mutation_id": mutation_id,
        "governance_eligibility_version": str(GOVERNANCE_ELIGIBILITY_VERSION or ""),
        "last_review_at": 0.0,
        "updated_at": now,
    }
    _upsert_row(
        conn,
        table="learning_application_effect",
        primary_key="effect_id",
        values={
            "effect_id": f"effect_{uuid.uuid4().hex[:16]}",
            "application_id": application_id,
            "scope": target_template_id,
            "effect_json": _json(effect_payload),
            "created_at": now,
        },
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
        sync_candidate_suggestion_lifecycle(
            conn,
            suggestion_id=suggestion_id,
            suggestion_status="applied",
            applied_mutation_id=mutation_id,
            now=now,
        )
    return {
        "application_id": application_id,
        "reservation_id": reservation_id,
        "suggestion_id": suggestion_id,
        "mutation_id": mutation_id,
    }


def _write_supervisor_rollback_domain(
    store,
    *,
    mutation_id: str,
    application_id: str,
    rollback: Mapping[str, Any],
    now: float,
) -> dict[str, Any]:
    """Mark a supervisor application/effect rolled back via the lean store.

    Runs after the coordinator commits: the store owns its own connection and
    cannot write inside the coordinator's open SQLite transaction (BEGIN
    IMMEDIATE holds a write lock).  ``get_application`` replaces the old
    details_json read, ``transition_application`` the log UPDATE, and
    ``update_effect`` the effect UPDATE.
    """
    previous_details = dict(store.get_application(application_id) or {})
    rollback_payload = {
        **dict(rollback),
        "mutation_id": mutation_id,
        "commit_boundary": "governance_mutation_coordinator",
    }
    app_res = store.transition_application(
        application_id,
        status="rolled_back",
        details_patch={
            "rollback": rollback_payload,
            "mutation_id": mutation_id,
        },
    )
    if not bool(app_res.get("ok")):
        raise RuntimeError("supervisor_rollback_application_missing")
    updated = store.update_effect(
        application_id,
        patch={
            "status": "rolled_back",
            "decision": rollback_payload,
            "mutation_id": mutation_id,
        },
    )
    if not updated:
        raise RuntimeError("supervisor_rollback_effect_missing")
    return {
        "application_id": application_id,
        "mutation_id": mutation_id,
        "previous_details": previous_details,
    }


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
    opened = None
    for event in iter_fact_events(conn, "position", entity_id=str(position_id)):
        payload = event.get("payload") or {}
        if str(payload.get("event_type") or "") == "opened":
            opened = payload
            break
    details = (opened or {}).get("details")
    if not isinstance(details, dict):
        details = _loads(details, {}) if details else {}
    return {
        "sl": _safe_float(details.get("sl")),
        "tp": _safe_float(details.get("tp")),
    }


def _review_to_supervisor_context(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    payload = _review_payload(conn, row)
    real_pnl = payload.get("real_pnl") or {}
    quality_context = payload.get("decision_quality_context")
    if not isinstance(quality_context, dict):
        quality_context = payload.get("decision_context")
    if not isinstance(quality_context, dict):
        quality_context = {}
    context_state = quality_context.get("context_state")
    if not isinstance(context_state, dict):
        context_state = payload.get("context_state")
    if not isinstance(context_state, dict):
        context_state = {}
    market_context = {
        "context_state": dict(context_state),
        "regime_id": str(
            payload.get("regime_id")
            or quality_context.get("regime_id")
            or payload.get("current_regime")
            or ""
        ),
        "regime_confidence": quality_context.get("regime_confidence"),
    }
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
        "market": market_context,
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
    if not canonical_ready(conn) and not _legacy_review_table_exists(conn):
        return []
    rows = []
    for row in iter_review_rows(conn, limit=0):
        created_at = _safe_float(row.get("created_at"))
        if not (start_ts <= created_at < end_ts):
            continue
        if abs(_safe_float(row.get("pnl"))) > float(small_abs_pnl):
            continue
        if review_has_system_contamination(_review_payload(conn, row)):
            continue
        rows.append(row)
    rows.sort(key=lambda row: (_safe_float(row.get("created_at")), str(row.get("review_id") or "")))
    return rows[: max(0, int(limit))]


def _amend_issue_count(conn: sqlite3.Connection, *, day: str) -> dict[str, int]:
    start_ts, end_ts = _day_bounds(day)
    counts: dict[str, int] = {}
    for event in iter_fact_events(conn, "position"):
        payload = event.get("payload") or {}
        event_ts = observed_epoch(payload.get("event_ts"))
        event_type = str(payload.get("event_type") or "")
        if start_ts <= event_ts < end_ts and event_type in ("amend_failed", "amend_skipped"):
            counts[event_type] = counts.get(event_type, 0) + 1
    return counts


def _counterfactual_summary(conn: sqlite3.Connection, *, day: str) -> dict[str, Any]:
    start_ts, end_ts = _day_bounds(day)
    try:
        rows = _execute(
            conn,
            """
            SELECT cf.label, cf.supervisor_event_type, cf.evidence_json, cf.review_id
            FROM supervisor_counterfactual_review cf
            WHERE cf.close_ts >= ? AND cf.close_ts < ?
            """,
            (start_ts, end_ts),
        ).fetchall()
        rows = _attach_reviews(conn, rows)
    except Exception:
        rows = []
    labels: dict[str, int] = {}
    events: dict[str, int] = {}
    for row in rows:
        if review_has_system_contamination(
            _review_payload(
                conn,
                row,
                source_id_key="source_review_id",
                inline_key="source_review_json",
                archive_key="source_review_archive_hash",
            )
        ):
            continue
        label = str(row["label"] or "")
        event_type = str(row["supervisor_event_type"] or "")
        evidence = row["evidence_json"] or {}
        if isinstance(evidence, str):
            try:
                evidence = json.loads(evidence or "{}")
            except Exception:
                evidence = {}
        if (
            bool((evidence or {}).get("evidence_invalidated"))
            or not bool(((evidence or {}).get("maturity") or {}).get("governance_eligible"))
        ):
            continue
        labels[label] = labels.get(label, 0) + 1
        events[event_type] = events.get(event_type, 0) + 1
    return {
        "day": day,
        "total": sum(labels.values()),
        "labels": labels,
        "events": events,
    }


def _iter_candidate_observation_reviews(
    conn: sqlite3.Connection,
    *,
    candidate_created_at: float,
    page_limit: int,
):
    page_offset = 0
    while True:
        rows = _execute(
            conn,
            """
            SELECT cf.counterfactual_id, cf.close_ts, cf.review_id,
                   cf.evidence_json AS counterfactual_evidence_json
            FROM supervisor_counterfactual_review cf
            WHERE cf.close_ts>=?
            ORDER BY cf.close_ts ASC, cf.counterfactual_id ASC
            LIMIT ? OFFSET ?
            """,
            (candidate_created_at, page_limit, page_offset),
        ).fetchall()
        if not rows:
            return
        page_offset += len(rows)
        for row in _attach_reviews(conn, rows):
            yield row
        del rows


def materialize_position_supervisor_candidate_observations(
    *,
    db_path: str | Path = STATE_DB,
    limit: int = 500,
    run_id: str = "",
) -> dict[str, Any]:
    """Replay supervisor candidates in the non-execution learning plane.

    Approved suggestions and the currently active applied suggestion are
    observations, never live controls.  This helper reconstructs a
    closed-position context from the outcome review, evaluates the candidate
    without a broker dependency, and persists an immutable ``learning_shadow``
    trace.  The trace is deliberately marked recovered and non-authoritative
    so it cannot be mistaken for a broker mutation or a live execution trace.
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
        trace_columns = state_table_columns(conn, "position_supervisor_trace")
        archive_capable = {
            "verdict_archive_hash",
            "verdict_raw_sha256",
            "verdict_raw_bytes",
        } <= trace_columns
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
        candidates = list_position_supervisor_canary_candidates(
            conn,
            limit=min(bounded_limit, 100),
        )
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
            seen_positions: set[str] = set()
            candidate_inserted = 0
            candidate_existing = 0
            candidate_evaluated = 0
            for row in _iter_candidate_observation_reviews(
                conn,
                candidate_created_at=candidate_created_at,
                page_limit=bounded_limit,
            ):
                item = dict(row)
                if review_has_system_contamination(_review_payload(conn, item)):
                    continue
                position_id = str(item.get("position_id") or "")
                if not position_id or position_id in seen_positions:
                    continue
                counterfactual_evidence = _loads(
                    item.get("counterfactual_evidence_json"), {}
                )
                maturity = dict((counterfactual_evidence or {}).get("maturity") or {})
                if (
                    not bool(maturity.get("governance_eligible"))
                    or bool((counterfactual_evidence or {}).get("evidence_invalidated"))
                ):
                    continue
                seen_positions.add(position_id)
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
                raw_context = {**context, "observation_contract": observation_contract}
                raw_verdict = dict(verdict)
                raw_execution = {
                    "execution_class": "shadow",
                    "is_real_execution": False,
                    "broker_mutation_attempted": False,
                    "observation_contract": observation_contract,
                }
                context_json = _json(raw_context)
                verdict_json = _json(raw_verdict)
                execution_json = _json(raw_execution)
                archive = None
                if archive_capable:
                    archive = archive_json_payload(
                        conn,
                        source_table="position_supervisor_trace",
                        source_id=trace_id,
                        payload_kind="supervisor_trace",
                        raw_json=supervisor_trace_archive_text(
                            context_json=_json(raw_context),
                            verdict_json=_json(raw_verdict),
                            risk_verdict_json="{}",
                            execution_json=_json(raw_execution),
                        ),
                    )
                    if archive:
                        context_json = _json(
                            compact_supervisor_mapping(
                                raw_context,
                                nested_keys=frozenset({"position", "account"}),
                            )
                        )
                        verdict_json = _json(
                            compact_supervisor_mapping(
                                raw_verdict,
                                nested_keys=frozenset(
                                    {"evidence", "recommended_controls", "supervisor_template"}
                                ),
                            )
                        )
                        execution_json = _json(
                            compact_supervisor_mapping(
                                raw_execution,
                                nested_keys=frozenset({"evidence", "controls"}),
                            )
                        )
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
                        context_json,
                        verdict_json,
                        execution_json,
                        str(run_id or ""),
                        now,
                    ),
                )
                if archive:
                    _execute(
                        conn,
                        """
                        UPDATE position_supervisor_trace
                        SET verdict_archive_hash=?, verdict_raw_sha256=?, verdict_raw_bytes=?
                        WHERE trace_id=?
                        """,
                        (
                            archive["archive_hash"],
                            archive["raw_sha256"],
                            archive["raw_bytes"],
                            trace_id,
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
            review_payload = _review_payload(conn, row)
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
        sl_policy = dict(base.get("sl_policy") or {})
        severity = min(1.0, max(0.0, avg_giveback))
        learning_bounds = dict(base.get("learning_bounds") or {})
        base_lock_multiplier = _safe_float(sl_policy.get("profit_lock_multiplier"), 0.60)
        min_lock_multiplier = _safe_float(
            learning_bounds.get("min_profit_lock_multiplier"),
            0.35,
        )
        max_lock_multiplier = _safe_float(
            learning_bounds.get("max_profit_lock_multiplier"),
            0.85,
        )
        # A generated candidate changes exactly one management control.  The
        # posture state machine owns regime behavior; this patch only relaxes
        # the profit-lock multiplier when the counterfactual evidence says
        # the existing stop was too tight.
        candidate_lock_multiplier = max(
            min_lock_multiplier,
            min(max_lock_multiplier, base_lock_multiplier - 0.10 * severity),
        )
        sl_policy["profit_lock_multiplier"] = round(candidate_lock_multiplier, 4)
        return {
            **base,
            "template_id": f"position_supervisor:auto_tpsl.{suffix}.v1",
            "template_version": f"auto_tpsl.{suffix}.v1",
            "template_role": "generated_dynamic_tpsl_capture_repair",
            "status": "candidate",
            "source": "generated_from_supervisor_learning",
            "base_template_id": PROFIT_PROTECTION_TEMPLATE_ID,
            "description": "Generated dynamic TP/SL supervisor template from MFE capture failure evidence.",
            "sl_policy": sl_policy,
            "candidate_patch": {
                "path": "sl_policy.profit_lock_multiplier",
                "base_value": round(base_lock_multiplier, 4),
                "candidate_value": round(candidate_lock_multiplier, 4),
                "regime_stratum": "range_capture",
            },
            "generation_context": {
                "schema_version": "position_supervisor_candidate_generation.v1",
                "day": day,
                "action": "mfe_capture_failure",
                "control": "sl_policy.profit_lock_multiplier",
                "regime_stratum": "range_capture",
                "base_template_id": PROFIT_PROTECTION_TEMPLATE_ID,
                "reason": "mfe_capture_failure",
                "source": "position_supervisor_advisory",
            },
            "generation_evidence": {
                "day": day,
                "capture_failed_count": capture_failed_count,
                "avg_failed_giveback_ratio": round(avg_giveback, 6),
                "avg_failed_profit_capture_ratio": round(avg_capture, 6),
                "control": "sl_policy.profit_lock_multiplier",
                "regime_stratum": "range_capture",
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
        candidate_template = evidence.get("candidate_template")
        if action == "switch_position_supervisor_template":
            contract = _single_control_candidate_contract(candidate_template)
            if not contract.get("ok"):
                skipped.append(
                    {
                        "action": action,
                        "reason": "single_control_candidate_contract_missing",
                        "target_template_id": target_template_id,
                        "contract": contract,
                    }
                )
                return
        if isinstance(candidate_template, Mapping):
            contract = _single_control_candidate_contract(candidate_template)
            if contract.get("ok"):
                evidence = {
                    **evidence,
                    "base_template": get_position_supervisor_template(
                        str(contract.get("base_template_id") or "")
                    ),
                    "candidate_patch": dict(
                        contract.get("candidate_patch") or {}
                    ),
                    "generation_context": dict(
                        candidate_template.get("generation_context") or {}
                    ),
                }
        suggestion_id = "psv_" + hashlib.sha1(
            f"{day}:{action}:{target_template_id}:{reason}".encode("utf-8")
        ).hexdigest()[:16]
        evidence = {
            **evidence,
            "replay_summary": replay_summary,
            "counterfactual_summary": counterfactual_summary,
        }
        eligibility_fingerprint = hashlib.sha256(
            _json(
                {
                    "schema_version": GOVERNANCE_ELIGIBILITY_VERSION,
                    "evidence_class": "position_supervisor_advisory",
                    "suggestion_id": suggestion_id,
                    "scope_type": "position_supervisor_template",
                    "scope_key": target_template_id,
                    "action": action,
                    "evidence": evidence,
                }
            ).encode("utf-8")
        ).hexdigest()
        evidence["governance_eligibility"] = {
            "governance_eligible": True,
            "governance_eligibility_version": GOVERNANCE_ELIGIBILITY_VERSION,
            "governance_eligibility_fingerprint": eligibility_fingerprint,
            "evidence_class": "position_supervisor_advisory",
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
                "governance_eligible": 1,
                "governance_eligibility_version": GOVERNANCE_ELIGIBILITY_VERSION,
                "governance_eligibility_fingerprint": eligibility_fingerprint,
                "governance_ineligible_reason": "",
            }
        )

    capture_failed_count = int(capture_failure_summary.get("capture_failed_count") or 0)
    mfe_then_loss_count = int(capture_failure_summary.get("mfe_then_loss_count") or 0)
    counterfactual_labels = dict(counterfactual_summary.get("labels") or {})
    over_protection_count = sum(
        int(counterfactual_labels.get(label) or 0)
        for label in ("protection_too_tight", "premature_tighten", "noise_stopout")
    )
    correct_stop_count = int(counterfactual_labels.get("correct_stop") or 0)
    protection_can_tighten = over_protection_count <= correct_stop_count
    if capture_failed_count >= 2 or (
        capture_failed_count >= 1 and _safe_float(capture_failure_summary.get("avg_failed_giveback_ratio")) >= 0.85
    ):
        if protection_can_tighten:
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
            capture_candidate = _build_single_control_candidate_template(
                day=day,
                action="mfe_capture_protection",
                base_template_id=DEFAULT_TEMPLATE_ID,
                control_path="thresholds.giveback_reduce_threshold",
                candidate_value=get_position_supervisor_template(
                    PROFIT_PROTECTION_TEMPLATE_ID
                ).get("thresholds", {}).get("giveback_reduce_threshold"),
                regime_stratum="range_capture",
                generation_reason="mfe_capture_failure",
            )
            if capture_candidate is None:
                skipped.append(
                    {
                        "action": "tighten_mfe_capture_protection",
                        "reason": "single_control_candidate_generation_failed",
                    }
                )
            else:
                _add(
                    "tighten_mfe_capture_protection",
                    min(0.82, 0.66 + 0.03 * capture_failed_count),
                    "closed losses had positive MFE but very low profit capture and high giveback",
                    {
                        "day": day,
                        "mfe_then_loss_count": mfe_then_loss_count,
                        "capture_failed_count": capture_failed_count,
                        "candidate_template_id": capture_candidate["template_id"],
                        "candidate_template": capture_candidate,
                        "candidate_actions": profit_summary.get("actions") or {},
                        "capture_failure_examples": capture_failure_summary.get("examples") or [],
                    },
                    target_template_id=capture_candidate["template_id"],
                )
        else:
            skipped.append(
                {
                    "action": "tighten_mfe_capture_protection",
                    "reason": "counterfactual evidence shows protection is already too aggressive",
                    "evidence": {
                        "day": day,
                        "capture_failed_count": capture_failed_count,
                        "over_protection_count": over_protection_count,
                        "correct_stop_count": correct_stop_count,
                    },
                }
            )

    if over_protection_count > correct_stop_count:
        relax_candidate = _build_single_control_candidate_template(
            day=day,
            action="overprotection_relief",
            base_template_id=DEFAULT_TEMPLATE_ID,
            control_path="thresholds.min_thesis_break_seconds",
            candidate_value=get_position_supervisor_template(
                CONSERVATIVE_TEMPLATE_ID
            ).get("thresholds", {}).get("min_thesis_break_seconds"),
            regime_stratum="transition_confirming",
            generation_reason="counterfactual_overprotection",
        )
        if relax_candidate is None:
            skipped.append(
                {
                    "action": "switch_position_supervisor_template",
                    "reason": "single_control_candidate_generation_failed",
                }
            )
        else:
            _add(
                "switch_position_supervisor_template",
                min(0.82, 0.66 + 0.02 * (over_protection_count - correct_stop_count)),
                "counterfactual review shows profit protection exits are too aggressive",
                {
                    "day": day,
                    "over_protection_count": over_protection_count,
                    "correct_stop_count": correct_stop_count,
                    "candidate_template_id": relax_candidate["template_id"],
                    "candidate_template": relax_candidate,
                },
                target_template_id=relax_candidate["template_id"],
            )

    if int(replay["comparison"].get("small_loss_closes_reduced") or 0) > 0:
        relax_candidate = _build_single_control_candidate_template(
            day=day,
            action="relax_thesis_break",
            base_template_id=DEFAULT_TEMPLATE_ID,
            control_path="thresholds.min_thesis_break_seconds",
            candidate_value=get_position_supervisor_template(
                CONSERVATIVE_TEMPLATE_ID
            ).get("thresholds", {}).get("min_thesis_break_seconds"),
            regime_stratum="transition_confirming",
            generation_reason="small_loss_replay",
        )
        if relax_candidate is not None:
            _add(
                "relax_thesis_break",
                0.76,
                "conservative supervisor template reduces small-loss full exits in offline replay",
                {
                    "day": day,
                    "default_small_loss_close_count": default_summary.get("small_loss_close_count"),
                    "candidate_small_loss_close_count": candidate_summary.get("small_loss_close_count"),
                    "candidate_template_id": relax_candidate["template_id"],
                    "candidate_template": relax_candidate,
                },
                target_template_id=relax_candidate["template_id"],
            )
    if (
        protection_can_tighten
        and int(default_summary.get("actions", {}).get("tighten", 0) or 0) > 0
    ):
        tighten_candidate = _build_single_control_candidate_template(
            day=day,
            action="tighten_profit_protection",
            base_template_id=DEFAULT_TEMPLATE_ID,
            control_path="thresholds.giveback_tighten_threshold",
            candidate_value=get_position_supervisor_template(
                PROFIT_PROTECTION_TEMPLATE_ID
            ).get("thresholds", {}).get("giveback_tighten_threshold"),
            regime_stratum="range_capture",
            generation_reason="historical_profit_protection_pressure",
        )
        if tighten_candidate is not None:
            _add(
                "tighten_profit_protection",
                0.68,
                "historical samples show frequent tighten/reduce pressure before exits",
                {
                    "day": day,
                    "default_actions": default_summary.get("actions") or {},
                    "candidate_actions": profit_summary.get("actions") or {},
                    "candidate_template_id": tighten_candidate["template_id"],
                    "candidate_template": tighten_candidate,
                },
                target_template_id=tighten_candidate["template_id"],
            )
    if int(default_summary.get("thesis_broken_close_count") or 0) >= 3:
        hold_window_candidate = _build_single_control_candidate_template(
            day=day,
            action="increase_min_hold_window",
            base_template_id=DEFAULT_TEMPLATE_ID,
            control_path="thresholds.min_thesis_break_seconds",
            candidate_value=get_position_supervisor_template(
                CONSERVATIVE_TEMPLATE_ID
            ).get("thresholds", {}).get("min_thesis_break_seconds"),
            regime_stratum="transition_confirming",
            generation_reason="early_thesis_break_replay",
        )
        if hold_window_candidate is not None:
            _add(
                "increase_min_hold_window",
                0.64,
                "multiple thesis-broken exits are small and early enough to require a minimum evidence window",
                {
                    "day": day,
                    "thesis_broken_close_count": default_summary.get("thesis_broken_close_count"),
                    "candidate_min_thesis_break_seconds": get_position_supervisor_template(CONSERVATIVE_TEMPLATE_ID).get("thresholds", {}).get("min_thesis_break_seconds"),
                    "candidate_template_id": hold_window_candidate["template_id"],
                    "candidate_template": hold_window_candidate,
                },
                target_template_id=hold_window_candidate["template_id"],
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
                     evidence_json, status, governance_eligible,
                     governance_eligibility_version, governance_eligibility_fingerprint,
                     governance_ineligible_reason, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?, ?, ?, ?)
                    ON CONFLICT(suggestion_id) DO UPDATE SET
                        scope_type=excluded.scope_type,
                        scope_key=excluded.scope_key,
                        action=excluded.action,
                        confidence=excluded.confidence,
                        reason=excluded.reason,
                        evidence_json=excluded.evidence_json,
                        governance_eligible=excluded.governance_eligible,
                        governance_eligibility_version=excluded.governance_eligibility_version,
                        governance_eligibility_fingerprint=excluded.governance_eligibility_fingerprint,
                        governance_ineligible_reason=CASE
                            WHEN policy_suggestion.status='rejected'
                             AND policy_suggestion.governance_ineligible_reason='eligibility_contract_invalid'
                            THEN ''
                            ELSE policy_suggestion.governance_ineligible_reason
                        END,
                        status=CASE
                            WHEN policy_suggestion.status='rejected'
                             AND policy_suggestion.governance_ineligible_reason='eligibility_contract_invalid'
                            THEN 'proposed'
                            ELSE policy_suggestion.status
                        END,
                        reviewed_at=CASE
                            WHEN policy_suggestion.status='rejected'
                             AND policy_suggestion.governance_ineligible_reason='eligibility_contract_invalid'
                            THEN 0.0
                            ELSE policy_suggestion.reviewed_at
                        END,
                        review_note=CASE
                            WHEN policy_suggestion.status='rejected'
                             AND policy_suggestion.governance_ineligible_reason='eligibility_contract_invalid'
                            THEN ''
                            ELSE policy_suggestion.review_note
                        END
                    """,
                    (
                        item["suggestion_id"],
                        item["scope_type"],
                        item["scope_key"],
                        item["action"],
                        float(item["confidence"]),
                        item["reason"],
                        json.dumps(item["evidence"], ensure_ascii=False),
                        int(item["governance_eligible"]),
                        item["governance_eligibility_version"],
                        item["governance_eligibility_fingerprint"],
                        item["governance_ineligible_reason"],
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

    def _claimed_v16_evidence_fingerprint(
        self,
        *,
        command_id: str,
        claim_token: str,
    ) -> str:
        """Read the command-bound evidence for a supplied V16 claim.

        The command fingerprint is the authority for the mutation binding.
        Candidate-review and policy-suggestion fingerprints describe different
        evidence surfaces and must not be substituted for it.
        """
        if not command_id or not claim_token:
            return ""
        conn = _connect(self.db_path, read_only=True)
        try:
            row = _execute(
                conn,
                """SELECT evidence_fingerprint
                   FROM v16_brain_command
                   WHERE command_id=? AND claim_status='claimed'
                     AND claim_token=?""",
                (str(command_id), str(claim_token)),
            ).fetchone()
            if not row:
                return ""
            return str(row["evidence_fingerprint"] or "")
        finally:
            conn.close()

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
        evidence_fingerprint: str = "",
    ) -> dict[str, Any]:
        from backend.services.governance_control_plans import (
            PositionSupervisorTemplatePlan,
            governance_coordinator_mode,
        )

        now = time.time()
        v16_evidence_fingerprint = str(evidence_fingerprint or "")
        if v16_command_id and v16_claim_token and not v16_evidence_fingerprint:
            v16_evidence_fingerprint = self._claimed_v16_evidence_fingerprint(
                command_id=v16_command_id,
                claim_token=v16_claim_token,
            )
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
                + (f":v16:{v16_command_id}" if v16_command_id else "")
            ),
            v16_command_id=v16_command_id,
            v16_claim_token=v16_claim_token,
            evidence_fingerprint=v16_evidence_fingerprint,
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
            # The learning_application rollback is written through the store
            # after commit (below); the coordinator transaction no longer paths
            # store writes through its own connection.
            mutation = plan.execute(self.db_path, transaction_writer=None)
        committed = self._committed(mutation)
        if committed:
            store = LearningApplicationStore(str(self.db_path))
            _write_supervisor_rollback_domain(
                store,
                mutation_id=str(mutation.get("mutation_id") or ""),
                application_id=application_id,
                rollback=rollback_details,
                now=now,
            )
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
