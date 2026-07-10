from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB
from backend.services.agent_authority import AgentAuthorityRegistryService
from backend.services.agent_scorecard import AgentScorecardService
from backend.services.proposal_registry import ProposalRegistryService
from backend.services._brain_helpers import connect as _connect, execute as _execute, loads as _loads


class AgentBriefingContextService:
    """Read-only context packet shared by autonomous agent reviewers."""

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = db_path

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
        conn = None
        try:
            conn = _connect(self.db_path, read_only=True)
            params: list[Any] = []
            where = ""
            if scope_type == "factor" and scope_key:
                where = "WHERE decision_context_json LIKE ?"
                params.append(f'%"primary_factor": "{scope_key}"%')
            params.append(limit)
            rows = _execute(
                conn,
                f"""
                SELECT experience_id, trade_id, regime_id, outcome_label,
                       reward_score, failure_tags_json, recommended_action,
                       evidence_strength, decision_context_json, created_at
                FROM experience_memory
                {where}
                ORDER BY evidence_strength DESC, created_at DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
            result = []
            for row in rows:
                context = _loads(row["decision_context_json"], {})
                result.append({
                    "experience_id": str(row["experience_id"] or ""),
                    "trade_id": str(row["trade_id"] or ""),
                    "regime_id": str(row["regime_id"] or ""),
                    "outcome_label": str(row["outcome_label"] or ""),
                    "reward_score": float(row["reward_score"] or 0.0),
                    "failure_tags": _loads(row["failure_tags_json"], []),
                    "recommended_action": str(row["recommended_action"] or "watch"),
                    "evidence_strength": float(row["evidence_strength"] or 0.0),
                    "primary_factor": str(context.get("primary_factor") or ""),
                    "summary_text": str(context.get("summary_text") or ""),
                    "created_at": float(row["created_at"] or 0.0),
                })
            return result
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
