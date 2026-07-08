from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB
from backend.services.agent_authority_registry import AgentAuthorityRegistryService


def attach_policy_suggestion_agent_context(
    evidence: dict[str, Any] | None,
    *,
    source_agent: str,
    scope_type: str,
    action: str,
    requested_writes: list[str] | tuple[str, ...] | str | None = None,
    status: str = "proposed",
    impact_level: str = "medium",
    db_path: str | Path = STATE_DB,
) -> dict[str, Any]:
    """Attach the shared pre-generation agent context to policy suggestions.

    This helper reuses AgentBriefingContextService instead of creating another
    context source. It mutates only the evidence payload that callers already
    persist; it does not write tables, approve proposals, or apply actions.
    """
    payload = dict(evidence or {})
    payload.setdefault("source_agent", source_agent)
    writes = requested_writes if requested_writes is not None else ["policy_suggestion"]
    payload.setdefault(
        "authority_verdict",
        AgentAuthorityRegistryService().evaluate_scope_write(
            source_agent,
            scope_type,
            action,
            requested_writes=writes,
            status=status,
            impact_level=impact_level,
        ),
    )
    context = payload.get("agent_context")
    if not isinstance(context, dict) or context.get("schema_version") != "agent_generation_context.v1":
        payload["agent_context"] = _agent_context(
            source_agent=source_agent,
            scope_type=scope_type,
            action=action,
            requested_writes=writes,
            status=status,
            impact_level=impact_level,
            db_path=db_path,
        )
    payload.setdefault("agent_context_required", True)
    return payload


def _agent_context(
    *,
    source_agent: str,
    scope_type: str,
    action: str,
    requested_writes: list[str] | tuple[str, ...] | str | None,
    status: str,
    impact_level: str,
    db_path: str | Path,
) -> dict[str, Any]:
    try:
        from backend.services.agent_briefing import AgentBriefingContextService

        return AgentBriefingContextService(db_path).agent_context(
            source_agent,
            scope_type=scope_type,
            action=action,
            requested_writes=requested_writes,
            status=status,
            impact_level=impact_level,
            limit=50,
        )
    except Exception as exc:
        authority = AgentAuthorityRegistryService().evaluate_scope_write(
            source_agent,
            scope_type,
            action,
            requested_writes=requested_writes,
            status=status,
            impact_level=impact_level,
        )
        return {
            "ok": False,
            "schema_version": "agent_generation_context.v1",
            "source_agent": source_agent,
            "canonical_source_agent": str(authority.get("canonical_source_agent") or source_agent),
            "scope_type": scope_type,
            "action": action,
            "authority_verdict": authority,
            "scorecard": {},
            "recent_loss_feedback": [],
            "review_rules": {
                "low_score_requires_extra_evidence": True,
                "contract_violation_blocks_auto_bridge": True,
                "negative_feedback_requires_counter_evidence": True,
                "high_score_changes_priority_only": True,
                "never_expands_execution_authority": True,
            },
            "context_status": "fallback",
            "error": f"{type(exc).__name__}: {exc}",
            "generated_at": time.time(),
            "boundary": {
                "read_only_context": True,
                "does_not_apply_policy_suggestion": True,
                "does_not_expand_authority": True,
            },
        }
