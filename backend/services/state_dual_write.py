"""Best-effort PostgreSQL dual-write audit for state.db migration.

SQLite remains the source of truth.  This module records replayable outbox
events after the primary SQLite ledger write succeeds, then asynchronously
copies those events into PostgreSQL when configured.  PostgreSQL outages must
never block trading, risk, or the live loop.
"""

from __future__ import annotations

import json
import os
import hashlib
import threading
import time
from pathlib import Path
from typing import Any

from loguru import logger

from backend.core.db import STATE_DB, connect_sqlite

SCHEMA_VERSION = "state_dual_write.v1"
EVENT_DECISION_LEDGER = "decision_ledger.v1"
EVENT_STATE_ROW = "state_row.v1"
PENDING_STATUSES = ("pending", "retry")

_worker: "StateDualWriteWorker | None" = None
_worker_lock = threading.Lock()
_schema_ready: set[str] = set()
_schema_lock = threading.Lock()


def _env_value(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value is not None:
        return value
    env_path = Path(".env")
    if not env_path.exists():
        return default
    try:
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            key, val = raw.split("=", 1)
            if key.strip() == name:
                return val.strip().strip('"').strip("'")
    except Exception:
        return default
    return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env_value(name, "1" if default else "0").strip().lower()
    return raw in {"1", "true", "yes", "on", "y"}


def audit_pg_dsn() -> str:
    return _env_value("QUANT_AUDIT_PG_DSN", "")


def dual_write_enabled() -> bool:
    return bool(audit_pg_dsn()) and _env_bool("QUANT_AUDIT_PG_DUAL_WRITE", False)


def ensure_outbox_schema(db_path: str | Path = STATE_DB) -> None:
    key = str(Path(db_path).resolve())
    with _schema_lock:
        if key in _schema_ready:
            return
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS state_dual_write_outbox (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT DEFAULT '',
                created_at REAL NOT NULL DEFAULT 0.0,
                updated_at REAL NOT NULL DEFAULT 0.0,
                synced_at REAL DEFAULT 0.0
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_state_dual_write_outbox_status
            ON state_dual_write_outbox(status, updated_at)
            """
        )
        conn.commit()
        with _schema_lock:
            _schema_ready.add(key)
    finally:
        conn.close()


def ensure_outbox_schema_on_conn(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS state_dual_write_outbox (
            event_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT DEFAULT '',
            created_at REAL NOT NULL DEFAULT 0.0,
            updated_at REAL NOT NULL DEFAULT 0.0,
            synced_at REAL DEFAULT 0.0
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_state_dual_write_outbox_status
        ON state_dual_write_outbox(status, updated_at)
        """
    )


def _payload_hash(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _event_id(prefix: str, *parts: Any) -> str:
    raw = "|".join(str(part) for part in parts)
    return f"{prefix}_{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:24]}"


def _enqueue_outbox_event_on_conn(
    conn,
    *,
    event_id: str,
    event_type: str,
    payload: dict[str, Any],
    now: float | None = None,
) -> bool:
    if not dual_write_enabled():
        return False
    ts = float(now or time.time())
    ensure_outbox_schema_on_conn(conn)
    conn.execute(
        """
        INSERT INTO state_dual_write_outbox
        (event_id, event_type, payload_json, status, attempts, last_error, created_at, updated_at, synced_at)
        VALUES (?, ?, ?, 'pending', 0, '', ?, ?, 0.0)
        ON CONFLICT(event_id) DO UPDATE SET
            event_type=excluded.event_type,
            payload_json=excluded.payload_json,
            status=CASE
                WHEN state_dual_write_outbox.status='synced' THEN 'synced'
                ELSE 'pending'
            END,
            updated_at=excluded.updated_at
        """,
        (
            event_id,
            event_type,
            json.dumps(payload, ensure_ascii=False, default=str),
            ts,
            ts,
        ),
    )
    return True


def _state_row_payload(
    *,
    db_path: str | Path,
    table_name: str,
    entity_key: str,
    row: dict[str, Any],
    operation: str,
    source_updated_at: float | None,
    event_id: str,
    now: float,
) -> dict[str, Any]:
    row_payload = dict(row)
    return {
        "schema_version": SCHEMA_VERSION,
        "source_db": str(Path(db_path)),
        "outbox_event_id": event_id,
        "event_type": EVENT_STATE_ROW,
        "table_name": str(table_name),
        "entity_key": str(entity_key),
        "operation": str(operation or "upsert"),
        "payload_hash": _payload_hash(row_payload),
        "source_updated_at": float(source_updated_at or row_payload.get("updated_at") or row_payload.get("created_at") or now),
        "row": row_payload,
        "created_at": now,
    }


def enqueue_state_row_event_on_conn(
    conn,
    *,
    db_path: str | Path,
    table_name: str,
    entity_key: str,
    row: dict[str, Any],
    operation: str = "upsert",
    source_updated_at: float | None = None,
) -> bool:
    """Queue a full-fidelity state row mirror event inside the caller's SQLite transaction."""
    if not dual_write_enabled():
        return False
    now = time.time()
    row_payload = dict(row)
    event_id = _event_id(
        "state",
        table_name,
        entity_key,
        operation,
        source_updated_at or row_payload.get("updated_at") or row_payload.get("created_at") or now,
        _payload_hash(row_payload),
    )
    payload = _state_row_payload(
        db_path=db_path,
        table_name=table_name,
        entity_key=entity_key,
        row=row_payload,
        operation=operation,
        source_updated_at=source_updated_at,
        event_id=event_id,
        now=now,
    )
    return _enqueue_outbox_event_on_conn(
        conn,
        event_id=event_id,
        event_type=EVENT_STATE_ROW,
        payload=payload,
        now=now,
    )


def enqueue_state_row_event(
    *,
    db_path: str | Path,
    table_name: str,
    entity_key: str,
    row: dict[str, Any],
    operation: str = "upsert",
    source_updated_at: float | None = None,
) -> bool:
    """Queue a state row mirror event using a short standalone SQLite transaction."""
    if not dual_write_enabled():
        return False
    try:
        conn = connect_sqlite(db_path)
        try:
            queued = enqueue_state_row_event_on_conn(
                conn,
                db_path=db_path,
                table_name=table_name,
                entity_key=entity_key,
                row=row,
                operation=operation,
                source_updated_at=source_updated_at,
            )
            conn.commit()
            return queued
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("[state_dual_write] state enqueue failed for {}:{}: {}", table_name, entity_key, exc)
        return False


def enqueue_decision_ledger_event(
    *,
    db_path: str | Path,
    decision: dict[str, Any],
    factor_snapshots: list[dict[str, Any]],
) -> bool:
    """Add a replayable outbox event after SQLite ledger persistence succeeds."""
    if not dual_write_enabled():
        return False
    event_id = str(decision.get("decision_id") or "")
    if not event_id:
        return False
    now = time.time()
    payload = {
        "schema_version": SCHEMA_VERSION,
        "source_db": str(Path(db_path)),
        "outbox_event_id": event_id,
        "event_type": EVENT_DECISION_LEDGER,
        "decision": decision,
        "factor_snapshots": [
            {"snapshot_seq": idx + 1, **row}
            for idx, row in enumerate(factor_snapshots)
        ],
        "created_at": now,
    }
    try:
        ensure_outbox_schema(db_path)
        conn = connect_sqlite(db_path)
        try:
            queued = _enqueue_outbox_event_on_conn(
                conn,
                event_id=event_id,
                event_type=EVENT_DECISION_LEDGER,
                payload=payload,
                now=now,
            )
            conn.commit()
            return queued
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("[state_dual_write] enqueue failed for {}: {}", event_id, exc)
        return False


def outbox_status(db_path: str | Path = STATE_DB) -> dict[str, Any]:
    ensure_outbox_schema(db_path)
    conn = connect_sqlite(db_path, read_only=True)
    conn.row_factory = __import__("sqlite3").Row
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM state_dual_write_outbox GROUP BY status"
        ).fetchall()
        latest = conn.execute(
            """
            SELECT event_id, status, attempts, last_error, updated_at, synced_at
            FROM state_dual_write_outbox
            ORDER BY updated_at DESC
            LIMIT 1
            """
        ).fetchone()
        return {
            "enabled": dual_write_enabled(),
            "dsn_configured": bool(audit_pg_dsn()),
            "counts": {str(row[0]): int(row[1]) for row in rows},
            "latest": dict(latest) if latest else None,
        }
    finally:
        conn.close()


def _load_pending_events(db_path: str | Path, limit: int) -> list[dict[str, Any]]:
    ensure_outbox_schema(db_path)
    conn = connect_sqlite(db_path, read_only=True)
    conn.row_factory = __import__("sqlite3").Row
    try:
        rows = conn.execute(
            """
            SELECT event_id, event_type, payload_json, attempts
            FROM state_dual_write_outbox
            WHERE status IN ('pending', 'retry')
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _mark_event_synced(db_path: str | Path, event_id: str) -> None:
    now = time.time()
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            """
            UPDATE state_dual_write_outbox
            SET status='synced', last_error='', updated_at=?, synced_at=?
            WHERE event_id=?
            """,
            (now, now, event_id),
        )
        conn.commit()
    finally:
        conn.close()


def _mark_event_retry(db_path: str | Path, event_id: str, attempts: int, error: str) -> None:
    now = time.time()
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            """
            UPDATE state_dual_write_outbox
            SET status='retry', attempts=?, last_error=?, updated_at=?
            WHERE event_id=?
            """,
            (int(attempts) + 1, str(error)[:1000], now, event_id),
        )
        conn.commit()
    finally:
        conn.close()


PG_DDL = """
CREATE TABLE IF NOT EXISTS audit_decision_ledger (
    decision_id TEXT PRIMARY KEY,
    trade_id TEXT DEFAULT '',
    position_id TEXT DEFAULT '',
    event_type TEXT NOT NULL,
    symbol TEXT DEFAULT '',
    timeframe TEXT DEFAULT '',
    decision_ts DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    regime_id TEXT DEFAULT '',
    regime_confidence DOUBLE PRECISION DEFAULT 0.0,
    portfolio_state_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    risk_state_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    policy_version TEXT DEFAULT '',
    factor_set_version TEXT DEFAULT '',
    action_score DOUBLE PRECISION DEFAULT 0.0,
    action_reason TEXT DEFAULT '',
    action_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    schema_version TEXT NOT NULL,
    source_db TEXT NOT NULL,
    outbox_event_id TEXT NOT NULL,
    synced_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS audit_decision_factor_snapshot (
    decision_id TEXT NOT NULL,
    snapshot_seq INTEGER NOT NULL,
    factor TEXT NOT NULL,
    source TEXT DEFAULT 'registry',
    raw_value DOUBLE PRECISION DEFAULT 0.0,
    normalized_value DOUBLE PRECISION DEFAULT 0.0,
    direction DOUBLE PRECISION DEFAULT 0.0,
    base_weight DOUBLE PRECISION DEFAULT 0.0,
    policy_weight DOUBLE PRECISION DEFAULT 0.0,
    shadow_score DOUBLE PRECISION DEFAULT 0.0,
    health_score DOUBLE PRECISION DEFAULT 0.0,
    gated INTEGER DEFAULT 0,
    gated_reason TEXT DEFAULT '',
    contribution_score DOUBLE PRECISION DEFAULT 0.0,
    schema_version TEXT NOT NULL,
    source_db TEXT NOT NULL,
    outbox_event_id TEXT NOT NULL,
    synced_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    PRIMARY KEY (decision_id, snapshot_seq)
);

CREATE INDEX IF NOT EXISTS idx_audit_decision_ledger_ts ON audit_decision_ledger(decision_ts);
CREATE INDEX IF NOT EXISTS idx_audit_decision_factor_factor ON audit_decision_factor_snapshot(factor);

CREATE TABLE IF NOT EXISTS audit_state_mirror_event (
    event_id TEXT PRIMARY KEY,
    table_name TEXT NOT NULL,
    entity_key TEXT NOT NULL,
    operation TEXT NOT NULL,
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    payload_hash TEXT NOT NULL DEFAULT '',
    source_updated_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    schema_version TEXT NOT NULL,
    source_db TEXT NOT NULL,
    outbox_event_id TEXT NOT NULL,
    synced_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS audit_state_mirror_latest (
    table_name TEXT NOT NULL,
    entity_key TEXT NOT NULL,
    operation TEXT NOT NULL,
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    payload_hash TEXT NOT NULL DEFAULT '',
    source_updated_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    last_event_id TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    source_db TEXT NOT NULL,
    synced_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    PRIMARY KEY (table_name, entity_key)
);

CREATE INDEX IF NOT EXISTS idx_audit_state_mirror_event_table
ON audit_state_mirror_event(table_name, source_updated_at);
"""


class PostgresAuditSink:
    def __init__(self, dsn: str):
        self.dsn = dsn

    def _connect(self):
        import psycopg

        return psycopg.connect(self.dsn, autocommit=False)

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            for statement in PG_DDL.split(";"):
                sql = statement.strip()
                if sql:
                    conn.execute(sql)
            conn.commit()

    def write_event(self, payload: dict[str, Any]) -> None:
        if str(payload.get("event_type") or "") == EVENT_STATE_ROW:
            self.write_state_row_event(payload)
            return
        decision = payload["decision"]
        snapshots = payload.get("factor_snapshots") or []
        synced_at = time.time()
        schema_version = str(payload.get("schema_version") or SCHEMA_VERSION)
        source_db = str(payload.get("source_db") or "")
        outbox_event_id = str(payload.get("outbox_event_id") or decision["decision_id"])
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_decision_ledger
                (decision_id, trade_id, position_id, event_type, symbol, timeframe,
                 decision_ts, regime_id, regime_confidence, portfolio_state_json,
                 risk_state_json, policy_version, factor_set_version, action_score,
                 action_reason, action_json, created_at, schema_version, source_db,
                 outbox_event_id, synced_at)
                VALUES
                (%(decision_id)s, %(trade_id)s, %(position_id)s, %(event_type)s, %(symbol)s, %(timeframe)s,
                 %(decision_ts)s, %(regime_id)s, %(regime_confidence)s, %(portfolio_state_json)s::jsonb,
                 %(risk_state_json)s::jsonb, %(policy_version)s, %(factor_set_version)s, %(action_score)s,
                 %(action_reason)s, %(action_json)s::jsonb, %(created_at)s, %(schema_version)s, %(source_db)s,
                 %(outbox_event_id)s, %(synced_at)s)
                ON CONFLICT (decision_id) DO UPDATE SET
                    trade_id=excluded.trade_id,
                    position_id=excluded.position_id,
                    event_type=excluded.event_type,
                    symbol=excluded.symbol,
                    timeframe=excluded.timeframe,
                    decision_ts=excluded.decision_ts,
                    regime_id=excluded.regime_id,
                    regime_confidence=excluded.regime_confidence,
                    portfolio_state_json=excluded.portfolio_state_json,
                    risk_state_json=excluded.risk_state_json,
                    policy_version=excluded.policy_version,
                    factor_set_version=excluded.factor_set_version,
                    action_score=excluded.action_score,
                    action_reason=excluded.action_reason,
                    action_json=excluded.action_json,
                    created_at=excluded.created_at,
                    schema_version=excluded.schema_version,
                    source_db=excluded.source_db,
                    outbox_event_id=excluded.outbox_event_id,
                    synced_at=excluded.synced_at
                """,
                {
                    **decision,
                    "schema_version": schema_version,
                    "source_db": source_db,
                    "outbox_event_id": outbox_event_id,
                    "synced_at": synced_at,
                },
            )
            for row in snapshots:
                conn.execute(
                    """
                    INSERT INTO audit_decision_factor_snapshot
                    (decision_id, snapshot_seq, factor, source, raw_value, normalized_value,
                     direction, base_weight, policy_weight, shadow_score, health_score,
                     gated, gated_reason, contribution_score, schema_version, source_db,
                     outbox_event_id, synced_at)
                    VALUES
                    (%(decision_id)s, %(snapshot_seq)s, %(factor)s, %(source)s, %(raw_value)s, %(normalized_value)s,
                     %(direction)s, %(base_weight)s, %(policy_weight)s, %(shadow_score)s, %(health_score)s,
                     %(gated)s, %(gated_reason)s, %(contribution_score)s, %(schema_version)s, %(source_db)s,
                     %(outbox_event_id)s, %(synced_at)s)
                    ON CONFLICT (decision_id, snapshot_seq) DO UPDATE SET
                        factor=excluded.factor,
                        source=excluded.source,
                        raw_value=excluded.raw_value,
                        normalized_value=excluded.normalized_value,
                        direction=excluded.direction,
                        base_weight=excluded.base_weight,
                        policy_weight=excluded.policy_weight,
                        shadow_score=excluded.shadow_score,
                        health_score=excluded.health_score,
                        gated=excluded.gated,
                        gated_reason=excluded.gated_reason,
                        contribution_score=excluded.contribution_score,
                        schema_version=excluded.schema_version,
                        source_db=excluded.source_db,
                        outbox_event_id=excluded.outbox_event_id,
                        synced_at=excluded.synced_at
                    """,
                    {
                        **row,
                        "schema_version": schema_version,
                        "source_db": source_db,
                        "outbox_event_id": outbox_event_id,
                        "synced_at": synced_at,
                    },
                )
            conn.commit()

    def write_state_row_event(self, payload: dict[str, Any]) -> None:
        synced_at = time.time()
        table_name = str(payload.get("table_name") or "")
        entity_key = str(payload.get("entity_key") or "")
        operation = str(payload.get("operation") or "upsert")
        row = payload.get("row") if isinstance(payload.get("row"), dict) else {}
        schema_version = str(payload.get("schema_version") or SCHEMA_VERSION)
        source_db = str(payload.get("source_db") or "")
        outbox_event_id = str(payload.get("outbox_event_id") or "")
        payload_hash = str(payload.get("payload_hash") or _payload_hash(row))
        source_updated_at = float(payload.get("source_updated_at") or payload.get("created_at") or 0.0)
        created_at = float(payload.get("created_at") or time.time())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_state_mirror_event
                (event_id, table_name, entity_key, operation, payload_json, payload_hash,
                 source_updated_at, created_at, schema_version, source_db, outbox_event_id, synced_at)
                VALUES
                (%(event_id)s, %(table_name)s, %(entity_key)s, %(operation)s, %(payload_json)s::jsonb,
                 %(payload_hash)s, %(source_updated_at)s, %(created_at)s, %(schema_version)s,
                 %(source_db)s, %(outbox_event_id)s, %(synced_at)s)
                ON CONFLICT (event_id) DO UPDATE SET
                    table_name=excluded.table_name,
                    entity_key=excluded.entity_key,
                    operation=excluded.operation,
                    payload_json=excluded.payload_json,
                    payload_hash=excluded.payload_hash,
                    source_updated_at=excluded.source_updated_at,
                    created_at=excluded.created_at,
                    schema_version=excluded.schema_version,
                    source_db=excluded.source_db,
                    outbox_event_id=excluded.outbox_event_id,
                    synced_at=excluded.synced_at
                """,
                {
                    "event_id": outbox_event_id,
                    "table_name": table_name,
                    "entity_key": entity_key,
                    "operation": operation,
                    "payload_json": json.dumps(row, ensure_ascii=False, default=str),
                    "payload_hash": payload_hash,
                    "source_updated_at": source_updated_at,
                    "created_at": created_at,
                    "schema_version": schema_version,
                    "source_db": source_db,
                    "outbox_event_id": outbox_event_id,
                    "synced_at": synced_at,
                },
            )
            conn.execute(
                """
                INSERT INTO audit_state_mirror_latest
                (table_name, entity_key, operation, payload_json, payload_hash,
                 source_updated_at, last_event_id, schema_version, source_db, synced_at)
                VALUES
                (%(table_name)s, %(entity_key)s, %(operation)s, %(payload_json)s::jsonb,
                 %(payload_hash)s, %(source_updated_at)s, %(last_event_id)s, %(schema_version)s,
                 %(source_db)s, %(synced_at)s)
                ON CONFLICT (table_name, entity_key) DO UPDATE SET
                    operation=excluded.operation,
                    payload_json=excluded.payload_json,
                    payload_hash=excluded.payload_hash,
                    source_updated_at=excluded.source_updated_at,
                    last_event_id=excluded.last_event_id,
                    schema_version=excluded.schema_version,
                    source_db=excluded.source_db,
                    synced_at=excluded.synced_at
                WHERE audit_state_mirror_latest.source_updated_at <= excluded.source_updated_at
                """,
                {
                    "table_name": table_name,
                    "entity_key": entity_key,
                    "operation": operation,
                    "payload_json": json.dumps(row, ensure_ascii=False, default=str),
                    "payload_hash": payload_hash,
                    "source_updated_at": source_updated_at,
                    "last_event_id": outbox_event_id,
                    "schema_version": schema_version,
                    "source_db": source_db,
                    "synced_at": synced_at,
                },
            )
            conn.commit()


def flush_once(*, db_path: str | Path = STATE_DB, limit: int = 20, sink: Any | None = None) -> dict[str, int]:
    if not dual_write_enabled() and sink is None:
        return {"processed": 0, "synced": 0, "failed": 0}
    active_sink = sink or PostgresAuditSink(audit_pg_dsn())
    active_sink.ensure_schema()
    processed = synced = failed = 0
    for event in _load_pending_events(db_path, limit):
        processed += 1
        try:
            active_sink.write_event(json.loads(event["payload_json"]))
            _mark_event_synced(db_path, event["event_id"])
            synced += 1
        except Exception as exc:
            failed += 1
            _mark_event_retry(db_path, event["event_id"], int(event.get("attempts") or 0), str(exc))
            logger.warning("[state_dual_write] flush failed for {}: {}", event["event_id"], exc)
    return {"processed": processed, "synced": synced, "failed": failed}


class StateDualWriteWorker:
    def __init__(self, *, db_path: str | Path = STATE_DB, interval_sec: float = 5.0, batch_size: int = 20):
        self.db_path = db_path
        self.interval_sec = max(1.0, float(interval_sec))
        self.batch_size = int(batch_size)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        if not dual_write_enabled():
            return False
        ensure_outbox_schema(self.db_path)
        if self._thread is not None and self._thread.is_alive():
            return True
        self._thread = threading.Thread(target=self._run, name="state-dual-write", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def _run(self) -> None:
        logger.info("[state_dual_write] worker started")
        while not self._stop.is_set():
            try:
                flush_once(db_path=self.db_path, limit=self.batch_size)
            except Exception as exc:
                logger.warning("[state_dual_write] worker cycle failed: {}", exc)
            self._stop.wait(self.interval_sec)
        logger.info("[state_dual_write] worker stopped")


def start_state_dual_write_worker() -> bool:
    global _worker
    with _worker_lock:
        if _worker is None:
            _worker = StateDualWriteWorker()
        return _worker.start()


def stop_state_dual_write_worker() -> None:
    global _worker
    with _worker_lock:
        if _worker is not None:
            _worker.stop()
            _worker = None
