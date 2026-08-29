from __future__ import annotations

import json
import hashlib
import time
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, connect_sqlite, get_state_pg_conn, is_state_db_path, state_table_exists
from backend.core.db_helpers import conn_is_pg as _conn_is_pg, pg_sql as _sql
from backend.core.db_helpers import dump_json as _dump_json
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

_AUDIT_CONTEXT_VOLATILE_KEYS = {
    "run_id",
    "trace_id",
    "request_id",
    "correlation_id",
    "timestamp",
    "created_at",
    "updated_at",
    "operation",
}


def _stable_value(value: Any) -> Any:
    """Return a JSON-safe semantic value for permission identity hashing."""

    if isinstance(value, dict):
        return {
            str(key): _stable_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key) not in _AUDIT_CONTEXT_VOLATILE_KEYS
        }
    if isinstance(value, (list, tuple, set)):
        values = [_stable_value(item) for item in value]
        # Violations/capabilities are sets semantically; sorting their JSON
        # forms prevents call-site ordering from creating a new audit row.
        return sorted(values, key=lambda item: _dump_json(item))
    return value


def artifact_permission_identity(
    artifact: dict[str, Any],
    *,
    artifact_path: str | Path | None = None,
) -> str:
    """Return a stable identity that changes when an artifact is replaced.

    Artifact metadata normally contains a content hash.  For older artifacts
    without one, the metadata/model file path, size and nanosecond mtime form a
    cheap replacement identity; this avoids hashing a model file on every hot
    scoring call while still invalidating the cache on replacement.
    """

    metadata: dict[str, Any] = {
        "model_type": str(artifact.get("model_type") or ""),
        "model_version": str(artifact.get("model_version") or ""),
        "feature_schema_version": str(artifact.get("feature_schema_version") or ""),
        "artifact_sha256": str(artifact.get("artifact_sha256") or ""),
        "model_file_sha256": str(
            artifact.get("model_file_sha256")
            or artifact.get("model_sha256")
            or ""
        ),
    }
    paths = {
        "artifact": artifact_path or artifact.get("artifact_path") or "",
        "model_file": artifact.get("model_file") or "",
    }
    for label, raw_path in paths.items():
        text = str(raw_path or "").strip()
        if not text:
            continue
        path = Path(text).expanduser()
        try:
            stat = path.stat()
            metadata[label] = {
                "path": str(path.resolve()),
                "size": int(stat.st_size),
                "mtime_ns": int(stat.st_mtime_ns),
            }
        except OSError:
            metadata[label] = {"path": str(path)}
    return hashlib.sha256(_dump_json(metadata).encode("utf-8")).hexdigest()


def _permission_audit_id(
    evaluation: dict[str, Any],
    *,
    context: dict[str, Any] | None = None,
) -> str:
    del context
    payload = {
        "model_type": str(evaluation.get("model_type") or ""),
        "artifact_identity": str(evaluation.get("artifact_identity") or ""),
        "status": str(evaluation.get("status") or ""),
        "reason": str(evaluation.get("reason") or ""),
        "capabilities": _stable_value(evaluation.get("capabilities") or {}),
        "violations": _stable_value(evaluation.get("violations") or []),
    }
    digest = hashlib.sha256(_dump_json(payload).encode("utf-8")).hexdigest()
    return f"mpa:{digest}"


def _use_pg(db_path: str | Path = STATE_DB) -> bool:
    return is_state_db_path(db_path)


def _connect(db_path: str | Path = STATE_DB, *, read_only: bool = False):
    conn = get_state_pg_conn(read_only=read_only) if _use_pg(db_path) else connect_sqlite(db_path, read_only=read_only)
    if not _use_pg(db_path):
        conn.row_factory = __import__("sqlite3").Row
    return conn


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
        "artifact_identity": artifact_permission_identity(
            artifact,
            artifact_path=artifact_path,
        ),
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
    audit_context = dict(context or {})
    # Keep the artifact identity in the existing context column so cleanup
    # can distinguish a replacement model file at the same path.  It is
    # semantic permission evidence, not a per-score occurrence field.
    audit_context.setdefault(
        "artifact_identity",
        str(evaluation.get("artifact_identity") or ""),
    )
    audit_id = _permission_audit_id(evaluation, context=audit_context)
    conn = _connect(db_path)
    try:
        if _conn_is_pg(conn):
            # The deterministic id is the idempotency boundary.  The
            # explicit lock keeps two concurrent scoring callers from both
            # passing the NOT EXISTS check even on legacy installations where
            # audit_id was not declared as a unique constraint.
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (audit_id,))
        cursor = _execute(
            conn,
            """
            INSERT INTO model_permission_audit
            (audit_id, model_type, artifact_path, status, reason, capabilities_json,
             violations_json, context_json, created_at)
            SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?
            WHERE NOT EXISTS (
                SELECT 1 FROM model_permission_audit WHERE audit_id=?
            )
            """,
            (
                audit_id,
                str(evaluation.get("model_type") or ""),
                str(evaluation.get("artifact_path") or ""),
                str(evaluation.get("status") or ""),
                str(evaluation.get("reason") or ""),
                json.dumps(evaluation.get("capabilities") or {}, ensure_ascii=False, sort_keys=True),
                json.dumps(evaluation.get("violations") or [], ensure_ascii=False, sort_keys=True),
                json.dumps(audit_context, ensure_ascii=False, sort_keys=True, default=str),
                now,
                audit_id,
            ),
        )
        conn.commit()
        inserted = int(getattr(cursor, "rowcount", 1) or 0) > 0
    finally:
        conn.close()
    return {
        **evaluation,
        "audit_id": audit_id,
        "audited_at": now,
        "audit_reused": not inserted,
    }


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
