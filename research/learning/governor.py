from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, STATE_DB_DDL, connect_sqlite, get_state_pg_conn, is_state_db_path
from backend.services.agent_authority_registry import AgentAuthorityRegistryService
from backend.services.governance_eligibility import GOVERNANCE_ELIGIBILITY_VERSION
from backend.services.policy_suggestion_context import attach_policy_suggestion_agent_context
from research.learning.effect_reconciliation import EffectEvaluation, evaluate_application_effect
from research.learning.governance_conflicts import GovernanceConflictResolver


class RuleEvolutionGovernor:
    """Govern rule-learning suggestions through approval, rollback, and audit."""

    def __init__(self, db_path: str | None = None):
        self.db_path = Path(db_path or str(STATE_DB))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _use_pg(self) -> bool:
        return is_state_db_path(self.db_path)

    def _sql(self, sql: str) -> str:
        return sql.replace("%", "%%").replace("?", "%s") if self._use_pg() else sql

    def _execute(self, conn, sql: str, params: tuple | list | None = None):
        if params is None:
            return conn.execute(self._sql(sql))
        return conn.execute(self._sql(sql), tuple(params))

    def _executemany(self, conn, sql: str, seq_of_params):
        if self._use_pg():
            cur = conn.cursor()
            cur.executemany(self._sql(sql), [tuple(params) for params in seq_of_params])
            return cur
        return conn.executemany(sql, seq_of_params)

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
    def _new_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:16]}"

    @staticmethod
    def _reward_from_review(item: dict) -> float:
        review = item.get("review") or {}
        pnl = float(item.get("pnl", 0.0) or 0.0)
        close_reason = str(review.get("close_reason", "") or "")
        context_integrity = str(review.get("context_integrity", "full") or "full")
        reward = 0.0
        if pnl > 0:
            reward = min(1.0, pnl / max(abs(pnl), 50.0))
        elif pnl < 0:
            reward = -min(1.0, abs(pnl) / max(abs(pnl), 50.0))
        reward_scale = 1.0
        if context_integrity != "full":
            reward_scale *= 0.5
        if close_reason in {"emergency_close", "restart_replay"}:
            reward_scale *= 0.6
        return reward * reward_scale

    @staticmethod
    def _has_supervisor_feedback(item: dict) -> bool:
        review = item.get("review") or {}
        inferred = review.get("inferred_close_supervisor") or {}
        close_source = str(review.get("close_reason_source") or "")
        close_reason = str(review.get("close_reason") or "")
        action = str(inferred.get("action") or "")
        event_type = str(inferred.get("event_type") or "")
        return bool(
            close_source.startswith("supervisor")
            or close_reason.startswith("supervisor")
            or event_type.startswith("supervisor_")
            or action in {"tighten", "reduce", "close"}
        )

    @classmethod
    def _supervisor_reward_from_review(cls, item: dict) -> float:
        reward = cls._reward_from_review(item)
        review = item.get("review") or {}
        pnl = float(item.get("pnl", 0.0) or 0.0)
        try:
            mfe = float(review.get("mfe", item.get("mfe", 0.0)) or 0.0)
        except Exception:
            mfe = 0.0
        try:
            giveback_ratio = float(review.get("giveback_ratio", 0.0) or 0.0)
        except Exception:
            giveback_ratio = 0.0
        try:
            profit_capture_ratio = float(review.get("profit_capture_ratio", 0.0) or 0.0)
        except Exception:
            profit_capture_ratio = 0.0
        if mfe > 0 and pnl < 0 and giveback_ratio >= 0.75 and profit_capture_ratio <= 0.15:
            reward -= 0.25
        elif mfe > 0 and pnl > 0 and profit_capture_ratio >= 0.45:
            reward += 0.15
        elif mfe > 0 and giveback_ratio >= 0.85 and profit_capture_ratio <= 0.20:
            reward -= 0.12
        return max(-1.0, min(1.0, reward))

    @staticmethod
    def _parse_application_row(row: sqlite3.Row) -> dict:
        item = dict(row)
        try:
            item["suggestion_ids"] = json.loads(item.pop("suggestion_ids_json") or "[]")
        except Exception:
            item["suggestion_ids"] = []
        try:
            item["details"] = json.loads(item.pop("details_json") or "{}")
        except Exception:
            item["details"] = {}
        return item

    @staticmethod
    def _parse_review_row(row: sqlite3.Row) -> dict:
        item = dict(row)
        try:
            item["failure_tags"] = json.loads(item.pop("failure_tags_json") or "[]")
        except Exception:
            item["failure_tags"] = []
        try:
            item["review"] = json.loads(item.pop("review_json") or "{}")
        except Exception:
            item["review"] = {}
        return item

    @staticmethod
    def _review_regime(item: dict) -> str:
        review = item.get("review") or {}
        for value in (
            review.get("regime_id"),
            review.get("regime_key"),
            review.get("entry_regime"),
            review.get("regime"),
        ):
            text = str(value or "").strip()
            if text:
                return text
        return ""

    @staticmethod
    def _review_is_contaminated(item: dict) -> bool:
        review = item.get("review") or {}
        tags = {str(tag) for tag in (item.get("failure_tags") or [])}
        close_reason = str(review.get("close_reason") or "")
        context_integrity = str(review.get("context_integrity") or "full")
        attribution_integrity = str(review.get("attribution_integrity") or "full")
        return bool(
            context_integrity != "full"
            or attribution_integrity == "missing"
            or close_reason in {"emergency_close", "restart_replay", "manual_close"}
            or tags & {"manual_intervention", "partial_context", "attribution_missing", "restart_replay"}
        )

    @classmethod
    def _comparable_reviews(
        cls,
        reviews: list[dict],
        *,
        regime: str,
    ) -> tuple[list[dict], int, int]:
        clean = [item for item in reviews if not cls._review_is_contaminated(item)]
        contaminated = len(reviews) - len(clean)
        if not regime:
            return clean, contaminated, 0
        matched = [item for item in clean if cls._review_regime(item) == regime]
        return matched, contaminated, len(clean) - len(matched)

    @classmethod
    def _select_effect_comparison(
        cls,
        post_reviews: list[dict],
        baseline_reviews: list[dict],
        *,
        target_regime: str,
        min_trades: int,
        baseline_min_trades: int,
        observe_trades: int,
    ) -> tuple[list[dict], list[dict], int, int, int, int, str]:
        comparison_regime = target_regime if target_regime else ""
        exact_post, post_contaminated, post_regime_mismatch = cls._comparable_reviews(
            post_reviews,
            regime=comparison_regime,
        )
        exact_baseline, baseline_contaminated, baseline_regime_mismatch = cls._comparable_reviews(
            baseline_reviews,
            regime=comparison_regime,
        )
        if not target_regime:
            return (
                exact_post,
                exact_baseline,
                post_contaminated,
                baseline_contaminated,
                0,
                0,
                "unstratified_no_regime",
            )
        if len(exact_post) >= int(min_trades) and len(exact_baseline) >= int(baseline_min_trades):
            return (
                exact_post,
                exact_baseline,
                post_contaminated,
                baseline_contaminated,
                post_regime_mismatch,
                baseline_regime_mismatch,
                "exact_regime",
            )
        clean_post, _, _ = cls._comparable_reviews(post_reviews, regime="")
        clean_baseline, _, _ = cls._comparable_reviews(baseline_reviews, regime="")
        fallback_min = max(int(observe_trades), int(min_trades), int(baseline_min_trades))
        if len(clean_post) >= fallback_min and len(clean_baseline) >= fallback_min:
            return (
                clean_post,
                clean_baseline,
                post_contaminated,
                baseline_contaminated,
                post_regime_mismatch,
                baseline_regime_mismatch,
                "unstratified_bounded",
            )
        return (
            exact_post,
            exact_baseline,
            post_contaminated,
            baseline_contaminated,
            post_regime_mismatch,
            baseline_regime_mismatch,
            "exact_regime_insufficient",
        )

    @staticmethod
    def _parse_evidence(row: sqlite3.Row) -> dict:
        try:
            value = row["evidence_json"]
        except Exception:
            value = "{}"
        if isinstance(value, dict):
            return value
        try:
            parsed = json.loads(value or "{}")
        except Exception:
            parsed = {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _factor_pruning_evidence_ready(evidence: dict, *, confidence: float) -> bool:
        if str(evidence.get("source_agent") or "") != "factor_pruning_governance":
            return False
        if str(evidence.get("source_kind") or "") != "factor_pruning_candidate_materializer":
            return False
        if confidence < 0.80:
            return False
        expected = evidence.get("expected_effect") or {}
        risk_verdict = evidence.get("risk_verdict") or {}
        decision_policy = evidence.get("decision_policy_preview") or {}
        decision = decision_policy.get("decision") or {}
        reasons = expected.get("reasons") or []
        reason_codes = {str(item.get("code") or "") for item in reasons if isinstance(item, dict)}
        current_weight = float(expected.get("current_weight") or decision.get("old_weight") or 0.0)
        target_weight = float(expected.get("suggested_target_weight") or decision.get("new_weight") or 0.0)
        legacy_pruning_evidence = {"low_weight_tail", "large_noise_family", "weak_factor_health"} <= reason_codes
        live_harm_evidence = (
            {"recent_live_decision_participation", "recent_loss_contribution_pressure"} <= reason_codes
            and bool(reason_codes & {"loss_win_contribution_sign_flip", "recent_loss_rate_pressure", "weak_factor_health"})
        )
        return bool(
            risk_verdict.get("allowed")
            and decision_policy.get("required")
            and decision
            and target_weight <= current_weight
            and (legacy_pruning_evidence or live_harm_evidence)
        )

    @staticmethod
    def _factor_model_evidence_ready(evidence: dict, *, confidence: float) -> bool:
        """Accept strong demo model evidence without pretending it is experience.

        A shadow model may contribute a governed factor action in demo nursery,
        but only through an explicit bridge envelope.  This keeps model
        authority advisory-only while preventing the governor from treating a
        valid model proposal as an unknown legacy action.
        """
        if str(evidence.get("source_agent") or "") != "lightgbm_shadow_models":
            return False
        if str(evidence.get("model_type") or "") != "factor_governance_lightgbm":
            return False
        if evidence.get("advisory_only") is not True:
            return False
        bridge = evidence.get("bridge") or {}
        if not (bridge.get("automatic_demo") is True and bridge.get("demo_nursery") is True):
            return False
        if not str(bridge.get("actor") or "").startswith("system:autonomous_learning.demo_nursery"):
            return False
        if str(evidence.get("governed_action") or "") != "downweight":
            return False
        active_context = evidence.get("active_factor_context") or {}
        if active_context.get("used_in_score") is not True or str(active_context.get("role") or "") != "alpha":
            return False
        if float(confidence) < 0.55:
            return False
        sample_count = int(evidence.get("sample_count") or 0)
        weak_sample_count = int(evidence.get("weak_sample_count") or 0)
        min_weakness = float(evidence.get("min_weakness_score") or 0.0)
        avg_weakness = float(evidence.get("avg_weakness_score") or 0.0)
        return (
            sample_count >= 2
            and weak_sample_count >= 2
            and min_weakness >= 0.85
            and avg_weakness >= 0.85
        )

    def list_suggestions(self, *, status: str | None = None, limit: int = 100) -> list[dict]:
        sql = """
            SELECT suggestion_id, scope_type, scope_key, action, confidence, reason,
                   evidence_json, status, reviewed_at, review_note,
                   governance_eligible, governance_eligibility_version,
                   governance_eligibility_fingerprint, governance_ineligible_reason,
                   created_at
            FROM policy_suggestion
        """
        params: list = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        with self._conn() as conn:
            rows = self._execute(conn, sql, tuple(params)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["evidence"] = json.loads(item.pop("evidence_json") or "{}")
            except Exception:
                item["evidence"] = {}
            result.append(item)
        return result

    def review_pending(self) -> dict[str, int]:
        """Auto-review proposed suggestions using accumulated pattern stats."""
        approved = 0
        rejected = 0
        unchanged = 0
        with self._conn() as conn:
            rows = self._execute(conn,
                """
                SELECT * FROM policy_suggestion
                WHERE status='proposed'
                ORDER BY created_at ASC
                """
            ).fetchall()
            now = time.time()
            for row in rows:
                scope_type = str(row["scope_type"] or "")
                scope_key = str(row["scope_key"] or "")
                action = str(row["action"] or "watch")
                confidence = float(row["confidence"] or 0.0)
                evidence = self._parse_evidence(row)
                stats = self._execute(conn,
                    """
                    SELECT * FROM experience_pattern_stats
                    WHERE scope_type=? AND scope_key=?
                    """,
                    (scope_type, scope_key),
                ).fetchone()
                if not stats:
                    suggestion_eligibility_ready = bool(
                        row["governance_eligible"]
                        and str(row["governance_eligibility_version"] or "")
                        == GOVERNANCE_ELIGIBILITY_VERSION
                        and str(row["governance_eligibility_fingerprint"] or "")
                    )
                    if not suggestion_eligibility_ready:
                        note = (
                            "rejected by governor: executable evidence eligibility "
                            "version/fingerprint missing"
                        )
                        self._execute(
                            conn,
                            """
                            UPDATE policy_suggestion
                            SET status='rejected', reviewed_at=?, review_note=?,
                                governance_eligible=0,
                                governance_ineligible_reason=?
                            WHERE suggestion_id=?
                            """,
                            (
                                now,
                                note,
                                "eligibility_contract_invalid",
                                row["suggestion_id"],
                            ),
                        )
                        rejected += 1
                        continue
                    status = "proposed"
                    note = ""
                    if action == "fix_stop_legality":
                        status = "rejected"
                        note = "rejected by governor: no autonomous executor for broker stop legality advisory"
                    elif scope_type == "position_supervisor_template":
                        has_replay = bool(evidence.get("replay_summary") or evidence.get("replay") or evidence.get("day"))
                        has_counterfactual = bool(evidence.get("counterfactual_summary") or evidence.get("counterfactual"))
                        if has_replay and has_counterfactual and confidence >= 0.60:
                            status = "approved"
                            note = "approved by governor: replay and counterfactual evidence present"
                    elif scope_type == "parameter_template" and action == "switch_parameter_template":
                        boundary = evidence.get("boundary") or {}
                        recommended_scope = str(
                            boundary.get("recommended_scope") or evidence.get("recommended_scope") or ""
                        )
                        has_target = bool(evidence.get("target_template_id"))
                        has_factor = bool(evidence.get("factor_id") or scope_key.split(":", 1)[0])
                        if has_target and has_factor and recommended_scope == "online_light" and confidence >= 0.55:
                            status = "approved"
                            note = "approved by governor: online_light parameter template switch evidence present"
                    elif scope_type == "factor" and action == "downweight":
                        if (
                            self._factor_pruning_evidence_ready(evidence, confidence=confidence)
                            or self._factor_model_evidence_ready(evidence, confidence=confidence)
                        ):
                            status = "approved"
                            note = (
                                "approved by governor: factor model evidence bridged through demo nursery"
                                if self._factor_model_evidence_ready(evidence, confidence=confidence)
                                else "approved by governor: factor pruning governance evidence present"
                            )
                    if status == "proposed":
                        status = "rejected"
                        note = "rejected by governor: no autonomous evidence rule available"

                    if status == "approved":
                        approved += 1
                    else:
                        rejected += 1
                    self._execute(conn,
                        """
                        UPDATE policy_suggestion
                        SET status=?, reviewed_at=?, review_note=?
                        WHERE suggestion_id=?
                        """,
                        (status, now, note, row["suggestion_id"]),
                    )
                    continue

                stats_version = str(stats["governance_eligibility_version"] or "")
                stats_fingerprint = str(stats["governance_eligibility_fingerprint"] or "")
                suggestion_version = str(row["governance_eligibility_version"] or "")
                suggestion_fingerprint = str(row["governance_eligibility_fingerprint"] or "")
                eligibility_ready = bool(
                    row["governance_eligible"]
                    and stats_version == GOVERNANCE_ELIGIBILITY_VERSION
                    and suggestion_version == GOVERNANCE_ELIGIBILITY_VERSION
                    and stats_fingerprint
                    and suggestion_fingerprint == stats_fingerprint
                )
                if not eligibility_ready:
                    note = (
                        "rejected by governor: executable evidence eligibility "
                        "version/fingerprint missing or mismatched"
                    )
                    self._execute(
                        conn,
                        """
                        UPDATE policy_suggestion
                        SET status='rejected', reviewed_at=?, review_note=?,
                            governance_eligible=0, governance_ineligible_reason=?
                        WHERE suggestion_id=?
                        """,
                        (now, note, "eligibility_contract_invalid", row["suggestion_id"]),
                    )
                    rejected += 1
                    continue

                sample_count = float(stats["effective_sample_count"] or 0.0)
                win_count = float(stats["weighted_win_count"] or 0.0)
                bad_loss_count = float(stats["weighted_bad_loss_count"] or 0.0)
                avg_reward = float(stats["weighted_avg_reward"] or 0.0)
                bad_rate = bad_loss_count / max(sample_count, 1e-12)
                note = ""
                status = "proposed"

                if action == "downweight":
                    if self._factor_model_evidence_ready(evidence, confidence=confidence):
                        status = "approved"
                        note = "approved by governor: factor model evidence bridged through demo nursery"
                    elif sample_count >= 3.0 and bad_loss_count >= 2.0 and avg_reward <= -0.20 and confidence >= 0.45:
                        status = "approved"
                        note = f"approved by governor: samples={sample_count}, avg_reward={avg_reward:.3f}"
                    elif sample_count >= 4.0 and avg_reward >= -0.05:
                        status = "rejected"
                        note = f"rejected by governor: negative evidence too weak avg_reward={avg_reward:.3f}"
                elif action == "boost_small":
                    if sample_count >= 4.0 and win_count >= 3.0 and avg_reward >= 0.20 and confidence >= 0.40:
                        status = "approved"
                        note = f"approved by governor: win_count={win_count}, avg_reward={avg_reward:.3f}"
                    elif sample_count >= 4.0 and avg_reward <= 0.05:
                        status = "rejected"
                        note = f"rejected by governor: positive evidence too weak avg_reward={avg_reward:.3f}"
                elif scope_type == "entry_cluster" and action in {"increase_same_direction_cooldown", "raise_pyramid_entry_threshold"}:
                    if sample_count >= 10.0 and bad_rate >= 0.50 and confidence >= 0.55:
                        status = "approved"
                        note = f"approved by governor: samples={sample_count}, bad_rate={bad_rate:.3f}"
                    elif sample_count >= 10.0 and bad_rate < 0.45 and avg_reward >= 0.02:
                        status = "rejected"
                        note = f"rejected by governor: cluster evidence recovered bad_rate={bad_rate:.3f}, avg_reward={avg_reward:.3f}"
                elif scope_type == "event_window" and action in {"tighten_event_window_sizing", "extend_event_post_window_review"}:
                    if sample_count >= 10.0 and (bad_rate >= 0.50 or avg_reward <= -0.05) and confidence >= 0.55:
                        status = "approved"
                        note = f"approved by governor: samples={sample_count}, bad_rate={bad_rate:.3f}, avg_reward={avg_reward:.3f}"
                    elif sample_count >= 10.0 and bad_rate < 0.45 and avg_reward >= 0.02:
                        status = "rejected"
                        note = f"rejected by governor: event-window evidence recovered bad_rate={bad_rate:.3f}, avg_reward={avg_reward:.3f}"
                elif scope_type == "entry_quality" and action in {
                    "raise_weak_signal_threshold",
                    "require_factor_agreement",
                    "suppress_recent_worst_factor",
                }:
                    if sample_count >= 5.0 and (bad_rate >= 0.60 or avg_reward <= -0.05) and confidence >= 0.55:
                        status = "approved"
                        note = f"approved by governor: samples={sample_count}, bad_rate={bad_rate:.3f}, avg_reward={avg_reward:.3f}"
                    elif sample_count >= 10.0 and bad_rate < 0.45 and avg_reward >= 0.02:
                        status = "rejected"
                        note = f"rejected by governor: entry-quality evidence recovered bad_rate={bad_rate:.3f}, avg_reward={avg_reward:.3f}"
                elif scope_type == "parameter_template" and action == "switch_parameter_template":
                    boundary = evidence.get("boundary") or {}
                    recommended_scope = str(boundary.get("recommended_scope") or evidence.get("recommended_scope") or "")
                    has_target = bool(evidence.get("target_template_id"))
                    has_factor = bool(evidence.get("factor_id") or scope_key.split(":", 1)[0])
                    if has_target and has_factor and recommended_scope == "online_light" and confidence >= 0.55:
                        status = "approved"
                        note = "approved by governor: online_light parameter template switch evidence present"
                elif action == "watch":
                    status = "rejected"
                    note = f"observation-only factor kept in stats, not promoted to executable suggestion (samples={sample_count})"

                if status == "proposed":
                    status = "rejected"
                    note = f"rejected by governor: autonomous evidence thresholds not met for {scope_type}/{action}"

                if status == "proposed" and now - float(row["created_at"] or 0.0) > 14 * 86400:
                    status = "rejected"
                    note = "stale suggestion auto-rejected after 14 days"

                if status == "approved":
                    approved += 1
                elif status == "rejected":
                    rejected += 1
                else:
                    unchanged += 1
                    continue

                self._execute(conn,
                    """
                    UPDATE policy_suggestion
                    SET status=?, reviewed_at=?, review_note=?
                    WHERE suggestion_id=?
                    """,
                    (status, now, note, row["suggestion_id"]),
                )
        conflict_result = self.resolve_conflicts()
        return {
            "approved": approved,
            "rejected": rejected,
            "unchanged": unchanged,
            "superseded": int(conflict_result.get("superseded", 0) or 0),
        }

    def resolve_conflicts(self) -> dict[str, Any]:
        """Mark stale/conflicting active policy suggestions as superseded."""
        resolver = GovernanceConflictResolver()
        with self._conn() as conn:
            rows = self._execute(
                conn,
                """
                SELECT suggestion_id, scope_type, scope_key, action, confidence,
                       evidence_json, status, reviewed_at, created_at
                FROM policy_suggestion
                WHERE status IN ('proposed', 'approved', 'applied')
                ORDER BY created_at ASC
                """,
            ).fetchall()
            result = resolver.resolve([dict(row) for row in rows])
            now = time.time()
            for item in result.get("superseded", []):
                self._execute(
                    conn,
                    """
                    UPDATE policy_suggestion
                    SET status='superseded', reviewed_at=?, review_note=?
                    WHERE suggestion_id=? AND status IN ('proposed', 'approved', 'applied')
                    """,
                    (
                        now,
                        str(item.get("reason") or "superseded by governance conflict resolver"),
                        str(item.get("suggestion_id") or ""),
                    ),
                )
        return {
            "winners": len(result.get("winners", [])),
            "superseded": len(result.get("superseded", [])),
            "items": result.get("superseded", []),
        }

    def reconcile_active(self) -> dict[str, int]:
        """Rollback approved suggestions if later evidence flips against them."""
        rolled_back = 0
        kept = 0
        with self._conn() as conn:
            rows = self._execute(conn,
                """
                SELECT * FROM policy_suggestion
                WHERE status='approved'
                ORDER BY created_at ASC
                """
            ).fetchall()
            now = time.time()
            for row in rows:
                stats = self._execute(conn,
                    """
                    SELECT * FROM experience_pattern_stats
                    WHERE scope_type=? AND scope_key=?
                    """,
                    (row["scope_type"], row["scope_key"]),
                ).fetchone()
                if not stats:
                    kept += 1
                    continue
                stats_fingerprint = str(stats["governance_eligibility_fingerprint"] or "")
                eligibility_ready = bool(
                    row["governance_eligible"]
                    and str(row["governance_eligibility_version"] or "")
                    == GOVERNANCE_ELIGIBILITY_VERSION
                    and str(stats["governance_eligibility_version"] or "")
                    == GOVERNANCE_ELIGIBILITY_VERSION
                    and stats_fingerprint
                    and str(row["governance_eligibility_fingerprint"] or "")
                    == stats_fingerprint
                )
                if not eligibility_ready:
                    kept += 1
                    continue
                sample_count = float(stats["effective_sample_count"] or 0.0)
                bad_loss_count = float(stats["weighted_bad_loss_count"] or 0.0)
                avg_reward = float(stats["weighted_avg_reward"] or 0.0)
                bad_rate = bad_loss_count / max(sample_count, 1e-12)
                scope_type = str(row["scope_type"] or "")
                action = str(row["action"] or "watch")
                should_rollback = False
                note = ""
                if action == "downweight" and sample_count >= 5.0 and avg_reward >= 0.12:
                    should_rollback = True
                    note = f"rolled back: factor recovered avg_reward={avg_reward:.3f}"
                elif action == "boost_small" and sample_count >= 5.0 and avg_reward <= -0.08:
                    should_rollback = True
                    note = f"rolled back: factor deteriorated avg_reward={avg_reward:.3f}"
                elif scope_type == "entry_cluster" and action in {"increase_same_direction_cooldown", "raise_pyramid_entry_threshold"}:
                    if sample_count >= 20.0 and bad_rate < 0.40 and avg_reward >= 0.03:
                        should_rollback = True
                        note = f"rolled back: entry cluster recovered bad_rate={bad_rate:.3f}, avg_reward={avg_reward:.3f}"
                elif scope_type == "event_window" and action in {"tighten_event_window_sizing", "extend_event_post_window_review"}:
                    if sample_count >= 20.0 and bad_rate < 0.40 and avg_reward >= 0.03:
                        should_rollback = True
                        note = f"rolled back: event window recovered bad_rate={bad_rate:.3f}, avg_reward={avg_reward:.3f}"
                elif scope_type == "entry_quality" and action in {
                    "raise_weak_signal_threshold",
                    "require_factor_agreement",
                    "suppress_recent_worst_factor",
                }:
                    if sample_count >= 20.0 and bad_rate < 0.40 and avg_reward >= 0.03:
                        should_rollback = True
                        note = f"rolled back: entry quality recovered bad_rate={bad_rate:.3f}, avg_reward={avg_reward:.3f}"

                if should_rollback:
                    self._execute(conn,
                        """
                        UPDATE policy_suggestion
                        SET status='rolled_back', reviewed_at=?, review_note=?
                        WHERE suggestion_id=?
                        """,
                        (now, note, row["suggestion_id"]),
                    )
                    rolled_back += 1
                else:
                    kept += 1
        return {"rolled_back": rolled_back, "kept": kept}

    def set_status(self, suggestion_id: str, status: str, note: str = "") -> bool:
        if status not in {"approved", "rejected", "rolled_back", "proposed", "superseded"}:
            raise ValueError(f"unsupported status: {status}")
        with self._conn() as conn:
            cur = self._execute(conn,
                """
                UPDATE policy_suggestion
                SET status=?, reviewed_at=?, review_note=?
                WHERE suggestion_id=?
                """,
                (status, time.time(), note, suggestion_id),
            )
            return cur.rowcount > 0

    def log_application(
        self,
        *,
        scope_type: str,
        scope_key: str,
        action: str,
        bias_multiplier: float,
        old_weight: float,
        new_weight: float,
        suggestion_ids: list[str],
        cycle_ts: float,
        status: str = "applied",
        details: dict | None = None,
        ) -> str:
        suggestion_ids = [str(item) for item in (suggestion_ids or []) if str(item)]
        suggestion_ids_json = json.dumps(sorted(set(suggestion_ids)), ensure_ascii=False)
        details_payload = dict(details or {})
        source_agent = str(details_payload.get("source_agent") or "autonomous_learning")
        details_payload.setdefault("source_agent", source_agent)
        details_payload.setdefault(
            "authority_verdict",
            AgentAuthorityRegistryService().evaluate_scope_write(
                source_agent,
                scope_type,
                action,
                requested_writes=["learning_application_log"],
                status=status,
                impact_level="medium",
            ),
        )
        details_json = json.dumps(details_payload, ensure_ascii=False, default=str)
        with self._conn() as conn:
            existing = self._execute(conn,
                """
                SELECT application_id, suggestion_ids_json, status
                FROM learning_application_log
                WHERE scope_type=? AND scope_key=? AND action=?
                  AND status IN ('prepared', 'applied', 'observing', 'effective')
                ORDER BY cycle_ts DESC, created_at DESC
                LIMIT 1
                """,
                (scope_type, scope_key, action),
            ).fetchone()
            if existing:
                try:
                    existing_ids = json.dumps(
                        sorted(set(str(item) for item in json.loads(existing["suggestion_ids_json"] or "[]"))),
                        ensure_ascii=False,
                    )
                except Exception:
                    existing_ids = "[]"
                existing_status = str(existing["status"] or "")
                if existing_ids == suggestion_ids_json and existing_status in {"prepared", "applied", "observing", "effective"}:
                    self._execute(conn,
                        """
                        UPDATE learning_application_log
                        SET status='superseded'
                        WHERE scope_type=? AND scope_key=? AND action=?
                          AND application_id<>?
                          AND status IN ('prepared', 'applied', 'observing', 'effective')
                          AND suggestion_ids_json=?
                        """,
                        (
                            scope_type,
                            scope_key,
                            action,
                            str(existing["application_id"]),
                            suggestion_ids_json,
                        ),
                    )
                    self._execute(conn,
                        """
                        UPDATE learning_application_effect
                        SET status='superseded', updated_at=?
                        WHERE application_id IN (
                            SELECT application_id
                            FROM learning_application_log
                            WHERE scope_type=? AND scope_key=? AND action=?
                              AND application_id<>?
                              AND status='superseded'
                              AND suggestion_ids_json=?
                        )
                        """,
                        (
                            time.time(),
                            scope_type,
                            scope_key,
                            action,
                            str(existing["application_id"]),
                            suggestion_ids_json,
                        ),
                    )
                    self._execute(conn,
                        """
                        UPDATE learning_application_log
                        SET cycle_ts=?, bias_multiplier=?, old_weight=?, new_weight=?, details_json=?
                        WHERE application_id=?
                        """,
                        (
                            float(cycle_ts),
                            float(bias_multiplier),
                            float(old_weight),
                            float(new_weight),
                            details_json,
                            str(existing["application_id"]),
                        ),
                    )
                    self._execute(conn,
                        """
                        UPDATE learning_application_effect
                        SET decision_json=?, updated_at=?
                        WHERE application_id=?
                        """,
                        (
                            json.dumps(
                                {
                                    "suggestion_ids": suggestion_ids,
                                    "bias_multiplier": bias_multiplier,
                                    "old_weight": old_weight,
                                    "new_weight": new_weight,
                                    "details": details_payload,
                                },
                                ensure_ascii=False,
                                default=str,
                            ),
                            time.time(),
                            str(existing["application_id"]),
                        ),
                    )
                    return str(existing["application_id"])

            application_id = self._new_id("lapp")
            effect_status = "prepared" if status == "prepared" else "observing"
            self._execute(conn,
                """
                INSERT INTO learning_application_log
                (application_id, cycle_ts, scope_type, scope_key, action, bias_multiplier,
                 old_weight, new_weight, suggestion_ids_json, status, details_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    application_id,
                    float(cycle_ts),
                    scope_type,
                    scope_key,
                    action,
                    float(bias_multiplier),
                    float(old_weight),
                    float(new_weight),
                    suggestion_ids_json,
                    status,
                    details_json,
                    time.time(),
                ),
            )
            self._execute(conn,
                """
                INSERT INTO learning_application_effect
                (application_id, scope_type, scope_key, action, status, decision_json,
                 updated_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(application_id) DO UPDATE SET
                    scope_type=excluded.scope_type,
                    scope_key=excluded.scope_key,
                    action=excluded.action,
                    status=excluded.status,
                    decision_json=excluded.decision_json,
                    updated_at=excluded.updated_at
                """,
                (
                    application_id,
                    scope_type,
                    scope_key,
                    action,
                    effect_status,
                    json.dumps(
                        {
                            "suggestion_ids": suggestion_ids,
                            "bias_multiplier": bias_multiplier,
                            "old_weight": old_weight,
                            "new_weight": new_weight,
                            "details": details_payload,
                        },
                        ensure_ascii=False,
                        default=str,
                    ),
                    time.time(),
                    time.time(),
                ),
            )
        return application_id

    def _persist_effect_evaluation(
        self,
        conn: Any,
        *,
        app: dict[str, Any],
        scope_type: str,
        scope_key: str,
        evaluation: EffectEvaluation,
        now: float,
    ) -> None:
        self._execute(conn,
            """
            INSERT INTO learning_application_effect
            (application_id, scope_type, scope_key, action, status,
             observed_trade_count, baseline_trade_count,
             post_avg_reward, baseline_avg_reward, delta_avg_reward,
             post_win_rate, baseline_win_rate, decision_json,
             last_review_at, updated_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(application_id) DO UPDATE SET
                scope_type=excluded.scope_type,
                scope_key=excluded.scope_key,
                action=excluded.action,
                status=excluded.status,
                observed_trade_count=excluded.observed_trade_count,
                baseline_trade_count=excluded.baseline_trade_count,
                post_avg_reward=excluded.post_avg_reward,
                baseline_avg_reward=excluded.baseline_avg_reward,
                delta_avg_reward=excluded.delta_avg_reward,
                post_win_rate=excluded.post_win_rate,
                baseline_win_rate=excluded.baseline_win_rate,
                decision_json=excluded.decision_json,
                last_review_at=excluded.last_review_at,
                updated_at=excluded.updated_at
            """,
            (
                app["application_id"],
                scope_type,
                scope_key,
                app["action"],
                evaluation.status,
                evaluation.post_count,
                evaluation.baseline_count,
                round(evaluation.post_avg, 6),
                round(evaluation.baseline_avg, 6),
                round(evaluation.delta, 6),
                round(evaluation.post_win_rate, 4),
                round(evaluation.baseline_win_rate, 4),
                json.dumps(evaluation.decision, ensure_ascii=False, default=str),
                evaluation.last_review_at,
                now,
                now,
            ),
        )
        self._execute(conn,
            """
            UPDATE learning_application_log
            SET status=?, details_json=?
            WHERE application_id=?
            """,
            (
                evaluation.status,
                json.dumps({**(app.get("details") or {}), "effect": evaluation.decision}, ensure_ascii=False, default=str),
                app["application_id"],
            ),
        )

    def reconcile_application_effects(
        self,
        *,
        min_trades: int = 3,
        observe_trades: int = 5,
        baseline_min_trades: int = 2,
        reward_delta_for_effective: float = 0.08,
        reward_delta_for_bad: float = -0.08,
        application_limit: int = 200,
        mixed_recheck_after_seconds: float = 6 * 3600.0,
        max_observation_age_seconds: float | None = None,
        terminalize_mixed_after_recheck: bool = False,
    ) -> dict[str, int]:
        observed = 0
        rolled_back = 0
        reinforced = 0
        waiting = 0
        rechecked_mixed = 0
        inconclusive = 0
        rollback_pending = 0
        pending_parameter_rollbacks: list[dict[str, Any]] = []

        with self._conn() as conn:
            now = time.time()
            if max_observation_age_seconds is None:
                try:
                    from config.runtime_config import shared as _runtime_config_shared

                    effect_days = max(
                        1,
                        int(getattr(_runtime_config_shared(), "learning_effect_inconclusive_after_days", 7) or 7),
                    )
                except Exception:
                    effect_days = 7
                max_observation_age_seconds = float(effect_days * 86400)
            rows = self._execute(conn,
                """
                SELECT l.*
                FROM learning_application_log l
                LEFT JOIN learning_application_effect e
                  ON e.application_id=l.application_id
                WHERE l.status IN ('applied', 'observing', 'effective', 'mixed')
                  AND (
                      l.status<>'mixed'
                      OR COALESCE(e.updated_at, 0)<=?
                  )
                ORDER BY
                    COALESCE(e.updated_at, 0) ASC,
                    CASE l.status
                        WHEN 'applied' THEN 0
                        WHEN 'observing' THEN 1
                        WHEN 'mixed' THEN 2
                        ELSE 3
                    END,
                    l.cycle_ts ASC
                LIMIT ?
                """,
                (
                    now - max(300.0, float(mixed_recheck_after_seconds or 0.0)),
                    max(1, min(int(application_limit or 200), 2000)),
                ),
            ).fetchall()

            for row in rows:
                app = self._parse_application_row(row)
                prior_status = str(app.get("status") or "")
                if prior_status == "mixed":
                    rechecked_mixed += 1
                scope_type = str(app.get("scope_type") or "")
                if scope_type not in {"factor", "parameter_template", "position_supervisor_template"}:
                    continue
                scope_key_for_effect = str(app.get("scope_key") or "")
                review_limit = max(int(observe_trades) * 5, int(observe_trades))
                next_application_row = self._execute(
                    conn,
                    """
                    SELECT application_id, action, cycle_ts
                    FROM learning_application_log
                    WHERE application_id<>?
                      AND scope_type=? AND scope_key=?
                      AND cycle_ts>?
                      AND status NOT IN ('superseded', 'rolled_back', 'rejected')
                    ORDER BY cycle_ts ASC
                    LIMIT 1
                    """,
                    (
                        app["application_id"],
                        scope_type,
                        str(app.get("scope_key") or ""),
                        float(app.get("cycle_ts") or 0.0),
                    ),
                ).fetchone()
                next_application = (
                    {
                        "application_id": str(next_application_row["application_id"] or ""),
                        "action": str(next_application_row["action"] or ""),
                        "cycle_ts": float(next_application_row["cycle_ts"] or 0.0),
                    }
                    if next_application_row
                    else None
                )
                observation_upper_bound = (
                    float(next_application["cycle_ts"])
                    if next_application
                    else 1.0e18
                )
                if scope_type == "position_supervisor_template":
                    if not scope_key_for_effect:
                        continue
                    post_rows = self._execute(conn,
                        """
                        SELECT r.review_id, r.trade_id, r.position_id, r.entry_decision_id, r.pnl,
                               r.outcome_label, r.failure_tags_json, r.summary_text, r.review_json, r.created_at
                        FROM trade_outcome_review r
                        WHERE r.created_at > ?
                          AND r.created_at < ?
                          AND (
                              r.review_json LIKE '%inferred_close_supervisor%'
                              OR r.review_json LIKE '%supervisor_%'
                          )
                        ORDER BY r.created_at DESC
                        LIMIT ?
                        """,
                        (float(app.get("cycle_ts") or 0.0), observation_upper_bound, review_limit),
                    ).fetchall()
                    post_reviews = [
                        r for r in (self._parse_review_row(row) for row in post_rows)
                        if self._has_supervisor_feedback(r)
                    ][: int(observe_trades)]

                    pre_rows = self._execute(conn,
                        """
                        SELECT r.review_id, r.trade_id, r.position_id, r.entry_decision_id, r.pnl,
                               r.outcome_label, r.failure_tags_json, r.summary_text, r.review_json, r.created_at
                        FROM trade_outcome_review r
                        WHERE r.created_at <= ?
                          AND (
                              r.review_json LIKE '%inferred_close_supervisor%'
                              OR r.review_json LIKE '%supervisor_%'
                          )
                        ORDER BY r.created_at DESC
                        LIMIT ?
                        """,
                        (float(app.get("cycle_ts") or 0.0), review_limit),
                    ).fetchall()
                    pre_reviews = [
                        r for r in (self._parse_review_row(row) for row in pre_rows)
                        if self._has_supervisor_feedback(r)
                    ][: int(observe_trades)]
                    reward_from_review = self._supervisor_reward_from_review
                else:
                    factor = (
                        str(app.get("scope_key") or "")
                        if scope_type == "factor"
                        else str((app.get("details") or {}).get("factor_id") or scope_key_for_effect.split(":", 1)[0])
                    )
                    if not factor:
                        continue
                    scope_key_for_effect = scope_key_for_effect or factor

                    post_rows = self._execute(conn,
                        """
                        SELECT r.review_id, r.trade_id, r.position_id, r.entry_decision_id, r.pnl,
                               r.outcome_label, r.failure_tags_json, r.summary_text, r.review_json, r.created_at
                        FROM trade_outcome_review r
                        WHERE r.created_at > ?
                          AND r.created_at < ?
                          AND EXISTS (
                              SELECT 1
                              FROM decision_factor_snapshot dfs
                              WHERE dfs.decision_id = r.entry_decision_id
                                AND dfs.factor = ?
                          )
                        ORDER BY r.created_at DESC
                        LIMIT ?
                        """,
                        (float(app.get("cycle_ts") or 0.0), observation_upper_bound, factor, review_limit),
                    ).fetchall()
                    post_reviews = [self._parse_review_row(r) for r in post_rows]

                    pre_rows = self._execute(conn,
                        """
                        SELECT r.review_id, r.trade_id, r.position_id, r.entry_decision_id, r.pnl,
                               r.outcome_label, r.failure_tags_json, r.summary_text, r.review_json, r.created_at
                        FROM trade_outcome_review r
                        WHERE r.created_at <= ?
                          AND EXISTS (
                              SELECT 1
                              FROM decision_factor_snapshot dfs
                              WHERE dfs.decision_id = r.entry_decision_id
                                AND dfs.factor = ?
                          )
                        ORDER BY r.created_at DESC
                        LIMIT ?
                        """,
                        (float(app.get("cycle_ts") or 0.0), factor, review_limit),
                    ).fetchall()
                    pre_reviews = [self._parse_review_row(r) for r in pre_rows]
                    reward_from_review = self._reward_from_review

                raw_post_reviews = list(post_reviews)
                raw_pre_reviews = list(pre_reviews)
                details = app.get("details") or {}
                target_regime = str(
                    details.get("regime_id")
                    or details.get("regime_key")
                    or details.get("entry_regime")
                    or ""
                ).strip()
                if not target_regime:
                    post_regimes = [
                        self._review_regime(item)
                        for item in raw_post_reviews
                        if self._review_regime(item)
                    ]
                    if post_regimes:
                        target_regime = max(set(post_regimes), key=post_regimes.count)
                regime_evidence_available = any(
                    self._review_regime(item)
                    for item in raw_post_reviews + raw_pre_reviews
                )
                comparison_target = target_regime if regime_evidence_available else ""
                (
                    post_reviews,
                    pre_reviews,
                    post_contaminated,
                    pre_contaminated,
                    post_regime_mismatch,
                    pre_regime_mismatch,
                    comparison_basis,
                ) = self._select_effect_comparison(
                    raw_post_reviews,
                    raw_pre_reviews,
                    target_regime=comparison_target,
                    min_trades=min_trades,
                    baseline_min_trades=baseline_min_trades,
                    observe_trades=observe_trades,
                )
                evaluation = evaluate_application_effect(
                    app=app,
                    scope_type=scope_type,
                    scope_key=scope_key_for_effect,
                    post_reviews=post_reviews,
                    baseline_reviews=pre_reviews,
                    raw_post_count=len(raw_post_reviews),
                    raw_baseline_count=len(raw_pre_reviews),
                    excluded_contaminated_post=post_contaminated,
                    excluded_contaminated_baseline=pre_contaminated,
                    excluded_regime_mismatch_post=post_regime_mismatch,
                    excluded_regime_mismatch_baseline=pre_regime_mismatch,
                    target_regime=target_regime,
                    regime_evidence_available=regime_evidence_available,
                    comparison_basis=comparison_basis,
                    next_application=next_application,
                    observation_upper_bound=observation_upper_bound,
                    reward_from_review=reward_from_review,
                    min_trades=min_trades,
                    observe_trades=observe_trades,
                    baseline_min_trades=baseline_min_trades,
                    reward_delta_for_effective=reward_delta_for_effective,
                    reward_delta_for_bad=reward_delta_for_bad,
                    max_observation_age_seconds=max_observation_age_seconds,
                    now=now,
                )
                if (
                    terminalize_mixed_after_recheck
                    and evaluation.status == "mixed"
                    and evaluation.post_count >= int(min_trades)
                    and evaluation.baseline_count >= int(baseline_min_trades)
                ):
                    decision = dict(evaluation.decision)
                    evidence_quality = dict(decision.get("evidence_quality") or {})
                    evidence_quality["causal_status"] = "demo_mixed_terminal_inconclusive"
                    evidence_quality["retry_via_new_application"] = True
                    decision["evidence_quality"] = evidence_quality
                    evaluation = EffectEvaluation(
                        decision=decision,
                        status="inconclusive",
                        post_count=evaluation.post_count,
                        baseline_count=evaluation.baseline_count,
                        post_avg=evaluation.post_avg,
                        baseline_avg=evaluation.baseline_avg,
                        delta=evaluation.delta,
                        post_win_rate=evaluation.post_win_rate,
                        baseline_win_rate=evaluation.baseline_win_rate,
                        last_review_at=evaluation.last_review_at,
                    )
                decision = evaluation.decision
                next_status = evaluation.status
                post_reviews = post_reviews[: evaluation.post_count]
                pre_reviews = pre_reviews[: evaluation.baseline_count]
                post_avg = evaluation.post_avg
                pre_avg = evaluation.baseline_avg
                delta = evaluation.delta
                post_win_rate = evaluation.post_win_rate
                pre_win_rate = evaluation.baseline_win_rate

                self._persist_effect_evaluation(
                    conn,
                    app=app,
                    scope_type=scope_type,
                    scope_key=scope_key_for_effect,
                    evaluation=evaluation,
                    now=now,
                )
                observed += 1

                if next_status == "observing":
                    waiting += 1
                    continue
                if next_status == "inconclusive":
                    inconclusive += 1
                    continue

                suggestion_ids = list(app.get("suggestion_ids") or [])
                if next_status == "ineffective" and suggestion_ids:
                    if scope_type == "parameter_template":
                        details = app.get("details") or {}
                        factor_id = str(details.get("factor_id") or factor)
                        regime_key = str(details.get("regime_key") or "")
                        old_template_id = str(details.get("old_template_id") or "")
                        new_template_id = str(details.get("new_template_id") or "")
                        if old_template_id:
                            pending_parameter_rollbacks.append(
                                {
                                    "application_id": str(app["application_id"]),
                                    "factor_id": factor_id,
                                    "regime_key": regime_key,
                                    "old_template_id": old_template_id,
                                    "new_template_id": new_template_id,
                                    "suggestion_ids": suggestion_ids,
                                    "delta": delta,
                                    "effect_decision": decision,
                                }
                            )
                    else:
                        self._executemany(conn,
                            """
                            UPDATE policy_suggestion
                            SET status='rolled_back', reviewed_at=?, review_note=?
                            WHERE suggestion_id=?
                            """,
                            [
                                (now, f"auto rollback by application effect delta={delta:.3f}", sid)
                                for sid in suggestion_ids
                            ],
                        )
                        rolled_back += 1
                elif next_status == "effective" and len(post_reviews) >= observe_trades:
                    suggestion_id = self._new_id("psg")
                    evidence = {
                        "source_agent": "autonomous_learning",
                        "source_application_id": app["application_id"],
                        "sample_count": len(post_reviews),
                        "baseline_sample_count": len(pre_reviews),
                        "post_avg_reward": round(post_avg, 6),
                        "baseline_avg_reward": round(pre_avg, 6),
                        "delta_avg_reward": round(delta, 6),
                        "authority_verdict": AgentAuthorityRegistryService().evaluate_scope_write(
                            "autonomous_learning",
                            scope_type,
                            app["action"],
                            requested_writes=["policy_suggestion"],
                            status="proposed",
                            impact_level="medium",
                        ),
                    }
                    evidence = attach_policy_suggestion_agent_context(
                        evidence,
                        source_agent="autonomous_learning",
                        scope_type=scope_type,
                        scope_key=scope_key_for_effect,
                        action=app["action"],
                        requested_writes=["policy_suggestion"],
                        status="proposed",
                        impact_level="medium",
                        db_path=self.db_path,
                    )
                    self._execute(conn,
                        """
                        INSERT INTO policy_suggestion
                        (suggestion_id, scope_type, scope_key, action, confidence, reason,
                         evidence_json, status, reviewed_at, review_note,
                         governance_eligible, governance_eligibility_version,
                         governance_eligibility_fingerprint,
                         governance_ineligible_reason, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'proposed', 0, ?,
                                0, ?, '', ?, ?)
                        """,
                        (
                            suggestion_id,
                            scope_type,
                            scope_key_for_effect,
                            app["action"],
                            min(0.75, 0.35 + max(0.0, delta)),
                            f"auto reinforced by application effect delta={delta:.3f}",
                            json.dumps(evidence, ensure_ascii=False, default=str),
                            "pending governance eligibility materialization from "
                            f"application {app['application_id']}",
                            GOVERNANCE_ELIGIBILITY_VERSION,
                            "application_effect_requires_governance_eligibility_materialization",
                            now,
                        ),
                    )
                    self._execute(conn,
                        """
                        UPDATE learning_application_effect
                        SET status='reinforced', updated_at=?
                        WHERE application_id=?
                        """,
                        (now, app["application_id"]),
                    )
                    self._execute(conn,
                        """
                        UPDATE learning_application_log
                        SET status='reinforced'
                        WHERE application_id=?
                        """,
                        (app["application_id"],),
                    )
                    reinforced += 1

        for item in pending_parameter_rollbacks:
            from backend.services.parameter_templates import ParameterTemplateService

            result = ParameterTemplateService(str(self.db_path)).rollback_template_application(
                application_id=str(item["application_id"]),
                factor_id=str(item["factor_id"]),
                regime_key=str(item["regime_key"]),
                current_template_id=str(item["new_template_id"]),
                previous_template_id=str(item["old_template_id"]),
                suggestion_ids=list(item["suggestion_ids"]),
                reason=(
                    "auto rollback by application effect "
                    f"delta={float(item['delta']):.3f}"
                ),
                evidence={
                    "effect_decision": item["effect_decision"],
                    "delta_avg_reward": float(item["delta"]),
                },
            )
            if result.get("ok"):
                rolled_back += 1
            else:
                rollback_pending += 1

        return {
            "observed": observed,
            "rolled_back": rolled_back,
            "reinforced": reinforced,
            "waiting": waiting,
            "rechecked_mixed": rechecked_mixed,
            "inconclusive": inconclusive,
            "rollback_pending": rollback_pending,
        }
