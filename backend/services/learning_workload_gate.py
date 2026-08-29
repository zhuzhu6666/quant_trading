"""Shared workload gate for scheduled learning/research jobs.

The gate only decides whether an expensive learning scan has new source facts
to consume.  It does not authorize trading or governance mutations; existing
RiskPolicy, V16 and Coordinator contracts remain the authorities for those
actions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, get_state_pg_conn, is_state_db_path
from backend.services.learning_cycle_watermark import LearningCycleWatermarkService
from backend.services.runtime_health_projection import RuntimeHealthProjectionService


RUN_NEW_FACTS = "run_new_facts"
RUN_PENDING_GOVERNANCE = "run_pending_governance"
SKIP_CLOSED_NO_NEW_FACTS = "skip_closed_no_new_facts"
RUN_UNKNOWN = "run_unknown"
_CONFIRMED_CLOSED_MARKET_STATUSES = frozenset(
    {"closed_confirmed", "closed_pending_positions"}
)


def _pending_governance(db_path: str | Path = STATE_DB) -> tuple[bool, str]:
    conn = None
    try:
        conn = get_state_pg_conn(read_only=True) if is_state_db_path(db_path) else None
        if conn is None:
            # SQLite fixtures may omit governance tables; absence means no
            # pending work rather than a reason to invent a second authority.
            import sqlite3

            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
        checks = (
            (
                """
                SELECT 1 FROM policy_suggestion
                WHERE status='approved' AND COALESCE(governance_eligible, 0)=1
                  AND COALESCE(applied_mutation_id, '')=''
                LIMIT 1
                """,
                "approved_policy_suggestion",
            ),
            (
                """
                SELECT 1 FROM factor_lifecycle_state
                WHERE COALESCE(lifecycle_stage, stage)='PROMOTION_PREPARED'
                LIMIT 1
                """,
                "promotion_prepared",
            ),
            (
                """
                SELECT 1 FROM governance_mutation_intent
                WHERE status IN ('reserved', 'prepared')
                LIMIT 1
                """,
                "reserved_governance_mutation",
            ),
        )
        for sql, reason in checks:
            try:
                if conn.execute(sql).fetchone():
                    return True, reason
            except Exception:
                # A missing optional table in an isolated fixture is not
                # pending work.  PostgreSQL production schemas are validated
                # at process start and therefore surface real failures below.
                if is_state_db_path(db_path):
                    return True, "pending_governance_check_unavailable"
        return False, ""
    except Exception:
        return True, "pending_governance_check_unavailable" if is_state_db_path(db_path) else ""
    finally:
        if conn is not None:
            conn.close()


class LearningWorkloadGate:
    """Compose the existing health projection and canonical fact watermark."""

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = Path(db_path)

    def evaluate(self) -> dict[str, Any]:
        # Fixtures and first-run installations may not have a state database
        # yet.  Treat an unavailable projection/watermark as *unknown* rather
        # than raising (or, worse, treating it as closed with no work).  The
        # callers deliberately continue on RUN_UNKNOWN, while production
        # health/readiness remains the authority for any fail-closed decision.
        try:
            health = RuntimeHealthProjectionService(self.db_path).latest(
                max_age_seconds=180.0
            )
        except Exception as exc:
            health = {
                "ok": False,
                "status": "unavailable",
                "reason_code": "learning_health_projection_unavailable",
                "error": str(exc),
            }
        try:
            watermark = LearningCycleWatermarkService(self.db_path).evaluate()
        except Exception as exc:
            watermark = {
                "ok": False,
                "status": "unknown",
                "reason": "learning_watermark_unavailable",
                "error": str(exc),
            }
        session = health.get("market_session") if isinstance(health, dict) else None
        session = dict(session) if isinstance(session, dict) else {}
        market_status = str(session.get("status") or "")
        market_closed = bool(
            health.get("ok")
            and market_status in _CONFIRMED_CLOSED_MARKET_STATUSES
            and session.get("can_open_positions") is False
        )
        watermark_known = bool(watermark.get("ok"))
        if not health.get("ok") or not session or not watermark_known:
            return {
                "status": RUN_UNKNOWN,
                "reason_code": "learning_workload_gate_unknown",
                "market_session": session,
                "market_status": market_status or "unknown",
                "health": health,
                "watermark": watermark,
                "pending_governance": False,
            }
        pending, pending_reason = _pending_governance(self.db_path)
        should_run = bool(watermark.get("should_run"))
        if market_closed and not should_run and pending:
            status = RUN_PENDING_GOVERNANCE
            reason = f"closed_market_pending:{pending_reason}"
        elif market_closed and not should_run:
            status = SKIP_CLOSED_NO_NEW_FACTS
            reason = "market_closed_no_new_facts"
        else:
            status = RUN_NEW_FACTS
            reason = (
                "new_canonical_facts"
                if should_run
                else "market_open_or_not_confirmed_closed"
            )
        return {
            "status": status,
            "reason_code": reason,
            "market_session": session,
            "market_status": market_status,
            "health": health,
            "watermark": watermark,
            "pending_governance": pending,
            "pending_reason": pending_reason,
        }


def evaluate_learning_workload(
    db_path: str | Path = STATE_DB,
) -> dict[str, Any]:
    return LearningWorkloadGate(db_path).evaluate()


__all__ = [
    "LearningWorkloadGate",
    "RUN_NEW_FACTS",
    "RUN_PENDING_GOVERNANCE",
    "RUN_UNKNOWN",
    "SKIP_CLOSED_NO_NEW_FACTS",
    "evaluate_learning_workload",
]
