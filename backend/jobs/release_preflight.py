"""Read-only release facts for the persistent PostgreSQL job worker."""
from __future__ import annotations

from typing import Any, Callable, Mapping


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
