from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_OWNER_SCHEMA_VERSION = "evolution_run_owner.v1"
_COORDINATOR_LOCK_NAME = "quant_autonomous_evolution_work"


def _host_boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _machine_id() -> str:
    try:
        return Path("/etc/machine-id").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _proc_start_ticks(pid: int) -> int:
    """Return Linux process start ticks, or zero when unavailable."""
    try:
        stat = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8")
        after_comm = stat.rsplit(")", 1)[1].split()
        # The first item after comm is field 3; starttime is field 22.
        return int(after_comm[19])
    except (OSError, ValueError, IndexError):
        return 0


_OWNER_IDENTITY: dict[str, Any] = {
    "schema_version": _OWNER_SCHEMA_VERSION,
    "boot_id": str(uuid.uuid4()),
    "pid": int(os.getpid()),
    "machine_id": _machine_id(),
    "host_boot_id": _host_boot_id(),
    "proc_start_ticks": _proc_start_ticks(os.getpid()),
    "process_started_at": time.time(),
    "process_role": str(os.getenv("QUANT_PROCESS_ROLE") or ""),
}


def set_evolution_owner_identity(*, boot_id: str) -> dict[str, Any]:
    """Bind ledger rows to the worker capability boot identity."""
    _OWNER_IDENTITY["boot_id"] = str(boot_id or _OWNER_IDENTITY["boot_id"])
    _OWNER_IDENTITY["pid"] = int(os.getpid())
    _OWNER_IDENTITY["machine_id"] = _machine_id()
    _OWNER_IDENTITY["host_boot_id"] = _host_boot_id()
    _OWNER_IDENTITY["proc_start_ticks"] = _proc_start_ticks(os.getpid())
    _OWNER_IDENTITY["process_started_at"] = time.time()
    _OWNER_IDENTITY["process_role"] = str(os.getenv("QUANT_PROCESS_ROLE") or "")
    return dict(_OWNER_IDENTITY)


def _refresh_owner_identity_after_fork() -> None:
    current_pid = int(os.getpid())
    if int(_OWNER_IDENTITY.get("pid") or 0) == current_pid:
        return
    _OWNER_IDENTITY.update(
        {
            "boot_id": str(uuid.uuid4()),
            "pid": current_pid,
            "machine_id": _machine_id(),
            "host_boot_id": _host_boot_id(),
            "proc_start_ticks": _proc_start_ticks(current_pid),
            "process_started_at": time.time(),
            "process_role": str(os.getenv("QUANT_PROCESS_ROLE") or ""),
        }
    )


def evolution_owner_identity() -> dict[str, Any]:
    _refresh_owner_identity_after_fork()
    return dict(_OWNER_IDENTITY)


def _owner_is_alive(owner: Any) -> bool | None:
    """Return True/False, or None when legacy identity cannot be verified."""
    if not isinstance(owner, dict) or owner.get("schema_version") != _OWNER_SCHEMA_VERSION:
        return None
    owner_machine = str(owner.get("machine_id") or "")
    current_machine = _machine_id()
    if not owner_machine or not current_machine or owner_machine != current_machine:
        return None
    owner_host = str(owner.get("host_boot_id") or "")
    current_host = _host_boot_id()
    if owner_host and current_host and owner_host != current_host:
        return False
    try:
        pid = int(owner.get("pid") or 0)
        expected_ticks = int(owner.get("proc_start_ticks") or 0)
    except (TypeError, ValueError):
        return None
    if pid <= 0 or expected_ticks <= 0:
        return None
    actual_ticks = _proc_start_ticks(pid)
    return bool(actual_ticks and actual_ticks == expected_ticks)

from backend.core.db import (
    STATE_DB,
    connect_sqlite,
    get_state_pg_conn,
    is_state_db_path,
    state_pg_enabled,
)
from backend.services.state_payloads import (
    ensure_state_payload_schema,
    payload_hash,
    put_mutation_payload,
    put_runtime_config_payload,
)


from backend.core.db_helpers import load_json as _loads


def _dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, default=str)


def _stable_hash(value: Any) -> str:
    # Keep the config hash contract identical to the governance coordinator:
    # whitespace is serialization detail, while the logical JSON content is
    # the authority binding.
    payload = json.dumps(
        value if value is not None else {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
        from backend.core.state_schema_migrations import require_state_schema_version

        conn = get_state_pg_conn(read_only=True)
        try:
            require_state_schema_version(conn)
        finally:
            conn.close()
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
                decision_json TEXT NOT NULL DEFAULT '{}',
                payload_hash TEXT NOT NULL DEFAULT '',
                canonical_event_id TEXT NOT NULL DEFAULT '',
                projection_type TEXT NOT NULL DEFAULT 'legacy',
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
        ensure_state_payload_schema(db_path)
    finally:
        conn.close()


def persist_runtime_config_snapshot(
    config: Any,
    *,
    source: str,
    db_path: str | Path = STATE_DB,
    run_id: str = "",
    mutation_id: str = "",
    conn: Any | None = None,
    created_at: float | None = None,
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
    payload_json = _dumps(payload)
    payload_hash_value = payload_hash(payload_json, namespace="runtime_config_payload.v1")
    now = float(created_at if created_at is not None else time.time())
    owned_conn = conn is None
    active_conn = conn or _connect(db_path)
    try:
        ensure_state_payload_schema(db_path, active_conn)
        if mutation_id:
            mutation_row = active_conn.execute(
                _p(
                    db_path,
                    """SELECT config_version, config_hash, source, run_id,
                              mutation_id, payload_hash, created_at
                       FROM runtime_config_snapshot
                       WHERE mutation_id=?
                       ORDER BY config_version DESC
                       LIMIT 1""",
                ),
                (str(mutation_id),),
            ).fetchone()
            if mutation_row:
                existing_mutation_hash = mutation_row["config_hash"] if hasattr(mutation_row, "keys") else mutation_row[1]
                if str(existing_mutation_hash or "") != config_hash:
                    raise ValueError(
                        f"runtime_config_snapshot_mutation_id_conflict:{mutation_id}"
                    )
                return {
                    "config_version": int(mutation_row["config_version"] if hasattr(mutation_row, "keys") else mutation_row[0] or 0),
                    "config_hash": config_hash,
                    "payload_hash": str(mutation_row["payload_hash"] if hasattr(mutation_row, "keys") else mutation_row[5] or payload_hash_value),
                    "source": str(mutation_row["source"] if hasattr(mutation_row, "keys") else mutation_row[2] or ""),
                    "run_id": str(mutation_row["run_id"] if hasattr(mutation_row, "keys") else mutation_row[3] or ""),
                    "mutation_id": str(mutation_row["mutation_id"] if hasattr(mutation_row, "keys") else mutation_row[4] or ""),
                    "created_at": float(mutation_row["created_at"] if hasattr(mutation_row, "keys") else mutation_row[6] or 0.0),
                    "reused": True,
                    "requested_source": str(source or ""),
                    "requested_run_id": str(run_id or ""),
                }
        # Identical payloads are deliberately still separate snapshot
        # occurrences.  Only an explicit mutation_id is idempotent; event
        # metadata such as source/run_id/created_at must not disappear merely
        # because the effective config did not change.
        put_runtime_config_payload(
            active_conn,
            payload_hash_value,
            payload_json,
            created_at=now,
        )
        cur = active_conn.execute(
            _p(db_path, """
            INSERT INTO runtime_config_snapshot
                (config_hash, source, config_json, run_id, mutation_id, payload_hash, created_at)
            VALUES (?, ?, '{}', ?, ?, ?, ?)
            RETURNING config_version
            """),
            (config_hash, str(source or ""), str(run_id or ""), str(mutation_id or ""), payload_hash_value, now),
        )
        row = cur.fetchone()
        config_version = row["config_version"] if hasattr(row, "keys") and "config_version" in row.keys() else (row[0] if row else 0)
        # ── canonical 增量镜像（配置载荷池；内容寻址、幂等、fail-open）──
        try:
            from backend.services.canonical_v2 import put_payload
            import json as _json_mod

            try:
                config_obj = _json_mod.loads(payload_json)
            except Exception:
                config_obj = {"config_json": str(payload_json)[:100000]}
            put_payload(
                active_conn,
                {
                    "config_version": int(config_version or 0),
                    "config_hash": str(config_hash or ""),
                    "source": str(source or ""),
                    "run_id": str(run_id or ""),
                    "mutation_id": str(mutation_id or ""),
                    "created_at": now,
                    "legacy_payload_hash": str(payload_hash_value or ""),
                    "config": config_obj,
                },
                payload_kind="runtime_config_version",
                schema_version="canonical_payload.v1",
                created_at=now,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[evolution] canonical config mirror failed config_hash=%s: %s",
                config_hash,
                exc,
            )
        if owned_conn:
            active_conn.commit()
        return {
            "config_version": int(config_version or 0),
            "config_hash": config_hash,
            "payload_hash": payload_hash_value,
            "source": str(source or ""),
            "run_id": str(run_id or ""),
            "mutation_id": str(mutation_id or ""),
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
            SELECT config_version, config_hash, source, run_id, mutation_id,
                   payload_hash, created_at
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
    stored_summary = dict(summary or {})
    # The owner identity is metadata only; the coordinator/advisory lock and
    # the domain transactions remain the execution authorities.
    stored_summary["_owner"] = evolution_owner_identity()
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
                _dumps(stored_summary),
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


def _try_acquire_coordinator_lock(conn: Any, db_path: str | Path) -> bool | None:
    """Acquire the coordinator lock in the caller's transaction.

    Keeping acquisition and the evolution_run scan/update on one connection
    closes the check-then-act window. SQLite has no process-independent
    advisory lock, so its isolated test database treats the helper as held.
    """
    if not _use_pg(db_path):
        return True
    try:
        row = conn.execute(
            _p(
                db_path,
                """
                SELECT pg_try_advisory_xact_lock(hashtext(?)) AS acquired
                """,
            ),
            (_COORDINATOR_LOCK_NAME,),
        ).fetchone()
        if row is None:
            return None
        if hasattr(row, "keys"):
            keys = list(row.keys())
            value = row[keys[0]] if keys else None
        else:
            value = row[0] if row else None
        return None if value is None else bool(value)
    except Exception:
        logger.exception("failed to acquire evolution coordinator advisory lock")
        return None


def _mark_orphaned_run_interrupted(
    conn: Any,
    db_path: str | Path,
    *,
    run_id: str,
    summary_json: str,
    ended_at: float,
) -> int:
    cursor = conn.execute(
        _p(
            db_path,
            """
            UPDATE evolution_run
            SET status='interrupted', summary_json=?, ended_at=?
            WHERE run_id=? AND status='running'
            """,
        ),
        (summary_json, ended_at, run_id),
    )
    return int(cursor.rowcount or 0)


def recover_orphaned_evolution_runs(
    *,
    db_path: str | Path = STATE_DB,
) -> dict[str, Any]:
    """Interrupt only running rows whose recorded owner is proven dead.

    New rows carry a PID plus Linux process start ticks and host boot id.  A
    PID alone is never sufficient because it can be reused. Legacy rows
    without owner identity remain on the age-based expiry path. The
    coordinator advisory xact lock is acquired on the same connection
    used for the scan and CAS updates; contention or acquisition failure
    leaves rows untouched.
    """
    ensure_evolution_ledger_tables(db_path)
    conn = _connect(db_path)
    if not _use_pg(db_path):
        conn.row_factory = __import__("sqlite3").Row
    try:
        lock_acquired = _try_acquire_coordinator_lock(conn, db_path)
        if lock_acquired is not True:
            try:
                conn.rollback()
            except Exception:
                pass
            reason = "coordinator_lock_contended" if lock_acquired is False else "coordinator_lock_unavailable"
            return {"interrupted_count": 0, "items": [], "reason": reason}
        now = time.time()
        interrupted: list[dict[str, Any]] = []
        rows = conn.execute(
            _p(
                db_path,
                """
                SELECT run_id, run_type, started_at, summary_json
                FROM evolution_run
                WHERE status='running' AND started_at > 0
                """,
            )
        ).fetchall()
        for row in rows:
            summary = _loads(row["summary_json"], {})
            owner = summary.get("_owner") if isinstance(summary, dict) else None
            owner_alive = _owner_is_alive(owner)
            if owner_alive is not False:
                continue
            run_id = str(row["run_id"] or "")
            recovery_summary = summary if isinstance(summary, dict) else {}
            recovery_summary["orphan_recovery"] = {
                "schema_version": "evolution_run_orphan_recovery.v1",
                "status": "interrupted",
                "recovered_at": now,
                "reason": "owner_process_not_alive_and_coordinator_lock_absent",
                "owner": owner,
            }
            updated = _mark_orphaned_run_interrupted(
                conn,
                db_path,
                run_id=run_id,
                summary_json=_dumps(recovery_summary),
                ended_at=now,
            )
            if updated != 1:
                continue
            interrupted.append(
                {
                    "run_id": run_id,
                    "run_type": str(row["run_type"] or ""),
                    "reason": "owner_process_not_alive_and_coordinator_lock_absent",
                }
            )
        conn.commit()
        return {"interrupted_count": len(interrupted), "items": interrupted}
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
    canonical_event_id: str = "",
    projection_type: str = "",
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
        ensure_state_payload_schema(db_path, conn)
        evidence_json = _dumps(evidence or {})
        risk_verdict_json = _dumps(risk_verdict or {})
        before_json = _dumps(before or {})
        after_json = _dumps(after or {})
        result_json = _dumps(result or {})
        rollback_json = _dumps(rollback or {})
        mutation_parts = {
            "evidence_json": evidence_json,
            "risk_verdict_json": risk_verdict_json,
            "before_json": before_json,
            "after_json": after_json,
            "result_json": result_json,
            "rollback_json": rollback_json,
        }
        mutation_hash = payload_hash(
            "\x00".join(f"{key}={mutation_parts[key]}" for key in sorted(mutation_parts)),
            namespace="mutation_payload.v1",
        )
        put_mutation_payload(conn, mutation_hash, mutation_parts)
        canonical_id = str(canonical_event_id or did)
        projection = str(projection_type or ("canonical" if canonical_id == did else "projection"))
        # Converged 8-column shape (PG == SQLite): rich semantic fields live in
        # decision_json, evidence/risk_verdict/before/after/result/rollback stay
        # interned via payload_hash -> mutation_payload, canonical linkage in columns.
        decision_json = _dumps(
            {
                "scope_type": str(scope_type or ""),
                "scope_key": str(scope_key or ""),
                "action": str(action or ""),
                "status": str(status or ""),
                "config_version": int(config_version or 0),
                "config_hash": str(config_hash or ""),
            }
        )
        conn.execute(
            _p(db_path, """
            INSERT INTO evolution_decision
            (decision_id, run_id, decision_type, decision_json, payload_hash,
             canonical_event_id, projection_type, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(decision_id) DO UPDATE SET
                run_id=excluded.run_id,
                decision_type=excluded.decision_type,
                decision_json=excluded.decision_json,
                payload_hash=excluded.payload_hash,
                canonical_event_id=excluded.canonical_event_id,
                projection_type=excluded.projection_type,
                created_at=excluded.created_at
            """),
            (
                did,
                str(run_id or ""),
                str(decision_type or ""),
                decision_json,
                mutation_hash,
                canonical_id,
                projection,
                time.time(),
            ),
        )
        # ── canonical 增量镜像（governance_command；同事务、幂等、fail-open）──
        try:
            from backend.services.canonical_v2 import record_governance_command_event
            record_governance_command_event(
                conn,
                decision_id=did,
                run_id=str(run_id or ""),
                decision_type=str(decision_type or ""),
                scope_type=str(scope_type or ""),
                scope_key=str(scope_key or ""),
                action=str(action or ""),
                status=str(status or ""),
                config_version=int(config_version or 0),
                config_hash=str(config_hash or ""),
                created_at=time.time(),
                legacy_payload_hash=str(mutation_hash or ""),
                evidence=evidence,
                risk_verdict=risk_verdict,
                before=before,
                after=after,
                result=result,
                rollback=rollback,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[evolution] canonical command mirror failed decision_id=%s: %s",
                did,
                exc,
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
    ensure_state_payload_schema(db_path)
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
            SELECT d.decision_id, d.run_id, d.decision_type, d.decision_json,
                   d.payload_hash, d.canonical_event_id, d.projection_type, d.created_at,
                   p.evidence_json, p.risk_verdict_json, p.before_json, p.after_json,
                   p.result_json, p.rollback_json,
                   cd.projection_type AS cd_projection_type,
                   cp.before_json AS cp_before_json, cp.after_json AS cp_after_json,
                   cp.result_json AS cp_result_json, cp.rollback_json AS cp_rollback_json
            FROM evolution_decision d
            LEFT JOIN mutation_payload p ON p.payload_hash=d.payload_hash
            LEFT JOIN evolution_decision cd
                   ON cd.decision_id=d.canonical_event_id
            LEFT JOIN mutation_payload cp ON cp.payload_hash=cd.payload_hash
            WHERE d.run_id=?
            ORDER BY d.created_at ASC
            """),
            (str(run_id or ""),),
        ).fetchall():
            d = dict(drow)
            meta = _loads(d.pop("decision_json", ""), {})
            is_api = d.get("projection_type") == "api" and d.get("cd_projection_type") == "canonical"
            decisions.append(
                {
                    "decision_id": d.get("decision_id"),
                    "run_id": d.get("run_id"),
                    "decision_type": d.get("decision_type"),
                    "scope_type": meta.get("scope_type", ""),
                    "scope_key": meta.get("scope_key", ""),
                    "action": meta.get("action", ""),
                    "status": meta.get("status", ""),
                    "evidence": _loads(d.get("evidence_json"), {}),
                    "risk_verdict": _loads(d.get("risk_verdict_json"), {}),
                    "before": _loads((d.get("cp_before_json") if is_api else d.get("before_json")), {}),
                    "after": _loads((d.get("cp_after_json") if is_api else d.get("after_json")), {}),
                    "result": _loads((d.get("cp_result_json") if is_api else d.get("result_json")), {}),
                    "rollback": _loads((d.get("cp_rollback_json") if is_api else d.get("rollback_json")), {}),
                    "config_version": meta.get("config_version", 0),
                    "config_hash": meta.get("config_hash", ""),
                    "payload_hash": d.get("payload_hash", ""),
                    "canonical_event_id": d.get("canonical_event_id", ""),
                    "projection_type": d.get("projection_type", ""),
                    "created_at": d.get("created_at"),
                }
            )
        item["decisions"] = decisions
        return item
    finally:
        conn.close()
