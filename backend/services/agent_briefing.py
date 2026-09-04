from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB
from backend.core.ttl_cache import TTLCache
from backend.services.agent_authority import AgentAuthorityRegistryService
from backend.services.agent_scorecard import AgentScorecardService
from backend.services.proposal_registry import ProposalRegistryService
from backend.services._brain_helpers import connect as _connect, execute as _execute, loads as _loads
from backend.services.canonical_v2_reader import iter_counterfactual_rows, review_row
from backend.services.review_contract import review_has_system_contamination
from backend.services.v16_brain_snapshot import BrainMemoryService


# The relevant-experience builder re-scans review blobs (and their
# counterfactuals) on every agent-context call; the governance cycle calls
# agent_context once per audited action.  A short TTL cache dedupes those
# identical reads within a cycle without changing freshness materially.
_EXPERIENCE_CACHE = TTLCache(maxsize=32, ttl_seconds=60.0)


class AgentBriefingContextService:
    """Read-only context packet shared by autonomous agent reviewers."""

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = db_path

    def _reviews_attached(self, conn: Any, rows: list[Any]) -> list[dict[str, Any]]:
        """Inner-join semantics against canonical review facts per row."""
        combined: list[dict[str, Any]] = []
        for row in rows:
            source_id = str(row["source_id"] or "") if "source_id" in (row.keys() if hasattr(row, "keys") else ()) else ""
            review = review_row(conn, source_id)
            if review is None:
                continue
            combined.append(
                {
                    **dict(row),
                    "source_review_id": source_id,
                    "source_trade_id": str(review.get("trade_id") or ""),
                    "source_position_id": str(review.get("position_id") or ""),
                    "source_pnl": review.get("pnl"),
                    "source_outcome_label": str(review.get("outcome_label") or ""),
                    "source_failure_tags_json": review.get("failure_tags_json") or "[]",
                    "source_created_at": review.get("created_at"),
                    "source_review_json": review.get("review_json") or {},
                }
            )
        return combined

    @staticmethod
    def _review_payload(conn: Any, row: Any) -> dict[str, Any]:
        try:
            keys = row.keys() if hasattr(row, "keys") else ()
            source_id = row["source_review_id"] if "source_review_id" in keys else ""
            inline_json = row["source_review_json"] if "source_review_json" in keys else "{}"
        except Exception:
            source_id, inline_json, archive_hash = "", "{}", ""
        payload = inline_json if isinstance(inline_json, dict) else _loads(inline_json, {})
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {
            "schema_version": "agent_briefing_boundary.v1",
            "read_only": True,
            "does_not_submit_orders": True,
            "does_not_apply_runtime_mutations": True,
            "does_not_change_agent_authority": True,
            "for_review_and_prompt_context_only": True,
        }

    def build(self, *, limit: int = 50) -> dict[str, Any]:
        limit = max(1, min(int(limit), 200))
        scorecard_service = AgentScorecardService(self.db_path)
        scorecard = scorecard_service.scorecard(limit=max(limit, 300))
        trade_feedback = scorecard_service.latest_trade_attributions(limit=limit, include_external_links=False)
        chain_health = scorecard_service.chain_health(limit=max(limit, 300))
        proposals = ProposalRegistryService(self.db_path).status(refresh=False)
        governance_coverage = self._governance_coverage()
        top_agents = (scorecard.get("items") or [])[:8]
        recent_losses = [
            {
                "review_id": item.get("review_id", ""),
                "trade_id": item.get("trade_id", ""),
                "pnl": item.get("pnl"),
                "outcome_label": item.get("outcome_label", ""),
                "failure_tags": item.get("failure_tags") or [],
                "feedback_targets": item.get("feedback_targets") or [],
                "system_judgement": item.get("system_judgement") or {},
                "lesson": {
                    "recommended_action": (item.get("lesson") or {}).get("recommended_action", ""),
                    "feedback_agents": (item.get("lesson") or {}).get("feedback_agents") or [],
                },
            }
            for item in (trade_feedback.get("items") or [])
            if float(item.get("pnl") or 0.0) < 0
        ][:10]
        return {
            "ok": True,
            "schema_version": "agent_briefing_context.v1",
            "chain_health": {
                "status": chain_health.get("status", "unknown"),
                "blockers": chain_health.get("blockers") or [],
                "checks": chain_health.get("checks") or [],
            },
            "proposal_flow": {
                "proposal_count": proposals.get("proposal_count", 0),
                "active_count": proposals.get("active_count", 0),
                "conflict_count": proposals.get("conflict_count", 0),
                "stale_evidence_count": proposals.get("stale_evidence_count", 0),
                "hard_stale_evidence_count": proposals.get("hard_stale_evidence_count", 0),
                "stale_replay_required_count": proposals.get("stale_replay_required_count", 0),
                "stale_review_required_count": proposals.get("stale_review_required_count", 0),
                "low_reliability_count": proposals.get("low_reliability_count", 0),
            },
            "agent_scorecard": {
                "summary": scorecard.get("summary") or {},
                "top_agents": top_agents,
            },
            "recent_trade_feedback": {
                "summary": trade_feedback.get("summary") or {},
                "recent_losses": recent_losses,
            },
            "relevant_experience": self._relevant_experience(limit=10),
            "governance_coverage": governance_coverage,
            "review_rules": {
                "low_score_requires_extra_evidence": True,
                "contract_violation_blocks_auto_bridge": True,
                "negative_feedback_requires_counter_evidence": True,
                "high_score_changes_priority_only": True,
                "never_expands_execution_authority": True,
                "candidate_context_required": True,
                "candidate_review_required_before_bridge": True,
            },
            "generated_at": time.time(),
            "boundary": self.boundary(),
        }

    def agent_context(
        self,
        source_agent: str,
        *,
        scope_type: str,
        scope_key: str = "",
        action: str,
        requested_writes: list[str] | tuple[str, ...] | str | None = None,
        status: str = "",
        impact_level: str = "",
        limit: int = 50,
    ) -> dict[str, Any]:
        """Return a compact pre-generation context packet for one source agent."""
        limit = max(1, min(int(limit), 200))
        registry = AgentAuthorityRegistryService()
        authority = registry.evaluate_scope_write(
            source_agent,
            scope_type,
            action,
            requested_writes=requested_writes,
            status=status,
            impact_level=impact_level,
        )
        canonical = str(authority.get("canonical_source_agent") or source_agent or "unknown")
        scorecard_service = AgentScorecardService(self.db_path)
        scorecard = scorecard_service.scorecard(limit=max(limit, 300))
        metric = next(
            (item for item in (scorecard.get("items") or []) if str(item.get("source_agent") or "") == canonical),
            {},
        )
        trade_feedback = scorecard_service.latest_trade_attributions(
            limit=limit,
            include_external_links=False,
        )
        recent_losses = []
        for item in trade_feedback.get("items") or []:
            if float(item.get("pnl") or 0.0) >= 0:
                continue
            targets = set(str(x) for x in (item.get("feedback_targets") or []))
            targets.update(str(x) for x in ((item.get("lesson") or {}).get("feedback_agents") or []))
            if canonical in targets or str(source_agent) in targets:
                recent_losses.append(
                    {
                        "review_id": item.get("review_id", ""),
                        "trade_id": item.get("trade_id", ""),
                        "pnl": item.get("pnl"),
                        "failure_tags": item.get("failure_tags") or [],
                        "recommended_action": (item.get("lesson") or {}).get("recommended_action", ""),
                    }
                )
        return {
            "ok": True,
            "schema_version": "agent_generation_context.v1",
            "source_agent": source_agent,
            "canonical_source_agent": canonical,
            "scope_type": scope_type,
            "scope_key": scope_key,
            "action": action,
            "authority_verdict": authority,
            "scorecard": metric,
            "recent_loss_feedback": recent_losses[:10],
            "relevant_experience": self._relevant_experience(
                scope_type=scope_type,
                scope_key=scope_key,
                limit=10,
            ),
            "review_rules": {
                "low_score_requires_extra_evidence": True,
                "contract_violation_blocks_auto_bridge": True,
                "negative_feedback_requires_counter_evidence": True,
                "high_score_changes_priority_only": True,
                "never_expands_execution_authority": True,
            },
            "boundary": {
                **self.boundary(),
                "pre_generation_context_only": True,
                "does_not_create_candidates": True,
            },
        }

    def _relevant_experience(
        self,
        *,
        scope_type: str = "",
        scope_key: str = "",
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Return compact, evidence-weighted lessons for generation context."""
        limit = max(1, min(int(limit), 50))
        cache_key = (
            "agent_experience.v1",
            str(self.db_path),
            str(scope_type),
            str(scope_key),
            int(limit),
        )
        cached = _EXPERIENCE_CACHE.get(cache_key)
        if cached is not None:
            return cached
        conn = None
        try:
            conn = _connect(self.db_path, read_only=True)
            params: list[Any] = []
            where = ""
            if scope_type == "factor" and scope_key:
                where = "WHERE decision_context_json LIKE ?"
                params.append(f'%"primary_factor": "{scope_key}"%')
            rows = _execute(
                conn,
                f"""
                SELECT e.experience_id, e.trade_id, e.regime_id, e.outcome_label,
                       e.reward_score, e.failure_tags_json, e.recommended_action,
                       e.evidence_strength, e.decision_context_json, e.created_at,
                       e.source_id
                FROM experience_memory e
                WHERE e.append_source='trade_lesson_memory.v1'
                {"AND " + where[6:] if where else ""}
                ORDER BY e.evidence_strength DESC, e.created_at DESC
                LIMIT ?
                """,
                (*params, min(200, max(limit, limit * 5))),
            ).fetchall()
            rows = self._reviews_attached(conn, rows)
            result = []
            for row in rows:
                review_json = self._review_payload(conn, row)
                if review_has_system_contamination(review_json):
                    continue
                review_id = str(row["source_review_id"] or "")
                review_failure_tags = _loads(row["source_failure_tags_json"], [])
                if not isinstance(review_failure_tags, list):
                    review_failure_tags = []
                review = {
                    "review_id": review_id,
                    "source_id": review_id,
                    "trade_id": str(row["source_trade_id"] or ""),
                    "position_id": str(row["source_position_id"] or ""),
                    "pnl": row["source_pnl"],
                    "outcome_label": str(row["source_outcome_label"] or ""),
                    "failure_tags": review_failure_tags,
                    "failure_tags_json": row["source_failure_tags_json"],
                    "review_json": review_json,
                    "created_at": row["source_created_at"],
                }
                counterfactuals = []
                if review_id:
                    counterfactual_rows = iter_counterfactual_rows(
                        conn,
                        limit=0,
                        review_id=review_id,
                        reverse=True,
                    )
                    for counterfactual in counterfactual_rows:
                        evidence = counterfactual.get("evidence") or _loads(counterfactual.get("evidence_json"), {})
                        if not isinstance(evidence, dict) or evidence.get("evidence_invalidated"):
                            continue
                        horizons = counterfactual.get("horizons") or _loads(counterfactual.get("horizons_json"), [])
                        counterfactuals.append({
                            "counterfactual_id": str(counterfactual.get("counterfactual_id") or ""),
                            "review_id": str(counterfactual.get("review_id") or review_id),
                            "trade_id": str(counterfactual.get("trade_id") or ""),
                            "position_id": str(counterfactual.get("position_id") or ""),
                            "label": str(counterfactual.get("label") or ""),
                            "confidence": counterfactual.get("confidence"),
                            "horizons": horizons if isinstance(horizons, list) else [],
                            "evidence": evidence,
                        })
                reconciled = BrainMemoryService.reconcile_trade_review(
                    review,
                    counterfactuals=counterfactuals,
                )
                posterior = (
                    (reconciled.get("structured") or {}).get("posterior_reconciliation")
                    or {}
                )
                selected = (posterior.get("local_arbitration") or {}).get("selected_conclusion") or {}
                source_action = str(row["recommended_action"] or "watch")
                evidence_eligible = bool(posterior.get("evidence_eligible", True))
                context = _loads(row["decision_context_json"], {})
                result.append({
                    "experience_id": str(row["experience_id"] or ""),
                    "trade_id": str(row["trade_id"] or ""),
                    "regime_id": str(row["regime_id"] or ""),
                    "outcome_label": str(row["outcome_label"] or ""),
                    "reward_score": float(row["reward_score"] or 0.0),
                    "failure_tags": _loads(row["failure_tags_json"], []),
                    # A source recommendation is retained for audit, but a
                    # supervisor posterior makes it non-actionable for the
                    # entry/factor generator.  The safe output is explicit.
                    "recommended_action": source_action if evidence_eligible else "observe_and_compare",
                    "source_recommended_action": source_action,
                    "evidence_strength": float(row["evidence_strength"] or 0.0),
                    "primary_factor": str(context.get("primary_factor") or ""),
                    "summary_text": str(context.get("summary_text") or ""),
                    "created_at": float(row["created_at"] or 0.0),
                    "causal_scope": str(posterior.get("causal_scope") or ""),
                    "action_owner": str(posterior.get("action_owner") or ""),
                    "evidence_eligible": evidence_eligible,
                    "posterior_action": str(selected.get("recommended_action") or ""),
                    "posterior_reconciliation": posterior,
                })
            cached_result = result[:limit]
            _EXPERIENCE_CACHE.put(cache_key, cached_result)
            return cached_result
        except Exception:
            return []
        finally:
            if conn is not None:
                conn.close()

    def _governance_coverage(self) -> dict[str, Any]:
        try:
            from backend.services.brain_governance_candidates import BrainGovernanceCandidateService
            from backend.services.brain_governance_candidate_review import BrainGovernanceCandidateReviewService
            from backend.services.proposal_registry import ProposalRegistryService

            context_coverage = BrainGovernanceCandidateService(self.db_path).generation_context_coverage(limit=200)
            bridge_coverage = BrainGovernanceCandidateReviewService(self.db_path).bridge_review_coverage(limit=200)
            proposal_context_coverage = ProposalRegistryService(self.db_path).generation_context_coverage(limit=200)
            status = "ok"
            if (
                context_coverage.get("status") == "degraded"
                or bridge_coverage.get("status") == "degraded"
                or proposal_context_coverage.get("status") == "degraded"
            ):
                status = "degraded"
            return {
                "schema_version": "agent_briefing_governance_coverage.v1",
                "status": status,
                "candidate_generation_context_coverage": {
                    "status": context_coverage.get("status", "unknown"),
                    "missing_required_context_count": context_coverage.get("missing_required_context_count", 0),
                    "legacy_missing_context_count": context_coverage.get("legacy_missing_context_count", 0),
                },
                "candidate_bridge_review_coverage": {
                    "status": bridge_coverage.get("status", "unknown"),
                    "missing_required_review_count": bridge_coverage.get("missing_required_review_count", 0),
                    "legacy_unreviewed_count": bridge_coverage.get("legacy_unreviewed_count", 0),
                },
                "proposal_generation_context_coverage": {
                    "status": proposal_context_coverage.get("status", "unknown"),
                    "missing_required_context_count": proposal_context_coverage.get("missing_required_context_count", 0),
                    "legacy_missing_context_count": proposal_context_coverage.get("legacy_missing_context_count", 0),
                },
            }
        except Exception as exc:
            return {
                "schema_version": "agent_briefing_governance_coverage.v1",
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }
