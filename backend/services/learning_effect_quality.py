"""Read-only quality/SLO view for the autonomous learning effect ledger."""
from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, connect_sqlite, get_state_pg_conn, is_state_db_path, state_table_exists
from backend.services.canonical_v2_reader import (
    iter_decision_factor_snapshots,
    iter_decision_rows,
    iter_reviews,
)
from backend.services.fact_envelope import observed_epoch
from backend.services.learning_application_store import LearningApplicationStore


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

    def _awe_mutation_coverage(self, conn: Any, *, now: float) -> dict[str, Any]:
        if not state_table_exists(conn, "runtime_config_snapshot") or not state_table_exists(conn, "learning_application_log"):
            return {"status": "unavailable", "mutation_count": 0, "covered_count": 0, "missing_run_ids": []}
        cutoff = now - 24 * 3600.0
        snapshots = conn.execute(
            self._sql(
                """
                SELECT run_id, MAX(created_at) AS created_at
                FROM runtime_config_snapshot
                WHERE source='awe_decision_policy_update_weight' AND created_at>=?
                GROUP BY run_id
                """
            ),
            (cutoff,),
        ).fetchall()
        mutation_at = {
            str(row["run_id"] or ""): float(row["created_at"] or 0.0)
            for row in snapshots
            if str(row["run_id"] or "")
        }
        mutation_runs = set(mutation_at)
        applications = conn.execute(
            self._sql("SELECT details_json FROM learning_application_log WHERE created_at>=?"),
            (cutoff,),
        ).fetchall()
        covered_runs: set[str] = set()
        covered_at: list[float] = []
        for row in applications:
            details = _loads(row["details_json"])
            if str(details.get("producer") or "") == "awe_adapt" and str(details.get("run_id") or ""):
                covered_runs.add(str(details["run_id"]))
                covered_at.append(float(details.get("applied_at") or details.get("cycle_ts") or 0.0))
        first_enforced_at = min((value for value in covered_at if value > 0.0), default=0.0)
        missing_all = mutation_runs - covered_runs
        missing = sorted(run_id for run_id in missing_all if first_enforced_at and mutation_at[run_id] >= first_enforced_at)
        legacy_missing = sorted(missing_all - set(missing))
        return {
            "status": "ok" if not missing else "degraded",
            "window_hours": 24,
            "mutation_count": len(mutation_runs),
            "covered_count": len(mutation_runs & covered_runs),
            "coverage_ratio": round(len(mutation_runs & covered_runs) / max(len(mutation_runs), 1), 6),
            "missing_run_ids": missing[:20],
            "legacy_missing_count": len(legacy_missing),
            "enforced_from": first_enforced_at,
        }

    def _retry_context(
        self,
        conn: Any,
        factor_cutoffs: dict[str, float],
    ) -> tuple[dict[str, float], list[tuple[str, str, float]]]:
        latest_review_by_factor: dict[str, float] = {}
        if factor_cutoffs:
            factors = sorted(factor_cutoffs)
            # Factor/decision pairs are derived only through the canonical
            # reader.  An unavailable or incomplete canonical stream yields
            # no evidence; it must never reopen a retired fact table.
            rows: list[dict[str, Any]] = []
            factor_set = set(factors)
            try:
                for decision in iter_decision_rows(conn, limit=500, reverse=True):
                    decision_id = str(decision.get("decision_id") or "")
                    if not decision_id:
                        continue
                    for snapshot in iter_decision_factor_snapshots(conn, decision_id):
                        if str(snapshot.get("factor") or "") not in factor_set:
                            continue
                        normalized = dict(snapshot)
                        normalized["decision_id"] = str(
                            normalized.get("decision_id") or decision_id
                        )
                        rows.append(normalized)
            except Exception:
                # Canonical read failure is fail-closed for retry evidence; it
                # must not reopen a retired fact table.
                rows = []
            cutoff = float(min(factor_cutoffs.values()) or 0.0)
            # Reviews and factor snapshots both flow through canonical readers.
            review_ts_by_decision: dict[str, float] = {}
            for record in iter_reviews(conn, limit=0):
                payload = record.get("payload") or {}
                entry_id = str(payload.get("entry_decision_id") or "")
                reviewed_at = observed_epoch(payload.get("created_at"))
                if entry_id and reviewed_at > cutoff:
                    review_ts_by_decision[entry_id] = max(review_ts_by_decision.get(entry_id, 0.0), reviewed_at)
            for row in rows:
                factor = str(row["factor"] or "")
                reviewed_at = review_ts_by_decision.get(str(row["decision_id"] or ""))
                if reviewed_at:
                    latest_review_by_factor[factor] = max(latest_review_by_factor.get(factor, 0.0), reviewed_at)
        if not factor_cutoffs:
            return latest_review_by_factor, []
        store = LearningApplicationStore(self.db_path)
        active_applications = [
            (
                str(app.get("scope_type") or ""),
                str(app.get("scope_key") or ""),
                float(app.get("created_at") or app.get("cycle_ts") or 0.0),
            )
            for app in store.iter_applications()
            if str(app.get("status") or "") not in ("superseded", "rolled_back", "rejected")
        ]
        return (
            latest_review_by_factor,
            active_applications,
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
        cycle_ts = float(row.get("cycle_ts") or row.get("created_at") or 0.0)
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
            store = LearningApplicationStore(self.db_path)
            rows = sorted(
                (eff for eff in store.iter_effects()),
                key=lambda eff: float(eff.get("updated_at") or eff.get("created_at") or 0.0),
                reverse=True,
            )[:max(1, min(int(limit), 5000))]
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
                decision = row.get("decision") or {}
                quality = decision.get("evidence_quality") if isinstance(decision.get("evidence_quality"), dict) else {}
                if str(row.get("status") or "") != "inconclusive" or not bool(quality.get("retry_via_new_application")):
                    continue
                scope_type = str(row.get("scope_type") or "")
                scope_key = str(row.get("scope_key") or "")
                if scope_type not in {"factor", "parameter_template"}:
                    continue
                factor = scope_key if scope_type == "factor" else scope_key.split(":", 1)[0]
                updated_at = float(row.get("updated_at") or 0.0)
                retry_factor_cutoffs[factor] = min(retry_factor_cutoffs.get(factor, updated_at), updated_at)
            latest_review_by_factor, active_applications = self._retry_context(conn, retry_factor_cutoffs)
            for row in rows:
                status = str(row.get("status") or "unknown")
                status_counts[status] += 1
                decision = row.get("decision") or {}
                quality = decision.get("evidence_quality") if isinstance(decision.get("evidence_quality"), dict) else {}
                reason = str(quality.get("causal_status") or "missing")
                reason_counts[reason] += 1
                if reason == "bounded_window_insufficient_samples" and status != "inconclusive":
                    bounded_nonterminal_count += 1
                if status in {"prepared", "observing", "mixed"}:
                    oldest_active_age = max(oldest_active_age, now - float(row.get("cycle_ts") or row.get("created_at") or now))
                    oldest_active_review_age = max(oldest_active_review_age, now - float(row.get("updated_at") or row.get("created_at") or now))
                if status == "inconclusive" and bool(quality.get("retry_via_new_application")):
                    retry_review_count += 1
                    eligible, eligibility_reason = self._retry_eligibility(row, latest_review_by_factor, active_applications)
                    item = {
                        "application_id": str(row.get("application_id")),
                        "scope_type": str(row.get("scope_type")),
                        "scope_key": str(row.get("scope_key")),
                        "action": str(row.get("action")),
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
            active = status_counts["prepared"] + status_counts["observing"] + status_counts["mixed"]
            terminal = sum(status_counts[key] for key in ("reinforced", "effective", "ineffective", "rolled_back", "inconclusive", "superseded"))
            confounded = reason_counts["confounded_by_concurrent_application"]
            closure_ratio = terminal / max(len(rows), 1)
            awe_mutation_coverage = self._awe_mutation_coverage(conn, now=now)
            from backend.services.experience_prior import ExperiencePriorService

            experience_prior = ExperiencePriorService(self.db_path).build(cache_seconds=0.0)
            checks = {
                "no_concurrent_attribution_backlog": confounded == 0,
                "all_awe_mutations_have_effect_ledger": not awe_mutation_coverage.get("missing_run_ids"),
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
                "awe_mutation_coverage": awe_mutation_coverage,
                "experience_prior": {
                    "status": experience_prior.get("status"),
                    "eligible_count": experience_prior.get("eligible_count", 0),
                    "bounded_factor_count": experience_prior.get("bounded_factor_count", 0),
                    "rejected_unbounded_count": experience_prior.get("rejected_unbounded_count", 0),
                    "boundary": experience_prior.get("boundary") or {},
                },
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
        return {"ok": False, "schema_version": "learning_effect_quality.v1", "status": reason, "status_counts": {}, "reason_counts": {}, "active_count": 0, "terminal_count": 0, "closure_ratio": 0.0, "awe_mutation_coverage": {}, "experience_prior": {}, "retry_reviews": [], "retry_review_count": 0, "retry_candidates": [], "retry_candidate_count": 0, "boundary": self.boundary()}
