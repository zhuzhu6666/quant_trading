from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, connect_sqlite, get_state_pg_conn, is_state_db_path, state_table_exists
from backend.core.state_store import (
    is_state_schema_write_sql,
    validate_runtime_state_schema,
)


FORBIDDEN_TRUE_CAPABILITIES = {
    "live_trading",
    "can_place_orders",
    "can_close_positions",
    "can_change_risk_limits",
    "can_increase_hard_risk_limits",
    "can_change_factor_weights",
    "can_bypass_risk_policy",
    "can_apply_policy_without_review",
    "can_release_market_connection",
}

REQUIRED_FALSE_CAPABILITIES = {
    "live_trading",
    "can_place_orders",
    "can_close_positions",
    "can_change_risk_limits",
    "can_bypass_risk_policy",
}

REQUIRED_TRUE_CAPABILITIES = {
    "advisory_only",
    "shadow_only",
}


def _use_pg(db_path: str | Path = STATE_DB) -> bool:
    return is_state_db_path(db_path)


def _connect(db_path: str | Path = STATE_DB, *, read_only: bool = False):
    conn = get_state_pg_conn(read_only=read_only) if _use_pg(db_path) else connect_sqlite(db_path, read_only=read_only)
    if not _use_pg(db_path):
        conn.row_factory = __import__("sqlite3").Row
    return conn


def _conn_is_pg(conn) -> bool:
    return conn.__class__.__module__.split(".", 1)[0] == "psycopg"


def _sql(conn, sql: str) -> str:
    return sql.replace("%", "%%").replace("?", "%s") if _conn_is_pg(conn) else sql


def _execute(conn, sql: str, params: Any = None):
    rendered = _sql(conn, sql)
    if _conn_is_pg(conn) and is_state_schema_write_sql(rendered):
        return validate_runtime_state_schema(conn, rendered)
    if params is None:
        return conn.execute(rendered)
    return conn.execute(rendered, params)


def ensure_model_permission_audit_table(db_path: str | Path = STATE_DB) -> None:
    conn = _connect(db_path)
    try:
        if _conn_is_pg(conn):
            if not state_table_exists(conn, "model_permission_audit"):
                raise RuntimeError("missing state table: model_permission_audit")
            return
        _execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS model_permission_audit (
                audit_id TEXT PRIMARY KEY,
                model_type TEXT DEFAULT '',
                artifact_path TEXT DEFAULT '',
                status TEXT DEFAULT '',
                reason TEXT DEFAULT '',
                capabilities_json TEXT DEFAULT '{}',
                violations_json TEXT DEFAULT '[]',
                context_json TEXT DEFAULT '{}',
                created_at REAL NOT NULL DEFAULT 0.0
            )
            """
        )
        _execute(
            conn,
            """
            CREATE INDEX IF NOT EXISTS idx_model_permission_audit_created
            ON model_permission_audit(created_at)
            """
        )
        _execute(
            conn,
            """
            CREATE INDEX IF NOT EXISTS idx_model_permission_audit_model
            ON model_permission_audit(model_type, status, created_at)
            """
        )
        conn.commit()
    finally:
        conn.close()


def _capabilities_from_artifact(artifact: dict[str, Any]) -> dict[str, Any]:
    capabilities = dict(artifact.get("capabilities") or {})
    metrics = dict(artifact.get("metrics") or {})
    params = dict(artifact.get("params") or artifact.get("parameters") or {})
    if "safe_for_live_trading" in metrics and "live_trading" not in capabilities:
        capabilities["live_trading"] = bool(metrics.get("safe_for_live_trading"))
    if "safe_for_live_trading" in params and "live_trading" not in capabilities:
        capabilities["live_trading"] = bool(params.get("safe_for_live_trading"))
    return capabilities


def evaluate_model_permissions(
    artifact: dict[str, Any],
    *,
    model_type: str | None = None,
    artifact_path: str | None = None,
    require_shadow: bool = True,
) -> dict[str, Any]:
    capabilities = _capabilities_from_artifact(artifact)
    violations: list[dict[str, Any]] = []
    for key in sorted(FORBIDDEN_TRUE_CAPABILITIES):
        if bool(capabilities.get(key)):
            violations.append(
                {
                    "capability": key,
                    "expected": False,
                    "actual": capabilities.get(key),
                    "reason": "forbidden_live_or_mutating_capability",
                }
            )
    for key in sorted(REQUIRED_FALSE_CAPABILITIES):
        if capabilities.get(key) is not None and bool(capabilities.get(key)):
            violations.append(
                {
                    "capability": key,
                    "expected": False,
                    "actual": capabilities.get(key),
                    "reason": "required_false_capability_is_true",
                }
            )
    if require_shadow:
        for key in sorted(REQUIRED_TRUE_CAPABILITIES):
            if capabilities.get(key) is not None and not bool(capabilities.get(key)):
                violations.append(
                    {
                        "capability": key,
                        "expected": True,
                        "actual": capabilities.get(key),
                        "reason": "required_shadow_capability_is_false",
                    }
                )
    ok = not violations
    return {
        "ok": ok,
        "status": "allowed" if ok else "blocked",
        "model_type": str(model_type or artifact.get("model_type") or ""),
        "artifact_path": str(artifact_path or artifact.get("artifact_path") or ""),
        "capabilities": capabilities,
        "violations": violations,
        "reason": "advisory_shadow_only" if ok else "model_permission_violation",
        "guardrails": [
            "MUST NOT place orders",
            "MUST NOT close positions",
            "MUST NOT change hard risk limits",
            "MUST NOT bypass RiskPolicyService",
            "MUST remain advisory/shadow unless explicitly promoted by a separate governed path",
        ],
    }


def audit_model_permissions(
    evaluation: dict[str, Any],
    *,
    db_path: str | Path = STATE_DB,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ensure_model_permission_audit_table(db_path)
    now = time.time()
    audit_id = f"mpa:{evaluation.get('model_type') or 'model'}:{int(now * 1000)}"
    conn = _connect(db_path)
    try:
        _execute(
            conn,
            """
            INSERT INTO model_permission_audit
            (audit_id, model_type, artifact_path, status, reason, capabilities_json,
             violations_json, context_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_id,
                str(evaluation.get("model_type") or ""),
                str(evaluation.get("artifact_path") or ""),
                str(evaluation.get("status") or ""),
                str(evaluation.get("reason") or ""),
                json.dumps(evaluation.get("capabilities") or {}, ensure_ascii=False, sort_keys=True),
                json.dumps(evaluation.get("violations") or [], ensure_ascii=False, sort_keys=True),
                json.dumps(context or {}, ensure_ascii=False, sort_keys=True, default=str),
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return {**evaluation, "audit_id": audit_id, "audited_at": now}


def validate_model_artifact(
    artifact_or_path: dict[str, Any] | str | Path,
    *,
    model_type: str | None = None,
    db_path: str | Path = STATE_DB,
    context: dict[str, Any] | None = None,
    require_shadow: bool = True,
    audit: bool = True,
) -> dict[str, Any]:
    artifact_path = ""
    if isinstance(artifact_or_path, (str, Path)):
        path = Path(str(artifact_or_path))
        artifact_path = str(path)
        artifact = json.loads(path.read_text(encoding="utf-8"))
    else:
        artifact = dict(artifact_or_path or {})
        artifact_path = str(artifact.get("artifact_path") or "")
    evaluation = evaluate_model_permissions(
        artifact,
        model_type=model_type,
        artifact_path=artifact_path,
        require_shadow=require_shadow,
    )
    if audit:
        return audit_model_permissions(evaluation, db_path=db_path, context=context)
    return evaluation


def list_model_permission_audits(
    *,
    db_path: str | Path = STATE_DB,
    limit: int = 100,
    model_type: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    ensure_model_permission_audit_table(db_path)
    clauses = []
    params: list[Any] = []
    if model_type:
        clauses.append("model_type=?")
        params.append(str(model_type))
    if status:
        clauses.append("status=?")
        params.append(str(status))
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    conn = _connect(db_path, read_only=True)
    try:
        rows = _execute(
            conn,
            f"""
            SELECT *
            FROM model_permission_audit
            {where}
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (*params, int(limit)),
        ).fetchall()
        items = []
        for row in rows:
            items.append(
                {
                    "audit_id": str(row["audit_id"] or ""),
                    "model_type": str(row["model_type"] or ""),
                    "artifact_path": str(row["artifact_path"] or ""),
                    "status": str(row["status"] or ""),
                    "reason": str(row["reason"] or ""),
                    "capabilities": json.loads(row["capabilities_json"] or "{}"),
                    "violations": json.loads(row["violations_json"] or "[]"),
                    "context": json.loads(row["context_json"] or "{}"),
                    "created_at": float(row["created_at"] or 0.0),
                }
            )
        return {"items": items, "count": len(items)}
    finally:
        conn.close()
