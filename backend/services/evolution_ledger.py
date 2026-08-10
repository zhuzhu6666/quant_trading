from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from backend.core.db import (
    STATE_DB,
    connect_sqlite,
    ensure_sqlite_columns,
    get_state_pg_conn,
    is_state_db_path,
    state_pg_enabled,
)


def _dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, default=str)


def _loads(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except Exception:
        return default


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(_dumps(value).encode("utf-8")).hexdigest()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _use_pg(db_path: str | Path) -> bool:
    return is_state_db_path(db_path)


def _connect(db_path: str | Path, *, read_only: bool = False):
    return get_state_pg_conn(read_only=read_only) if _use_pg(db_path) else connect_sqlite(db_path, read_only=read_only)


def _p(db_path: str | Path, sql: str) -> str:
    return sql.replace("?", "%s") if _use_pg(db_path) else sql


def _as_dict(config: Any) -> dict[str, Any]:
    if config is None:
        return {}
    if isinstance(config, dict):
        return dict(config)
    if is_dataclass(config):
        return asdict(config)
    if hasattr(config, "to_dict"):
        try:
            return dict(config.to_dict())
        except Exception:
            return {}
    return {}


def ensure_evolution_ledger_tables(db_path: str | Path = STATE_DB) -> None:
    if _use_pg(db_path):
        return
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runtime_config_snapshot (
                config_version INTEGER PRIMARY KEY AUTOINCREMENT,
                config_hash TEXT NOT NULL,
                source TEXT DEFAULT '',
                config_json TEXT NOT NULL DEFAULT '{}',
                run_id TEXT DEFAULT '',
                mutation_id TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL DEFAULT 0.0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runtime_config_overlay (
                overlay_id TEXT PRIMARY KEY,
                overlay_json TEXT NOT NULL DEFAULT '{}',
                overlay_hash TEXT DEFAULT '',
                source TEXT DEFAULT '',
                run_id TEXT DEFAULT '',
                mutation_id TEXT NOT NULL DEFAULT '',
                legacy_authority_json TEXT NOT NULL DEFAULT '{}',
                updated_at REAL NOT NULL DEFAULT 0.0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS factor_catalog_snapshot (
                snapshot_id TEXT PRIMARY KEY,
                run_id TEXT DEFAULT '',
                catalog_hash TEXT DEFAULT '',
                catalog_json TEXT NOT NULL DEFAULT '[]',
                source TEXT DEFAULT '',
                created_at REAL NOT NULL DEFAULT 0.0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS evolution_run (
                run_id TEXT PRIMARY KEY,
                run_type TEXT NOT NULL,
                trigger_source TEXT DEFAULT '',
                status TEXT DEFAULT 'running',
                config_version INTEGER DEFAULT 0,
                config_hash TEXT DEFAULT '',
                summary_json TEXT DEFAULT '{}',
                started_at REAL NOT NULL DEFAULT 0.0,
                ended_at REAL DEFAULT 0.0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS evolution_decision (
                decision_id TEXT PRIMARY KEY,
                run_id TEXT DEFAULT '',
                decision_type TEXT NOT NULL,
                scope_type TEXT DEFAULT '',
                scope_key TEXT DEFAULT '',
                action TEXT DEFAULT '',
                status TEXT DEFAULT '',
                evidence_json TEXT DEFAULT '{}',
                risk_verdict_json TEXT DEFAULT '{}',
                before_json TEXT DEFAULT '{}',
                after_json TEXT DEFAULT '{}',
                result_json TEXT DEFAULT '{}',
                rollback_json TEXT DEFAULT '{}',
                config_version INTEGER DEFAULT 0,
                config_hash TEXT DEFAULT '',
                created_at REAL NOT NULL DEFAULT 0.0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS replay_report (
                replay_run_id TEXT PRIMARY KEY,
                scope_json TEXT NOT NULL DEFAULT '{}',
                input_dataset_hash TEXT DEFAULT '',
                runtime_config_hash TEXT DEFAULT '',
                code_version TEXT DEFAULT '',
                decision_count INTEGER DEFAULT 0,
                matched_live_count INTEGER DEFAULT 0,
                mismatch_count INTEGER DEFAULT 0,
                metric_summary_json TEXT NOT NULL DEFAULT '{}',
                replay_error TEXT DEFAULT '',
                evidence_grade TEXT DEFAULT '',
                artifact_path TEXT DEFAULT '',
                artifact_hash TEXT DEFAULT '',
                status TEXT DEFAULT 'completed',
                created_at REAL NOT NULL DEFAULT 0.0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS release_run (
                run_id TEXT PRIMARY KEY,
                release_class TEXT DEFAULT '',
                status TEXT DEFAULT 'started',
                summary_json TEXT NOT NULL DEFAULT '{}',
                checklist_json TEXT NOT NULL DEFAULT '{}',
                runtime_config_hash TEXT DEFAULT '',
                replay_run_id TEXT DEFAULT '',
                replay_artifact_hash TEXT DEFAULT '',
                incident_mode TEXT DEFAULT '',
                readiness_posture TEXT DEFAULT '',
                tests_json TEXT NOT NULL DEFAULT '[]',
                rollback_ref_json TEXT NOT NULL DEFAULT '{}',
                created_by TEXT DEFAULT '',
                created_at REAL NOT NULL DEFAULT 0.0,
                updated_at REAL NOT NULL DEFAULT 0.0
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_runtime_config_snapshot_hash ON runtime_config_snapshot(config_hash, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_factor_catalog_snapshot_created ON factor_catalog_snapshot(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_evolution_run_type ON evolution_run(run_type, status, started_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_evolution_decision_run ON evolution_decision(run_id, decision_type, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_replay_report_created ON replay_report(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_replay_report_grade ON replay_report(evidence_grade, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_release_run_created ON release_run(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_release_run_status ON release_run(status, created_at)")
        conn.commit()
    finally:
        conn.close()


def ensure_evolution_columns(db_path: str | Path = STATE_DB) -> None:
    if _use_pg(db_path):
        return
    ensure_sqlite_columns(
        db_path,
        "position_supervisor_trace",
        {
            "trace_integrity": "trace_integrity TEXT DEFAULT 'full'",
            "config_version": "config_version INTEGER DEFAULT 0",
            "config_hash": "config_hash TEXT DEFAULT ''",
            "evolution_run_id": "evolution_run_id TEXT DEFAULT ''",
        },
    )
    ensure_sqlite_columns(
        db_path,
        "autonomous_learning_sample",
        {
            "config_version": "config_version INTEGER DEFAULT 0",
            "config_hash": "config_hash TEXT DEFAULT ''",
            "evolution_run_id": "evolution_run_id TEXT DEFAULT ''",
        },
    )


def persist_runtime_config_snapshot(
    config: Any,
    *,
    source: str,
    db_path: str | Path = STATE_DB,
    run_id: str = "",
    conn: Any | None = None,
) -> dict[str, Any]:
    """Persist a config snapshot, optionally inside the caller's transaction.

    Passing ``conn`` lets the runtime overlay and its matching snapshot commit
    atomically.  Existing callers keep the historical owned-connection
    behaviour.
    """
    if conn is None:
        ensure_evolution_ledger_tables(db_path)
    from config.runtime_config import canonical_runtime_config_payload

    payload = canonical_runtime_config_payload(_as_dict(config))
    config_hash = _stable_hash(payload)
    now = time.time()
    owned_conn = conn is None
    active_conn = conn or _connect(db_path)
    try:
        existing = active_conn.execute(
            _p(db_path, """
            SELECT config_version, config_hash, source, run_id, created_at
            FROM runtime_config_snapshot
            ORDER BY config_version DESC
            LIMIT 1
            """),
        ).fetchone()
        if existing:
            existing_hash = existing["config_hash"] if hasattr(existing, "keys") else existing[1]
            existing_source_value = existing["source"] if hasattr(existing, "keys") else existing[2]
            existing_run_value = existing["run_id"] if hasattr(existing, "keys") else existing[3]
            if str(existing_hash or "") == config_hash:
                config_version = existing["config_version"] if hasattr(existing, "keys") else existing[0]
                existing_source = existing_source_value
                existing_run_id = existing_run_value
                existing_created_at = existing["created_at"] if hasattr(existing, "keys") else existing[4]
                return {
                    "config_version": int(config_version or 0),
                    "config_hash": config_hash,
                    "source": str(existing_source or ""),
                    "run_id": str(existing_run_id or ""),
                    "created_at": float(existing_created_at or 0.0),
                    "reused": True,
                    "requested_source": str(source or ""),
                    "requested_run_id": str(run_id or ""),
                }
        cur = active_conn.execute(
            _p(db_path, """
            INSERT INTO runtime_config_snapshot (config_hash, source, config_json, run_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            RETURNING config_version
            """),
            (config_hash, str(source or ""), _dumps(payload), str(run_id or ""), now),
        )
        row = cur.fetchone()
        config_version = row["config_version"] if hasattr(row, "keys") and "config_version" in row.keys() else (row[0] if row else 0)
        if owned_conn:
            active_conn.commit()
        return {
            "config_version": int(config_version or 0),
            "config_hash": config_hash,
            "source": str(source or ""),
            "created_at": now,
            "reused": False,
        }
    finally:
        if owned_conn:
            active_conn.close()


def current_runtime_config_snapshot(
    *,
    db_path: str | Path = STATE_DB,
    create_if_missing: bool = True,
    source: str = "runtime_current",
) -> dict[str, Any]:
    ensure_evolution_ledger_tables(db_path)
    conn = _connect(db_path, read_only=True)
    if not _use_pg(db_path):
        conn.row_factory = __import__("sqlite3").Row
    try:
        row = conn.execute(
            """
            SELECT config_version, config_hash, source, created_at
            FROM runtime_config_snapshot
            ORDER BY config_version DESC
            LIMIT 1
            """
        ).fetchone()
        if row:
            return dict(row)
    finally:
        conn.close()
    if not create_if_missing:
        return {"config_version": 0, "config_hash": "", "source": "", "created_at": 0.0}
    try:
        from config.runtime_config import shared

        return persist_runtime_config_snapshot(shared(), source=source, db_path=db_path)
    except Exception:
        return {"config_version": 0, "config_hash": "", "source": "", "created_at": 0.0}


def start_evolution_run(
    *,
    run_type: str,
    trigger_source: str = "",
    db_path: str | Path = STATE_DB,
    run_id: str = "",
    config: Any = None,
    summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ensure_evolution_ledger_tables(db_path)
    expire_stale_evolution_runs(db_path=db_path)
    snapshot = persist_runtime_config_snapshot(config, source=f"evolution_run:{run_type}", db_path=db_path, run_id=run_id) if config is not None else current_runtime_config_snapshot(db_path=db_path)
    rid = str(run_id or _new_id("evorun"))
    now = time.time()
    conn = _connect(db_path)
    try:
        conn.execute(
            _p(db_path, """
            INSERT INTO evolution_run
            (run_id, run_type, trigger_source, status, config_version, config_hash,
             summary_json, started_at, ended_at)
            VALUES (?, ?, ?, 'running', ?, ?, ?, ?, 0.0)
            ON CONFLICT(run_id) DO UPDATE SET
                run_type=excluded.run_type,
                trigger_source=excluded.trigger_source,
                status=excluded.status,
                config_version=excluded.config_version,
                config_hash=excluded.config_hash,
                summary_json=excluded.summary_json,
                started_at=excluded.started_at,
                ended_at=excluded.ended_at
            """),
            (
                rid,
                str(run_type or ""),
                str(trigger_source or ""),
                int(snapshot.get("config_version") or 0),
                str(snapshot.get("config_hash") or ""),
                _dumps(summary or {}),
                now,
            ),
        )
        conn.commit()
        # Runtime-config snapshots also carry the run_id that created the
        # snapshot. The evolution ledger owns a distinct run id, so it must
        # win when callers later pass this payload to finish_evolution_run().
        return {**snapshot, "run_id": rid, "started_at": now}
    finally:
        conn.close()


def finish_evolution_run(
    run_id: str,
    *,
    status: str = "completed",
    summary: dict[str, Any] | None = None,
    db_path: str | Path = STATE_DB,
) -> None:
    ensure_evolution_ledger_tables(db_path)
    conn = _connect(db_path)
    try:
        conn.execute(
            _p(db_path, """
            UPDATE evolution_run
            SET status=?, summary_json=?, ended_at=?
            WHERE run_id=?
            """),
            (str(status or "completed"), _dumps(summary or {}), time.time(), str(run_id or "")),
        )
        conn.commit()
    finally:
        conn.close()


def expire_stale_evolution_runs(
    *,
    db_path: str | Path = STATE_DB,
    max_age_sec: float = 3600,
    run_type_max_age_sec: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Mark interrupted running evolution runs as expired.

    This only changes the run status ledger. It does not mutate samples,
    decisions, runtime config, or any trading state.
    """
    ensure_evolution_ledger_tables(db_path)
    now = time.time()
    run_type_max_age_sec = dict(run_type_max_age_sec or {})
    conn = _connect(db_path)
    if not _use_pg(db_path):
        conn.row_factory = __import__("sqlite3").Row
    try:
        rows = conn.execute(
            _p(db_path, """
            SELECT run_id, run_type, started_at, summary_json
            FROM evolution_run
            WHERE status='running' AND started_at > 0
            """)
        ).fetchall()
        expired = []
        for row in rows:
            run_type = str(row["run_type"] or "")
            max_age = float(run_type_max_age_sec.get(run_type, max_age_sec))
            age_sec = max(0.0, now - float(row["started_at"] or 0.0))
            if age_sec < max_age:
                continue
            summary = _loads(row["summary_json"], {})
            summary["expired_by"] = "expire_stale_evolution_runs"
            summary["expired_at"] = now
            summary["age_sec"] = round(age_sec, 3)
            conn.execute(
                _p(db_path, """
                UPDATE evolution_run
                SET status='expired', summary_json=?, ended_at=?
                WHERE run_id=? AND status='running'
                """),
                (_dumps(summary), now, str(row["run_id"] or "")),
            )
            expired.append(
                {
                    "run_id": str(row["run_id"] or ""),
                    "run_type": run_type,
                    "age_sec": round(age_sec, 3),
                }
            )
        conn.commit()
        return {"expired_count": len(expired), "items": expired}
    finally:
        conn.close()


def record_evolution_decision(
    *,
    run_id: str = "",
    decision_type: str,
    scope_type: str = "",
    scope_key: str = "",
    action: str = "",
    status: str = "",
    evidence: dict[str, Any] | None = None,
    risk_verdict: dict[str, Any] | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    rollback: dict[str, Any] | None = None,
    config_version: int = 0,
    config_hash: str = "",
    db_path: str | Path = STATE_DB,
    decision_id: str = "",
) -> str:
    ensure_evolution_ledger_tables(db_path)
    did = str(decision_id or _new_id("evodec"))
    if (not config_version or not config_hash) and run_id:
        conn = _connect(db_path, read_only=True)
        if not _use_pg(db_path):
            conn.row_factory = __import__("sqlite3").Row
        try:
            row = conn.execute(_p(db_path, "SELECT config_version, config_hash FROM evolution_run WHERE run_id=?"), (run_id,)).fetchone()
            if row:
                config_version = int(row["config_version"] or 0)
                config_hash = str(row["config_hash"] or "")
        finally:
            conn.close()
    conn = _connect(db_path)
    try:
        conn.execute(
            _p(db_path, """
            INSERT INTO evolution_decision
            (decision_id, run_id, decision_type, scope_type, scope_key, action, status,
             evidence_json, risk_verdict_json, before_json, after_json, result_json,
             rollback_json, config_version, config_hash, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(decision_id) DO UPDATE SET
                run_id=excluded.run_id,
                decision_type=excluded.decision_type,
                scope_type=excluded.scope_type,
                scope_key=excluded.scope_key,
                action=excluded.action,
                status=excluded.status,
                evidence_json=excluded.evidence_json,
                risk_verdict_json=excluded.risk_verdict_json,
                before_json=excluded.before_json,
                after_json=excluded.after_json,
                result_json=excluded.result_json,
                rollback_json=excluded.rollback_json,
                config_version=excluded.config_version,
                config_hash=excluded.config_hash,
                created_at=excluded.created_at
            """),
            (
                did,
                str(run_id or ""),
                str(decision_type or ""),
                str(scope_type or ""),
                str(scope_key or ""),
                str(action or ""),
                str(status or ""),
                _dumps(evidence or {}),
                _dumps(risk_verdict or {}),
                _dumps(before or {}),
                _dumps(after or {}),
                _dumps(result or {}),
                _dumps(rollback or {}),
                int(config_version or 0),
                str(config_hash or ""),
                time.time(),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return did


def list_evolution_runs(*, db_path: str | Path = STATE_DB, limit: int = 100) -> dict[str, Any]:
    ensure_evolution_ledger_tables(db_path)
    expire_stale_evolution_runs(db_path=db_path)
    conn = _connect(db_path, read_only=True)
    if not _use_pg(db_path):
        conn.row_factory = __import__("sqlite3").Row
    try:
        rows = conn.execute(
            _p(db_path, """
            SELECT *
            FROM evolution_run
            ORDER BY started_at DESC
            LIMIT ?
            """),
            (max(1, int(limit)),),
        ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["summary"] = _loads(item.pop("summary_json", "{}"), {})
            items.append(item)
        return {"items": items}
    finally:
        conn.close()


def get_evolution_run(run_id: str, *, db_path: str | Path = STATE_DB) -> dict[str, Any]:
    ensure_evolution_ledger_tables(db_path)
    conn = _connect(db_path, read_only=True)
    if not _use_pg(db_path):
        conn.row_factory = __import__("sqlite3").Row
    try:
        row = conn.execute(_p(db_path, "SELECT * FROM evolution_run WHERE run_id=?"), (str(run_id or ""),)).fetchone()
        if not row:
            return {}
        item = dict(row)
        item["summary"] = _loads(item.pop("summary_json", "{}"), {})
        decisions = []
        for drow in conn.execute(
            _p(db_path, """
            SELECT *
            FROM evolution_decision
            WHERE run_id=?
            ORDER BY created_at ASC
            """),
            (str(run_id or ""),),
        ).fetchall():
            d = dict(drow)
            for key in ("evidence_json", "risk_verdict_json", "before_json", "after_json", "result_json", "rollback_json"):
                d[key[:-5] if key.endswith("_json") else key] = _loads(d.pop(key, "{}"), {})
            decisions.append(d)
        item["decisions"] = decisions
        return item
    finally:
        conn.close()
