from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, state_table_exists
from backend.services.brain_action_planner import _connect, _execute, _loads


REGISTRY_VERSION = "agent_authority_registry.v1"


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, tuple):
        return [str(item) for item in value]
    if value in (None, ""):
        return []
    return [str(value)]


def policy_suggestion_requested_writes(source_agent: str, evidence: dict[str, Any]) -> list[str]:
    """Return write audit targets, treating approved manual bridges as bridge output."""
    source = AgentAuthorityRegistryService._canonical_source(source_agent)
    contract = AgentAuthorityRegistryService._source_contract(source)
    bridge = evidence.get("bridge") if isinstance(evidence, dict) else {}
    if (
        isinstance(bridge, dict)
        and bridge.get("manual_only") is True
        and (contract or {}).get("policy_suggestion_write") == "manual_bridge_only"
    ):
        return []
    if (
        isinstance(evidence, dict)
        and evidence.get("advisory_only") is True
        and (contract or {}).get("policy_suggestion_write") == "only_through_existing_governance_services"
    ):
        return []
    return ["policy_suggestion"]


def infer_policy_suggestion_source_agent(evidence: dict[str, Any], *, scope_type: str = "", action: str = "") -> str:
    """Infer source agent for older policy_suggestion rows that predate source_agent.

    This only classifies known advisory/shadow schemas. Unknown legacy rows keep
    the historical autonomous_learning default for compatibility.
    """
    if not isinstance(evidence, dict):
        return "autonomous_learning"
    explicit = _text(evidence.get("source_agent"), "")
    if explicit:
        return explicit
    schema = _text(evidence.get("schema_version"), "").lower()
    model_type = _text(evidence.get("model_type"), "").lower()
    scope = _text(scope_type).lower()
    act = _text(action).lower()
    if schema in {"factor_governance_advisory.v1", "meta_model_governance_advisory.v1"}:
        return "lightgbm_shadow_models"
    if "lightgbm" in model_type or model_type in {"factor_governance", "meta_model"}:
        return "lightgbm_shadow_models"
    if scope == "meta_model" or "meta_model" in act:
        return "lightgbm_shadow_models"
    return "autonomous_learning"


class AgentAuthorityRegistryService:
    """Machine-readable authority contract for autonomous agents.

    The registry is intentionally code-defined for v1: it adds one shared source
    of truth without adding a migration or giving any agent new execution power.
    """

    AGENTS: dict[str, dict[str, Any]] = {
        "v16_brain": {
            "source_kind": "brain_medium_impact_governance",
            "capability_scope": "medium_impact_governance",
            "allowed_writes": [
                "brain_action_plan",
                "brain_action_plan_eval",
                "brain_governance_candidate",
                "brain_medium_impact_governance",
                "brain_memory",
            ],
            "control_surfaces": [
                "proposal_governance",
                "factor_weight",
                "parameter_template",
                "position_supervisor_template",
                "context_policy",
                "replay",
                "memory",
            ],
            "policy_suggestion_write": "manual_bridge_only",
            "authority_state": "requires_control_gate",
            "forbidden_actions": ["submit_order", "apply_runtime_overlay", "apply_factor_weight"],
        },
        "autonomous_learning": {
            "source_kind": "rule_evolution_governor",
            "capability_scope": "legacy_policy_governance",
            "allowed_writes": ["policy_suggestion", "evolution_run", "learning_application_log", "experience_memory"],
            "control_surfaces": ["factor_weight", "parameter_template", "context_policy", "memory"],
            "policy_suggestion_write": "native",
            "authority_state": "requires_control_gate",
            "forbidden_actions": ["submit_order", "bypass_risk_policy"],
        },
        "factor_governance": {
            "source_kind": "factor_governance_orchestrator",
            "capability_scope": "factor_catalog_runtime_governance",
            "allowed_writes": [
                "policy_suggestion",
                "factor_catalog",
                "runtime_config_overlay",
                "evolution_decision",
                "learning_application_log",
            ],
            "control_surfaces": ["factor_weight", "parameter_template", "runtime_config", "model_stage"],
            "policy_suggestion_write": "native_governed",
            "authority_state": "requires_control_gate",
            "forbidden_actions": ["submit_order", "bypass_decision_policy", "bypass_risk_policy"],
        },
        "factor_pruning_governance": {
            "source_kind": "factor_pruning_candidate_materializer",
            "capability_scope": "factor_catalog_runtime_governance",
            "allowed_writes": ["brain_governance_candidate"],
            "control_surfaces": ["factor_weight", "proposal_governance"],
            "policy_suggestion_write": "manual_bridge_only",
            "authority_state": "requires_control_gate",
            "forbidden_actions": ["write_policy_suggestion_directly", "apply_factor_weight", "submit_order"],
        },
        "llm_advisory": {
            "source_kind": "llm_advisory_service",
            "capability_scope": "explanation_review_only",
            "allowed_writes": ["llm_advisory_audit"],
            "control_surfaces": ["macro_advisory", "proposal_governance", "factor_weight", "context_policy"],
            "policy_suggestion_write": "never_direct",
            "authority_state": "advisory_only",
            "forbidden_actions": ["write_policy_suggestion", "apply_runtime_overlay", "submit_order", "approve_proposal"],
        },
        "lightgbm_shadow_models": {
            "source_kind": "model_shadow_audit",
            "capability_scope": "shadow_or_advisory",
            "allowed_writes": [
                "open_quality_shadow_audit",
                "position_quality_shadow_audit",
                "factor_governance_shadow_audit",
                "meta_model_shadow_audit",
            ],
            "control_surfaces": ["model_stage", "factor_weight", "trade_execution", "position_sizing"],
            "policy_suggestion_write": "only_through_existing_governance_services",
            "authority_state": "no_execution_authority",
            "forbidden_actions": ["write_policy_suggestion_directly", "apply_model_stage", "submit_order"],
        },
    }

    SYSTEM_SOURCES: dict[str, dict[str, Any]] = {
        "policy_suggestion": {
            "source_kind": "legacy_policy_queue",
            "capability_scope": "legacy_read_model",
            "allowed_writes": ["policy_suggestion"],
            "control_surfaces": ["factor_weight", "parameter_template", "context_policy"],
            "policy_suggestion_write": "legacy",
            "authority_state": "requires_control_gate",
            "forbidden_actions": ["submit_order", "bypass_risk_policy"],
        },
        "live_autonomy": {
            "source_kind": "live_autonomy_control_service",
            "capability_scope": "incident_tightening_only",
            "allowed_writes": ["live_autonomy_unlock_event", "runtime_incident_control"],
            "control_surfaces": ["incident_control", "live_autonomy"],
            "policy_suggestion_write": "never_direct",
            "authority_state": "requires_control_gate",
            "forbidden_actions": ["submit_order", "loosen_runtime_incident_mode"],
        },
    }

    SOURCE_ALIASES = {
        "open_quality_model": "lightgbm_shadow_models",
        "position_quality_model": "lightgbm_shadow_models",
        "factor_governance_model": "lightgbm_shadow_models",
        "meta_model": "lightgbm_shadow_models",
        "evolution_decision": "factor_governance",
    }

    @classmethod
    def control_surface(cls, scope_type: str, action: str) -> str:
        scope = _text(scope_type).lower()
        act = _text(action).lower()
        if "supervisor" in scope or "supervisor" in act:
            return "position_supervisor_template"
        if "parameter" in scope or "template" in act:
            return "parameter_template"
        if "factor" in scope or "weight" in act or act in {"update_weight", "downweight", "boost"}:
            return "factor_weight"
        if "context" in scope or "context" in act:
            return "context_policy"
        if "incident" in scope or "incident" in act:
            return "incident_control"
        if "model" in scope or "model" in act:
            return "model_stage"
        if "replay" in scope or "replay" in act:
            return "replay"
        if "memory" in scope or "memory" in act:
            return "memory"
        return scope or act or "unknown"

    @classmethod
    def _canonical_source(cls, source_agent: str) -> str:
        source = _text(source_agent, "unknown")
        return cls.SOURCE_ALIASES.get(source, source)

    @classmethod
    def canonical_source(cls, source_agent: str) -> str:
        return cls._canonical_source(source_agent)

    @classmethod
    def _source_contract(cls, source_agent: str) -> dict[str, Any] | None:
        source = cls._canonical_source(source_agent)
        return cls.AGENTS.get(source) or cls.SYSTEM_SOURCES.get(source)

    def evaluate_scope_write(
        self,
        source_agent: str,
        scope_type: str,
        action: str,
        requested_writes: list[str] | tuple[str, ...] | str | None = None,
        *,
        status: str = "",
        impact_level: str = "",
    ) -> dict[str, Any]:
        return self.evaluate(
            source_agent,
            self.control_surface(scope_type, action),
            action,
            requested_writes=requested_writes,
            status=status,
            impact_level=impact_level,
        )

    def list_agents(self) -> dict[str, Any]:
        return {
            "ok": True,
            "schema_version": REGISTRY_VERSION,
            "registry_version": REGISTRY_VERSION,
            "registered_agents": len(self.AGENTS),
            "sources": [
                {"source_agent": source_agent, **contract}
                for source_agent, contract in sorted(self.AGENTS.items())
            ],
            "system_sources": [
                {"source_agent": source_agent, **contract}
                for source_agent, contract in sorted(self.SYSTEM_SOURCES.items())
            ],
            "aliases": dict(sorted(self.SOURCE_ALIASES.items())),
            "boundary": self.boundary(),
        }

    def get_agent(self, source_agent: str) -> dict[str, Any]:
        canonical = self._canonical_source(source_agent)
        contract = self._source_contract(source_agent)
        if not contract:
            return {
                "ok": False,
                "schema_version": REGISTRY_VERSION,
                "source_agent": _text(source_agent, "unknown"),
                "canonical_source_agent": canonical,
                "registered": False,
                "authority_state": "review_only",
                "boundary": self.boundary(),
            }
        return {
            "ok": True,
            "schema_version": REGISTRY_VERSION,
            "source_agent": _text(source_agent, "unknown"),
            "canonical_source_agent": canonical,
            "registered": canonical in self.AGENTS,
            "system_source": canonical in self.SYSTEM_SOURCES,
            **contract,
            "boundary": self.boundary(),
        }

    def evaluate(
        self,
        source_agent: str,
        control_surface: str,
        action: str,
        requested_writes: list[str] | tuple[str, ...] | str | None = None,
        *,
        status: str = "",
        impact_level: str = "",
    ) -> dict[str, Any]:
        source = self._canonical_source(source_agent)
        contract = self._source_contract(source)
        surface = _text(control_surface, "unknown")
        action_text = _text(action, surface)
        writes = _list(requested_writes)
        violations: list[str] = []
        registered = source in self.AGENTS
        system_source = source in self.SYSTEM_SOURCES
        if not contract:
            violations.append("unknown_source_agent")
        allowed_writes = set(contract.get("allowed_writes") or []) if contract else set()
        for write in writes:
            if write and write not in allowed_writes:
                violations.append(f"write_not_allowed:{write}")
        if surface in {"trade_execution", "position_sizing", "broker_order"}:
            violations.append(f"direct_execution_surface_forbidden:{surface}")
        if source == "llm_advisory" and surface not in {"macro_advisory", "proposal_governance"} and "llm_advisory_audit" not in writes:
            violations.append(f"llm_advisory_executable_surface_forbidden:{surface}")

        gates = self.required_gate(surface, action_text, source)
        authority_state = self.authority_state(
            source_agent=source,
            status=status,
            required_gate=gates,
            impact_level=impact_level,
            violations=violations,
        )
        advisory_only = source == "llm_advisory" or "advisory_only" in gates
        allowed = bool(contract) and not violations and not advisory_only
        manual_bridge_required = (contract or {}).get("policy_suggestion_write") == "manual_bridge_only"
        return {
            "schema_version": "agent_authority_verdict.v1",
            "registry_version": REGISTRY_VERSION,
            "source_agent": _text(source_agent, "unknown"),
            "canonical_source_agent": source,
            "registered": registered,
            "system_source": system_source,
            "control_surface": surface,
            "action": action_text,
            "required_gate": gates,
            "authority_state": authority_state,
            "allowed": allowed,
            "violations": violations,
            "manual_bridge_required": manual_bridge_required,
            "advisory_only": advisory_only,
            "allowed_writes": sorted(allowed_writes),
            "forbidden_actions": list((contract or {}).get("forbidden_actions") or []),
        }

    def required_gate(self, control_surface: str, action: str, source_agent: str) -> list[str]:
        source = self._canonical_source(source_agent)
        surface = _text(control_surface, "unknown")
        if source in {"llm_advisory", "lightgbm_shadow_models"}:
            return ["advisory_only"]
        if surface == "factor_weight":
            return ["DecisionPolicy", "RiskPolicyService"]
        if surface in {"parameter_template", "position_supervisor_template", "context_policy", "incident_control", "model_stage", "replay"}:
            return ["RiskPolicyService"]
        if surface in {"trade_execution", "position_sizing", "broker_order"}:
            return ["RiskPolicyService", "ExecutionGate"]
        return ["review"]

    def authority_state(
        self,
        *,
        source_agent: str,
        status: str,
        required_gate: list[str],
        impact_level: str,
        violations: list[str] | None = None,
    ) -> str:
        source = self._canonical_source(source_agent)
        normalized_status = _text(status).lower()
        impact = _text(impact_level).lower()
        if source in {"llm_advisory", "lightgbm_shadow_models"}:
            return "advisory_only"
        if violations:
            return "blocked_by_agent_authority"
        if normalized_status in {"applied", "rolled_back"}:
            return "already_executed_audit"
        if impact in {"observe", "shadow"}:
            return "no_execution_authority"
        if "RiskPolicyService" in required_gate or "DecisionPolicy" in required_gate:
            return "requires_control_gate"
        return "review_only"

    def status(self, *, db_path: str | Path = STATE_DB, limit: int = 500) -> dict[str, Any]:
        unknown_sources: list[dict[str, Any]] = []
        contract_violations: list[dict[str, Any]] = []
        limit = max(1, min(int(limit), 1000))
        conn = _connect(db_path, read_only=True)
        try:
            if state_table_exists(conn, "proposal_registry"):
                rows = _execute(
                    conn,
                    """
                    SELECT proposal_id, source_agent, control_surface, proposal_type,
                           required_gate_json, authority_state, status, updated_at
                    FROM proposal_registry
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                for row in rows:
                    verdict = self.evaluate(
                        row["source_agent"],
                        row["control_surface"],
                        row["proposal_type"],
                        status=row["status"],
                    )
                    self._append_findings(
                        source_ref_type="proposal_registry",
                        source_ref_id=row["proposal_id"],
                        verdict=verdict,
                        unknown_sources=unknown_sources,
                        contract_violations=contract_violations,
                    )
                    gates = _loads(row["required_gate_json"], [])
                    if verdict.get("advisory_only") and gates != ["advisory_only"]:
                        contract_violations.append(
                            {
                                "source_ref_type": "proposal_registry",
                                "source_ref_id": row["proposal_id"],
                                "source_agent": row["source_agent"],
                                "reason": "advisory_source_gate_must_be_advisory_only",
                            }
                        )
            if state_table_exists(conn, "brain_governance_candidate"):
                rows = _execute(
                    conn,
                    """
                    SELECT candidate_id, source_agent, scope_type, action, status
                    FROM brain_governance_candidate
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                for row in rows:
                    surface = self.control_surface(row["scope_type"], row["action"])
                    verdict = self.evaluate(
                        row["source_agent"],
                        surface,
                        row["action"],
                        requested_writes=["brain_governance_candidate"],
                        status=row["status"],
                    )
                    self._append_findings(
                        source_ref_type="brain_governance_candidate",
                        source_ref_id=row["candidate_id"],
                        verdict=verdict,
                        unknown_sources=unknown_sources,
                        contract_violations=contract_violations,
                    )
            if state_table_exists(conn, "policy_suggestion"):
                rows = _execute(
                    conn,
                    """
                    SELECT suggestion_id, scope_type, action, evidence_json, status
                    FROM policy_suggestion
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                for row in rows:
                    evidence = _loads(row["evidence_json"], {})
                    source_agent = infer_policy_suggestion_source_agent(evidence, scope_type=row["scope_type"], action=row["action"])
                    if not source_agent:
                        continue
                    surface = self.control_surface(row["scope_type"], row["action"])
                    verdict = self.evaluate(
                        source_agent,
                        surface,
                        row["action"],
                        requested_writes=policy_suggestion_requested_writes(source_agent, evidence),
                        status=row["status"],
                    )
                    self._append_findings(
                        source_ref_type="policy_suggestion",
                        source_ref_id=row["suggestion_id"],
                        verdict=verdict,
                        unknown_sources=unknown_sources,
                        contract_violations=contract_violations,
                    )
        finally:
            conn.close()
        status = "ok" if not unknown_sources and not contract_violations else "degraded"
        return {
            "ok": status == "ok",
            "schema_version": "agent_authority_status.v1",
            "status": status,
            "registry_version": REGISTRY_VERSION,
            "registered_agents": len(self.AGENTS),
            "registered_agent_ids": sorted(self.AGENTS),
            "system_source_ids": sorted(self.SYSTEM_SOURCES),
            "unknown_sources": unknown_sources,
            "contract_violations": contract_violations,
            "boundary": self.boundary(),
        }

    @staticmethod
    def _append_findings(
        *,
        source_ref_type: str,
        source_ref_id: str,
        verdict: dict[str, Any],
        unknown_sources: list[dict[str, Any]],
        contract_violations: list[dict[str, Any]],
    ) -> None:
        if "unknown_source_agent" in verdict.get("violations", []):
            unknown_sources.append(
                {
                    "source_ref_type": source_ref_type,
                    "source_ref_id": source_ref_id,
                    "source_agent": verdict.get("source_agent"),
                }
            )
        for violation in verdict.get("violations", []):
            if violation == "unknown_source_agent":
                continue
            contract_violations.append(
                {
                    "source_ref_type": source_ref_type,
                    "source_ref_id": source_ref_id,
                    "source_agent": verdict.get("source_agent"),
                    "control_surface": verdict.get("control_surface"),
                    "reason": violation,
                }
            )

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {
            "schema_version": "agent_authority_boundary.v1",
            "code_defined_contract": True,
            "does_not_submit_orders": True,
            "does_not_apply_runtime_mutations": True,
            "does_not_expand_agent_authority": True,
            "unknown_sources_review_only": True,
        }
