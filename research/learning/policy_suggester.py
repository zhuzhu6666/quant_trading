from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from backend.core.db import STATE_DB, STATE_DB_DDL, connect_sqlite, get_state_pg_conn, is_state_db_path
from backend.services.governance_eligibility import (
    GOVERNANCE_ELIGIBILITY_VERSION,
    GovernanceEligibility,
    evaluate_governance_eligibility,
)
from backend.services.policy_suggestion_context import attach_policy_suggestion_agent_context
from backend.services.policy_suggestion_identity import deterministic_policy_suggestion_id


class PolicySuggester:
    """Conservative rule-based learning suggester."""

    def __init__(self, db_path: str | None = None):
        self.db_path = Path(db_path or str(STATE_DB))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _use_pg(self) -> bool:
        return is_state_db_path(self.db_path)

    def _p(self) -> str:
        return "%s" if self._use_pg() else "?"

    @contextmanager
    def _conn(self):
        conn = get_state_pg_conn() if self._use_pg() else connect_sqlite(self.db_path)
        if not self._use_pg():
            conn.row_factory = sqlite3.Row
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self._conn() as conn:
            if not self._use_pg():
                conn.executescript(STATE_DB_DDL)

    @staticmethod
    def _experience_eligibility(experience: dict) -> GovernanceEligibility:
        context = experience.get("decision_context_json")
        context = context if isinstance(context, dict) else {}
        review = context.get("review_json")
        review = review if isinstance(review, dict) else {}
        source_table = str(experience.get("source_table") or "").strip()
        source_id = str(experience.get("source_id") or "").strip()
        experience_id = str(experience.get("experience_id") or "").strip()
        context_integrity = str(context.get("context_integrity") or review.get("context_integrity") or "missing").lower()
        attribution_integrity = str(
            context.get("attribution_integrity") or review.get("attribution_integrity") or "missing"
        ).lower()
        failure_tags = {str(value) for value in experience.get("failure_tags", []) or []}
        contamination_tags = {
            "manual_intervention",
            "restart_replay",
            "partial_context",
            "attribution_missing",
            "system_contaminated",
        }
        system_issue = review.get("system_issue_context")
        system_issue = system_issue if isinstance(system_issue, dict) else {}
        contaminated = bool(
            failure_tags & contamination_tags
            or system_issue.get("contaminates_learning")
            or system_issue.get("contaminated")
        )
        full_context = context_integrity == "full" and attribution_integrity == "full"
        lineage_ids = [value for value in (
            f"source:{source_table}:{source_id}" if source_table and source_id else "",
            f"experience:{experience_id}" if experience_id else "",
        ) if value]
        return evaluate_governance_eligibility(
            {
                "sample_id": experience_id,
                "sample_type": "factor_experience",
                "source_table": source_table,
                "source_id": source_id,
                "label_status": "matured",
                "matured": True,
                "integrity": "full" if full_context else context_integrity,
                "system_contaminated": contaminated,
                "model_ready": bool(full_context and source_table and source_id and experience_id),
                "allowed_uses": ["executable_governance"] if full_context else [],
                "lineage_ids": lineage_ids,
                "lineage_complete": len(lineage_ids) == 2,
                "lineage_unique": len(lineage_ids) == len(set(lineage_ids)),
            }
        )

    @staticmethod
    def _next_eligibility_fingerprint(previous: str, eligibility: GovernanceEligibility) -> str:
        fingerprints = [value for value in (str(previous or ""), eligibility.eligibility_fingerprint) if value]
        return hashlib.sha256(
            json.dumps(
                {
                    "schema_version": GOVERNANCE_ELIGIBILITY_VERSION,
                    "evidence_fingerprints": fingerprints,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def suggest_from_experience(self, experience: dict) -> dict | None:
        primary_factor = str(experience.get("primary_factor", "") or "")
        if not primary_factor:
            return None

        reward = float(experience.get("reward_score", 0.0) or 0.0)
        outcome_label = str(experience.get("outcome_label", "") or "")
        decision_context = experience.get("decision_context_json")
        decision_context = (
            decision_context if isinstance(decision_context, dict) else {}
        )
        supervisor_feedback = decision_context.get("supervisor_feedback")
        supervisor_feedback = (
            supervisor_feedback if isinstance(supervisor_feedback, dict) else {}
        )
        supervisor_entry_failure = bool(supervisor_feedback.get("entry_failure"))
        eligibility = self._experience_eligibility(experience)
        now = time.time()
        with self._conn() as conn:
            p = self._p()
            row = conn.execute(
                f"""
                SELECT * FROM experience_pattern_stats
                WHERE scope_type='factor' AND scope_key={p}
                """,
                (primary_factor,),
            ).fetchone()
            if row:
                sample_count = int(row["sample_count"]) + 1
                win_count = int(row["win_count"]) + (1 if reward > 0 else 0)
                bad_loss_count = int(row["bad_loss_count"]) + (1 if outcome_label == "bad_loss" or supervisor_entry_failure else 0)
                prev_avg = float(row["avg_reward"] or 0.0)
                avg_reward = prev_avg + (reward - prev_avg) / max(sample_count, 1)
                effective_sample_count = float(row["effective_sample_count"] or 0.0)
                weighted_win_count = float(row["weighted_win_count"] or 0.0)
                weighted_bad_loss_count = float(row["weighted_bad_loss_count"] or 0.0)
                weighted_avg_reward = float(row["weighted_avg_reward"] or 0.0)
                previous_fingerprint = (
                    str(row["governance_eligibility_fingerprint"] or "")
                    if str(row["governance_eligibility_version"] or "") == GOVERNANCE_ELIGIBILITY_VERSION
                    else ""
                )
            else:
                sample_count = 1
                win_count = 1 if reward > 0 else 0
                bad_loss_count = 1 if outcome_label == "bad_loss" or supervisor_entry_failure else 0
                avg_reward = reward
                effective_sample_count = 0.0
                weighted_win_count = 0.0
                weighted_bad_loss_count = 0.0
                weighted_avg_reward = 0.0
                previous_fingerprint = ""

            weight = float(eligibility.effective_weight)
            previous_weighted_reward_sum = weighted_avg_reward * effective_sample_count
            if weight > 0.0:
                effective_sample_count += weight
                weighted_win_count += weight if reward > 0 else 0.0
                weighted_bad_loss_count += weight if outcome_label == "bad_loss" or supervisor_entry_failure else 0.0
                weighted_avg_reward = (previous_weighted_reward_sum + weight * reward) / effective_sample_count
                eligibility_fingerprint = self._next_eligibility_fingerprint(previous_fingerprint, eligibility)
            else:
                eligibility_fingerprint = previous_fingerprint

            if effective_sample_count >= 3.0 and weighted_avg_reward <= -0.20:
                action = "downweight"
                confidence = min(0.95, 0.45 + 0.08 * effective_sample_count + 0.10 * weighted_bad_loss_count)
                if supervisor_entry_failure:
                    reason = f"factor {primary_factor} repeatedly led to supervisor thesis-broken exits ({effective_sample_count:g} weighted samples)"
                else:
                    reason = f"factor {primary_factor} shows repeated negative outcomes ({effective_sample_count:g} weighted samples)"
            elif effective_sample_count >= 4.0 and weighted_win_count >= 3.0 and weighted_avg_reward >= 0.22:
                action = "boost_small"
                confidence = min(0.85, 0.40 + 0.05 * effective_sample_count)
                reason = f"factor {primary_factor} shows stable positive outcomes ({effective_sample_count:g} weighted samples)"
            else:
                action = "watch"
                confidence = 0.0
                reason = f"factor {primary_factor} still accumulating eligible evidence"

            conn.execute(
                f"""
                INSERT INTO experience_pattern_stats
                (scope_type, scope_key, sample_count, win_count, bad_loss_count,
                 avg_reward, effective_sample_count, weighted_win_count,
                 weighted_bad_loss_count, weighted_avg_reward,
                 governance_eligibility_version, governance_eligibility_fingerprint,
                 last_outcome_label, recommended_action, updated_at)
                VALUES ('factor', {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p})
                ON CONFLICT(scope_type, scope_key) DO UPDATE SET
                    sample_count=excluded.sample_count,
                    win_count=excluded.win_count,
                    bad_loss_count=excluded.bad_loss_count,
                    avg_reward=excluded.avg_reward,
                    effective_sample_count=excluded.effective_sample_count,
                    weighted_win_count=excluded.weighted_win_count,
                    weighted_bad_loss_count=excluded.weighted_bad_loss_count,
                    weighted_avg_reward=excluded.weighted_avg_reward,
                    governance_eligibility_version=excluded.governance_eligibility_version,
                    governance_eligibility_fingerprint=excluded.governance_eligibility_fingerprint,
                    last_outcome_label=excluded.last_outcome_label,
                    recommended_action=excluded.recommended_action,
                    updated_at=excluded.updated_at
                """,
                (
                    primary_factor,
                    sample_count,
                    win_count,
                    bad_loss_count,
                    round(avg_reward, 6),
                    round(effective_sample_count, 6),
                    round(weighted_win_count, 6),
                    round(weighted_bad_loss_count, 6),
                    round(weighted_avg_reward, 6),
                    GOVERNANCE_ELIGIBILITY_VERSION,
                    eligibility_fingerprint,
                    outcome_label,
                    action,
                    now,
                ),
            )

            if not eligibility.eligible or action == "watch":
                return None

            payload = {
                "source_table": experience.get("source_table", ""),
                "source_id": experience.get("source_id", ""),
                "append_source": experience.get("append_source", ""),
                "sample_count": sample_count,
                "win_count": win_count,
                "bad_loss_count": bad_loss_count,
                "avg_reward": round(avg_reward, 6),
                "effective_sample_count": round(effective_sample_count, 6),
                "weighted_win_count": round(weighted_win_count, 6),
                "weighted_bad_loss_count": round(weighted_bad_loss_count, 6),
                "weighted_avg_reward": round(weighted_avg_reward, 6),
                "governance_eligibility_version": GOVERNANCE_ELIGIBILITY_VERSION,
                "governance_eligibility_fingerprint": eligibility_fingerprint,
                "experience_id": experience.get("experience_id", ""),
                "failure_tags": experience.get("failure_tags", []),
                "supervisor_entry_failure": supervisor_entry_failure,
            }
            payload = attach_policy_suggestion_agent_context(
                payload,
                source_agent="autonomous_learning",
                scope_type="factor",
                action=action,
                requested_writes=["policy_suggestion"],
                status="proposed",
                impact_level="medium",
                db_path=self.db_path,
            )
            suggestion_id = deterministic_policy_suggestion_id(
                writer="policy_suggester",
                scope_type="factor",
                scope_key=primary_factor,
                action=action,
                evidence=payload,
                status="proposed",
                qualification_fingerprint=eligibility_fingerprint,
                prefix="psg_factor",
            )
            conn.execute(
                f"""
                INSERT INTO policy_suggestion
                (suggestion_id, scope_type, scope_key, action, confidence, reason,
                 evidence_json, status, governance_eligible,
                 governance_eligibility_version, governance_eligibility_fingerprint,
                 governance_ineligible_reason, created_at)
                VALUES ({p}, 'factor', {p}, {p}, {p}, {p}, {p}, 'proposed', 1, {p}, {p}, '', {p})
                ON CONFLICT(suggestion_id) DO NOTHING
                """,
                (
                    suggestion_id,
                    primary_factor,
                    action,
                    round(confidence, 6),
                    reason,
                    json.dumps(payload, ensure_ascii=False, default=str),
                    GOVERNANCE_ELIGIBILITY_VERSION,
                    eligibility_fingerprint,
                    now,
                ),
            )

        return {
            "suggestion_id": suggestion_id,
            "scope_type": "factor",
            "scope_key": primary_factor,
            "action": action,
            "confidence": float(confidence),
            "reason": reason,
        }
