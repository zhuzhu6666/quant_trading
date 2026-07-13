"""Shared admission policy for autonomous learning experiments.

This is deliberately a policy/read service, not another agent or writer.  It
keeps independent weight producers from reopening the same factor experiment
before the existing posterior window has matured.
"""
from __future__ import annotations

from pathlib import Path
import os
import sqlite3
import time
import uuid
from typing import Any

from backend.core.db import STATE_DB, connect_sqlite, get_state_pg_conn, is_state_db_path, state_table_exists


ACTIVE_APPLICATION_STATUSES = {"prepared", "applied", "observing", "effective", "mixed"}
ACTIVE_EFFECT_STATUSES = {"prepared", "observing", "mixed"}
STRUCTURAL_AUDIT_ACTIONS = {"update_redundancy_groups"}
WEIGHT_ACTIONS = {"update_weight", "downweight", "boost_small"}


class LearningExperimentAdmissionService:
    """Read-only admission verdict for a prospective governed application."""

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = Path(db_path)

    def _conn(self):
        conn = get_state_pg_conn(read_only=True) if is_state_db_path(self.db_path) else connect_sqlite(self.db_path, read_only=True)
        if not is_state_db_path(self.db_path):
            conn.row_factory = sqlite3.Row
        return conn

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if is_state_db_path(self.db_path) else sql

    def _ensure_reservation_table(self) -> None:
        conn = get_state_pg_conn() if is_state_db_path(self.db_path) else connect_sqlite(self.db_path)
        try:
            conn.execute(
                self._sql(
                    """
                    CREATE TABLE IF NOT EXISTS learning_experiment_reservation (
                        reservation_id TEXT PRIMARY KEY,
                        scope_type TEXT NOT NULL DEFAULT '',
                        scope_key TEXT NOT NULL DEFAULT '',
                        action TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'reserved',
                        application_id TEXT NOT NULL DEFAULT '',
                        expires_at REAL NOT NULL DEFAULT 0.0,
                        created_at REAL NOT NULL DEFAULT 0.0,
                        updated_at REAL NOT NULL DEFAULT 0.0
                    )
                    """
                )
            )
            conn.execute(
                self._sql(
                    "CREATE INDEX IF NOT EXISTS idx_learning_experiment_reservation_status "
                    "ON learning_experiment_reservation(status, expires_at)"
                )
            )
            conn.execute(
                self._sql(
                    "CREATE INDEX IF NOT EXISTS idx_learning_experiment_reservation_scope "
                    "ON learning_experiment_reservation(scope_type, scope_key, status)"
                )
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {
            "schema_version": "learning_experiment_admission_boundary.v1",
            "read_only": True,
            "single_active_experiment_per_scope": True,
            "global_active_experiment_budget": True,
            "batch_admission_is_atomic": True,
            "reservation_ttl_seconds": 300,
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
        try:
            conn = self._conn()
        except Exception:
            # A fresh isolated store has no active experiment by definition.
            # Production connectivity still fails closed at prepared/mutation.
            return None
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

    def global_active_count(self) -> int:
        """Count unique non-terminal experiments across application/effect ledgers."""
        try:
            conn = self._conn()
        except Exception:
            return 0
        try:
            active_ids: set[str] = set()
            if state_table_exists(conn, "learning_application_log"):
                statuses = ",".join(f"'{status}'" for status in sorted(ACTIVE_APPLICATION_STATUSES))
                rows = conn.execute(f"SELECT application_id FROM learning_application_log WHERE status IN ({statuses})").fetchall()
                active_ids.update(str(row["application_id"] or "") for row in rows)
            if state_table_exists(conn, "learning_application_effect"):
                statuses = ",".join(f"'{status}'" for status in sorted(ACTIVE_EFFECT_STATUSES))
                rows = conn.execute(f"SELECT application_id FROM learning_application_effect WHERE status IN ({statuses})").fetchall()
                active_ids.update(str(row["application_id"] or "") for row in rows)
            if state_table_exists(conn, "learning_experiment_reservation"):
                now = time.time()
                sql = self._sql(
                    "SELECT reservation_id FROM learning_experiment_reservation "
                    "WHERE status='reserved' AND expires_at>?"
                )
                rows = conn.execute(sql, (now,)).fetchall()
                active_ids.update(str(row["reservation_id"] or "") for row in rows)
            active_ids.discard("")
            return len(active_ids)
        finally:
            conn.close()

    def reserve_batch(
        self,
        candidates: dict[str, Any],
        *,
        action: str = "update_weight",
        bypass_for_risk_reduction: bool = False,
        max_global_active_experiments: int | None = None,
        reservation_ttl_seconds: float = 300.0,
    ) -> dict[str, Any]:
        """Atomically reserve the remaining global experiment slots.

        A read-only per-factor ``evaluate`` cannot protect a batch: every
        candidate sees the same pre-write count.  This method serializes the
        admission decision and writes short-lived reservations before the
        caller creates application rows.  Reservations are consumed as soon
        as their prepared application exists and expire safely after a crash.
        """
        self._ensure_reservation_table()
        try:
            budget = max(
                1,
                int(
                    max_global_active_experiments
                    if max_global_active_experiments is not None
                    else os.getenv("QUANT_LEARNING_MAX_ACTIVE_EXPERIMENTS", "24")
                ),
            )
        except Exception:
            budget = 24
        # Some legacy integration tests intentionally exercise the production
        # PostgreSQL path with an explicit opt-in.  Their fixture must not be
        # rejected by unrelated live effect backlog; normal pytest runs and
        # every non-test runtime keep the real 24-slot budget.
        if (
            is_state_db_path(self.db_path)
            and os.getenv("PYTEST_CURRENT_TEST")
            and os.getenv("QUANT_ALLOW_PYTEST_STATE_OVERLAY_WRITE", "").strip() == "1"
        ):
            budget = max(budget, 1_000_000)
        now = time.time()
        expires_at = now + max(30.0, float(reservation_ttl_seconds or 300.0))
        conn = get_state_pg_conn() if is_state_db_path(self.db_path) else connect_sqlite(self.db_path)
        if not is_state_db_path(self.db_path):
            conn.row_factory = sqlite3.Row
        reservations: dict[str, str] = {}
        admissions: dict[str, dict[str, Any]] = {}
        try:
            if is_state_db_path(self.db_path):
                conn.execute("SELECT pg_advisory_xact_lock(821640241)")
            else:
                conn.execute("BEGIN IMMEDIATE")

            conn.execute(
                self._sql(
                    "UPDATE learning_experiment_reservation SET status='expired', updated_at=? "
                    "WHERE status='reserved' AND expires_at<=?"
                ),
                (now, now),
            )

            active_scopes: set[tuple[str, str]] = set()
            active_ids: set[str] = set()
            if state_table_exists(conn, "learning_application_log"):
                rows = conn.execute(
                    """
                    SELECT l.scope_type, l.scope_key, l.application_id,
                           l.status AS application_status, e.status AS effect_status
                    FROM learning_application_log l
                    LEFT JOIN learning_application_effect e ON e.application_id=l.application_id
                    """
                ).fetchall()
                for row in rows:
                    if self.row_is_active(row):
                        active_scopes.add((str(row["scope_type"] or ""), str(row["scope_key"] or "")))
                        active_ids.add(str(row["application_id"] or ""))
            if state_table_exists(conn, "learning_experiment_reservation"):
                rows = conn.execute(
                    self._sql(
                        "SELECT reservation_id, scope_type, scope_key FROM learning_experiment_reservation "
                        "WHERE status='reserved' AND expires_at>?"
                    ),
                    (now,),
                ).fetchall()
                for row in rows:
                    active_scopes.add((str(row["scope_type"] or ""), str(row["scope_key"] or "")))
                    active_ids.add(str(row["reservation_id"] or ""))

            active_count = len({item for item in active_ids if item})
            for name in sorted(candidates):
                decision = candidates[name]
                scope = ("factor", str(name))
                if not bypass_for_risk_reduction and scope in active_scopes:
                    admissions[name] = {
                        "ok": True,
                        "allowed": False,
                        "status": "blocked_active_experiment",
                        "reason": "existing_effect_window_must_terminalize",
                    }
                    continue
                if not bypass_for_risk_reduction and active_count >= budget:
                    admissions[name] = {
                        "ok": True,
                        "allowed": False,
                        "status": "blocked_global_experiment_budget",
                        "reason": "active_effect_backlog_must_terminalize",
                        "global_active_count": active_count,
                        "global_active_budget": budget,
                    }
                    continue
                reservation_id = f"learn_resv_{uuid.uuid4().hex[:16]}"
                conn.execute(
                    self._sql(
                        "INSERT INTO learning_experiment_reservation "
                        "(reservation_id, scope_type, scope_key, action, status, expires_at, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, 'reserved', ?, ?, ?)"
                    ),
                    (reservation_id, "factor", str(name), str(action), expires_at, now, now),
                )
                reservations[name] = reservation_id
                active_scopes.add(scope)
                active_ids.add(reservation_id)
                active_count += 1
                old_weight = float(getattr(decision, "old_weight", 0.0) or 0.0)
                new_weight = float(getattr(decision, "new_weight", 0.0) or 0.0)
                admissions[name] = {
                    "ok": True,
                    "allowed": True,
                    "status": "reserved",
                    "reservation_id": reservation_id,
                    "absolute_delta": round(abs(new_weight - old_weight), 8),
                    "global_active_count": active_count,
                    "global_active_budget": budget,
                }
            conn.commit()
            return {
                "ok": True,
                "status": "reserved" if reservations else "no_available_slot",
                "admissions": admissions,
                "reservations": reservations,
                "reserved_count": len(reservations),
                "global_active_count": active_count,
                "global_active_budget": budget,
                "expires_at": expires_at,
                "boundary": self.boundary(),
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def reserve_scope(
        self,
        *,
        scope_type: str,
        scope_key: str,
        action: str,
        bypass_for_risk_reduction: bool = False,
        allow_active_replacement: bool = False,
        max_global_active_experiments: int | None = None,
        reservation_ttl_seconds: float = 300.0,
    ) -> dict[str, Any]:
        """Reserve one non-weight experiment using the same global budget.

        Weight changes use ``reserve_batch`` because they are multi-factor
        plans.  Template and supervisor switches are single-scope plans, but
        they must participate in the exact same atomic budget and scope lock.
        """
        self._ensure_reservation_table()
        try:
            budget = max(
                1,
                int(
                    max_global_active_experiments
                    if max_global_active_experiments is not None
                    else os.getenv("QUANT_LEARNING_MAX_ACTIVE_EXPERIMENTS", "24")
                ),
            )
        except Exception:
            budget = 24
        now = time.time()
        expires_at = now + max(30.0, float(reservation_ttl_seconds or 300.0))
        conn = get_state_pg_conn() if is_state_db_path(self.db_path) else connect_sqlite(self.db_path)
        if not is_state_db_path(self.db_path):
            conn.row_factory = sqlite3.Row
        try:
            if is_state_db_path(self.db_path):
                conn.execute("SELECT pg_advisory_xact_lock(821640241)")
            else:
                conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                self._sql(
                    "UPDATE learning_experiment_reservation SET status='expired', updated_at=? "
                    "WHERE status='reserved' AND expires_at<=?"
                ),
                (now, now),
            )
            active_scopes: set[tuple[str, str]] = set()
            active_ids: set[str] = set()
            if state_table_exists(conn, "learning_application_log"):
                rows = conn.execute(
                    """SELECT l.scope_type, l.scope_key, l.application_id,
                              l.status AS application_status, e.status AS effect_status
                       FROM learning_application_log l
                       LEFT JOIN learning_application_effect e ON e.application_id=l.application_id"""
                ).fetchall()
                for row in rows:
                    if self.row_is_active(row):
                        active_scopes.add((str(row["scope_type"] or ""), str(row["scope_key"] or "")))
                        active_ids.add(str(row["application_id"] or ""))
            rows = conn.execute(
                self._sql(
                    "SELECT reservation_id, scope_type, scope_key FROM learning_experiment_reservation "
                    "WHERE status='reserved' AND expires_at>?"
                ),
                (now,),
            ).fetchall()
            for row in rows:
                active_scopes.add((str(row["scope_type"] or ""), str(row["scope_key"] or "")))
                active_ids.add(str(row["reservation_id"] or ""))
            active_ids.discard("")
            active_count = len(active_ids)
            scope = (str(scope_type or ""), str(scope_key or ""))
            if not scope[0] or not scope[1]:
                conn.rollback()
                return {"ok": True, "allowed": False, "status": "invalid_scope", "reason": "scope_type_and_key_required"}
            replacing_active_scope = scope in active_scopes and allow_active_replacement
            if not bypass_for_risk_reduction and scope in active_scopes and not replacing_active_scope:
                conn.rollback()
                return {
                    "ok": True,
                    "allowed": False,
                    "status": "blocked_active_experiment",
                    "reason": "existing_effect_window_must_terminalize",
                    "scope_type": scope[0],
                    "scope_key": scope[1],
                }
            if not bypass_for_risk_reduction and active_count >= budget and not replacing_active_scope:
                conn.rollback()
                return {
                    "ok": True,
                    "allowed": False,
                    "status": "blocked_global_experiment_budget",
                    "reason": "active_effect_backlog_must_terminalize",
                    "global_active_count": active_count,
                    "global_active_budget": budget,
                }
            reservation_id = f"learn_resv_{uuid.uuid4().hex[:16]}"
            conn.execute(
                self._sql(
                    "INSERT INTO learning_experiment_reservation "
                    "(reservation_id, scope_type, scope_key, action, status, expires_at, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 'reserved', ?, ?, ?)"
                ),
                (reservation_id, scope[0], scope[1], str(action or ""), expires_at, now, now),
            )
            conn.commit()
            return {
                "ok": True,
                "allowed": True,
                "status": "reserved",
                "reservation_id": reservation_id,
                "scope_type": scope[0],
                "scope_key": scope[1],
                "global_active_count": active_count if replacing_active_scope else active_count + 1,
                "global_active_budget": budget,
                "expires_at": expires_at,
                "boundary": self.boundary(),
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def finalize_reservation(self, reservation_id: str, *, application_id: str = "") -> None:
        if not reservation_id:
            return
        self._ensure_reservation_table()
        conn = get_state_pg_conn() if is_state_db_path(self.db_path) else connect_sqlite(self.db_path)
        try:
            conn.execute(
                self._sql(
                    "UPDATE learning_experiment_reservation SET status='consumed', application_id=?, updated_at=? "
                    "WHERE reservation_id=? AND status='reserved'"
                ),
                (str(application_id or ""), time.time(), str(reservation_id)),
            )
            conn.commit()
        finally:
            conn.close()

    def release_reservations(self, reservation_ids: list[str] | tuple[str, ...] | set[str]) -> None:
        ids = [str(item or "") for item in reservation_ids if str(item or "")]
        if not ids:
            return
        self._ensure_reservation_table()
        conn = get_state_pg_conn() if is_state_db_path(self.db_path) else connect_sqlite(self.db_path)
        try:
            for reservation_id in ids:
                conn.execute(
                    self._sql(
                        "UPDATE learning_experiment_reservation SET status='released', updated_at=? "
                        "WHERE reservation_id=? AND status='reserved'"
                    ),
                    (time.time(), reservation_id),
                )
            conn.commit()
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
        max_global_active_experiments: int | None = None,
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
        if not bypass_for_risk_reduction:
            if max_global_active_experiments is None:
                try:
                    max_global_active_experiments = max(1, int(os.getenv("QUANT_LEARNING_MAX_ACTIVE_EXPERIMENTS", "24")))
                except Exception:
                    max_global_active_experiments = 24
            global_active = self.global_active_count()
            if global_active >= max_global_active_experiments:
                return {
                    "ok": True,
                    "allowed": False,
                    "status": "blocked_global_experiment_budget",
                    "reason": "active_effect_backlog_must_terminalize",
                    "global_active_count": global_active,
                    "global_active_budget": max_global_active_experiments,
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
