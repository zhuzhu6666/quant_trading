from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from backend.core.db import (
    STATE_DB,
    STATE_DB_DDL,
    connect_sqlite,
    get_state_pg_conn,
    is_state_db_path,
)
from backend.services.agent_authority import AgentAuthorityRegistryService
from backend.services.brain_governance_candidates import sync_candidate_suggestion_lifecycle
from backend.services.governance_eligibility import GOVERNANCE_ELIGIBILITY_VERSION
from backend.services.learning_application_store import LearningApplicationStore
from backend.services.policy_suggestion_context import attach_policy_suggestion_agent_context
from backend.services.policy_suggestion_identity import deterministic_policy_suggestion_id
from backend.services.review_contract import review_has_system_contamination
from backend.services.canonical_v2_reader import (
    canonical_ready,
    iter_decision_factor_snapshots,
    iter_review_rows_desc,
)
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
        # Keep the lean application-row shape for LearningApplicationStore
        # callers; review facts are parsed by _parse_review_row below.
        item = dict(row)
        try:
            item["suggestion_ids"] = json.loads(
                item.pop("suggestion_ids_json", "[]") or "[]"
            )
        except Exception:
            item["suggestion_ids"] = []
        try:
            item["details"] = json.loads(item.pop("details_json", "{}") or "{}")
        except Exception:
            item["details"] = {}
        return item

    def _write_application(
        self,
        application_id: str,
        *,
        status: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> bool:
        """Narrow lean log write: update the status column + details_json only.

        The canonical store owns creation via prepare_application, but its
        transition_application only accepts a fixed status set, so the leading
        (reuse) path and reconcile still need a minimal status/details_json
        update here that never references the removed wide columns.
        """
        now = time.time()
        with self._conn() as conn:
            row = conn.execute(
                self._sql(
                    "SELECT details_json, status FROM learning_application_log "
                    "WHERE application_id=?"
                ),
                (str(application_id),),
            ).fetchone()
            if not row:
                return False
            raw = row["details_json"]
            try:
                data = json.loads(raw or "{}") if isinstance(raw, str) else dict(raw or {})
            except Exception:
                data = {}
            if not isinstance(data, dict):
                data = {}
            if details is not None:
                data.update(details)
            lifecycle = dict(data.get("application_state") or {})
            if status is not None:
                lifecycle["status"] = status
            lifecycle["updated_at"] = now
            if status == "applied":
                lifecycle.setdefault("applied_at", now)
            elif status == "mutation_failed":
                lifecycle.setdefault("failed_at", now)
            data["application_state"] = lifecycle
            new_status = (
                status
                if status is not None
                else str(row["status"] or lifecycle.get("status") or "")
            )
            conn.execute(
                self._sql(
                    "UPDATE learning_application_log SET status=?, details_json=?, "
                    "updated_at=? WHERE application_id=?"
                ),
                (
                    new_status,
                    json.dumps(data, ensure_ascii=False, default=str),
                    now,
                    str(application_id),
                ),
            )
        return True

    def _effect_ts(self, eff: dict | None) -> float:
        try:
            return float(eff.get("updated_at") or eff.get("created_at") or 0.0)
        except Exception:
            return 0.0

    def _app_ts(self, app: dict) -> float:
        try:
            return float(app.get("cycle_ts") or app.get("created_at") or 0.0)
        except Exception:
            return 0.0

    @staticmethod
    def _parse_review_row(row: sqlite3.Row | dict[str, Any]) -> dict:
        item = dict(row)
        try:
            raw_failure_tags = item.pop("failure_tags_json", [])
            item["failure_tags"] = (
                json.loads(raw_failure_tags or "[]")
                if isinstance(raw_failure_tags, str)
                else raw_failure_tags
            )
        except Exception:
            item["failure_tags"] = []
        inline_json = item.pop("review_json", {})
        try:
            item["review"] = (
                json.loads(inline_json or "{}")
                if isinstance(inline_json, str)
                else inline_json
            )
        except Exception:
            item["review"] = {}
        if not isinstance(item["review"], dict):
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
            review_has_system_contamination(review)
            or context_integrity != "full"
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
        refs = evidence.get("evidence_refs") or {}
        if not isinstance(refs, dict):
            refs = {}
        model_evidence = refs.get("model_evidence") or refs.get("model_contract") or {}
        if not isinstance(model_evidence, dict):
            model_evidence = {}

        def value(name: str, default: Any = "") -> Any:
            # A reviewed candidate bridge has its writer identity at the
            # top-level (factor_pruning_governance), while the model contract
            # remains nested under evidence_refs.model_evidence.  Prefer the
            # nested model contract for model-owned fields.
            for source in (model_evidence, evidence, refs):
                if name in source and source.get(name) not in (None, ""):
                    return source.get(name)
            return default

        if str(value("source_agent") or "") != "lightgbm_shadow_models":
            return False
        if str(value("model_type") or "") != "factor_governance_lightgbm":
            return False
        if value("advisory_only") is not True:
            return False
        bridge = evidence.get("bridge") or {}
        from config.runtime_config import DEMO_AUTONOMY_MODES

        autonomy_mode = str(bridge.get("autonomy_mode") or "").strip().lower()
        if not (
            bridge.get("automatic_demo") is True
            and (
                bridge.get("demo_nursery") is True
                or autonomy_mode in DEMO_AUTONOMY_MODES
            )
        ):
            return False
        actor = str(bridge.get("actor") or "")
        if not (
            actor.startswith("system:autonomous_learning.demo_nursery")
            or actor.startswith("system:factor_pruning_governance.demo_nursery")
        ):
            return False
        if str(value("governed_action") or "") != "downweight":
            return False
        promotion_gate = value("promotion_gate") or {}
        if not isinstance(promotion_gate, dict):
            return False
        if promotion_gate.get("passed") is not True:
            return False
        if value("mutation_eligible") is not True:
            return False
        if not str(value("artifact_sha256") or ""):
            return False
        if str(value("factor_generation") or "") != "runtime_bounded_v1":
            return False
        if not str(value("lineage_hash") or ""):
            return False
        if not str(value("label_contract_hash") or ""):
            return False
        candidate_id = str(evidence.get("candidate_id") or refs.get("candidate_id") or "")
        if not candidate_id:
            return False
        candidate_review = bridge.get("candidate_review") or {}
        if not (
            bridge.get("candidate_review_required_before_submit") is True
            and isinstance(candidate_review, dict)
            and candidate_review.get("bridge_ready") is True
            and str(candidate_review.get("review_id") or "")
        ):
            return False
        counter_evidence = evidence.get("counter_evidence_refs") or {}
        if not isinstance(counter_evidence, dict):
            return False
        factor_counter_evidence = counter_evidence.get("factor_counter_evidence")
        if not isinstance(factor_counter_evidence, dict) or not factor_counter_evidence:
            return False
        if str(factor_counter_evidence.get("status") or "").lower() in {"superseded", "rolled_back"}:
            return False
        active_context = value("active_factor_context") or {}
        if not isinstance(active_context, dict):
            return False
        if active_context.get("used_in_score") is not True or str(active_context.get("role") or "") != "alpha":
            return False
        if float(confidence) < 0.55:
            return False
        sample_count = int(value("sample_count") or 0)
        weak_sample_count = int(value("weak_sample_count") or 0)
        min_weakness = float(value("min_weakness_score") or 0.0)
        avg_weakness = float(value("avg_weakness_score") or 0.0)
        return (
            sample_count >= 20
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
                        sync_candidate_suggestion_lifecycle(
                            conn,
                            suggestion_id=str(row["suggestion_id"] or ""),
                            suggestion_status="rejected",
                            now=now,
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
                    sync_candidate_suggestion_lifecycle(
                        conn,
                        suggestion_id=str(row["suggestion_id"] or ""),
                        suggestion_status=status,
                        now=now,
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
                    sync_candidate_suggestion_lifecycle(
                        conn,
                        suggestion_id=str(row["suggestion_id"] or ""),
                        suggestion_status="rejected",
                        now=now,
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
                sync_candidate_suggestion_lifecycle(
                    conn,
                    suggestion_id=str(row["suggestion_id"] or ""),
                    suggestion_status=status,
                    now=now,
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
                       evidence_json, status, reviewed_at, created_at,
                       governance_eligible, governance_eligibility_version,
                       governance_eligibility_fingerprint, applied_mutation_id
                FROM policy_suggestion
                WHERE status IN ('proposed', 'approved')
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
                    WHERE suggestion_id=? AND status IN ('proposed', 'approved')
                    """,
                    (
                        now,
                        str(item.get("reason") or "superseded by governance conflict resolver"),
                        str(item.get("suggestion_id") or ""),
                    ),
                )
                sync_candidate_suggestion_lifecycle(
                    conn,
                    suggestion_id=str(item.get("suggestion_id") or ""),
                    suggestion_status="superseded",
                    now=now,
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
                    sync_candidate_suggestion_lifecycle(
                        conn,
                        suggestion_id=str(row["suggestion_id"] or ""),
                        suggestion_status="rolled_back",
                        now=now,
                    )
                    rolled_back += 1
                else:
                    kept += 1
        return {"rolled_back": rolled_back, "kept": kept}

    def set_status(self, suggestion_id: str, status: str, note: str = "") -> bool:
        if status not in {"approved", "rejected", "rolled_back", "proposed", "superseded"}:
            raise ValueError(f"unsupported status: {status}")
        with self._conn() as conn:
            now = time.time()
            cur = self._execute(conn,
                """
                UPDATE policy_suggestion
                SET status=?, reviewed_at=?, review_note=?
                WHERE suggestion_id=?
                """,
                (status, now, note, suggestion_id),
            )
            sync_candidate_suggestion_lifecycle(
                conn,
                suggestion_id=suggestion_id,
                suggestion_status=status,
                now=now,
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
        normalized_sids = json.dumps(sorted(set(suggestion_ids)), ensure_ascii=False)
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
        # cycle_ts lives inside details_json so store-parsed application dicts can
        # recover the observation-window boundary.
        details_payload["cycle_ts"] = float(cycle_ts)
        store = LearningApplicationStore(str(self.db_path))
        active_statuses = {"prepared", "applied", "observing", "effective"}

        def _app_ts(item: dict) -> float:
            try:
                return float(item.get("cycle_ts") or 0.0)
            except Exception:
                return 0.0

        def _app_sids(item: dict) -> str:
            try:
                return json.dumps(
                    sorted(set(str(i) for i in (item.get("suggestion_ids") or []))),
                    ensure_ascii=False,
                )
            except Exception:
                return "[]"

        def _matches_scope(item: dict) -> bool:
            return (
                str(item.get("scope_type") or "") == scope_type
                and str(item.get("scope_key") or "") == scope_key
                and str(item.get("action") or "") == action
            )

        # Reuse the newest active application for this scope+action when the
        # suggestion batch is identical; otherwise create a fresh one.
        existing = None
        existing_ts = float("-inf")
        for item in store.iter_applications(scope_type=scope_type, scope_key=scope_key):
            if not _matches_scope(item):
                continue
            if str(item.get("status") or "") not in active_statuses:
                continue
            ts = _app_ts(item)
            if ts > existing_ts:
                existing_ts = ts
                existing = item
        if existing is not None and _app_sids(existing) == normalized_sids:
            existing_id = str(existing["application_id"])
            for item in store.iter_applications(scope_type=scope_type, scope_key=scope_key):
                if not _matches_scope(item):
                    continue
                if str(item.get("application_id")) == existing_id:
                    continue
                if str(item.get("status") or "") not in active_statuses:
                    continue
                if _app_sids(item) == normalized_sids:
                    store.transition_application(
                        str(item["application_id"]), status="superseded"
                    )
                    store.update_effect(
                        str(item["application_id"]), patch={"status": "superseded"}
                    )
            refreshed = dict(existing)
            refreshed.update(details_payload)
            refreshed["bias_multiplier"] = float(bias_multiplier)
            refreshed["old_weight"] = float(old_weight)
            refreshed["new_weight"] = float(new_weight)
            refreshed["cycle_ts"] = float(cycle_ts)
            refreshed["suggestion_ids"] = suggestion_ids
            self._write_application(
                existing_id, details=refreshed, status=str(existing.get("status") or "")
            )
            store.update_effect(
                existing_id,
                patch={
                    "decision": {
                        "suggestion_ids": suggestion_ids,
                        "bias_multiplier": float(bias_multiplier),
                        "old_weight": float(old_weight),
                        "new_weight": float(new_weight),
                        "details": details_payload,
                    },
                    "status": ("prepared" if status == "prepared" else "observing"),
                    "updated_at": float(cycle_ts),
                },
            )
            return existing_id

        application_id = store.prepare_application(
            scope_type=scope_type,
            scope_key=scope_key,
            action=action,
            status=status,
            run_id=str(details_payload.get("run_id") or ""),
            source=str(
                details_payload.get("source")
                or details_payload.get("mutation_source")
                or ""
            ),
            bias_multiplier=float(bias_multiplier),
            old_weight=float(old_weight),
            new_weight=float(new_weight),
            suggestion_ids=suggestion_ids,
            cycle_ts=float(cycle_ts),
            details=details_payload,
        )
        effect_status = "prepared" if status == "prepared" else "observing"
        store.write_effect(
            application_id=application_id,
            scope_type=scope_type,
            scope_key=scope_key,
            action=action,
            status=effect_status,
            decision={
                "suggestion_ids": suggestion_ids,
                "bias_multiplier": float(bias_multiplier),
                "old_weight": float(old_weight),
                "new_weight": float(new_weight),
                "details": details_payload,
            },
            last_review_at=0.0,
            updated_at=float(cycle_ts),
        )
        return application_id

    def _persist_effect_evaluation(
        self,
        *,
        app: dict[str, Any],
        scope_type: str,
        scope_key: str,
        evaluation: EffectEvaluation,
        now: float,
    ) -> None:
        store = LearningApplicationStore(str(self.db_path))
        effect_patch = {
            "status": evaluation.status,
            "observed_trade_count": evaluation.post_count,
            "baseline_trade_count": evaluation.baseline_count,
            "post_avg_reward": round(evaluation.post_avg, 6),
            "baseline_avg_reward": round(evaluation.baseline_avg, 6),
            "post_win_rate": round(evaluation.post_win_rate, 4),
            "baseline_win_rate": round(evaluation.baseline_win_rate, 4),
            "decision": evaluation.decision,
            "last_review_at": evaluation.last_review_at,
            "updated_at": now,
        }
        if evaluation.delta is not None:
            effect_patch["delta_avg_reward"] = round(evaluation.delta, 6)
        application_id = str(app["application_id"])
        # Upsert the single effect row for this application (mirrors the old
        # INSERT ... ON CONFLICT(application_id) DO UPDATE shape).
        if not store.update_effect(application_id, patch=effect_patch):
            store.write_effect(
                application_id=application_id,
                scope_type=scope_type,
                scope_key=scope_key,
                action=str(app.get("action") or ""),
                status=evaluation.status,
                observed_trade_count=evaluation.post_count,
                baseline_trade_count=evaluation.baseline_count,
                post_avg_reward=round(evaluation.post_avg, 6),
                baseline_avg_reward=round(evaluation.baseline_avg, 6),
                delta_avg_reward=(
                    round(evaluation.delta, 6) if evaluation.delta is not None else None
                ),
                post_win_rate=round(evaluation.post_win_rate, 4),
                baseline_win_rate=round(evaluation.baseline_win_rate, 4),
                decision=evaluation.decision,
                last_review_at=evaluation.last_review_at,
                updated_at=now,
            )
        self._write_application(
            application_id,
            status=evaluation.status,
            details={"effect": evaluation.decision},
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
        # The lean store owns its own connection and cannot write inside the
        # open SQLite write transaction below (single-writer lock), so
        # reinforcements are applied after the transaction commits.
        reinforced_application_ids: list[str] = []

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
            store = LearningApplicationStore(str(self.db_path))
            all_apps = list(store.iter_applications())
            effects_by_app: dict[str, dict[str, Any]] = {}
            for eff in store.iter_effects():
                eff_aid = str(eff.get("application_id") or "")
                if not eff_aid:
                    continue
                cur = effects_by_app.get(eff_aid)
                if cur is None or self._effect_ts(eff) > self._effect_ts(cur):
                    effects_by_app[eff_aid] = eff

            active_log_statuses = {"applied", "observing", "effective", "mixed"}
            _status_rank = {"applied": 0, "observing": 1, "mixed": 2}
            mixed_cutoff = now - max(300.0, float(mixed_recheck_after_seconds or 0.0))
            candidates = []
            for app in all_apps:
                if str(app.get("status") or "") not in active_log_statuses:
                    continue
                eff_updated_at = self._effect_ts(
                    effects_by_app.get(str(app.get("application_id") or ""))
                )
                if str(app.get("status") or "") == "mixed":
                    if eff_updated_at > mixed_cutoff:
                        continue
                candidates.append(
                    (
                        eff_updated_at,
                        _status_rank.get(str(app.get("status") or ""), 3),
                        self._app_ts(app),
                        app,
                    )
                )
            candidates.sort(key=lambda item: (item[0], item[1], item[2]))
            rows = [
                item[3]
                for item in candidates[
                    : max(1, min(int(application_limit or 200), 2000))
                ]
            ]
            canonical_reviews = [
                self._parse_review_row(row)
                for row in (
                    iter_review_rows_desc(conn, limit=0)
                    if canonical_ready(conn)
                    else []
                )
            ]
            factor_names_by_decision: dict[str, set[str]] = {}

            def review_timestamp(item: dict[str, Any]) -> float:
                try:
                    return float(item.get("created_at") or 0.0)
                except Exception:
                    return 0.0

            def review_has_factor(item: dict[str, Any], factor: str) -> bool:
                decision_id = str(item.get("entry_decision_id") or "")
                if not decision_id:
                    return False
                if decision_id not in factor_names_by_decision:
                    factor_names_by_decision[decision_id] = {
                        str(snapshot.get("factor") or "")
                        for snapshot in iter_decision_factor_snapshots(conn, decision_id)
                        if str(snapshot.get("factor") or "")
                    }
                return str(factor) in factor_names_by_decision[decision_id]

            for app in rows:
                prior_status = str(app.get("status") or "")
                if prior_status == "mixed":
                    rechecked_mixed += 1
                scope_type = str(app.get("scope_type") or "")
                if scope_type not in {
                    "factor",
                    "parameter_template",
                    "position_supervisor_template",
                    "entry_quality",
                }:
                    continue
                scope_key_for_effect = str(app.get("scope_key") or "")
                review_limit = max(int(observe_trades) * 5, int(observe_trades))
                raw_post_count_override: int | None = None
                raw_pre_count_override: int | None = None
                current_cycle_ts = self._app_ts(app)
                next_application = None
                next_cycle = float("inf")
                for other in all_apps:
                    if str(other.get("application_id")) == str(app.get("application_id") or ""):
                        continue
                    if str(other.get("scope_type") or "") != scope_type:
                        continue
                    if str(other.get("scope_key") or "") != str(app.get("scope_key") or ""):
                        continue
                    other_ts = self._app_ts(other)
                    if other_ts <= current_cycle_ts:
                        continue
                    if str(other.get("status") or "") in {"superseded", "rolled_back", "rejected"}:
                        continue
                    if other_ts < next_cycle:
                        next_cycle = other_ts
                        next_application = {
                            "application_id": str(other.get("application_id") or ""),
                            "action": str(other.get("action") or ""),
                            "cycle_ts": other_ts,
                        }
                observation_upper_bound = (
                    float(next_application["cycle_ts"])
                    if next_application
                    else 1.0e18
                )
                review_scan_limit = min(
                    max(review_limit * 10, int(observe_trades) * 20, 500),
                    5000,
                )
                if scope_type == "position_supervisor_template":
                    if not scope_key_for_effect:
                        continue
                    post_rows = [
                        item
                        for item in canonical_reviews
                        if self._app_ts(app) < review_timestamp(item) < observation_upper_bound
                    ][:review_scan_limit]
                    post_reviews = [
                        r for r in post_rows
                        if self._has_supervisor_feedback(r)
                    ][: int(observe_trades)]

                    pre_rows = [
                        item
                        for item in canonical_reviews
                        if review_timestamp(item) <= self._app_ts(app)
                    ][:review_scan_limit]
                    pre_reviews = [
                        r for r in pre_rows
                        if self._has_supervisor_feedback(r)
                    ][: int(observe_trades)]
                    reward_from_review = self._supervisor_reward_from_review
                elif scope_type == "entry_quality":
                    parsed_post = [
                        item
                        for item in canonical_reviews
                        if self._app_ts(app) < review_timestamp(item) < observation_upper_bound
                    ][:review_scan_limit]
                    parsed_pre = [
                        item
                        for item in canonical_reviews
                        if review_timestamp(item) <= self._app_ts(app)
                    ][:review_scan_limit]
                    raw_post_count_override = len(parsed_post)
                    raw_pre_count_override = len(parsed_pre)

                    def distinct_positions(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
                        seen: set[str] = set()
                        result: list[dict[str, Any]] = []
                        for item in items:
                            identity = str(
                                item.get("position_id")
                                or item.get("trade_id")
                                or item.get("review_id")
                                or ""
                            )
                            if identity in seen:
                                continue
                            seen.add(identity)
                            result.append(item)
                        return result

                    post_reviews = distinct_positions(parsed_post)[: int(observe_trades)]
                    pre_reviews = distinct_positions(parsed_pre)[: int(observe_trades)]
                    reward_from_review = self._reward_from_review
                else:
                    factor = (
                        str(app.get("scope_key") or "")
                        if scope_type == "factor"
                        else str(app.get("factor_id") or scope_key_for_effect.split(":", 1)[0])
                    )
                    if not factor:
                        continue
                    scope_key_for_effect = scope_key_for_effect or factor

                    post_reviews = [
                        item
                        for item in canonical_reviews
                        if self._app_ts(app) < review_timestamp(item) < observation_upper_bound
                        and review_has_factor(item, factor)
                    ][:review_scan_limit]
                    pre_reviews = [
                        item
                        for item in canonical_reviews
                        if review_timestamp(item) <= self._app_ts(app)
                        and review_has_factor(item, factor)
                    ][:review_scan_limit]
                    reward_from_review = self._reward_from_review

                raw_post_reviews = list(post_reviews)
                raw_pre_reviews = list(pre_reviews)
                target_regime = str(
                    app.get("regime_id")
                    or app.get("regime_key")
                    or app.get("entry_regime")
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
                    raw_post_count=(
                        raw_post_count_override
                        if raw_post_count_override is not None
                        else len(raw_post_reviews)
                    ),
                    raw_baseline_count=(
                        raw_pre_count_override
                        if raw_pre_count_override is not None
                        else len(raw_pre_reviews)
                    ),
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
                    min_trades=max(5, min_trades) if scope_type == "entry_quality" else min_trades,
                    observe_trades=max(5, observe_trades) if scope_type == "entry_quality" else observe_trades,
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
                if scope_type == "entry_quality":
                    controls = dict(app.get("controls") or {})
                    threshold = float(controls.get("min_abs_signal_score") or 0.0)

                    def entry_metrics(items: list[dict[str, Any]]) -> dict[str, Any]:
                        if not items:
                            return {
                                "distinct_positions": 0,
                                "below_applied_threshold_open_count": 0,
                                "weak_signal_overtraded_rate": 0.0,
                                "avg_entry_quality": 0.0,
                                "mfe_zero_loss_rate": 0.0,
                            }
                        below = 0
                        weak = 0
                        entry_total = 0.0
                        zero_mfe_losses = 0
                        losses = 0
                        for item in items:
                            review = item.get("review") or {}
                            score = review.get("signal_score")
                            if score is not None and abs(float(score or 0.0)) < threshold:
                                below += 1
                            tags = {str(tag) for tag in (item.get("failure_tags") or [])}
                            weak += int("weak_signal_overtraded" in tags)
                            entry_total += float(
                                item.get("entry_quality")
                                or review.get("entry_quality")
                                or 0.0
                            )
                            if float(item.get("pnl") or 0.0) < 0:
                                losses += 1
                                zero_mfe_losses += int(float(item.get("mfe") or 0.0) <= 0.0)
                        count = len(items)
                        return {
                            "distinct_positions": count,
                            "below_applied_threshold_open_count": below,
                            "weak_signal_overtraded_rate": round(weak / count, 6),
                            "avg_entry_quality": round(entry_total / count, 6),
                            "mfe_zero_loss_rate": round(
                                zero_mfe_losses / max(losses, 1), 6
                            ),
                        }

                    decision["entry_quality_effect"] = {
                        "threshold": threshold,
                        "post": entry_metrics(post_reviews),
                        "baseline": entry_metrics(pre_reviews),
                        "success_contract": {
                            "below_threshold_open_count": 0,
                            "weak_signal_overtraded_relative_reduction": 0.5,
                            "min_post_independent_positions": int(observe_trades),
                        },
                    }
                next_status = evaluation.status
                post_reviews = post_reviews[: evaluation.post_count]
                pre_reviews = pre_reviews[: evaluation.baseline_count]
                post_avg = evaluation.post_avg
                pre_avg = evaluation.baseline_avg
                delta = evaluation.delta
                post_win_rate = evaluation.post_win_rate
                pre_win_rate = evaluation.baseline_win_rate

                # The store writes on its own connection and cannot write while
                # this reconcile connection still holds an uncommitted write from
                # a previous iteration (e.g. policy_suggestion), so flush first.
                conn.commit()
                self._persist_effect_evaluation(
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
                        factor_id = str(app.get("factor_id") or "")
                        regime_key = str(app.get("regime_key") or "")
                        old_template_id = str(app.get("old_template_id") or "")
                        new_template_id = str(app.get("new_template_id") or "")
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
                    suggestion_id = deterministic_policy_suggestion_id(
                        writer="rule_evolution_governor",
                        scope_type=scope_type,
                        scope_key=scope_key_for_effect,
                        action=app["action"],
                        evidence=evidence,
                        status="proposed",
                        qualification_fingerprint="",
                        prefix="psg_effect",
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
                        ON CONFLICT(suggestion_id) DO NOTHING
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
                    reinforced_application_ids.append(str(app["application_id"]))
                    reinforced += 1

        # Apply reinforcements now that the write transaction above has
        # committed and released its lock (the store needs its own writer).
        for _reinforced_application_id in reinforced_application_ids:
            store.update_effect(
                _reinforced_application_id, patch={"status": "reinforced"}
            )
            self._write_application(_reinforced_application_id, status="reinforced")

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
