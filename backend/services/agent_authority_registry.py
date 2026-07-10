"""Legacy re-export — consolidated into agent_authority.py."""
from backend.services.agent_authority import (  # noqa: F401
    AgentAuthorityRegistryService,
    evaluate, evaluate_scope_write, control_surface, canonical_source,
    source_contract, required_gate, authority_state,
    infer_policy_suggestion_source_agent, policy_suggestion_requested_writes,
    AGENTS, SYSTEM_SOURCES, SOURCE_ALIASES, REGISTRY_VERSION,
)
