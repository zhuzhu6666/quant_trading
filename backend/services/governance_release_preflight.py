"""Read-only integrity gate before governance coordinator enforcement."""
from __future__ import annotations

from typing import Any, Callable, Mapping


def _mapping(row: Any, columns: list[str]) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    if isinstance(row, (tuple, list)):
        return dict(zip(columns, row))
    return {}


def collect_governance_release_preflight(
    *,
    conn_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Audit durable mutation/V16 bindings without publishing or recovery."""

    try:
        if conn_factory is None:
            from backend.core.db import get_state_pg_conn

            conn_factory = get_state_pg_conn
        conn = conn_factory()
        try:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    """
                    SELECT
                        g.mutation_id,
                        g.risk_class,
                        g.status,
                        g.projection_status,
                        g.v16_command_id,
                        g.committed_config_hash,
                        g.domain_hash,
                        v.command_id AS bound_v16_command_id,
                        v.claim_status AS v16_claim_status,
                        v.finalized_mutation_id,
                        v.finalized_config_hash,
                        v.finalized_domain_hash
                    FROM governance_mutation_intent AS g
                    LEFT JOIN v16_brain_command AS v
                      ON v.command_id = NULLIF(g.v16_command_id, '')
                    WHERE g.status IN ('reserved', 'prepared', 'committed')
                    ORDER BY g.created_at, g.mutation_id
                    """
                )
                columns = [str(item[0]) for item in (cursor.description or [])]
                rows = [_mapping(row, columns) for row in cursor.fetchall()]
            finally:
                cursor.close()
        finally:
            conn.close()

        in_flight_ids: list[str] = []
        degraded_projection_ids: list[str] = []
        missing_hash_ids: list[str] = []
        invalid_v16_binding_ids: list[str] = []
        committed_count = 0
        expanding_count = 0
        for row in rows:
            mutation_id = str(row.get("mutation_id") or "")
            status = str(row.get("status") or "")
            if status in {"reserved", "prepared"}:
                in_flight_ids.append(mutation_id)
                continue
            if status != "committed":
                continue
            committed_count += 1
            config_hash = str(row.get("committed_config_hash") or "")
            domain_hash = str(row.get("domain_hash") or "")
            if not config_hash or not domain_hash:
                missing_hash_ids.append(mutation_id)
            if str(row.get("projection_status") or "") != "current":
                degraded_projection_ids.append(mutation_id)
            if str(row.get("risk_class") or "") != "risk_expanding":
                continue
            expanding_count += 1
            valid_v16 = bool(
                str(row.get("v16_command_id") or "")
                and str(row.get("bound_v16_command_id") or "")
                == str(row.get("v16_command_id") or "")
                and str(row.get("v16_claim_status") or "") == "finalized"
                and str(row.get("finalized_mutation_id") or "") == mutation_id
                and str(row.get("finalized_config_hash") or "") == config_hash
                and str(row.get("finalized_domain_hash") or "") == domain_hash
            )
            if not valid_v16:
                invalid_v16_binding_ids.append(mutation_id)

        blockers: list[str] = []
        if in_flight_ids:
            blockers.append("governance_mutation_in_flight")
        if degraded_projection_ids:
            blockers.append("committed_governance_projection_not_current")
        if missing_hash_ids:
            blockers.append("committed_governance_hash_binding_missing")
        if invalid_v16_binding_ids:
            blockers.append("expanding_governance_v16_binding_invalid")
        blockers = sorted(set(blockers))
        return {
            "ok": not blockers,
            "status": "passed" if not blockers else "blocked",
            "blockers": blockers,
            "committed_count": committed_count,
            "expanding_count": expanding_count,
            "in_flight_mutation_ids": in_flight_ids,
            "degraded_projection_mutation_ids": degraded_projection_ids,
            "missing_hash_mutation_ids": missing_hash_ids,
            "invalid_v16_binding_mutation_ids": invalid_v16_binding_ids,
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": "error",
            "blockers": ["governance_release_preflight_error"],
            "reason": f"{type(exc).__name__}:{exc}",
        }
