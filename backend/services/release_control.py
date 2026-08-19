from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from backend.core.db import (
    STATE_DB,
    connect_sqlite,
    get_state_pg_conn,
    is_state_db_path,
    state_table_exists,
)
from backend.core.state_store import (
    is_state_schema_write_sql,
    validate_runtime_state_schema,
)
from backend.services.evolution_ledger import current_runtime_config_snapshot
from backend.services.incident_controls import RuntimeIncidentControlService
from backend.services.replay_harness import ReplayHarnessService

from backend.core.db_helpers import (
    load_json as _loads,
    conn_is_pg as _conn_is_pg,
    pg_sql as _sql,
)



def _dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, default=str)


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


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def ensure_release_run_table(db_path: str | Path = STATE_DB) -> None:
    conn = _connect(db_path)
    try:
        _execute(
            conn,
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
            """,
        )
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_release_run_created ON release_run(created_at)")
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_release_run_status ON release_run(status, created_at)")
        conn.commit()
    finally:
        conn.close()


def ensure_release_approval_event_table(db_path: str | Path = STATE_DB) -> None:
    conn = _connect(db_path)
    try:
        _execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS release_approval_event (
                event_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                action TEXT DEFAULT '',
                actor TEXT DEFAULT '',
                decision TEXT DEFAULT '',
                reason TEXT DEFAULT '',
                evidence_refs_json TEXT NOT NULL DEFAULT '{}',
                boundary_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL DEFAULT 0.0
            )
            """,
        )
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_release_approval_run ON release_approval_event(run_id, created_at)")
        _execute(
            conn,
            "CREATE INDEX IF NOT EXISTS idx_release_approval_decision ON release_approval_event(decision, created_at)",
        )
        conn.commit()
    finally:
        conn.close()


class ReleaseControlService:
    """V15 release run ledger.

    This service records release readiness evidence only. It does not mutate
    RuntimeConfig, factor weights, orders, or broker state.
    """

    VALID_STATUSES = {"started", "completed", "failed", "cancelled"}

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = db_path

    def build_checklist(
        self,
        *,
        readiness: dict[str, Any] | None = None,
        tests: list[dict[str, Any]] | None = None,
        rollback_ref: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        snapshot = current_runtime_config_snapshot(db_path=self.db_path, create_if_missing=False)
        replay = ReplayHarnessService(self.db_path).latest_report()
        incident = RuntimeIncidentControlService(self.db_path).status()
        posture = str((readiness or {}).get("autonomy_health", {}).get("posture") or "")
        replay_grade = str(replay.get("evidence_grade") or "")
        checklist = {
            "schema_version": "v15_release_checklist.v1",
            "generated_at": time.time(),
            "read_only": True,
            "runtime_config_snapshot": {
                "ok": bool(snapshot.get("config_hash")),
                "config_hash": str(snapshot.get("config_hash") or ""),
                "source": str(snapshot.get("source") or ""),
                "created_at": _safe_float(snapshot.get("created_at")),
            },
            "replay": {
                "ok": bool(replay.get("replay_run_id")) and not replay.get("replay_error"),
                "replay_run_id": str(replay.get("replay_run_id") or ""),
                "artifact_hash": str(replay.get("artifact_hash") or ""),
                "evidence_grade": replay_grade,
                "created_at": _safe_float(replay.get("created_at")),
            },
            "incident_control": {
                "mode": str(incident.get("mode") or "normal"),
                "readiness_effect": incident.get("readiness_effect") or {},
            },
            "readiness": {
                "ready_for_release": bool(
                    (readiness or {}).get(
                        "ready_for_release",
                        not bool((readiness or {}).get("blockers") or []),
                    )
                ),
                "ready_for_live_execution": bool(
                    (readiness or {}).get("ready_for_live_execution", False)
                ),
                "ready_for_live_alpha": bool(
                    (readiness or {}).get("ready_for_live_alpha", False)
                ),
                "ready_for_autonomous_mutation": bool(
                    (readiness or {}).get("ready_for_autonomous_mutation", False)
                ),
                "autonomy_posture": posture,
                "blocker_count": len((readiness or {}).get("blockers") or []),
            },
            "tests": tests or [],
            "rollback_ref": rollback_ref or {},
            "control_plane_boundaries": {
                "audit_only": True,
                "runtime_overlay_is_source_of_truth": True,
                "runtime_snapshot_required_for_rollback": True,
                "risk_policy_service_required_for_risk_mutations": True,
                "decision_policy_required_for_weight_writes": True,
            },
        }
        checklist["ok"] = (
            bool(checklist["runtime_config_snapshot"]["ok"])
            and bool(checklist["replay"]["ok"])
            and replay_grade not in {"failed"}
            and bool(checklist["readiness"]["ready_for_release"])
        )
        return checklist

    def start_release(
        self,
        *,
        release_class: str,
        summary: dict[str, Any] | None = None,
        tests: list[dict[str, Any]] | None = None,
        rollback_ref: dict[str, Any] | None = None,
        created_by: str = "system",
        readiness: dict[str, Any] | None = None,
        run_id: str = "",
    ) -> dict[str, Any]:
        run = self._assemble_row(
            run_id=str(run_id or f"release_{uuid.uuid4().hex[:16]}"),
            release_class=release_class,
            status="started",
            summary=summary or {},
            tests=tests or [],
            rollback_ref=rollback_ref or {},
            created_by=created_by,
            readiness=readiness,
            created_at=time.time(),
        )
        self._upsert(run)
        return run

    def finish_release(
        self,
        run_id: str,
        *,
        status: str = "completed",
        summary: dict[str, Any] | None = None,
        tests: list[dict[str, Any]] | None = None,
        rollback_ref: dict[str, Any] | None = None,
        readiness: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        existing = self.get_release(run_id)
        if not existing.get("run_id"):
            return {"ok": False, "status": "missing_release_run", "run_id": str(run_id or "")}
        final_status = str(status or "completed").strip().lower()
        if final_status not in self.VALID_STATUSES:
            final_status = "failed"
        run = self._assemble_row(
            run_id=existing["run_id"],
            release_class=str(existing.get("release_class") or ""),
            status=final_status,
            summary=summary if summary is not None else existing.get("summary") or {},
            tests=tests if tests is not None else existing.get("tests") or [],
            rollback_ref=rollback_ref if rollback_ref is not None else existing.get("rollback_ref") or {},
            created_by=str(existing.get("created_by") or ""),
            readiness=readiness,
            created_at=_safe_float(existing.get("created_at")),
        )
        self._upsert(run)
        return run

    def latest_release(self) -> dict[str, Any]:
        conn = _connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, "release_run"):
                return {"ok": False, "status": "missing_table"}
            row = _execute(
                conn,
                """
                SELECT *
                FROM release_run
                ORDER BY created_at DESC
                LIMIT 1
                """,
            ).fetchone()
            if not row:
                return {"ok": False, "status": "missing_release_run"}
            return self._row_to_release(dict(row))
        finally:
            conn.close()

    def close_stale_started_release(
        self,
        *,
        max_age_seconds: float = 3600.0,
        actor: str = "system:release_watchdog",
    ) -> dict[str, Any]:
        """Cancel one abandoned release without creating a new release run."""
        latest = self.latest_release()
        if not latest.get("run_id"):
            return {"ok": True, "status": "no_release"}
        if str(latest.get("status") or "") != "started":
            return {
                "ok": True,
                "status": "not_started",
                "run_id": str(latest.get("run_id") or ""),
            }
        age_seconds = max(0.0, time.time() - _safe_float(latest.get("created_at")))
        threshold = max(300.0, float(max_age_seconds or 3600.0))
        if age_seconds < threshold:
            return {
                "ok": True,
                "status": "still_fresh",
                "run_id": str(latest.get("run_id") or ""),
                "age_seconds": age_seconds,
                "max_age_seconds": threshold,
            }
        summary = dict(latest.get("summary") or {})
        summary["stale_release_watchdog"] = {
            "status": "cancelled",
            "reason": "started_release_exceeded_max_age",
            "age_seconds": age_seconds,
            "max_age_seconds": threshold,
        }
        result = self.finish_release(
            str(latest["run_id"]),
            status="cancelled",
            summary=summary,
        )
        self.record_approval_event(
            str(latest["run_id"]),
            action="stale_release_watchdog",
            actor=actor,
            decision="cancelled",
            reason="started_release_exceeded_max_age",
            evidence_refs={
                "age_seconds": age_seconds,
                "max_age_seconds": threshold,
            },
        )
        return {
            "ok": bool(result.get("ok")),
            "status": "cancelled",
            "run_id": str(latest["run_id"]),
            "age_seconds": age_seconds,
            "max_age_seconds": threshold,
        }

    def get_release(self, run_id: str) -> dict[str, Any]:
        conn = _connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, "release_run"):
                return {"ok": False, "status": "missing_table", "run_id": str(run_id or "")}
            row = _execute(conn, "SELECT * FROM release_run WHERE run_id = ? LIMIT 1", (str(run_id or ""),)).fetchone()
            if not row:
                return {"ok": False, "status": "missing_release_run", "run_id": str(run_id or "")}
            return self._row_to_release(dict(row))
        finally:
            conn.close()

    def record_approval_event(
        self,
        run_id: str,
        *,
        action: str = "approval_decision",
        actor: str = "system",
        decision: str = "recorded",
        reason: str = "",
        evidence_refs: dict[str, Any] | list[Any] | None = None,
        event_id: str = "",
        created_at: float | None = None,
    ) -> dict[str, Any]:
        release = self.get_release(run_id)
        if not release.get("run_id"):
            return {"ok": False, "status": "missing_release_run", "run_id": str(run_id or "")}
        event = {
            "ok": True,
            "schema_version": "release_approval_event.v1",
            "event_id": str(event_id or f"approval_{uuid.uuid4().hex[:16]}"),
            "run_id": str(release.get("run_id") or ""),
            "action": str(action or "approval_decision"),
            "actor": str(actor or "system"),
            "decision": str(decision or "recorded"),
            "reason": str(reason or ""),
            "evidence_refs": evidence_refs if evidence_refs is not None else {},
            "boundary": self._approval_boundary(),
            "created_at": _safe_float(created_at, time.time()),
        }
        ensure_release_approval_event_table(self.db_path)
        conn = _connect(self.db_path)
        try:
            _execute(
                conn,
                """
                INSERT INTO release_approval_event
                (event_id, run_id, action, actor, decision, reason,
                 evidence_refs_json, boundary_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["event_id"],
                    event["run_id"],
                    event["action"],
                    event["actor"],
                    event["decision"],
                    event["reason"],
                    _dumps(event["evidence_refs"]),
                    _dumps(event["boundary"]),
                    event["created_at"],
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return event

    def approval_trail(self, run_id: str, *, limit: int = 100) -> dict[str, Any]:
        release = self.get_release(run_id)
        if not release.get("run_id"):
            return {"ok": False, "status": "missing_release_run", "run_id": str(run_id or "")}
        conn = _connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, "release_approval_event"):
                events: list[dict[str, Any]] = []
            else:
                rows = _execute(
                    conn,
                    """
                    SELECT *
                    FROM release_approval_event
                    WHERE run_id = ?
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    (str(run_id or ""), max(1, min(int(limit or 100), 500))),
                ).fetchall()
                events = [self._row_to_approval_event(dict(row)) for row in rows]
        finally:
            conn.close()
        return {
            "ok": True,
            "schema_version": "release_approval_trail.v1",
            "run_id": str(release.get("run_id") or ""),
            "release_status": str(release.get("status") or ""),
            "event_count": len(events),
            "events": events,
            "boundary": self._approval_boundary(),
        }

    def _assemble_row(
        self,
        *,
        run_id: str,
        release_class: str,
        status: str,
        summary: dict[str, Any],
        tests: list[dict[str, Any]],
        rollback_ref: dict[str, Any],
        created_by: str,
        readiness: dict[str, Any] | None,
        created_at: float,
    ) -> dict[str, Any]:
        checklist = self.build_checklist(readiness=readiness, tests=tests, rollback_ref=rollback_ref)
        return {
            "ok": True,
            "schema_version": "release_run.v1",
            "run_id": run_id,
            "release_class": str(release_class or ""),
            "status": str(status or "started"),
            "summary": summary,
            "checklist": checklist,
            "runtime_config_hash": str(checklist["runtime_config_snapshot"].get("config_hash") or ""),
            "replay_run_id": str(checklist["replay"].get("replay_run_id") or ""),
            "replay_artifact_hash": str(checklist["replay"].get("artifact_hash") or ""),
            "incident_mode": str(checklist["incident_control"].get("mode") or ""),
            "readiness_posture": str(checklist["readiness"].get("autonomy_posture") or ""),
            "tests": tests,
            "rollback_ref": rollback_ref,
            "created_by": str(created_by or ""),
            "created_at": _safe_float(created_at, time.time()),
            "updated_at": time.time(),
        }

    def _upsert(self, run: dict[str, Any]) -> None:
        ensure_release_run_table(self.db_path)
        conn = _connect(self.db_path)
        try:
            _execute(
                conn,
                """
                INSERT INTO release_run
                (run_id, release_class, status, summary_json, checklist_json,
                 runtime_config_hash, replay_run_id, replay_artifact_hash,
                 incident_mode, readiness_posture, tests_json, rollback_ref_json,
                 created_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    release_class=excluded.release_class,
                    status=excluded.status,
                    summary_json=excluded.summary_json,
                    checklist_json=excluded.checklist_json,
                    runtime_config_hash=excluded.runtime_config_hash,
                    replay_run_id=excluded.replay_run_id,
                    replay_artifact_hash=excluded.replay_artifact_hash,
                    incident_mode=excluded.incident_mode,
                    readiness_posture=excluded.readiness_posture,
                    tests_json=excluded.tests_json,
                    rollback_ref_json=excluded.rollback_ref_json,
                    created_by=excluded.created_by,
                    updated_at=excluded.updated_at
                """,
                (
                    str(run.get("run_id") or ""),
                    str(run.get("release_class") or ""),
                    str(run.get("status") or ""),
                    _dumps(run.get("summary") or {}),
                    _dumps(run.get("checklist") or {}),
                    str(run.get("runtime_config_hash") or ""),
                    str(run.get("replay_run_id") or ""),
                    str(run.get("replay_artifact_hash") or ""),
                    str(run.get("incident_mode") or ""),
                    str(run.get("readiness_posture") or ""),
                    _dumps(run.get("tests") or []),
                    _dumps(run.get("rollback_ref") or {}),
                    str(run.get("created_by") or ""),
                    _safe_float(run.get("created_at")),
                    _safe_float(run.get("updated_at")),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _row_to_release(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "schema_version": "release_run.v1",
            "run_id": str(row.get("run_id") or ""),
            "release_class": str(row.get("release_class") or ""),
            "status": str(row.get("status") or ""),
            "summary": _loads(row.get("summary_json"), {}),
            "checklist": _loads(row.get("checklist_json"), {}),
            "runtime_config_hash": str(row.get("runtime_config_hash") or ""),
            "replay_run_id": str(row.get("replay_run_id") or ""),
            "replay_artifact_hash": str(row.get("replay_artifact_hash") or ""),
            "incident_mode": str(row.get("incident_mode") or ""),
            "readiness_posture": str(row.get("readiness_posture") or ""),
            "tests": _loads(row.get("tests_json"), []),
            "rollback_ref": _loads(row.get("rollback_ref_json"), {}),
            "created_by": str(row.get("created_by") or ""),
            "created_at": _safe_float(row.get("created_at")),
            "updated_at": _safe_float(row.get("updated_at")),
        }

    @staticmethod
    def _approval_boundary() -> dict[str, Any]:
        return {
            "schema_version": "release_approval_boundary.v1",
            "audit_only": True,
            "does_not_change_release_status": True,
            "does_not_change_runtime_overlay": True,
            "does_not_change_runtime_snapshot": True,
            "does_not_change_factor_weights": True,
            "does_not_change_orders_or_positions": True,
            "risk_policy_service_required_for_risk_mutations": True,
            "decision_policy_required_for_weight_writes": True,
            "runtime_overlay_snapshot_required_for_config_changes": True,
        }

    @staticmethod
    def _row_to_approval_event(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "schema_version": "release_approval_event.v1",
            "event_id": str(row.get("event_id") or ""),
            "run_id": str(row.get("run_id") or ""),
            "action": str(row.get("action") or ""),
            "actor": str(row.get("actor") or ""),
            "decision": str(row.get("decision") or ""),
            "reason": str(row.get("reason") or ""),
            "evidence_refs": _loads(row.get("evidence_refs_json"), {}),
            "boundary": _loads(row.get("boundary_json"), ReleaseControlService._approval_boundary()),
            "created_at": _safe_float(row.get("created_at")),
        }
