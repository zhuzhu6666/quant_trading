"""Read-only quality/SLO view for the autonomous learning effect ledger."""
from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, connect_sqlite, get_state_pg_conn, is_state_db_path, state_table_exists


def _loads(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(str(raw or "{}"))
    except Exception:
        return {}


class LearningEffectQualityService:
    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = db_path

    def _conn(self):
        conn = get_state_pg_conn(read_only=True) if is_state_db_path(self.db_path) else connect_sqlite(self.db_path, read_only=True)
        if not is_state_db_path(self.db_path):
            conn.row_factory = __import__("sqlite3").Row
        return conn

    def _sql(self, sql: str) -> str:
        return sql.replace("%", "%%").replace("?", "%s") if is_state_db_path(self.db_path) else sql

    def _retry_context(
        self,
        conn: Any,
        factor_cutoffs: dict[str, float],
    ) -> tuple[dict[str, float], list[tuple[str, str, float]]]:
        latest_review_by_factor: dict[str, float] = {}
        if factor_cutoffs and state_table_exists(conn, "trade_outcome_review") and state_table_exists(conn, "decision_factor_snapshot"):
            factors = sorted(factor_cutoffs)
            placeholders = ",".join("?" for _ in factors)
            latest_reviews = conn.execute(
                self._sql(f"""
                SELECT dfs.factor, MAX(r.created_at) AS latest_review_at
                FROM trade_outcome_review r
                JOIN decision_factor_snapshot dfs ON dfs.decision_id=r.entry_decision_id
                WHERE r.created_at>? AND dfs.factor IN ({placeholders})
                GROUP BY dfs.factor
                """),
                (min(factor_cutoffs.values()), *factors),
            ).fetchall()
            latest_review_by_factor = {
                str(row["factor"]): float(row["latest_review_at"] or 0.0) for row in latest_reviews
            }
        if not factor_cutoffs or not state_table_exists(conn, "learning_application_log"):
            return latest_review_by_factor, []
        active_applications = conn.execute(
            """
            SELECT scope_type, scope_key, cycle_ts
            FROM learning_application_log
            WHERE status NOT IN ('superseded','rolled_back','rejected')
            """
        ).fetchall()
        return (
            latest_review_by_factor,
            [
                (str(row["scope_type"] or ""), str(row["scope_key"] or ""), float(row["cycle_ts"] or 0.0))
                for row in active_applications
            ],
        )

    @staticmethod
    def _retry_eligibility(
        row: Any,
        latest_review_by_factor: dict[str, float],
        active_applications: list[tuple[str, str, float]],
    ) -> tuple[bool, str]:
        scope_type = str(row["scope_type"] or "")
        scope_key = str(row["scope_key"] or "")
        if scope_type not in {"factor", "parameter_template"}:
            return False, "unsupported_retry_scope"
        factor = scope_key if scope_type == "factor" else scope_key.split(":", 1)[0]
        latest_review_at = latest_review_by_factor.get(factor, 0.0)
        if latest_review_at <= float(row["updated_at"] or 0.0):
            return False, "no_new_evidence_after_terminalization"
        cycle_ts = float(row["cycle_ts"] or 0.0)
        if any(
            active_scope_type == scope_type and active_scope_key == scope_key and active_cycle_ts > cycle_ts
            for active_scope_type, active_scope_key, active_cycle_ts in active_applications
        ):
            return False, "newer_application_already_exists"
        return True, "new_review_evidence_available"

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {
            "schema_version": "learning_effect_quality_boundary.v1",
            "read_only": True,
            "retry_is_eligibility_only": True,
            "retry_requires_governor_decision": True,
            "does_not_create_application": True,
            "does_not_apply_runtime_mutation": True,
            "does_not_change_agent_authority": True,
        }

    def status(self, *, limit: int = 1000) -> dict[str, Any]:
        now = time.time()
        conn = self._conn()
        try:
            if not state_table_exists(conn, "learning_application_effect"):
                return self._empty("missing_effect_ledger")
            rows = conn.execute(
                """
                SELECT e.application_id, e.scope_type, e.scope_key, e.action, e.status,
                       e.observed_trade_count, e.baseline_trade_count, e.decision_json,
                       e.updated_at, e.created_at, l.cycle_ts
                FROM learning_application_effect e
                LEFT JOIN learning_application_log l ON l.application_id=e.application_id
                ORDER BY e.updated_at DESC LIMIT ?
                """ if not is_state_db_path(self.db_path) else
                """
                SELECT e.application_id, e.scope_type, e.scope_key, e.action, e.status,
                       e.observed_trade_count, e.baseline_trade_count, e.decision_json,
                       e.updated_at, e.created_at, l.cycle_ts
                FROM learning_application_effect e
                LEFT JOIN learning_application_log l ON l.application_id=e.application_id
                ORDER BY e.updated_at DESC LIMIT %s
                """,
                (max(1, min(int(limit), 5000)),),
            ).fetchall()
            status_counts: Counter[str] = Counter()
            reason_counts: Counter[str] = Counter()
            oldest_active_age = 0.0
            oldest_active_review_age = 0.0
            bounded_nonterminal_count = 0
            retry_review_count = 0
            retry_candidate_count = 0
            retry_candidates: list[dict[str, Any]] = []
            retry_reviews: list[dict[str, Any]] = []
            retry_factor_cutoffs: dict[str, float] = {}
            for row in rows:
                decision = _loads(row["decision_json"])
                quality = decision.get("evidence_quality") if isinstance(decision.get("evidence_quality"), dict) else {}
                if str(row["status"] or "") != "inconclusive" or not bool(quality.get("retry_via_new_application")):
                    continue
                scope_type = str(row["scope_type"] or "")
                scope_key = str(row["scope_key"] or "")
                if scope_type not in {"factor", "parameter_template"}:
                    continue
                factor = scope_key if scope_type == "factor" else scope_key.split(":", 1)[0]
                updated_at = float(row["updated_at"] or 0.0)
                retry_factor_cutoffs[factor] = min(retry_factor_cutoffs.get(factor, updated_at), updated_at)
            latest_review_by_factor, active_applications = self._retry_context(conn, retry_factor_cutoffs)
            for row in rows:
                status = str(row["status"] or "unknown")
                status_counts[status] += 1
                decision = _loads(row["decision_json"])
                quality = decision.get("evidence_quality") if isinstance(decision.get("evidence_quality"), dict) else {}
                reason = str(quality.get("causal_status") or "missing")
                reason_counts[reason] += 1
                if reason == "bounded_window_insufficient_samples" and status != "inconclusive":
                    bounded_nonterminal_count += 1
                if status in {"observing", "mixed"}:
                    oldest_active_age = max(oldest_active_age, now - float(row["cycle_ts"] or row["created_at"] or now))
                    oldest_active_review_age = max(oldest_active_review_age, now - float(row["updated_at"] or row["created_at"] or now))
                if status == "inconclusive" and bool(quality.get("retry_via_new_application")):
                    retry_review_count += 1
                    eligible, eligibility_reason = self._retry_eligibility(row, latest_review_by_factor, active_applications)
                    item = {
                        "application_id": str(row["application_id"]),
                        "scope_type": str(row["scope_type"]),
                        "scope_key": str(row["scope_key"]),
                        "action": str(row["action"]),
                        "reason": reason,
                        "retry_eligible": eligible,
                        "eligibility_reason": eligibility_reason,
                    }
                    if len(retry_reviews) < 50:
                        retry_reviews.append(item)
                    if eligible:
                        retry_candidate_count += 1
                        if len(retry_candidates) < 50:
                            retry_candidates.append(item)
            active = status_counts["observing"] + status_counts["mixed"]
            terminal = sum(status_counts[key] for key in ("reinforced", "effective", "ineffective", "rolled_back", "inconclusive", "superseded"))
            confounded = reason_counts["confounded_by_concurrent_application"]
            closure_ratio = terminal / max(len(rows), 1)
            checks = {
                "no_concurrent_attribution_backlog": confounded == 0,
                "active_window_age_under_30d": oldest_active_age <= 30 * 86400.0,
                "active_review_age_under_24h": oldest_active_review_age <= 86400.0,
                "bounded_windows_terminalize": bounded_nonterminal_count == 0,
                "closure_ratio_at_least_70pct": closure_ratio >= 0.70,
            }
            posture = "ok" if all(checks.values()) else "degraded"
            return {
                "ok": True,
                "schema_version": "learning_effect_quality.v1",
                "status": posture,
                "status_counts": dict(sorted(status_counts.items())),
                "reason_counts": dict(sorted(reason_counts.items())),
                "active_count": active,
                "terminal_count": terminal,
                "closure_ratio": round(closure_ratio, 6),
                "oldest_active_age_seconds": round(max(0.0, oldest_active_age), 3),
                "oldest_active_review_age_seconds": round(max(0.0, oldest_active_review_age), 3),
                "bounded_nonterminal_count": bounded_nonterminal_count,
                "slo": {"status": posture, "checks": checks},
                "retry_reviews": retry_reviews,
                "retry_review_count": retry_review_count,
                "retry_candidates": retry_candidates,
                "retry_candidate_count": retry_candidate_count,
                "boundary": self.boundary(),
            }
        finally:
            conn.close()

    def _empty(self, reason: str) -> dict[str, Any]:
        return {"ok": False, "schema_version": "learning_effect_quality.v1", "status": reason, "status_counts": {}, "reason_counts": {}, "active_count": 0, "terminal_count": 0, "closure_ratio": 0.0, "retry_reviews": [], "retry_review_count": 0, "retry_candidates": [], "retry_candidate_count": 0, "boundary": self.boundary()}
