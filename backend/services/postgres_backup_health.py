"""Canonical projection for verified Windows-pull backup receipts.

The Windows client owns its local file and reports success only after it has
validated the received ``pg_dump`` archive. This service is the single writer
of that compact receipt in ``runtime_kv``; API and readiness only read it. It
never changes PostgreSQL recovery, trading authority, or release flags.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, state_table_exists
from backend.services._brain_helpers import connect, execute, loads, safe_float


BACKUP_HEALTH_KEY = "postgres_backup_health.v1"
SCHEMA_VERSION = "postgres_backup_health.v1"


class PostgresBackupHealthService:
    """Persist and read the Windows-pull backup receipt without re-evaluating it."""

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = Path(db_path)

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {
            "schema_version": "postgres_backup_health_boundary.v1",
            "external_authority": "windows_pull_client",
            "canonical_writer": "PostgresBackupHealthService",
            "backup_observer": "scripts/record_windows_pull_backup.py",
            "restore_drill_verifier": "scripts/verify_state_restore.py (explicit opt-in)",
            "restore_drill_recorder": "scripts/record_windows_restore_drill.py",
            "read_model_only": True,
            "does_not_authorize_trading": True,
            "does_not_change_postgresql_recovery": True,
            "does_not_enable_release_flags": True,
        }

    def publish(self, observation: dict[str, Any]) -> dict[str, Any]:
        """Publish a sanitized backup observation after a completed command."""
        now = time.time()
        conn = connect(self.db_path)
        try:
            if not state_table_exists(conn, "runtime_kv"):
                raise RuntimeError("runtime_kv_missing")
            existing_row = execute(
                conn,
                "SELECT value_json FROM runtime_kv WHERE key=?",
                (BACKUP_HEALTH_KEY,),
            ).fetchone()
            existing = loads(dict(existing_row).get("value_json"), {}) if existing_row else {}
            candidate = dict(observation or {})
            if "restore_drill" not in candidate and isinstance(existing, dict):
                candidate["restore_drill"] = existing.get("restore_drill")
            payload = self._sanitize(candidate, now=now)
            execute(
                conn,
                """
                INSERT INTO runtime_kv (key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json=excluded.value_json,
                    updated_at=excluded.updated_at
                """,
                (BACKUP_HEALTH_KEY, json.dumps(payload, ensure_ascii=False, sort_keys=True), now),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return {**payload, "published_at": now, "boundary": self.boundary()}

    def record_windows_pull(
        self,
        *,
        completed_at: float,
        byte_count: int,
        sha256: str,
    ) -> dict[str, Any]:
        """Record a client-validated, off-host logical snapshot receipt.

        The caller is the root-owned SSH bridge.  The receipt deliberately
        says that the client reported validation: the server never pretends it
        can read a Windows disk that may be offline.
        """
        completed = safe_float(completed_at)
        size = int(byte_count or 0)
        digest = str(sha256 or "").lower()
        if (
            completed <= 0
            or completed > time.time() + 900.0
            or size <= 0
            or len(digest) != 64
            or any(
                char not in "0123456789abcdef" for char in digest
            )
        ):
            raise ValueError("windows_pull_receipt_invalid")
        return self.publish(
            {
                "ok": True,
                "status": "healthy",
                "source": "windows_pull",
                "backup": {
                    "method": "logical_pg_dump_custom",
                    "database": "quant_audit",
                    "completed_at": completed,
                    "byte_count": size,
                    "sha256": digest,
                    "client_archive_verified": True,
                    "offsite_copy": "client_reported",
                    "server_retention": "none",
                },
                "observed_at": time.time(),
            }
        )

    def record_restore_drill(self, verification: dict[str, Any]) -> dict[str, Any]:
        """Record an already-completed isolated verification in the same projection.

        The caller must opt in explicitly. This only records a compact result;
        it cannot restore, promote, or change a database DSN.
        """
        report = dict(verification or {})
        drill = {
            "status": "healthy" if bool(report.get("ok")) else "degraded",
            "verified_at": time.time(),
            "client_verified_at": safe_float(report.get("verified_at")),
            "verification_source": str(
                report.get("verification_source") or "operator_reported"
            ),
            "state_schema_status": str(
                (report.get("state_schema") or {}).get("status") or "unavailable"
            ),
            "table_counts_status": str(
                (report.get("table_counts") or {}).get("status") or "unavailable"
            ),
            "memory_integrity_status": str(
                (report.get("memory_integrity") or {}).get("status") or "unavailable"
            ),
            "requires_manual_promotion": True,
        }
        existing = self._stored_observation()
        if not existing:
            existing = {
                "ok": False,
                "status": "degraded",
                "reason_code": "backup_health_observation_missing",
            }
        return self.publish({**existing, "restore_drill": drill})

    def _stored_observation(self) -> dict[str, Any]:
        """Load the unexpanded stored payload for an explicit write update."""
        conn = connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, "runtime_kv"):
                raise RuntimeError("runtime_kv_missing")
            row = execute(
                conn,
                "SELECT value_json FROM runtime_kv WHERE key=?",
                (BACKUP_HEALTH_KEY,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return {}
        payload = loads(dict(row).get("value_json"), {})
        return payload if isinstance(payload, dict) else {}

    def latest(self) -> dict[str, Any]:
        try:
            conn = connect(self.db_path, read_only=True)
        except Exception as exc:
            return self._unavailable(f"state_unavailable:{type(exc).__name__}: {exc}")
        try:
            if not state_table_exists(conn, "runtime_kv"):
                return self._unavailable("runtime_kv_missing")
            row = execute(
                conn,
                "SELECT value_json, updated_at FROM runtime_kv WHERE key=?",
                (BACKUP_HEALTH_KEY,),
            ).fetchone()
        except Exception as exc:
            return self._unavailable(f"backup_health_query_failed:{type(exc).__name__}: {exc}")
        finally:
            conn.close()
        if not row:
            return {
                "ok": False,
                "schema_version": SCHEMA_VERSION,
                "status": "missing",
                "reason_code": "backup_health_observation_missing",
                "updated_at": 0.0,
                "boundary": self.boundary(),
            }
        row_data = dict(row)
        payload = loads(row_data.get("value_json"), {})
        if not isinstance(payload, dict):
            return self._unavailable("backup_health_payload_invalid")
        restore_drill = payload.get("restore_drill")
        restore_status = (
            str(restore_drill.get("status") or "missing")
            if isinstance(restore_drill, dict)
            else "missing"
        )
        status = str(payload.get("status") or "unavailable")
        reason_code = str(payload.get("reason_code") or "")
        source = str(payload.get("source") or "unknown")
        if source != "windows_pull":
            status = "unavailable"
            reason_code = "backup_health_source_not_current"
        elif status == "healthy" and restore_status != "healthy":
            status = "degraded"
            reason_code = "restore_drill_missing" if restore_status == "missing" else "restore_drill_not_healthy"
        return {
            **payload,
            "ok": bool(payload.get("ok")) and status == "healthy",
            "status": status,
            "reason_code": reason_code,
            "updated_at": safe_float(row_data.get("updated_at")),
            "boundary": self.boundary(),
        }

    def _sanitize(self, observation: dict[str, Any], *, now: float) -> dict[str, Any]:
        value = dict(observation or {})
        # Credentials and cipher material must never leak into runtime_kv or
        # the authenticated operational API, even if a command wrapper adds a
        # verbose field in a future release.
        forbidden = ("secret", "password", "cipher", "access_key", "private_key", "token")

        def clean(item: Any) -> Any:
            if isinstance(item, dict):
                return {
                    str(key): clean(nested)
                    for key, nested in item.items()
                    if not any(marker in str(key).lower() for marker in forbidden)
                }
            if isinstance(item, list):
                return [clean(nested) for nested in item]
            return item

        clean_value = clean(value)
        status = str(clean_value.get("status") or "unavailable")
        if status not in {"healthy", "degraded", "unavailable"}:
            status = "unavailable"
        return {
            **clean_value,
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "observed_at": safe_float(clean_value.get("observed_at"), now),
            "source": str(clean_value.get("source") or "unknown"),
            "boundary": self.boundary(),
        }

    def _unavailable(self, error: str) -> dict[str, Any]:
        return {
            "ok": False,
            "schema_version": SCHEMA_VERSION,
            "status": "unavailable",
            "reason_code": "backup_health_unavailable",
            "errors": [str(error)],
            "boundary": self.boundary(),
        }
