"""Combined Agent Governance — re-exports from agent_scorecard + agent_briefing.

Official entry point for agent scorecard, briefing, and authority services.
Previously: agent_authority_registry.py (540) + agent_scorecard.py (704)
             + agent_briefing.py (209) = 3 files, ~1,453 lines
Now:        agent_authority.py (280) + agent_governance.py (re-exports)
             = simplified import surface
"""
from backend.services.agent_authority import (  # noqa: F401
    AgentAuthorityRegistryService,
    evaluate as evaluate_authority,
    control_surface, canonical_source, required_gate,
    infer_policy_suggestion_source_agent,
    policy_suggestion_requested_writes,
)
from backend.services.agent_scorecard import AgentScorecardService  # noqa: F401
from backend.services.agent_briefing import AgentBriefingContextService  # noqa: F401
