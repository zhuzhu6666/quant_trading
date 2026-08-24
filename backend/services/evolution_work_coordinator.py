"""Cross-job serialization for autonomous research and governance work.

The scheduler already prevents two instances of the *same* job, but evolution,
nursery and factor governance are different jobs that touch overlapping
evidence and lifecycle state.  This coordinator gives that work one process-
independent PostgreSQL advisory lock without turning it into another agent or
another decision authority.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from backend.core.db import get_state_pg_conn

logger = logging.getLogger(__name__)

LOCK_NAME = "quant_autonomous_evolution_work"


class EvolutionWorkCoordinator:
    def __init__(self, conn_factory: Callable[..., Any] = get_state_pg_conn):
        self._conn_factory = conn_factory

    @staticmethod
    def _scalar(row: Any) -> bool:
        if row is None:
            return False
        if hasattr(row, "keys"):
            keys = list(row.keys())
            return bool(row[keys[0]]) if keys else False
        return bool(row[0])

    def run(self, job_name: str, fn: Callable[[], Any]) -> Any:
        """Run one heavy autonomous job, or return a non-error busy result."""
        conn = None
        acquired = False
        started_at = time.time()
        try:
            conn = self._conn_factory()
            row = conn.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s))",
                (LOCK_NAME,),
            ).fetchone()
            acquired = self._scalar(row)
            if not acquired:
                # pg_try_advisory_lock() is a SELECT and therefore starts a
                # transaction on a normal psycopg connection.  End it before
                # returning so a busy scheduler attempt cannot become an
                # idle-in-transaction backend.
                rollback = getattr(conn, "rollback", None)
                if callable(rollback):
                    rollback()
                logger.info(
                    "[evolution_coordinator] skip %s: another autonomous work item is active",
                    job_name,
                )
                return {
                    "ok": True,
                    "status": "skipped_busy",
                    "job_name": job_name,
                    "reason": "autonomous_work_lock_held",
                }
            # The advisory lock is session-scoped.  Commit only the lock
            # acquisition transaction, then run the heavy job outside that
            # transaction.  The job owns its own business transactions.
            commit = getattr(conn, "commit", None)
            if callable(commit):
                commit()
            logger.info("[evolution_coordinator] started %s", job_name)
            return fn()
        except Exception:
            logger.exception("[evolution_coordinator] %s failed", job_name)
            raise
        finally:
            if conn is not None:
                if acquired:
                    try:
                        conn.execute(
                            "SELECT pg_advisory_unlock(hashtext(%s))",
                            (LOCK_NAME,),
                        )
                    except Exception:
                        logger.exception(
                            "[evolution_coordinator] failed to release lock for %s",
                            job_name,
                        )
                try:
                    conn.close()
                except Exception:
                    pass
            if acquired:
                logger.info(
                    "[evolution_coordinator] finished %s in %.1fs",
                    job_name,
                    time.time() - started_at,
                )


def coordinated_job(job_name: str, fn: Callable[[], Any]) -> Callable[[], Any]:
    """Return a scheduler-compatible wrapper around the shared coordinator."""

    def _run() -> Any:
        return EvolutionWorkCoordinator().run(job_name, fn)

    _run.__name__ = f"coordinated_{job_name}"
    return _run
