"""Read-only release facts for the persistent PostgreSQL job worker."""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Mapping

from backend.jobs.capability import STATUS_KEY


def validate_persistent_job_worker_settings() -> None:
    """Validate the canonical deployment YAML used by the worker."""

    from backend.services import config_service

    payload = config_service.get_config()
    path = str(payload.get("path") or "config/settings.yaml")
    if not payload.get("exists"):
        raise RuntimeError(f"job_worker_settings_missing:{path}")
    if payload.get("parse_error"):
        raise RuntimeError(
            f"job_worker_settings_parse_error:{path}:{payload.get('parse_error')}"
        )
    try:
        config_service._validate_parsed_runtime_config(payload.get("parsed"))
    except Exception as exc:
        raise RuntimeError(
            f"job_worker_settings_invalid:{path}:{type(exc).__name__}:{exc}"
        ) from exc


def validate_persistent_job_worker_startup() -> None:
    """Validate deployment config and the minimum state schema without DDL."""

    from backend.core.db import init_state_db

    validate_persistent_job_worker_settings()

    # init_state_db is a read-only minimum-version assertion for runtime
    # processes; versioned migration scripts remain the sole schema writer.
    init_state_db()


def _row_mapping(row: Any, columns: list[str]) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    if isinstance(row, (tuple, list)):
        return dict(zip(columns, row))
    return {}


def collect_persistent_job_worker_release_preflight(
    *,
    conn_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Prove the disabled worker can be enabled without duplicate ownership."""

    from backend.core.state_schema_migrations import STATE_SCHEMA_MIN_VERSION
    from backend.jobs.handlers import persistent_job_handlers
    from backend.jobs.manager import JobManager

    try:
        validate_persistent_job_worker_startup()
        handlers = persistent_job_handlers()
        handler_kinds = set(handlers)
        expected_kinds = set(JobManager.PERSISTENT_JOB_KINDS)
        missing_handlers = sorted(expected_kinds - handler_kinds)
        unexpected_handlers = sorted(handler_kinds - expected_kinds)
        non_callable_handlers = sorted(
            kind for kind, handler in handlers.items() if not callable(handler)
        )

        if conn_factory is None:
            from backend.core.db import get_state_pg_conn

            conn_factory = get_state_pg_conn
        conn = conn_factory()
        try:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    """
                    SELECT kind, status, COUNT(*) AS row_count,
                           COUNT(*) FILTER (
                               WHERE status = 'running'
                                 AND lease_expires_at > EXTRACT(EPOCH FROM CURRENT_TIMESTAMP)
                           ) AS active_lease_count
                    FROM jobs
                    WHERE handler_version = 'v1'
                      AND status IN ('pending', 'queued', 'retry_wait', 'running')
                    GROUP BY kind, status
                    """
                )
                columns = [str(item[0]) for item in (cursor.description or [])]
                rows = [_row_mapping(row, columns) for row in cursor.fetchall()]
            finally:
                cursor.close()
        finally:
            conn.close()

        active_lease_count = sum(int(row.get("active_lease_count") or 0) for row in rows)
        unsupported_runnable_kinds = sorted(
            {
                str(row.get("kind") or "")
                for row in rows
                if str(row.get("kind") or "") not in handler_kinds
            }
        )
        blockers: list[str] = []
        if missing_handlers or unexpected_handlers or non_callable_handlers:
            blockers.append("persistent_job_handler_registry_divergent")
        if unsupported_runnable_kinds:
            blockers.append("persistent_job_kind_unsupported")
        if active_lease_count:
            blockers.append("persistent_job_active_lease_exists_before_enable")
        blockers = sorted(set(blockers))
        return {
            "ok": not blockers,
            "status": "passed" if not blockers else "blocked",
            "blockers": blockers,
            "schema_min_version": STATE_SCHEMA_MIN_VERSION,
            "handler_kinds": sorted(handler_kinds),
            "expected_kinds": sorted(expected_kinds),
            "missing_handlers": missing_handlers,
            "unexpected_handlers": unexpected_handlers,
            "non_callable_handlers": non_callable_handlers,
            "unsupported_runnable_kinds": unsupported_runnable_kinds,
            "active_lease_count": active_lease_count,
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": "error",
            "blockers": ["persistent_job_worker_preflight_error"],
            "reason": f"{type(exc).__name__}:{exc}",
        }


def collect_persistent_job_worker_capability(
    *,
    expected_flags: Mapping[str, Any],
    conn_factory: Callable[[], Any] | None = None,
    now: Callable[[], float] = time.time,
    max_age_sec: float = 30.0,
) -> dict[str, Any]:
    """Verify that the enabled worker owns a fresh, code-complete process."""

    from backend.core.static_feature_flags import static_feature_flags_fingerprint
    from backend.jobs.manager import JobManager

    try:
        if conn_factory is None:
            from backend.core.db import get_state_pg_conn

            conn_factory = get_state_pg_conn
        conn = conn_factory()
        try:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "SELECT value_json, updated_at FROM runtime_kv "
                    "WHERE key=%s LIMIT 1",
                    (STATUS_KEY,),
                )
                row = cursor.fetchone()
                columns = [str(item[0]) for item in (cursor.description or [])]
            finally:
                cursor.close()
        finally:
            conn.close()

        if row is None:
            return {
                "ok": False,
                "status": "blocked",
                "blockers": ["persistent_job_worker_capability_missing"],
                "worker_status": None,
                "age_seconds": None,
                "handler_kinds": [],
                "expected_kinds": sorted(JobManager.PERSISTENT_JOB_KINDS),
                "process_static_feature_flags": {},
            }
        record = _row_mapping(row, columns) if row is not None else {}
        raw_payload = record.get("value_json")
        payload = (
            dict(raw_payload)
            if isinstance(raw_payload, Mapping)
            else json.loads(str(raw_payload or "{}"))
        )
        if not isinstance(payload, dict):
            payload = {}
        updated_at = float(payload.get("updated_at") or record.get("updated_at") or 0.0)
        age_seconds = float(now()) - updated_at
        handler_kinds = sorted(str(kind) for kind in payload.get("handler_kinds") or [])
        expected_kinds = sorted(JobManager.PERSISTENT_JOB_KINDS)
        process_flags = (
            dict(payload.get("process_static_feature_flags") or {})
            if isinstance(payload.get("process_static_feature_flags"), Mapping)
            else {}
        )
        process_values = (
            dict(process_flags.get("values") or {})
            if isinstance(process_flags.get("values"), Mapping)
            else {}
        )
        try:
            pid = int(payload.get("pid") or 0)
            process_pid = int(process_flags.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
            process_pid = 0

        blockers: list[str] = []
        if payload.get("schema_version") != "persistent_job_worker_capability.v1":
            blockers.append("persistent_job_worker_capability_schema_invalid")
        if not str(payload.get("worker_id") or "") or not str(payload.get("boot_id") or ""):
            blockers.append("persistent_job_worker_identity_missing")
        if pid <= 0 or float(payload.get("started_at") or 0.0) <= 0.0:
            blockers.append("persistent_job_worker_process_invalid")
        if not (0.0 <= age_seconds <= float(max_age_sec)):
            blockers.append("persistent_job_worker_capability_stale")
        if str(payload.get("status") or "") not in {"running", "idle", "busy"}:
            blockers.append("persistent_job_worker_not_ready")
        if handler_kinds != expected_kinds:
            blockers.append("persistent_job_handler_registry_divergent")
        if not (
            process_flags.get("schema_version") == "static_feature_flags.v1"
            and process_values == dict(expected_flags)
            and str(process_flags.get("fingerprint") or "")
            == static_feature_flags_fingerprint(process_values)
            and process_pid == pid
            and float(process_flags.get("process_started_at") or 0.0) > 0.0
        ):
            blockers.append("persistent_job_worker_static_flags_unconfirmed")
        blockers = sorted(set(blockers))
        return {
            "ok": not blockers,
            "status": "passed" if not blockers else "blocked",
            "blockers": blockers,
            "worker_status": payload.get("status"),
            "worker_id": payload.get("worker_id"),
            "boot_id": payload.get("boot_id"),
            "pid": payload.get("pid"),
            "started_at": payload.get("started_at"),
            "updated_at": updated_at or None,
            "age_seconds": age_seconds,
            "current_job_id": payload.get("current_job_id"),
            "current_kind": payload.get("current_kind"),
            "handler_kinds": handler_kinds,
            "expected_kinds": expected_kinds,
            "process_static_feature_flags": process_flags,
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": "error",
            "blockers": ["persistent_job_worker_capability_error"],
            "reason": f"{type(exc).__name__}:{exc}",
        }
