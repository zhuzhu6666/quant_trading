"""Shared admission policy for autonomous learning experiments.

This is deliberately a policy/read service, not another agent or writer.  It
keeps independent weight producers from reopening the same factor experiment
before the existing posterior window has matured.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, connect_sqlite, get_state_pg_conn, is_state_db_path, state_table_exists


ACTIVE_APPLICATION_STATUSES = {"applied", "observing", "effective", "mixed"}
ACTIVE_EFFECT_STATUSES = {"observing", "mixed"}
STRUCTURAL_AUDIT_ACTIONS = {"update_redundancy_groups"}
WEIGHT_ACTIONS = {"update_weight", "downweight", "boost_small"}


class LearningExperimentAdmissionService:
    """Read-only admission verdict for a prospective governed application."""

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = Path(db_path)

    def _conn(self):
        conn = get_state_pg_conn(read_only=True) if is_state_db_path(self.db_path) else connect_sqlite(self.db_path, read_only=True)
        if not is_state_db_path(self.db_path):
            conn.row_factory = __import__("sqlite3").Row
        return conn

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {
            "schema_version": "learning_experiment_admission_boundary.v1",
            "read_only": True,
            "single_active_experiment_per_scope": True,
            "does_not_apply_runtime_mutation": True,
            "does_not_create_application": True,
            "risk_reduction_bypass_requires_existing_control_path": True,
        }

    @staticmethod
    def row_is_active(row: Any) -> bool:
        if row is None:
            return False
        application_status = str(row["application_status"] or "")
        effect_status = str(row["effect_status"] or "")
        return application_status in ACTIVE_APPLICATION_STATUSES or effect_status in ACTIVE_EFFECT_STATUSES

    def active(self, *, scope_type: str, scope_key: str) -> dict[str, Any] | None:
        if not scope_type or not scope_key:
            return None
        conn = self._conn()
        try:
            if not state_table_exists(conn, "learning_application_log"):
                return None
            sql = """
                SELECT l.application_id, l.action, l.status AS application_status,
                       l.cycle_ts, l.created_at, e.status AS effect_status,
                       e.observed_trade_count, e.baseline_trade_count, e.updated_at
                FROM learning_application_log l
                LEFT JOIN learning_application_effect e ON e.application_id=l.application_id
                WHERE l.scope_type=? AND l.scope_key=?
                ORDER BY l.cycle_ts DESC, l.created_at DESC
            """
            if is_state_db_path(self.db_path):
                sql = sql.replace("?", "%s")
            for row in conn.execute(sql, (scope_type, scope_key)).fetchall():
                application_status = str(row["application_status"] or "")
                effect_status = str(row["effect_status"] or "")
                if self.row_is_active(row):
                    return {
                        "application_id": str(row["application_id"] or ""),
                        "action": str(row["action"] or ""),
                        "application_status": application_status,
                        "effect_status": effect_status,
                        "cycle_ts": float(row["cycle_ts"] or 0.0),
                        "updated_at": float(row["updated_at"] or 0.0),
                        "observed_trade_count": int(row["observed_trade_count"] or 0),
                        "baseline_trade_count": int(row["baseline_trade_count"] or 0),
                    }
            return None
        finally:
            conn.close()

    def evaluate(
        self,
        *,
        scope_type: str,
        scope_key: str,
        action: str,
        old_weight: float | None = None,
        new_weight: float | None = None,
        min_abs_delta: float = 0.002,
        min_relative_delta: float = 0.05,
        bypass_for_risk_reduction: bool = False,
    ) -> dict[str, Any]:
        if action in STRUCTURAL_AUDIT_ACTIONS:
            return {
                "ok": True,
                "allowed": True,
                "status": "structural_audit_only",
                "effect_tracking": "not_trade_attributed",
                "boundary": self.boundary(),
            }
        active = self.active(scope_type=scope_type, scope_key=scope_key)
        if active and not bypass_for_risk_reduction:
            return {
                "ok": True,
                "allowed": False,
                "status": "blocked_active_experiment",
                "reason": "existing_effect_window_must_terminalize",
                "active_application": active,
                "boundary": self.boundary(),
            }
        delta = None
        threshold = None
        if action in WEIGHT_ACTIONS and old_weight is not None and new_weight is not None:
            delta = abs(float(new_weight) - float(old_weight))
            threshold = max(float(min_abs_delta), abs(float(old_weight)) * float(min_relative_delta))
            if delta < threshold:
                return {
                    "ok": True,
                    "allowed": False,
                    "status": "blocked_immaterial_delta",
                    "reason": "weight_delta_below_experiment_materiality",
                    "absolute_delta": round(delta, 8),
                    "required_delta": round(threshold, 8),
                    "boundary": self.boundary(),
                }
        return {
            "ok": True,
            "allowed": True,
            "status": "admitted",
            "absolute_delta": round(delta, 8) if delta is not None else None,
            "required_delta": round(threshold, 8) if threshold is not None else None,
            "boundary": self.boundary(),
        }
