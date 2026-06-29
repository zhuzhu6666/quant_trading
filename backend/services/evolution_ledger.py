from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, connect_sqlite, ensure_sqlite_columns


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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_runtime_config_snapshot_hash ON runtime_config_snapshot(config_hash, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_evolution_run_type ON evolution_run(run_type, status, started_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_evolution_decision_run ON evolution_decision(run_id, decision_type, created_at)")
        conn.commit()
    finally:
        conn.close()


def ensure_evolution_columns(db_path: str | Path = STATE_DB) -> None:
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
) -> dict[str, Any]:
    ensure_evolution_ledger_tables(db_path)
    payload = _as_dict(config)
    config_hash = _stable_hash(payload)
    now = time.time()
    conn = connect_sqlite(db_path)
    try:
        cur = conn.execute(
            """
            INSERT INTO runtime_config_snapshot (config_hash, source, config_json, run_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (config_hash, str(source or ""), _dumps(payload), str(run_id or ""), now),
        )
        conn.commit()
        return {
            "config_version": int(cur.lastrowid or 0),
            "config_hash": config_hash,
            "source": str(source or ""),
            "created_at": now,
        }
    finally:
        conn.close()


def current_runtime_config_snapshot(
    *,
    db_path: str | Path = STATE_DB,
    create_if_missing: bool = True,
    source: str = "runtime_current",
) -> dict[str, Any]:
    ensure_evolution_ledger_tables(db_path)
    conn = connect_sqlite(db_path)
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
    snapshot = persist_runtime_config_snapshot(config, source=f"evolution_run:{run_type}", db_path=db_path, run_id=run_id) if config is not None else current_runtime_config_snapshot(db_path=db_path)
    rid = str(run_id or _new_id("evorun"))
    now = time.time()
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO evolution_run
            (run_id, run_type, trigger_source, status, config_version, config_hash,
             summary_json, started_at, ended_at)
            VALUES (?, ?, ?, 'running', ?, ?, ?, ?, 0.0)
            """,
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
        return {"run_id": rid, **snapshot, "started_at": now}
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
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            """
            UPDATE evolution_run
            SET status=?, summary_json=?, ended_at=?
            WHERE run_id=?
            """,
            (str(status or "completed"), _dumps(summary or {}), time.time(), str(run_id or "")),
        )
        conn.commit()
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
        conn = connect_sqlite(db_path)
        conn.row_factory = __import__("sqlite3").Row
        try:
            row = conn.execute("SELECT config_version, config_hash FROM evolution_run WHERE run_id=?", (run_id,)).fetchone()
            if row:
                config_version = int(row["config_version"] or 0)
                config_hash = str(row["config_hash"] or "")
        finally:
            conn.close()
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO evolution_decision
            (decision_id, run_id, decision_type, scope_type, scope_key, action, status,
             evidence_json, risk_verdict_json, before_json, after_json, result_json,
             rollback_json, config_version, config_hash, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
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
    conn = connect_sqlite(db_path)
    conn.row_factory = __import__("sqlite3").Row
    try:
        rows = conn.execute(
            """
            SELECT *
            FROM evolution_run
            ORDER BY started_at DESC
            LIMIT ?
            """,
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
    conn = connect_sqlite(db_path)
    conn.row_factory = __import__("sqlite3").Row
    try:
        row = conn.execute("SELECT * FROM evolution_run WHERE run_id=?", (str(run_id or ""),)).fetchone()
        if not row:
            return {}
        item = dict(row)
        item["summary"] = _loads(item.pop("summary_json", "{}"), {})
        decisions = []
        for drow in conn.execute(
            """
            SELECT *
            FROM evolution_decision
            WHERE run_id=?
            ORDER BY created_at ASC
            """,
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

