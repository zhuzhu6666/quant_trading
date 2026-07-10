"""Persistent state transitions for governed learning applications."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, connect_sqlite, get_state_pg_conn, is_state_db_path
from research.learning.governor import RuleEvolutionGovernor


def _loads(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    try:
        value = json.loads(str(raw or "{}"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


class LearningApplicationStateService:
    """Small durable state machine around config-changing experiments."""

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = Path(db_path)

    def _use_pg(self) -> bool:
        return is_state_db_path(self.db_path)

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self._use_pg() else sql

    def _conn(self, *, read_only: bool = False):
        if self._use_pg():
            return get_state_pg_conn(read_only=read_only)
        conn = connect_sqlite(self.db_path, read_only=read_only)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {
            "schema_version": "learning_application_state_boundary.v1",
            "states": ["prepared", "applied", "observing", "mutation_failed"],
            "terminal_states": ["validated", "rolled_back", "inconclusive", "superseded"],
            "prepared_is_fail_closed": True,
            "idempotency_key": "run_id+scope_key",
            "recovery_uses_runtime_snapshot_fact": True,
        }

    def prepare(
        self,
        *,
        scope_key: str,
        old_weight: float,
        new_weight: float,
        suggestion_ids: list[str],
        cycle_ts: float,
        details: dict[str, Any],
    ) -> str:
        old_weight = float(old_weight or 0.0)
        new_weight = float(new_weight or 0.0)
        payload = dict(details or {})
        payload["application_state"] = {
            "status": "prepared",
            "prepared_at": float(cycle_ts),
            "updated_at": float(cycle_ts),
        }
        return RuleEvolutionGovernor(str(self.db_path)).log_application(
            scope_type="factor",
            scope_key=str(scope_key),
            action="update_weight",
            bias_multiplier=(new_weight / old_weight) if old_weight else 1.0,
            old_weight=old_weight,
            new_weight=new_weight,
            suggestion_ids=suggestion_ids,
            cycle_ts=float(cycle_ts),
            status="prepared",
            details=payload,
        )

    def transition(
        self,
        application_id: str,
        *,
        status: str,
        details_patch: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        status = str(status or "")
        if status not in {"applied", "observing", "mutation_failed", "superseded"}:
            raise ValueError(f"unsupported application transition: {status}")
        now = time.time()
        conn = self._conn()
        try:
            row = conn.execute(
                self._sql("SELECT details_json, status FROM learning_application_log WHERE application_id=?"),
                (str(application_id),),
            ).fetchone()
            if not row:
                return {"ok": False, "status": "missing", "application_id": str(application_id)}
            details = _loads(row["details_json"])
            details.update(dict(details_patch or {}))
            lifecycle = dict(details.get("application_state") or {})
            lifecycle.update({"status": status, "updated_at": now})
            if status == "applied":
                lifecycle.setdefault("applied_at", now)
            elif status == "mutation_failed":
                lifecycle.setdefault("failed_at", now)
            details["application_state"] = lifecycle
            effect_status = "observing" if status in {"applied", "observing"} else "superseded"
            conn.execute(
                self._sql(
                    "UPDATE learning_application_log SET status=?, details_json=? WHERE application_id=?"
                ),
                (status, json.dumps(details, ensure_ascii=False, default=str), str(application_id)),
            )
            conn.execute(
                self._sql(
                    "UPDATE learning_application_effect SET status=?, updated_at=? WHERE application_id=?"
                ),
                (effect_status, now, str(application_id)),
            )
            conn.commit()
            return {
                "ok": True,
                "status": status,
                "effect_status": effect_status,
                "application_id": str(application_id),
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def recover_prepared(self, *, grace_seconds: float = 60.0, limit: int = 200) -> dict[str, Any]:
        """Resolve interrupted prepared applications from snapshot facts."""
        now = time.time()
        conn = self._conn(read_only=True)
        try:
            rows = conn.execute(
                self._sql(
                    """
                    SELECT application_id, details_json, created_at
                    FROM learning_application_log
                    WHERE status='prepared'
                    ORDER BY created_at ASC
                    LIMIT ?
                    """
                ),
                (max(1, min(int(limit), 2000)),),
            ).fetchall()
            snapshots = []
            for row in rows:
                details = _loads(row["details_json"])
                run_id = str(details.get("run_id") or "")
                source = str(details.get("mutation_source") or details.get("source") or "")
                snapshot = None
                if run_id and source:
                    snapshot = conn.execute(
                        self._sql(
                            """
                            SELECT config_version, config_hash, created_at
                            FROM runtime_config_snapshot
                            WHERE run_id=? AND source=?
                            ORDER BY config_version DESC
                            LIMIT 1
                            """
                        ),
                        (run_id, source),
                    ).fetchone()
                snapshots.append((row, details, snapshot))
        finally:
            conn.close()

        applied = 0
        failed = 0
        waiting = 0
        for row, details, snapshot in snapshots:
            application_id = str(row["application_id"] or "")
            if snapshot:
                self.transition(
                    application_id,
                    status="applied",
                    details_patch={
                        "recovery": {
                            "status": "snapshot_confirmed",
                            "config_version": int(snapshot["config_version"] or 0),
                            "config_hash": str(snapshot["config_hash"] or ""),
                            "recovered_at": now,
                        }
                    },
                )
                applied += 1
            elif now - float(row["created_at"] or 0.0) >= max(1.0, float(grace_seconds)):
                self.transition(
                    application_id,
                    status="mutation_failed",
                    details_patch={
                        "recovery": {
                            "status": "snapshot_missing",
                            "recovered_at": now,
                            "run_id": str(details.get("run_id") or ""),
                        }
                    },
                )
                failed += 1
            else:
                waiting += 1
        return {
            "ok": True,
            "schema_version": "learning_application_recovery.v1",
            "checked": len(snapshots),
            "applied": applied,
            "mutation_failed": failed,
            "waiting": waiting,
            "boundary": self.boundary(),
        }
