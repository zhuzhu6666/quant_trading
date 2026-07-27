"""Read-only projection of policy controls that live is allowed to consume."""
from __future__ import annotations

import json
from typing import Any, Iterable

from backend.core.db import state_table_columns, state_table_exists


def _is_pg(conn: Any) -> bool:
    return conn.__class__.__module__.split(".", 1)[0] == "psycopg"


def _sql(conn: Any, sql: str) -> str:
    return sql.replace("?", "%s") if _is_pg(conn) else sql


def load_live_policy_controls(
    conn: Any,
    *,
    scope_type: str,
    allowed_actions: Iterable[str],
    limit: int,
    coordinator_mode: str | None = None,
    legacy_tightening_actions: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Load applied controls, never merely approved suggestions.

    Compatibility is intentionally one-way and conservative:

    * ``off``/``dual_record`` may retain already-applied legacy tightening
      controls as ``legacy_quarantined``; callers whose action set mixes
      tightening and expansion must identify the tightening subset explicitly;
    * ``enforce`` requires ``applied_mutation_id`` to reference a committed
      governance mutation;
    * an applied row with a dangling/non-committed mutation id is always
      rejected, in every mode.
    """
    if not state_table_exists(conn, "policy_suggestion"):
        return []
    if coordinator_mode is None:
        try:
            from backend.core.static_feature_flags import shared_static_feature_flags

            coordinator_mode = (
                shared_static_feature_flags().governance_mutation_coordinator_v2_mode
            )
        except Exception:
            # Invalid release configuration must fail closed.
            return []
    mode = str(coordinator_mode or "off").strip().lower()
    strict = mode == "enforce"
    if mode not in {"off", "dual_record", "enforce"}:
        return []

    columns = state_table_columns(conn, "policy_suggestion")
    reason_expr = "reason" if "reason" in columns else "'' AS reason"
    evidence_expr = (
        "evidence_json" if "evidence_json" in columns else "'{}' AS evidence_json"
    )
    reviewed_expr = (
        "reviewed_at" if "reviewed_at" in columns else "created_at AS reviewed_at"
    )
    mutation_expr = (
        "applied_mutation_id"
        if "applied_mutation_id" in columns
        else "'' AS applied_mutation_id"
    )
    rows = conn.execute(
        _sql(
            conn,
            f"""
            SELECT suggestion_id, scope_key, action, confidence, {reason_expr},
                   {evidence_expr}, {reviewed_expr}, created_at, {mutation_expr}
            FROM policy_suggestion
            WHERE scope_type=?
              AND status='applied'
            ORDER BY reviewed_at DESC, created_at DESC
            LIMIT ?
            """,
        ),
        (str(scope_type), max(1, min(int(limit) * 4, 1000))),
    ).fetchall()
    actions = {str(item) for item in allowed_actions}
    legacy_actions = (
        actions
        if legacy_tightening_actions is None
        else {str(item) for item in legacy_tightening_actions} & actions
    )
    candidates = [dict(row) for row in rows if str(dict(row).get("action") or "") in actions]
    application_statuses: dict[str, set[str]] = {}
    if candidates and state_table_exists(conn, "learning_application_log"):
        application_columns = state_table_columns(conn, "learning_application_log")
        if {"scope_type", "suggestion_ids_json", "status"} <= application_columns:
            application_rows = conn.execute(
                _sql(
                    conn,
                    """
                    SELECT suggestion_ids_json, status
                    FROM learning_application_log
                    WHERE scope_type=?
                    """,
                ),
                (str(scope_type),),
            ).fetchall()
            for application in application_rows:
                payload = dict(application).get("suggestion_ids_json") or []
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload or "[]")
                    except (TypeError, ValueError):
                        payload = []
                if not isinstance(payload, list):
                    continue
                status = str(dict(application).get("status") or "")
                for suggestion_id in payload:
                    application_statuses.setdefault(str(suggestion_id), set()).add(status)
    mutation_ids = sorted(
        {
            str(row.get("applied_mutation_id") or "")
            for row in candidates
            if str(row.get("applied_mutation_id") or "")
        }
    )
    committed: set[str] = set()
    if mutation_ids and state_table_exists(conn, "governance_mutation_intent"):
        intent_columns = state_table_columns(conn, "governance_mutation_intent")
        if {"mutation_id", "status"} <= intent_columns:
            placeholders = ",".join("?" for _ in mutation_ids)
            intent_rows = conn.execute(
                _sql(
                    conn,
                    f"""
                    SELECT mutation_id
                    FROM governance_mutation_intent
                    WHERE status='committed'
                      AND mutation_id IN ({placeholders})
                    """,
                ),
                tuple(mutation_ids),
            ).fetchall()
            committed = {
                str(dict(row).get("mutation_id") or "") for row in intent_rows
            }

    accepted: list[dict[str, Any]] = []
    for row in candidates:
        binding_statuses = application_statuses.get(str(row.get("suggestion_id") or ""))
        if binding_statuses and not binding_statuses.intersection(
            {"prepared", "applied", "observing", "effective", "mixed"}
        ):
            continue
        mutation_id = str(row.get("applied_mutation_id") or "")
        if mutation_id:
            if mutation_id not in committed:
                continue
            row["governance_authority"] = "committed_mutation"
            row["committed_mutation_id"] = mutation_id
        else:
            if strict or str(row.get("action") or "") not in legacy_actions:
                continue
            row["governance_authority"] = "legacy_quarantined"
            row["committed_mutation_id"] = ""
        accepted.append(row)
        if len(accepted) >= max(1, int(limit)):
            break
    return accepted
