"""Agent Authority Registry — simplified permission contract.

Reduced from 540 lines to ~150 lines by removing boilerplate serialization
methods (list_agents, get_agent, status, boundary, _append_findings) that
were only used by API endpoints and readiness.

Core logic preserved: evaluate(), control_surface(), required_gate(),
authority_state(), canonical_source(), infer_policy_suggestion_source_agent().
"""
from __future__ import annotations

from typing import Any

from backend.services._brain_helpers import as_list, text


REGISTRY_VERSION = "agent_authority_registry.v1"

AGENTS: dict[str, dict[str, Any]] = {
    "v16_brain": {
        "source_kind": "brain_medium_impact_governance",
        "capability_scope": "medium_impact_governance",
        "allowed_writes": ["brain_action_plan", "brain_action_plan_eval",
                           "brain_governance_candidate", "brain_medium_impact_governance", "brain_memory",
                           "brain_state_snapshot", "v16_brain_command"],
        "control_surfaces": ["proposal_governance", "entry_quality", "factor_weight", "parameter_template",
                             "position_supervisor_template", "context_policy", "replay", "memory"],
        # V16 can select and route a candidate, but the policy queue remains a
        # governed bridge owned by the downstream system.  Keeping this field
        # as manual_bridge_only preserves the source-agent contract: V16 never
        # receives direct policy_suggestion write authority.
        "policy_suggestion_write": "manual_bridge_only",
        "bridge_mode": "governed_system_bridge",
        "delegation_targets": {
            "autonomous_learning": {
                "owns": ["entry_quality", "entry_parameters", "context_policy", "learning_application"],
            },
            "factor_governance": {
                "owns": ["factor_weight", "factor_catalog_governance"],
                "delegates_execution": ["parameter_template", "context_policy"],
                "delegated_execution_owner": "autonomous_learning",
            },
            "position_supervisor_governance": {
                "owns": ["position_supervisor_template", "supervisor_protection_policy"],
            },
        },
        "authority_state": "requires_control_gate",
        "forbidden_actions": ["submit_order", "apply_runtime_overlay", "apply_factor_weight"],
    },
    "autonomous_learning": {
        "source_kind": "rule_evolution_governor",
        "capability_scope": "legacy_policy_governance",
        "allowed_writes": ["policy_suggestion", "evolution_run", "learning_application_log", "experience_memory"],
        "control_surfaces": ["entry_quality", "factor_weight", "parameter_template", "context_policy", "memory"],
        "execution_owner": ["entry_quality", "parameter_template", "context_policy", "learning_application"],
        "receives_handoffs_from": ["factor_governance", "v16_brain"],
        "policy_suggestion_write": "native",
        "requires_v16_command": True,
        "risk_reduction_exception": "rollback_or_reduce_only",
        "authority_state": "requires_control_gate",
        "forbidden_actions": ["submit_order", "bypass_risk_policy"],
    },
    "factor_governance": {
        "source_kind": "factor_governance_orchestrator",
        "capability_scope": "factor_catalog_runtime_governance",
        "allowed_writes": ["policy_suggestion", "factor_catalog", "runtime_config_overlay",
                           "evolution_decision", "learning_application_log"],
        "control_surfaces": ["factor_weight", "parameter_template", "runtime_config", "model_stage"],
        "execution_owner": ["factor_weight", "factor_catalog_governance", "model_stage"],
        "delegated_execution_owner": {"parameter_template": "autonomous_learning", "context_policy": "autonomous_learning"},
        "policy_suggestion_write": "native_governed",
        "requires_v16_command": True,
        "risk_reduction_exception": "rollback_or_reduce_only",
        "authority_state": "requires_control_gate",
        "forbidden_actions": ["submit_order", "bypass_decision_policy", "bypass_risk_policy"],
    },
    "position_supervisor_governance": {
        "source_kind": "position_supervisor_governance",
        "capability_scope": "position_supervisor_template_governance",
        "allowed_writes": ["runtime_config_overlay", "runtime_config_snapshot",
                           "learning_application_log", "evolution_decision"],
        "control_surfaces": ["position_supervisor_template"],
        "execution_owner": ["position_supervisor_template"],
        "policy_suggestion_write": "native_governed",
        "requires_v16_command": True,
        "risk_reduction_exception": "rollback_or_reduce_only",
        "authority_state": "requires_control_gate",
        "forbidden_actions": ["submit_order", "bypass_risk_policy"],
    },
    "factor_pruning_governance": {
        "source_kind": "factor_pruning_candidate_materializer",
        "capability_scope": "factor_catalog_runtime_governance",
        "allowed_writes": ["brain_governance_candidate"],
        "control_surfaces": ["factor_weight", "proposal_governance"],
        "policy_suggestion_write": "manual_bridge_only",
        "requires_v16_command": False,
        "authority_state": "requires_control_gate",
        "forbidden_actions": ["write_policy_suggestion_directly", "apply_factor_weight", "submit_order"],
    },
    "llm_advisory": {
        "source_kind": "llm_advisory_service",
        "capability_scope": "explanation_review_only",
        "allowed_writes": ["llm_advisory_audit"],
        "control_surfaces": ["macro_advisory", "proposal_governance", "factor_weight", "context_policy"],
        "policy_suggestion_write": "never_direct",
        "requires_v16_command": False,
        "authority_state": "advisory_only",
        "forbidden_actions": ["write_policy_suggestion", "apply_runtime_overlay", "submit_order", "approve_proposal"],
    },
    "lightgbm_shadow_models": {
        "source_kind": "model_shadow_audit",
        "capability_scope": "shadow_or_advisory",
        "allowed_writes": ["open_quality_shadow_audit", "position_quality_shadow_audit",
                           "factor_governance_shadow_audit"],
        "control_surfaces": ["model_stage", "factor_weight", "trade_execution", "position_sizing"],
        "policy_suggestion_write": "only_through_existing_governance_services",
        "requires_v16_command": False,
        "authority_state": "no_execution_authority",
        "forbidden_actions": ["write_policy_suggestion_directly", "apply_model_stage", "submit_order"],
    },
}

SYSTEM_SOURCES: dict[str, dict[str, Any]] = {
    "policy_suggestion": {
        "source_kind": "legacy_policy_queue", "capability_scope": "legacy_read_model",
        "allowed_writes": ["policy_suggestion"], "control_surfaces": ["factor_weight", "parameter_template", "context_policy"],
        "policy_suggestion_write": "legacy", "authority_state": "requires_control_gate",
        "forbidden_actions": ["submit_order", "bypass_risk_policy"],
    },
    "live_autonomy": {
        "source_kind": "live_autonomy_control_service", "capability_scope": "incident_tightening_only",
        "allowed_writes": ["live_autonomy_unlock_event", "runtime_incident_control"],
        "control_surfaces": ["incident_control", "live_autonomy"],
        "policy_suggestion_write": "never_direct", "authority_state": "requires_control_gate",
        "forbidden_actions": ["submit_order", "loosen_runtime_incident_mode"],
    },
}

SOURCE_ALIASES = {
    "open_quality_model": "lightgbm_shadow_models",
    "position_quality_model": "lightgbm_shadow_models",
    "factor_governance_model": "lightgbm_shadow_models",
    "evolution_decision": "factor_governance",
}


def canonical_source(source_agent: str) -> str:
    return SOURCE_ALIASES.get(text(source_agent, "unknown"), text(source_agent, "unknown"))


def source_contract(source_agent: str) -> dict[str, Any] | None:
    source = canonical_source(source_agent)
    return AGENTS.get(source) or SYSTEM_SOURCES.get(source)


def control_surface(scope_type: str, action: str) -> str:
    s = text(scope_type).lower()
    a = text(action).lower()
    if "supervisor" in s or "supervisor" in a:
        return "position_supervisor_template"
    if "parameter" in s or "template" in a:
        return "parameter_template"
    if "factor" in s or "weight" in a or a in {"update_weight", "downweight", "boost"}:
        return "factor_weight"
    if "context" in s or "context" in a:
        return "context_policy"
    if "incident" in s or "incident" in a:
        return "incident_control"
    if "model" in s or "model" in a:
        return "model_stage"
    if "replay" in s or "replay" in a:
        return "replay"
    if "memory" in s or "memory" in a:
        return "memory"
    return s or a or "unknown"


def required_gate(control_surface_name: str, action: str, source_agent: str) -> list[str]:
    source = canonical_source(source_agent)
    surface = text(control_surface_name, "unknown")
    if source in {"llm_advisory", "lightgbm_shadow_models"}:
        return ["advisory_only"]
    if surface == "factor_weight":
        return ["DecisionPolicy", "RiskPolicyService"]
    if surface in {"entry_quality", "parameter_template", "position_supervisor_template", "context_policy",
                   "incident_control", "model_stage", "replay"}:
        return ["RiskPolicyService"]
    if surface in {"trade_execution", "position_sizing", "broker_order"}:
        return ["RiskPolicyService", "ExecutionGate"]
    return ["review"]


def execution_owner(control_surface_name: str) -> str:
    surface = text(control_surface_name, "unknown")
    for source_agent, contract in AGENTS.items():
        if surface in set(contract.get("execution_owner") or []):
            return source_agent
    return ""


def authority_state(*, source_agent: str, status: str = "", required_gate_list: list[str] | None = None,
                    impact_level: str = "", violations: list[str] | None = None) -> str:
    source = canonical_source(source_agent)
    if source in {"llm_advisory", "lightgbm_shadow_models"}:
        return "advisory_only"
    if violations:
        return "blocked_by_agent_authority"
    ns = text(status).lower()
    if ns in {"applied", "rolled_back"}:
        return "already_executed_audit"
    if text(impact_level).lower() in {"observe", "shadow"}:
        return "no_execution_authority"
    gate_list = required_gate_list or []
    if "RiskPolicyService" in gate_list or "DecisionPolicy" in gate_list:
        return "requires_control_gate"
    return "review_only"


def evaluate(source_agent: str, control_surface_name: str, action: str,
             requested_writes: list[str] | tuple[str, ...] | str | None = None,
             *, status: str = "", impact_level: str = "") -> dict[str, Any]:
    source = canonical_source(source_agent)
    contract = source_contract(source)
    surface = text(control_surface_name, "unknown")
    action_text = text(action, surface)
    writes = as_list(requested_writes)
    violations: list[str] = []
    if not contract:
        violations.append("unknown_source_agent")
    allowed_writes_set = set(contract.get("allowed_writes") or []) if contract else set()
    for w in writes:
        if w and w not in allowed_writes_set:
            violations.append(f"write_not_allowed:{w}")
    if surface in {"trade_execution", "position_sizing", "broker_order"}:
        violations.append(f"direct_execution_surface_forbidden:{surface}")
    if source == "llm_advisory" and surface not in {"macro_advisory", "proposal_governance"} and "llm_advisory_audit" not in writes:
        violations.append(f"llm_advisory_executable_surface_forbidden:{surface}")
    gates = required_gate(surface, action_text, source)
    adv_state = authority_state(source_agent=source, status=status, required_gate_list=gates,
                                impact_level=impact_level, violations=violations)
    advisory = source == "llm_advisory" or "advisory_only" in gates
    return {
        "schema_version": "agent_authority_verdict.v1", "registry_version": REGISTRY_VERSION,
        "source_agent": text(source_agent, "unknown"), "canonical_source_agent": source,
        "registered": source in AGENTS, "system_source": source in SYSTEM_SOURCES,
        "control_surface": surface, "action": action_text, "required_gate": gates,
        "authority_state": adv_state, "allowed": bool(contract) and not violations and not advisory,
        "violations": violations,
        "manual_bridge_required": (contract or {}).get("policy_suggestion_write") == "manual_bridge_only",
        "advisory_only": advisory,
        "allowed_writes": sorted(allowed_writes_set) if contract else [],
        "forbidden_actions": list((contract or {}).get("forbidden_actions") or []),
    }


def evaluate_scope_write(source_agent: str, scope_type: str, action: str,
                         requested_writes: list[str] | tuple[str, ...] | str | None = None,
                         *, status: str = "", impact_level: str = "") -> dict[str, Any]:
    return evaluate(source_agent, control_surface(scope_type, action), action,
                    requested_writes=requested_writes, status=status, impact_level=impact_level)


def policy_suggestion_requested_writes(source_agent: str, evidence: dict[str, Any]) -> list[str]:
    source = canonical_source(source_agent)
    contract = source_contract(source)
    bridge = evidence.get("bridge") if isinstance(evidence, dict) else {}
    if (isinstance(bridge, dict) and bridge.get("manual_only") is True
            and (contract or {}).get("policy_suggestion_write") == "manual_bridge_only"):
        return []
    # In demo_nursery the existing governance service may perform the bridge
    # automatically.  The source agent still does not receive a direct
    # policy_suggestion write; the system actor is audited as the bridge.
    if (
        isinstance(bridge, dict)
        and bridge.get("automatic_demo") is True
        and bridge.get("demo_nursery") is True
        and str(bridge.get("actor") or "").startswith(
            (
                "system:autonomous_demo_nursery",
                "system:autonomous_demo_apply_stepper",
                "system:autonomous_learning.demo_nursery",
                "system:factor_pruning_governance.demo_nursery",
            )
        )
        and (contract or {}).get("policy_suggestion_write") == "manual_bridge_only"
    ):
        return []
    if (isinstance(evidence, dict) and evidence.get("advisory_only") is True
            and (contract or {}).get("policy_suggestion_write") == "only_through_existing_governance_services"):
        return []
    return ["policy_suggestion"]


def infer_policy_suggestion_source_agent(evidence: dict[str, Any], *, scope_type: str = "", action: str = "") -> str:
    if not isinstance(evidence, dict):
        return "autonomous_learning"
    explicit = text(evidence.get("source_agent"), "")
    if explicit:
        return explicit
    schema = text(evidence.get("schema_version"), "").lower()
    model_type = text(evidence.get("model_type"), "").lower()
    if schema == "factor_governance_advisory.v1":
        return "lightgbm_shadow_models"
    if "lightgbm" in model_type or model_type == "factor_governance":
        return "lightgbm_shadow_models"
    return "autonomous_learning"


# Backward-compatible class wrapper (same signature as old AgentAuthorityRegistryService)
class AgentAuthorityRegistryService:
    """Agent authority permission contract. Delegates to module-level functions."""

    AGENTS = AGENTS
    SYSTEM_SOURCES = SYSTEM_SOURCES
    SOURCE_ALIASES = SOURCE_ALIASES

    @classmethod
    def canonical_source(cls, source_agent: str) -> str:
        return canonical_source(source_agent)

    @classmethod
    def control_surface(cls, scope_type: str, action: str) -> str:
        return control_surface(scope_type, action)

    @classmethod
    def required_gate(cls, control_surface_name: str, action: str, source_agent: str) -> list[str]:
        return required_gate(control_surface_name, action, source_agent)

    @classmethod
    def execution_owner(cls, control_surface_name: str) -> str:
        return execution_owner(control_surface_name)

    @classmethod
    def authority_state(cls, *, source_agent: str, status: str = "", required_gate: list[str] | None = None,
                        impact_level: str = "", violations: list[str] | None = None) -> str:
        return authority_state(source_agent=source_agent, status=status, required_gate_list=required_gate,
                               impact_level=impact_level, violations=violations)

    def evaluate(self, source_agent: str, control_surface_name: str, action: str,
                 requested_writes: list[str] | tuple[str, ...] | str | None = None,
                 *, status: str = "", impact_level: str = "") -> dict[str, Any]:
        return evaluate(source_agent, control_surface_name, action, requested_writes, status=status, impact_level=impact_level)

    def evaluate_scope_write(self, source_agent: str, scope_type: str, action: str,
                             requested_writes: list[str] | tuple[str, ...] | str | None = None,
                             *, status: str = "", impact_level: str = "") -> dict[str, Any]:
        return evaluate_scope_write(source_agent, scope_type, action, requested_writes, status=status, impact_level=impact_level)

    def list_agents(self) -> dict[str, Any]:
        return {"ok": True, "schema_version": REGISTRY_VERSION, "registry_version": REGISTRY_VERSION,
                "registered_agents": len(self.AGENTS),
                "sources": [{"source_agent": k, **v} for k, v in sorted(self.AGENTS.items())],
                "system_sources": [{"source_agent": k, **v} for k, v in sorted(self.SYSTEM_SOURCES.items())],
                "aliases": dict(sorted(self.SOURCE_ALIASES.items())), "boundary": {"read_only": True}}

    def get_agent(self, source_agent: str) -> dict[str, Any]:
        canonical = canonical_source(source_agent)
        contract = source_contract(source_agent)
        if not contract:
            return {"ok": False, "schema_version": REGISTRY_VERSION, "source_agent": text(source_agent, "unknown"),
                    "canonical_source_agent": canonical, "registered": False, "authority_state": "review_only"}
        return {"ok": True, "schema_version": REGISTRY_VERSION, "source_agent": text(source_agent, "unknown"),
                "canonical_source_agent": canonical, "registered": canonical in self.AGENTS,
                "system_source": canonical in self.SYSTEM_SOURCES, **contract}

    def status(self, *, db_path: str | Path | None = None, limit: int = 500) -> dict[str, Any]:
        """Audit DB tables for unknown sources and contract violations."""
        from pathlib import Path
        from backend.core.db import STATE_DB, state_table_exists
        from backend.services._brain_helpers import connect as _s_connect, execute as _s_execute, loads as _s_loads

        db = str(db_path) if db_path is not None else STATE_DB
        limit = max(1, min(int(limit), 1000))
        unknown_sources: list[dict[str, Any]] = []
        contract_violations: list[dict[str, Any]] = []

        try:
            conn = _s_connect(db, read_only=True)
        except Exception:
            return {"ok": False, "schema_version": "agent_authority_status.v1", "status": "error",
                    "registry_version": REGISTRY_VERSION, "registered_agents": len(self.AGENTS),
                    "registered_agent_ids": sorted(self.AGENTS), "system_source_ids": sorted(self.SYSTEM_SOURCES),
                    "unknown_sources": [], "contract_violations": [], "boundary": {"read_only": True}}
        try:
            if state_table_exists(conn, "proposal_registry"):
                rows = _s_execute(conn, """SELECT proposal_id, source_agent, control_surface, proposal_type,
                    required_gate_json, authority_state, status, updated_at
                    FROM proposal_registry ORDER BY updated_at DESC LIMIT ?""", (limit,)).fetchall()
                for row in rows:
                    verdict = evaluate(
                        row["source_agent"], str(row["control_surface"] or ""), str(row["proposal_type"] or ""),
                        status=str(row["status"] or ""))
                    self._append_findings(ref_type="proposal_registry", ref_id=row["proposal_id"],
                                          verdict=verdict, unknown_sources=unknown_sources, violations=contract_violations)
                    gates = _s_loads(row["required_gate_json"], [])
                    if verdict.get("advisory_only") and gates != ["advisory_only"]:
                        contract_violations.append({
                            "source_ref_type": "proposal_registry", "source_ref_id": row["proposal_id"],
                            "source_agent": row["source_agent"],
                            "reason": "advisory_source_gate_must_be_advisory_only",
                        })

            if state_table_exists(conn, "brain_governance_candidate"):
                rows = _s_execute(conn, """SELECT candidate_id, source_agent, scope_type, action, status
                    FROM brain_governance_candidate ORDER BY created_at DESC LIMIT ?""", (limit,)).fetchall()
                for row in rows:
                    surface = control_surface(str(row["scope_type"] or ""), str(row["action"] or ""))
                    verdict = evaluate(row["source_agent"], surface, str(row["action"] or ""),
                                       requested_writes=["brain_governance_candidate"], status=str(row["status"] or ""))
                    self._append_findings(ref_type="brain_governance_candidate", ref_id=row["candidate_id"],
                                          verdict=verdict, unknown_sources=unknown_sources, violations=contract_violations)

            if state_table_exists(conn, "policy_suggestion"):
                rows = _s_execute(conn, """SELECT suggestion_id, scope_type, action, evidence_json, status
                    FROM policy_suggestion ORDER BY created_at DESC LIMIT ?""", (limit,)).fetchall()
                for row in rows:
                    evidence = _s_loads(row["evidence_json"], {})
                    src = infer_policy_suggestion_source_agent(evidence, scope_type=str(row["scope_type"] or ""),
                                                               action=str(row["action"] or ""))
                    if not src:
                        continue
                    surface = control_surface(str(row["scope_type"] or ""), str(row["action"] or ""))
                    verdict = evaluate(src, surface, str(row["action"] or ""),
                                       requested_writes=policy_suggestion_requested_writes(src, evidence),
                                       status=str(row["status"] or ""))
                    self._append_findings(ref_type="policy_suggestion", ref_id=row["suggestion_id"],
                                          verdict=verdict, unknown_sources=unknown_sources, violations=contract_violations)
        finally:
            try:
                conn.close()
            except Exception:
                pass
        st = "ok" if not unknown_sources and not contract_violations else "degraded"
        return {"ok": st == "ok", "schema_version": "agent_authority_status.v1", "status": st,
                "registry_version": REGISTRY_VERSION, "registered_agents": len(self.AGENTS),
                "registered_agent_ids": sorted(self.AGENTS), "system_source_ids": sorted(self.SYSTEM_SOURCES),
                "unknown_sources": unknown_sources, "contract_violations": contract_violations,
                "boundary": {"read_only": True}}

    @staticmethod
    def _append_findings(*, ref_type: str, ref_id: str, verdict: dict[str, Any],
                         unknown_sources: list[dict[str, Any]], violations: list[dict[str, Any]]) -> None:
        if "unknown_source_agent" in verdict.get("violations", []):
            unknown_sources.append({"source_ref_type": ref_type, "source_ref_id": ref_id,
                                     "source_agent": verdict.get("source_agent")})
        for v in verdict.get("violations", []):
            if v == "unknown_source_agent":
                continue
            violations.append({"source_ref_type": ref_type, "source_ref_id": ref_id,
                                "source_agent": verdict.get("source_agent"),
                                "control_surface": verdict.get("control_surface"), "reason": v})

    @staticmethod
    def _source_contract(source_agent: str) -> dict[str, Any] | None:
        return source_contract(source_agent)

    @staticmethod
    def _canonical_source(source_agent: str) -> str:
        return canonical_source(source_agent)
