from __future__ import annotations

import ast
import json
import time
from pathlib import Path
from typing import Any

from backend.core.db import (
    DUCKDB_EXTERNAL,
    STATE_DB,
    connect_duckdb,
    connect_sqlite,
    get_state_pg_conn,
    is_state_db_path,
    state_pg_enabled,
    state_table_columns,
    state_table_exists,
)
from backend.services.agent_authority import AgentAuthorityRegistryService
from backend.services.agent_governance import AgentBriefingContextService, AgentScorecardService
from backend.services.autonomous_evolution_cycle import AutonomousEvolutionCycleService
from backend.services.meta_governance import MetaGovernanceService
from backend.services.proposal_registry import ProposalRegistryService
from backend.services.review_contract import review_has_system_contamination
from backend.services.stability import measure, record_timing, timing_snapshot
from research.meta_model_lightgbm import MetaModelLightGBMService


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
LOG_PATH = PROJECT_ROOT / "logs" / "backend_uvicorn.log"

KNOWN_OBSERVATION_COMPONENTS = {
    "disk_space": "known_disk_space_degraded",
    "bar_m1": "m1_data_feed_observation",
}
BLOCKING_COMPONENTS = {
    "ctrader_bridge",
    "live_loop",
    "db_ctrader_data",
}


def _loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _table_exists(conn: Any, table: str) -> bool:
    return state_table_exists(conn, table)


def _use_pg(db_path: str | Path = STATE_DB) -> bool:
    return is_state_db_path(db_path)


def _connect_state(db_path: str | Path = STATE_DB):
    conn = get_state_pg_conn(read_only=True) if _use_pg(db_path) else connect_sqlite(db_path, read_only=True)
    if not _use_pg(db_path):
        conn.row_factory = __import__("sqlite3").Row
    return conn


def _conn_is_pg(conn) -> bool:
    return conn.__class__.__module__.split(".", 1)[0] == "psycopg"


def _sql(conn, sql: str) -> str:
    return sql.replace("%", "%%").replace("?", "%s") if _conn_is_pg(conn) else sql


def _execute(conn, sql: str, params: Any = None):
    if params is None:
        return conn.execute(_sql(conn, sql))
    return conn.execute(_sql(conn, sql), params)


class BackendReadinessService:
    """Aggregated backend contract for the mini-program/backend handoff."""

    def __init__(self, *, db_path: str | Path = STATE_DB):
        self.db_path = Path(db_path)

    def build(self) -> dict[str, Any]:
        build_started = time.perf_counter()
        live_status = self._timed_component("live_status", self._live_status)
        market_session = dict(live_status.get("market_session") or {})
        system_health = self._timed_component("system_health", self._system_health)
        metrics = self._timed_component("metrics", self._metrics_status)
        risk_metrics = self._timed_component(
            "risk_metrics",
            self._risk_metrics_status,
        )
        model_status = self._timed_component("model_status", self._model_status)
        high_load = self._timed_component("high_load", lambda: self._high_load_status(market_session))
        governance = self._timed_component("governance", self._governance_status)
        factor_data = self._timed_component("factor_data", self._factor_data_status)
        runtime_health_projection = self._timed_component(
            "runtime_health_projection", self._runtime_health_projection_status
        )
        runtime_factor_budget = self._timed_component("runtime_factor_budget", self._runtime_factor_budget_status)
        governance_freshness = self._timed_component("governance_freshness", self._governance_freshness_status)
        runtime_weight_integrity = self._timed_component("runtime_weight_integrity", self._runtime_weight_integrity_status)
        factor_blend_health = self._timed_component("factor_blend_health", self._factor_blend_health_status)
        factor_pruning_candidates = self._timed_component("factor_pruning_candidates", self._factor_pruning_candidates_status)
        execution_semantics = self._timed_component("execution_semantics", self._execution_semantics_status)
        startup_status = self._timed_component("startup_status", self._startup_status)
        config_runtime_drift = self._timed_component("config_runtime_drift", self._config_runtime_drift_status)
        audit_health = self._timed_component("audit_health", self._audit_health_status)
        background_jobs = self._timed_component("background_jobs", self._background_jobs_status)
        replay = self._timed_component("replay", self._replay_status)
        incident_control = self._timed_component("incident_control", self._incident_control_status)
        release = self._timed_component("release", self._release_status)
        learning_repair = self._timed_component("learning_repair", self._learning_repair_status)
        stability = self._timed_component(
            "stability",
            lambda: self._stability_status(
                governance_freshness=governance_freshness,
                model_status=model_status,
            ),
        )
        learning_worker = self._timed_component(
            "learning_worker",
            lambda: self._learning_worker_capability_status(
                runtime_snapshot=dict(stability.get("runtime_config_snapshot") or {}),
                runtime_overlay=dict(stability.get("runtime_config_overlay") or {}),
            ),
        )
        autonomy_health = self._timed_component(
            "autonomy_health",
            lambda: self._autonomy_health_status(
                live_status=live_status,
                system_health=system_health,
                governance=governance,
                stability=stability,
                replay=replay,
                governance_freshness=governance_freshness,
                model_status=model_status,
            ),
        )
        is_runtime_state_db = _use_pg(self.db_path)
        blockers = []
        blockers.extend(system_health.get("blocking_components") or [])
        execution_blockers = list(execution_semantics.get("blocking_components") or [])
        if is_runtime_state_db:
            blockers.extend(execution_blockers)
        startup_blockers = list(startup_status.get("blocking_components") or [])
        if is_runtime_state_db and execution_semantics.get("effective_send_orders"):
            blockers.extend(startup_blockers)
        model_permission_blocked = not model_status.get("permission_ok", True)
        if is_runtime_state_db and model_permission_blocked:
            blockers.append({"component": "model_permissions", "status": "blocked"})
        overlay_status = dict((stability.get("runtime_config_overlay") or {}))
        overlay_suspicious = bool(overlay_status.get("suspicious"))
        if is_runtime_state_db and overlay_suspicious and execution_semantics.get("effective_send_orders"):
            blockers.append(
                {
                    "component": "runtime_config_overlay",
                    "status": "critical",
                    "reason": "suspicious_active_overlay",
                    "suspicious_factors": overlay_status.get("suspicious_factors") or [],
                }
            )
        ready_for_frontend = not blockers
        known_observations = []
        known_observations.extend(system_health.get("known_observations") or [])
        if metrics.get("status") == "degraded":
            known_observations.append(
                {
                    "component": "metrics_backend",
                    "status": "degraded",
                    "classification": "prometheus_fallback_active",
                }
            )
        if not is_runtime_state_db:
            known_observations.extend(
                {
                    **item,
                    "classification": "execution_semantics_offline_context",
                }
                for item in execution_blockers
            )
        known_observations.extend(startup_status.get("known_observations") or [])
        if not is_runtime_state_db or not execution_semantics.get("effective_send_orders"):
            known_observations.extend(
                {
                    **item,
                    "classification": "startup_degraded_non_live",
                }
                for item in startup_blockers
            )
        if not is_runtime_state_db and model_permission_blocked:
            known_observations.append({"component": "model_permissions", "status": "blocked", "classification": "offline_context"})
        if overlay_suspicious and not execution_semantics.get("effective_send_orders"):
            known_observations.append(
                {
                    "component": "runtime_config_overlay",
                    "status": "suspicious",
                    "classification": "startup_degraded_non_live",
                    "reason": "suspicious_active_overlay",
                    "suspicious_factors": overlay_status.get("suspicious_factors") or [],
                }
            )
        known_observations.extend(config_runtime_drift.get("known_observations") or [])
        known_observations.extend(audit_health.get("known_observations") or [])
        if str(autonomy_health.get("posture") or "") in {"constrained", "shadow_only", "frozen"}:
            known_observations.append(
                {
                    "component": "autonomy_health",
                    "status": str(autonomy_health.get("posture") or ""),
                    "classification": "autonomy_health_read_only",
                    "blockers": autonomy_health.get("blockers") or [],
                }
            )
        if str(incident_control.get("mode") or "normal") != "normal":
            known_observations.append(
                {
                    "component": "runtime_incident_control",
                    "status": str(incident_control.get("mode") or ""),
                    "classification": "operator_incident_control",
                    "readiness_effect": incident_control.get("readiness_effect") or {},
                }
            )
        readiness_dimensions = self._build_readiness_dimensions(
            is_runtime_state_db=is_runtime_state_db,
            global_blockers=blockers,
            live_status=live_status,
            execution_semantics=execution_semantics,
            startup_status=startup_status,
            incident_control=incident_control,
            runtime_weight_integrity=runtime_weight_integrity,
            factor_blend_health=factor_blend_health,
            governance=governance,
            config_runtime_drift=config_runtime_drift,
            audit_health=audit_health,
            replay=replay,
            stability=stability,
            learning_worker=learning_worker,
            risk_metrics=risk_metrics,
        )
        payload = {
            "ok": True,
            "schema_version": "backend_readiness.v1",
            "generated_at": time.time(),
            "ready_for_frontend": ready_for_frontend,
            "ready_for_live_execution": readiness_dimensions["ready_for_live_execution"],
            "ready_for_live_alpha": readiness_dimensions["ready_for_live_alpha"],
            "ready_for_autonomous_mutation": readiness_dimensions["ready_for_autonomous_mutation"],
            "ready_for_release": readiness_dimensions["ready_for_release"],
            "readiness_dimensions": readiness_dimensions,
            "backend_service": self._service_status(),
            "system_health": system_health,
            "metrics": metrics,
            "risk_metrics": risk_metrics,
            "market_session": market_session,
            "live": {
                "ctrader": live_status.get("ctrader") or {},
                "loop": live_status.get("loop") or {},
                "readiness": live_status.get("readiness") or {},
            },
            "high_load": high_load,
            "models": model_status,
            "governance": governance,
            "factor_data": factor_data,
            "runtime_health_projection": runtime_health_projection,
            "runtime_factor_budget": runtime_factor_budget,
            "governance_freshness": governance_freshness,
            "runtime_weight_integrity": runtime_weight_integrity,
            "factor_blend_health": factor_blend_health,
            "factor_pruning_candidates": factor_pruning_candidates,
            "execution_semantics": execution_semantics,
            "startup": startup_status,
            "config_runtime_drift": config_runtime_drift,
            "mutation_policy": self._mutation_policy_status(),
            "audit_health": audit_health,
            "background_jobs": background_jobs,
            "replay": replay,
            "incident_control": incident_control,
            "release": release,
            "learning_repair": learning_repair,
            "learning_worker": learning_worker,
            "autonomy_health": autonomy_health,
            "stability": stability,
            "v15": {
                "schema_version": "v15_readiness_contract.v1",
                "runtime": {
                    "execution_semantics": execution_semantics,
                    "startup": startup_status,
                    "live": {
                        "ctrader": live_status.get("ctrader") or {},
                        "loop": live_status.get("loop") or {},
                        "readiness": live_status.get("readiness") or {},
                    },
                    "risk_metrics": risk_metrics,
                },
                "overlay": stability.get("runtime_config_overlay") or {},
                "snapshot": stability.get("runtime_config_snapshot") or {},
                "catalog": governance.get("factor_governance_runtime") or {},
                "worker": {
                    "background_jobs": background_jobs,
                    "governance_freshness": governance_freshness,
                    "capability": learning_worker,
                },
                "replay": replay,
                "incident_control": incident_control,
                "release": release,
                "autonomy_health": autonomy_health,
                "control_plane_boundaries": {
                    "runtime_overlay_is_source_of_truth": True,
                    "runtime_snapshot_required_for_rollback": True,
                    "risk_policy_service_required": True,
                    "decision_policy_required_for_weight_writes": True,
                    "models_shadow_or_advisory_only": True,
                    "incident_controls_require_risk_policy": True,
                },
            },
            "frontend_contract": {
                "preferred_entry": "/api/ops/backend-readiness",
                "v15_replay_latest": "/api/ops/replay/latest",
                "v15_replay_run": "/api/ops/replay/run",
                "v15_replay_bar_run": "/api/ops/replay/bar-run",
                "v15_incident_control": "/api/ops/incident-control",
                "v15_phase0_completion": "/api/ops/v15/phase0",
                "v15_release_latest": "/api/ops/release/latest",
                "v15_release_start": "/api/ops/release/start",
                "v15_release_finish": "/api/ops/release/{run_id}/finish",
                "v16_brain_state": "/api/ops/brain/state",
                "v16_brain_memory": "/api/ops/brain/memory",
                "v16_brain_commands": "/api/ops/brain/commands",
                "v16_brain_action_plans": "/api/ops/brain/action-plans",
                "v16_brain_action_plan_evals": "/api/ops/brain/action-plan-evals",
                "v16_brain_low_impact_executions": "/api/ops/brain/low-impact-executions",
                "v16_brain_low_impact_execution_run": "/api/ops/brain/low-impact-executions/run",
                "v16_brain_medium_impact_governance": "/api/ops/brain/medium-impact-governance",
                "v16_brain_medium_impact_governance_materialize": "/api/ops/brain/medium-impact-governance/materialize",
                "factor_pruning_governance_materialize": "/api/ops/factor/pruning-governance/materialize",
                "factor_pruning_governance_promote_ready": "/api/ops/factor/pruning-governance/promote-ready",
                "factor_pruning_governance_bridge_ready": "/api/ops/factor/pruning-governance/bridge-ready",
                "factor_governance_effects": "/api/ops/factor/governance-effects",
                "factor_governance_effects_reconcile": "/api/ops/factor/governance-effects/reconcile",
                "learning_effect_quality": "/api/learning/effect-quality",
                "v16_brain_governance_candidates": "/api/ops/brain/governance-candidates",
                "v16_brain_governance_candidate_submit": "/api/ops/brain/governance-candidates/{candidate_id}/submit",
                "v16_brain_governance_candidate_reviews": "/api/ops/brain/governance-candidate-reviews",
                "v16_brain_governance_candidate_review_run": "/api/ops/brain/governance-candidates/review",
                "v16_brain_live_ready_guardrails": "/api/ops/brain/live-ready-guardrails",
                "v16_brain_live_ready_guardrail_evaluate": "/api/ops/brain/live-ready-guardrails/evaluate",
                "v16_brain_live_ready_guardrail_tighten": "/api/ops/brain/live-ready-guardrails/tighten",
                "autonomy_proposals": "/api/ops/autonomy/proposals",
                "autonomy_proposals_refresh": "/api/ops/autonomy/proposals/refresh",
                "agent_authority": "/api/ops/agent-authority",
                "agent_scorecard": "/api/ops/agent-scorecard",
                "agent_briefing": "/api/ops/agent-briefing",
                "agent_trade_attribution": "/api/ops/agent-trade-attribution",
                "agent_chain_health": "/api/ops/agent-chain-health",
                "live_autonomy_status": "/api/ops/autonomy/live-status",
                "live_autonomy_unlock_evaluate": "/api/ops/autonomy/live-unlock/evaluate",
                "live_autonomy_unlock": "/api/ops/autonomy/live-unlock",
                "live_autonomy_revoke": "/api/ops/autonomy/live-unlock/revoke",
                "model_shadow_report": "/api/learning/model/meta-lightgbm/shadow-report",
                "model_shadow_report_snapshots": "/api/learning/model/meta-lightgbm/shadow-report/snapshots",
                "model_governance_materialize": "/api/learning/model/meta-lightgbm/governance-suggestion",
                "offmarket_high_load_audits": "/api/learning/model/offmarket-high-load/audits",
                "must_not_call_live_mutation_from_model_pages": True,
            },
            "blockers": blockers,
            "known_observations": known_observations,
        }
        phase0 = self._v15_phase0_status(payload)
        payload["v15"]["phase0"] = phase0
        payload["v15_phase0"] = phase0
        brain_state = self._timed_component("brain_state", lambda: self._brain_state_status(payload))
        payload["brain_state"] = brain_state
        brain_action_plans = self._timed_component("brain_action_plans", lambda: self._brain_action_plan_status())
        payload["brain_action_plans"] = brain_action_plans
        brain_action_plan_evals = self._timed_component("brain_action_plan_evals", lambda: self._brain_action_plan_eval_status())
        payload["brain_action_plan_evals"] = brain_action_plan_evals
        brain_low_impact_executions = self._timed_component("brain_low_impact_executions", lambda: self._brain_low_impact_execution_status())
        payload["brain_low_impact_executions"] = brain_low_impact_executions
        brain_medium_impact_governance = self._timed_component("brain_medium_impact_governance", lambda: self._brain_medium_impact_governance_status())
        payload["brain_medium_impact_governance"] = brain_medium_impact_governance
        brain_governance_candidates = self._timed_component("brain_governance_candidates", lambda: self._brain_governance_candidate_status())
        payload["brain_governance_candidates"] = brain_governance_candidates
        v16_brain_orchestration = self._timed_component(
            "v16_brain_orchestration",
            lambda: self._v16_brain_orchestration_status(),
        )
        payload["v16_brain_orchestration"] = v16_brain_orchestration
        entry_quality_governance = self._timed_component(
            "entry_quality_governance",
            lambda: self._entry_quality_governance_status(),
        )
        payload["entry_quality_governance"] = entry_quality_governance
        candidate_generation_context_coverage = self._timed_component("candidate_generation_context_coverage", lambda: self._candidate_generation_context_coverage_status())
        payload["candidate_generation_context_coverage"] = candidate_generation_context_coverage
        factor_pruning_governance = self._timed_component("factor_pruning_governance", lambda: self._factor_pruning_governance_status())
        payload["factor_pruning_governance"] = factor_pruning_governance
        factor_governance_effects = self._timed_component("factor_governance_effects", lambda: self._factor_governance_effect_status())
        payload["factor_governance_effects"] = factor_governance_effects
        learning_effect_quality = self._timed_component("learning_effect_quality", lambda: self._learning_effect_quality_status())
        payload["learning_effect_quality"] = learning_effect_quality
        if str(learning_effect_quality.get("status") or "") == "degraded":
            payload["known_observations"].append(
                {
                    "component": "learning_effect_quality",
                    "status": "degraded",
                    "classification": "learning_quality_read_only",
                    "slo": learning_effect_quality.get("slo") or {},
                }
            )
        brain_governance_candidate_reviews = self._timed_component("brain_governance_candidate_reviews", lambda: self._brain_governance_candidate_review_status())
        payload["brain_governance_candidate_reviews"] = brain_governance_candidate_reviews
        candidate_bridge_review_coverage = self._timed_component("candidate_bridge_review_coverage", lambda: self._candidate_bridge_review_coverage_status())
        payload["candidate_bridge_review_coverage"] = candidate_bridge_review_coverage
        brain_live_ready_guardrails = self._timed_component("brain_live_ready_guardrails", lambda: self._brain_live_ready_guardrail_status())
        payload["brain_live_ready_guardrails"] = brain_live_ready_guardrails
        proposal_registry = self._timed_component("proposal_registry", lambda: self._proposal_registry_status())
        payload["proposal_registry"] = proposal_registry
        proposal_generation_context_coverage = self._timed_component(
            "proposal_generation_context_coverage",
            lambda: self._proposal_generation_context_coverage_status(),
        )
        payload["proposal_generation_context_coverage"] = proposal_generation_context_coverage
        agent_authority = self._timed_component("agent_authority", lambda: self._agent_authority_status())
        payload["agent_authority"] = agent_authority
        agent_scorecard = self._timed_component("agent_scorecard", lambda: self._agent_scorecard_status())
        payload["agent_scorecard"] = agent_scorecard
        agent_briefing = self._timed_component("agent_briefing", lambda: self._agent_briefing_status())
        payload["agent_briefing"] = agent_briefing
        agent_chain_health = self._timed_component("agent_chain_health", lambda: self._agent_chain_health_status())
        payload["agent_chain_health"] = agent_chain_health
        live_autonomy = self._timed_component("live_autonomy", lambda: self._live_autonomy_status(payload))
        payload["live_autonomy"] = live_autonomy
        autonomous_evolution_cycle = self._timed_component(
            "autonomous_evolution_cycle",
            lambda: self._autonomous_evolution_cycle_status(payload),
        )
        payload["autonomous_evolution_cycle"] = autonomous_evolution_cycle
        payload["v16"] = {
            "schema_version": "v16_readiness_contract.v1",
            "phase": "phase5_live_ready_guardrails",
            "brain_state": brain_state,
            "action_plans": brain_action_plans,
            "action_plan_evals": brain_action_plan_evals,
            "low_impact_executions": brain_low_impact_executions,
            "medium_impact_governance": brain_medium_impact_governance,
            "governance_candidates": brain_governance_candidates,
            "orchestration": v16_brain_orchestration,
            "candidate_generation_context_coverage": candidate_generation_context_coverage,
            "factor_pruning_governance": factor_pruning_governance,
            "factor_governance_effects": factor_governance_effects,
            "learning_effect_quality": learning_effect_quality,
            "runtime_factor_budget": runtime_factor_budget,
            "runtime_health_projection": runtime_health_projection,
            "governance_candidate_reviews": brain_governance_candidate_reviews,
            "candidate_bridge_review_coverage": candidate_bridge_review_coverage,
            "live_ready_guardrails": brain_live_ready_guardrails,
            "proposal_registry": proposal_registry,
            "proposal_generation_context_coverage": proposal_generation_context_coverage,
            "agent_authority": agent_authority,
            "agent_scorecard": agent_scorecard,
            "agent_briefing": agent_briefing,
            "agent_chain_health": agent_chain_health,
            "live_autonomy": live_autonomy,
            "autonomous_evolution_cycle": autonomous_evolution_cycle,
            "control_plane_boundaries": {
                "read_only": True,
                "affects_trading": False,
                "shadow_action_plans_record_only": True,
                "shadow_action_evals_record_only": True,
                "low_impact_execution_requires_risk_policy": True,
                "low_impact_execution_whitelist_only": True,
                "medium_impact_governance_candidates_only": True,
                "medium_impact_governance_suggestions_only": False,
                "medium_impact_policy_suggestion_bridge_manual_only": True,
                "medium_impact_policy_suggestion_direct_write": False,
                "candidate_generation_context_required": True,
                "candidate_review_bridge_preview_only": True,
                "meta_brain_command_only": True,
                "v16_command_owner": True,
                "v16_downstream_execution_owner": True,
                "v16_direct_policy_suggestion_write": False,
                "v16_direct_runtime_mutation": False,
                "v16_posterior_must_dispatch_to_specialist": True,
                "demo_nursery_automatic_governance_enabled": governance.get("autonomy_mode") in {"demo_nursery", "demo_autonomous"},
                "demo_nursery_human_approval_required": False,
                "demo_nursery_system_runner": "AutonomousEvolutionNurseryRunner",
                "candidate_review_llm_advisory_only": True,
                "candidate_bridge_requires_review": True,
                "medium_impact_future_apply_requires_decision_policy": True,
                "live_ready_guardrails_only": True,
                "live_ready_tightening_only": True,
                "live_ready_tightening_uses_incident_control": True,
                "risk_policy_service_required_for_future_actions": True,
                "decision_policy_required_for_future_weight_writes": True,
                "runtime_overlay_snapshot_required_for_future_mutations": True,
                "models_shadow_or_advisory_only": True,
                "proposal_registry_review_only": True,
                "proposal_generation_context_required": True,
                "agent_authority_registry_is_source_of_truth": True,
                "agent_scorecard_read_only": True,
                "agent_briefing_read_only": True,
                "agent_trade_feedback_read_only": True,
                "live_autonomy_requires_manual_unlock": True,
                "autonomous_evolution_cycle_read_only": True,
            },
        }
        autonomous_blueprint = self._timed_component("autonomous_blueprint", lambda: self._autonomous_blueprint_status(payload))
        payload["autonomous_blueprint"] = autonomous_blueprint
        payload["v16"]["autonomous_blueprint"] = autonomous_blueprint
        record_timing("backend_readiness.build", time.perf_counter() - build_started, extra={"ready": ready_for_frontend})
        return payload

    def _learning_repair_status(self) -> dict[str, Any]:
        from config.runtime_config import (
            autonomy_expansion_freeze_applies,
            governance_expansion_is_paused,
            shared as runtime_config,
        )

        cfg = runtime_config()
        configured_expansion_frozen = bool(getattr(cfg, "autonomy_expansion_frozen", True))
        governance_expansion_paused = governance_expansion_is_paused(cfg)
        effective_expansion_frozen = autonomy_expansion_freeze_applies(cfg)
        result: dict[str, Any] = {
            "schema_version": "learning_repair_readiness.v1",
            "expansion_frozen": effective_expansion_frozen,
            "configured_expansion_frozen": configured_expansion_frozen,
            "governance_expansion_paused": governance_expansion_paused,
            "freeze_applies_to_current_mode": effective_expansion_frozen,
            "blocks_demo_governance": governance_expansion_paused,
            "autonomy_mode": str(getattr(cfg, "autonomy_mode", "") or "manual"),
            "governance_horizon_minutes": int(
                getattr(cfg, "supervisor_counterfactual_governance_horizon_minutes", 60) or 60
            ),
            "canary_required": int(getattr(cfg, "supervisor_canary_mature_trade_count", 50) or 50),
            "active_effect_limit": 24,
        }
        try:
            conn = _connect_state(self.db_path)
        except Exception as exc:
            return {**result, "ok": False, "reason": f"state_unavailable:{exc}"}
        try:
            maturity_rows = []
            if (
                _table_exists(conn, "supervisor_counterfactual_review")
                and _table_exists(conn, "trade_outcome_review")
            ):
                maturity_rows = _execute(
                    conn,
                    """
                    SELECT c.position_id, c.close_ts, c.evidence_json,
                           r.review_id AS source_review_id,
                           r.review_json AS source_review_json
                    FROM supervisor_counterfactual_review c
                    LEFT JOIN trade_outcome_review r ON r.review_id=c.review_id
                    ORDER BY c.updated_at DESC
                    """,
                ).fetchall()
            canary_started_at = 0.0
            canary_suggestion_id = ""
            canary_template_id = ""
            shadow_position_ids: set[str] = set()
            if _table_exists(conn, "policy_suggestion"):
                candidate = _execute(
                    conn,
                    "SELECT suggestion_id, scope_key, created_at FROM policy_suggestion WHERE scope_type='position_supervisor_template' AND status='approved' ORDER BY created_at DESC LIMIT 1",
                ).fetchone()
                canary_started_at = _safe_float(dict(candidate).get("created_at")) if candidate else 0.0
                canary_suggestion_id = str(dict(candidate).get("suggestion_id") or "") if candidate else ""
                canary_template_id = str(dict(candidate).get("scope_key") or "") if candidate else ""
            if canary_template_id and _table_exists(conn, "position_supervisor_trace"):
                shadow_rows = _execute(
                    conn,
                    """
                    SELECT DISTINCT position_id
                    FROM position_supervisor_trace
                    WHERE template_id=?
                      AND stage='learning_shadow'
                      AND execution_status='observation_only'
                      AND trace_integrity='recovered'
                      AND execution_reason=?
                      AND event_ts>=?
                    """,
                    (
                        canary_template_id,
                        f"learning_worker_candidate_replay:{canary_suggestion_id}",
                        canary_started_at,
                    ),
                ).fetchall()
                shadow_position_ids = {str(dict(row).get("position_id") or "") for row in shadow_rows}
            mature_positions: set[str] = set()
            sessions: set[str] = set()
            regimes: set[str] = set()
            immature = 0
            historical_immature_excluded = 0
            candidate_review_positions: set[str] = set()
            invalid_evidence = 0
            invalidated_counterfactuals = 0
            now = time.time()
            for row in maturity_rows:
                item = dict(row)
                evidence = item.get("evidence_json") or {}
                if isinstance(evidence, str):
                    evidence = _loads(evidence, {})
                maturity = dict((evidence or {}).get("maturity") or {})
                close_ts = _safe_float(item.get("close_ts"))
                eligible = bool(maturity.get("governance_eligible"))
                counterfactual_invalidated = bool((evidence or {}).get("evidence_invalidated"))
                source_review_invalid = (
                    not str(item.get("source_review_id") or "")
                    or review_has_system_contamination(
                        _loads(item.get("source_review_json"), {})
                    )
                )
                if counterfactual_invalidated:
                    invalidated_counterfactuals += 1
                position_id = str(item.get("position_id") or "")
                in_current_canary = (
                    canary_started_at > 0
                    and close_ts >= canary_started_at
                    and position_id in shadow_position_ids
                )
                if source_review_invalid or counterfactual_invalidated:
                    if in_current_canary:
                        invalid_evidence += 1
                    continue
                overdue_immature = (
                    not eligible
                    and close_ts <= now - result["governance_horizon_minutes"] * 60
                )
                if in_current_canary:
                    candidate_review_positions.add(position_id)
                    if overdue_immature:
                        immature += 1
                elif overdue_immature:
                    historical_immature_excluded += 1
                if eligible and in_current_canary:
                    mature_positions.add(position_id)
                    hour = time.gmtime(close_ts).tm_hour
                    sessions.add("asia" if hour < 7 else "europe" if hour < 13 else "us")
                    regimes.add(str((evidence or {}).get("regime") or "unknown"))

            active_effects = 0
            effect_ages: list[float] = []
            if _table_exists(conn, "learning_application_effect"):
                rows = _execute(
                    conn,
                    "SELECT status, updated_at FROM learning_application_effect WHERE status IN ('prepared','observing','mixed')",
                ).fetchall()
                active_effects = len(rows)
                effect_ages = [max(0.0, now - _safe_float(dict(row).get("updated_at"))) for row in rows]

            budget = {"reserved": 0, "consumed": 0}
            if _table_exists(conn, "nursery_exploration_reservation"):
                trade_date = time.strftime("%Y-%m-%d", time.gmtime())
                rows = _execute(
                    conn,
                    "SELECT status, COUNT(DISTINCT reservation_id) AS n FROM nursery_exploration_reservation WHERE trade_date=? GROUP BY status",
                    (trade_date,),
                ).fetchall()
                budget.update({str(dict(row).get("status")): int(dict(row).get("n") or 0) for row in rows})
        finally:
            conn.close()
        checks = {
            "active_effect_capacity": active_effects <= 24,
            "no_invalidated_active_evidence": invalid_evidence == 0,
            "candidate_observation_available": bool(shadow_position_ids),
            # Individual trades can remain ineligible when their observation
            # window crosses a market closure or has missing M1 bars.  They are
            # excluded from the mature cohort; requiring every observed trade
            # to mature would make the freeze permanent.  The actual safety
            # threshold remains canary_sample_count below.
            "counterfactual_maturity": bool(mature_positions),
            "canary_sample_count": len(mature_positions) >= result["canary_required"],
            "canary_session_coverage": len(sessions - {"unknown"}) >= 2,
            "canary_regime_coverage": len(regimes - {"unknown"}) >= 2,
        }
        return {
            **result,
            "ok": all(checks.values()),
            "checks": checks,
            "immature_counterfactual_count": immature,
            "historical_immature_excluded_count": historical_immature_excluded,
            "invalid_evidence_count": invalid_evidence,
            "invalidated_counterfactual_count": invalidated_counterfactuals,
            "active_effect_count": active_effects,
            "active_effect_age_seconds": {
                "max": max(effect_ages, default=0.0),
                "over_7d": sum(age >= 7 * 86400 for age in effect_ages),
            },
            "exploration_budget_usage": budget,
            "canary": {
                "started_at": canary_started_at,
                "suggestion_id": canary_suggestion_id,
                "template_id": canary_template_id,
                "evidence_source": "learning_worker_closed_position_replay",
                "stage": "learning_shadow",
                "broker_mutation_allowed": False,
                "shadow_position_count": len(shadow_position_ids),
                "reviewed_position_count": len(candidate_review_positions),
                "mature_trade_count": len(mature_positions),
                "sessions": sorted(sessions - {"unknown"}),
                "regimes": sorted(regimes - {"unknown"}),
            },
        }

    @staticmethod
    def _timed_component(name: str, func):
        with measure(f"backend_readiness.{name}"):
            return func()

    @staticmethod
    def _live_status() -> dict[str, Any]:
        try:
            from backend.services.live_service import get_status

            return get_status()
        except Exception as exc:
            return {"error": str(exc), "market_session": {}}

    @staticmethod
    def _service_status() -> dict[str, Any]:
        return {
            "service": "quant-backend.service",
            "managed_by": "systemd",
            "port": 8000,
            "status": "running",
        }

    @staticmethod
    def _execution_semantics_status() -> dict[str, Any]:
        try:
            from backend.services.execution_semantics import current_execution_semantics

            semantics = current_execution_semantics().to_dict()
        except Exception as exc:
            semantics = {
                "system_mode": "unknown",
                "ctrader_send_orders": False,
                "factor_dry_run": True,
                "effective_send_orders": False,
                "blocking_reason": f"{type(exc).__name__}: {exc}",
            }
        blocking_reason = str(semantics.get("blocking_reason") or "")
        return {
            **semantics,
            "blocking_components": (
                [{"component": "execution_semantics", "status": "critical", "reason": blocking_reason}]
                if blocking_reason
                else []
            ),
        }

    @staticmethod
    def _startup_status() -> dict[str, Any]:
        try:
            from backend.services.startup_status import startup_issues

            issues = startup_issues()
        except Exception:
            issues = []
        return {
            "issues": issues,
            "blocking_components": [
                {"component": item.get("component"), "status": item.get("status"), "message": item.get("message")}
                for item in issues
                if item.get("blocking")
            ],
            "known_observations": [
                {
                    "component": item.get("component"),
                    "status": item.get("status"),
                    "classification": "startup_degraded",
                    "message": item.get("message"),
                }
                for item in issues
                if not item.get("blocking")
            ],
        }

    @staticmethod
    def _config_runtime_drift_status() -> dict[str, Any]:
        try:
            from backend.services.config_service import config_runtime_drift

            drift = config_runtime_drift()
        except Exception as exc:
            drift = {
                "drift": True,
                "changed_keys": [],
                "changed_key_count": 0,
                "semantic_drift": True,
                "error": f"{type(exc).__name__}: {exc}",
            }
        observations = []
        if drift.get("semantic_drift"):
            observations.append({"component": "config_runtime_drift", "status": "degraded", "reason": "semantic_drift"})
        return {**drift, "blocking_components": [], "known_observations": observations}

    @staticmethod
    def _mutation_policy_status() -> dict[str, Any]:
        try:
            from backend.services.mutation_audit import mutation_policy_contract

            return {"schema_version": "mutation_policy.v1", "classes": mutation_policy_contract()}
        except Exception as exc:
            return {"schema_version": "mutation_policy.v1", "classes": {}, "error": str(exc)}

    @staticmethod
    def _audit_health_status() -> dict[str, Any]:
        try:
            from backend.services.mutation_audit import audit_health

            health = audit_health()
        except Exception as exc:
            health = {"ok": False, "last_error": str(exc)}
        observations = []
        if not health.get("ok", True):
            observations.append({"component": "mutation_audit", "status": "critical", "reason": health.get("last_error", "")})
        return {**health, "blocking_components": [], "known_observations": observations}

    @staticmethod
    def _background_jobs_status() -> dict[str, Any]:
        try:
            from backend.jobs import get_job_manager

            jobs = [job.to_dict() for job in get_job_manager().list()]
        except Exception as exc:
            return {"ok": False, "error": str(exc), "running": 0, "failed_recent": 0, "jobs": []}
        running = [job for job in jobs if str(job.get("status") or "").lower() in {"running", "pending"}]
        failed = [job for job in jobs if str(job.get("status") or "").lower() in {"failed", "error"}]
        return {
            "ok": True,
            "running": len(running),
            "failed_recent": len(failed),
            "jobs": jobs[-20:],
        }

    def _model_status(self) -> dict[str, Any]:
        from backend.services.model_influence import ModelInfluenceService
        from backend.services.model_influence_governance import ModelInfluenceGovernanceService
        from config.runtime_config import shared as runtime_config
        from research.factor_governance_lightgbm import FactorGovernanceLightGBMService
        from research.open_quality_lightgbm import OpenQualityLightGBMService
        from research.position_quality_lightgbm import PositionQualityLightGBMService

        service = MetaModelLightGBMService(db_path=self.db_path)
        report = service.build_shadow_report(limit=200, include_samples=False)
        artifact = dict(report.get("artifact_summary") or {})
        metrics = dict(artifact.get("metrics") or {})
        holdout = dict(metrics.get("holdout") or {})
        holdout_accuracy = _safe_float(holdout.get("accuracy"))
        evaluated_count = int(report.get("evaluated_count") or 0)
        permission = self._latest_permission_audit("meta_model_lightgbm")
        eligible = (
            evaluated_count >= 200
            and holdout_accuracy >= 0.6
            and bool(permission.get("ok", True))
            and bool((metrics or {}).get("safe_for_live_trading", False))
        )
        artifact_services = {
            "open_quality_lightgbm": OpenQualityLightGBMService(db_path=self.db_path),
            "position_quality_lightgbm": PositionQualityLightGBMService(db_path=self.db_path),
            "factor_governance_lightgbm": FactorGovernanceLightGBMService(db_path=self.db_path),
            "meta_model_lightgbm": service,
        }
        gate_service = ModelInfluenceGovernanceService(self.db_path)
        promotion_gates = {}
        for model_type, artifact_service in artifact_services.items():
            latest_path = artifact_service.latest_artifact_path()
            promotion_gates[model_type] = (
                gate_service.evaluate_artifact(latest_path)
                if latest_path else {"passed": False, "reason": "artifact_missing", "failed_checks": ["artifact_missing"]}
            )
        influence_status = ModelInfluenceService(self.db_path).status(runtime_config())
        return {
            "meta_lightgbm": {
                "report": report,
                "promotion_gate": {
                    "eligible_for_live": False,
                    "eligible_for_governor_review": bool(evaluated_count >= 30),
                    "computed_live_eligibility_would_be": eligible,
                    "reason": (
                        "shadow_only_artifact"
                        if not eligible
                        else "would_require_governance_contract_change_before_live"
                    ),
                    "min_holdout_accuracy": 0.6,
                    "holdout_accuracy": holdout_accuracy,
                    "min_evaluated_count": 200,
                    "evaluated_count": evaluated_count,
                },
            },
            "promotion_gates": promotion_gates,
            "influence": influence_status,
            "permission_ok": bool(permission.get("ok", True)),
            "latest_permission_audit": permission,
        }

    def _latest_permission_audit(self, model_type: str) -> dict[str, Any]:
        conn = _connect_state(self.db_path)
        try:
            if not _table_exists(conn, "model_permission_audit"):
                return {"ok": True, "status": "missing_table"}
            row = _execute(
                conn,
                """
                SELECT *
                FROM model_permission_audit
                WHERE model_type=?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (model_type,),
            ).fetchone()
            if not row:
                return {"ok": True, "status": "none"}
            keys = set(row.keys())
            result = _loads(row["result_json"], {}) if "result_json" in keys else {
                "capabilities": _loads(row["capabilities_json"], {}) if "capabilities_json" in keys else {},
                "violations": _loads(row["violations_json"], []) if "violations_json" in keys else [],
                "context": _loads(row["context_json"], {}) if "context_json" in keys else {},
                "reason": str(row["reason"] or "") if "reason" in keys else "",
            }
            status = str(row["status"] or "")
            return {
                "ok": status != "blocked",
                "audit_id": str(row["audit_id"] or ""),
                "model_type": str(row["model_type"] or ""),
                "status": status,
                "result": result,
                "created_at": _safe_float(row["created_at"]),
            }
        finally:
            conn.close()

    def _governance_status(self) -> dict[str, Any]:
        conn = _connect_state(self.db_path)
        try:
            try:
                from config.runtime_config import shared as runtime_config

                cfg = runtime_config()
                autonomy_mode = str(getattr(cfg, "autonomy_mode", "") or "manual")
                demo_auto_apply = bool(getattr(cfg, "autonomy_demo_auto_apply", False))
            except Exception:
                autonomy_mode = "unknown"
                demo_auto_apply = False
            automatic_execution_enabled = autonomy_mode in {"demo_autonomous", "demo_nursery"} and demo_auto_apply
            counts = {}
            normalized_counts = {}
            if _table_exists(conn, "policy_suggestion"):
                rows = _execute(
                    conn,
                    """
                    SELECT status, action, reason, review_note, evidence_json
                    FROM policy_suggestion
                    """
                ).fetchall()
                from backend.services.policy_suggestion_status import count_policy_suggestion_statuses

                counted = count_policy_suggestion_statuses([dict(row) for row in rows])
                counts = counted["raw"]
                normalized_counts = counted["normalized"]
            snapshots = MetaGovernanceService(self.db_path).list_shadow_report_snapshots(limit=5)
            return {
                "policy_suggestion_counts": counts,
                "policy_suggestion_counts_raw": counts,
                "policy_suggestion_counts_normalized": normalized_counts,
                "pending_review_count": int(counts.get("proposed", 0)) + int(counts.get("pending_review", 0)),
                "autonomous_pending_count": int(normalized_counts.get("proposed", 0)),
                "meta_shadow_report_snapshots": snapshots,
                "automatic_execution_enabled": automatic_execution_enabled,
                "autonomy_mode": autonomy_mode,
                "autonomy_demo_auto_apply": demo_auto_apply,
                "factor_governance_runtime": self._factor_governance_runtime_status(),
            }
        finally:
            conn.close()

    def _factor_governance_runtime_status(self) -> dict[str, Any]:
        try:
            from config.runtime_config import shared as runtime_config

            cfg = runtime_config()
            enabled = bool(getattr(cfg, "factor_governance_enabled", True))
            cron = str(getattr(cfg, "factor_governance_cron", "*/15 * * * *") or "*/15 * * * *")
            stale_after_sec = float(getattr(cfg, "factor_governance_stale_after_sec", 7200.0) or 7200.0)
        except Exception:
            enabled = True
            cron = "*/15 * * * *"
            stale_after_sec = 7200.0

        conn = _connect_state(self.db_path)
        now = time.time()
        try:
            latest_run: dict[str, Any] = {}
            latest_snapshot: dict[str, Any] = {}
            if _table_exists(conn, "evolution_run"):
                row = _execute(
                    conn,
                    """
                    SELECT run_id, status, trigger_source, started_at, ended_at, summary_json
                    FROM evolution_run
                    WHERE run_type='factor_governance_autonomous'
                    ORDER BY started_at DESC
                    LIMIT 1
                    """,
                ).fetchone()
                if row:
                    started_at = _safe_float(row["started_at"])
                    ended_at = _safe_float(row["ended_at"])
                    latest_run = {
                        "run_id": str(row["run_id"] or ""),
                        "status": str(row["status"] or ""),
                        "trigger_source": str(row["trigger_source"] or ""),
                        "started_at": started_at,
                        "ended_at": ended_at,
                        "age_seconds": round(max(0.0, now - (ended_at or started_at)), 3) if (ended_at or started_at) else None,
                        "summary": _loads(row["summary_json"], {}),
                    }
            if _table_exists(conn, "factor_catalog_snapshot"):
                row = _execute(
                    conn,
                    """
                    SELECT snapshot_id, run_id, source, catalog_hash, created_at
                    FROM factor_catalog_snapshot
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                ).fetchone()
                if row:
                    created_at = _safe_float(row["created_at"])
                    latest_snapshot = {
                        "snapshot_id": str(row["snapshot_id"] or ""),
                        "run_id": str(row["run_id"] or ""),
                        "source": str(row["source"] or ""),
                        "catalog_hash": str(row["catalog_hash"] or ""),
                        "created_at": created_at,
                        "age_seconds": round(max(0.0, now - created_at), 3) if created_at else None,
                    }
        finally:
            conn.close()

        if not enabled:
            status = "disabled"
            ok = True
            stale = False
        elif not latest_run:
            status = "missing_run"
            ok = False
            stale = True
        elif str(latest_run.get("status") or "").lower() == "failed":
            status = "failed"
            ok = False
            failed_age = latest_run.get("age_seconds")
            stale = failed_age is None or float(failed_age) > stale_after_sec
        elif not latest_snapshot:
            status = "missing_catalog_snapshot"
            ok = False
            stale = True
        else:
            run_age_raw = latest_run.get("age_seconds")
            snapshot_age_raw = latest_snapshot.get("age_seconds")
            if run_age_raw is None or snapshot_age_raw is None:
                stale = True
                status = "timestamp_unknown"
                ok = False
            else:
                run_age = float(run_age_raw)
                snapshot_age = float(snapshot_age_raw)
                stale = run_age > stale_after_sec or snapshot_age > stale_after_sec
                status = "stale" if stale else "fresh"
                ok = not stale
        return {
            "ok": ok,
            "status": status,
            "enabled": enabled,
            "cron": cron,
            "stale": stale,
            "stale_after_seconds": stale_after_sec,
            "latest_run": latest_run,
            "latest_catalog_snapshot": latest_snapshot,
        }

    def _factor_data_status(self) -> dict[str, Any]:
        state_counts: dict[str, Any] = {}
        conn = _connect_state(self.db_path)
        try:
            if _table_exists(conn, "factor_health"):
                rows = _execute(
                    conn,
                    "SELECT status, COUNT(*) AS n FROM factor_health GROUP BY status"
                ).fetchall()
                state_counts["factor_health_by_status"] = {
                    str(row["status"] or "UNKNOWN"): int(row["n"] or 0) for row in rows
                }
                state_counts["factor_health_total"] = sum(state_counts["factor_health_by_status"].values())
        finally:
            conn.close()

        external_counts: dict[str, int] = {}
        try:
            dconn = connect_duckdb(DUCKDB_EXTERNAL, read_only=True)
            try:
                for table in ["cot_gold", "etf_holdings", "macro_daily", "cb_gold", "etf_daily"]:
                    try:
                        external_counts[table] = int(dconn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] or 0)
                    except Exception:
                        external_counts[table] = 0
            finally:
                dconn.close()
        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
                "state": state_counts,
                "external_counts": external_counts,
            }

        return {
            "ok": bool(state_counts.get("factor_health_total", 0) > 0 and external_counts.get("macro_daily", 0) > 0),
            "state": state_counts,
            "external_counts": external_counts,
            "canonical_frame": "data.factor_frame.FactorFrameBuilder",
            "last_enrichment": self._last_factor_frame_enrichment(),
        }

    @staticmethod
    def _last_factor_frame_enrichment() -> dict[str, Any]:
        try:
            from data.factor_frame import latest_factor_frame_status

            status = latest_factor_frame_status()
            return {
                "ok": bool(status.get("ok", True)),
                "updated_at": _safe_float(status.get("updated_at")),
                "error": str(status.get("error") or ""),
            }
        except Exception as exc:
            return {"ok": False, "updated_at": 0.0, "error": str(exc)}

    def _governance_freshness_status(self) -> dict[str, Any]:
        tables = [
            "meta_model_shadow_audit",
            "factor_governance_shadow_audit",
            "position_quality_shadow_audit",
            "shadow_factor_perf",
            "factor_health",
            "lifecycle_events",
            "evolution_decision",
            "factor_catalog_snapshot",
        ]
        now = time.time()
        freshness: dict[str, Any] = {}
        conn = _connect_state(self.db_path)
        try:
            for table in tables:
                if not _table_exists(conn, table):
                    freshness[table] = {"status": "missing_table"}
                    continue
                ts_col = "updated_at"
                cols = state_table_columns(conn, table)
                if "created_at" in cols:
                    ts_col = "created_at"
                elif "updated_at" in cols:
                    ts_col = "updated_at"
                elif "timestamp" in cols:
                    ts_col = "timestamp"
                else:
                    freshness[table] = {"status": "no_timestamp"}
                    continue
                latest = _safe_float(_execute(conn, f"SELECT MAX({ts_col}) AS ts FROM {table}").fetchone()["ts"])
                age_sec = max(0.0, now - latest) if latest > 0 else None
                freshness[table] = {
                    "latest_ts": latest,
                    "age_seconds": round(age_sec, 3) if age_sec is not None else None,
                    "status": "fresh" if age_sec is not None and age_sec <= 3 * 86400 else "stale_or_empty",
                }
        finally:
            conn.close()
        return {"tables": freshness}

    def _stability_status(
        self,
        *,
        governance_freshness: dict[str, Any],
        model_status: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "schema_version": "backend_stability.v1",
            "timings": timing_snapshot("backend_readiness."),
            "runtime_config_snapshot": self._runtime_config_snapshot_status(),
            "runtime_config_overlay": self._runtime_config_overlay_status(),
            "freshness_watchdog": self._freshness_watchdog_status(
                governance_freshness=governance_freshness,
                model_status=model_status,
            ),
            "rollback_policy": {
                "schema_version": "rollback_policy_observation.v1",
                "hard_risk_limits_mutable": False,
                "model_live_permission_mutable": False,
                "requires_governed_scope": True,
                "requires_rollback_payload": True,
                "allowed_low_risk_scopes": [
                    "position_supervisor_template",
                    "parameter_template_online_light",
                ],
            },
        }

    def _runtime_kv_get(self, key: str, default: Any = None) -> Any:
        conn = _connect_state(self.db_path)
        try:
            if not _table_exists(conn, "runtime_kv"):
                return default
            row = _execute(
                conn,
                "SELECT value_json, updated_at FROM runtime_kv WHERE key=? LIMIT 1",
                (key,),
            ).fetchone()
            if not row:
                return default
            payload = _loads(row["value_json"], default)
            if isinstance(payload, dict):
                payload.setdefault("runtime_kv_updated_at", _safe_float(row["updated_at"]))
            return payload
        finally:
            conn.close()

    def _learning_worker_capability_status(
        self,
        *,
        runtime_snapshot: dict[str, Any],
        runtime_overlay: dict[str, Any],
    ) -> dict[str, Any]:
        from backend.services.learning_worker_capability import STATUS_KEY

        try:
            payload = self._runtime_kv_get(STATUS_KEY, {}) or {}
        except Exception as exc:
            return {
                "schema_version": "learning_worker_readiness.v2",
                "ok": False,
                "state": "error",
                "boot_status": "unknown",
                "mutation_capability": {"available": False, "status": "unknown"},
                "error": f"{type(exc).__name__}: {exc}",
            }
        if not isinstance(payload, dict) or not payload:
            return {
                "schema_version": "learning_worker_readiness.v2",
                "ok": False,
                "state": "unknown",
                "boot_status": "unknown",
                "observation_capability": {"available": False, "status": "unknown"},
                "research_capability": {"available": False, "status": "unknown"},
                "mutation_capability": {"available": False, "status": "unknown"},
                "reason": "learning_worker_projection_missing",
            }

        now = time.time()
        updated_at = _safe_float(payload.get("runtime_kv_updated_at") or payload.get("updated_at"))
        age_seconds = max(0.0, now - updated_at) if updated_at > 0 else None
        stale_after_seconds = 75.0
        fresh = age_seconds is not None and age_seconds <= stale_after_seconds
        worker_config_hash = str(payload.get("config_hash") or "")
        backend_config_hash = str(runtime_snapshot.get("config_hash") or "")
        worker_overlay_hash = str(payload.get("overlay_hash") or "")
        backend_overlay_hash = str(runtime_overlay.get("overlay_hash") or "")
        config_hash_match = bool(
            worker_config_hash
            and backend_config_hash
            and worker_config_hash == backend_config_hash
        )
        overlay_hash_match = (
            worker_overlay_hash == backend_overlay_hash
            and (bool(worker_overlay_hash) or not backend_overlay_hash)
        )
        boot_status = str(payload.get("boot_status") or "unknown")
        observation = dict(payload.get("observation_capability") or {})
        research = dict(payload.get("research_capability") or {})
        mutation = dict(payload.get("mutation_capability") or {})
        raw_mutation_available = bool(mutation.get("available"))
        operational = bool(
            boot_status == "ready"
            and fresh
            and config_hash_match
            and overlay_hash_match
        )
        mutation["available"] = bool(raw_mutation_available and operational)
        if raw_mutation_available and not operational:
            mutation["raw_available"] = True
            mutation["status"] = (
                "stale_projection"
                if not fresh
                else "config_hash_diverged"
                if not config_hash_match
                else "overlay_hash_diverged"
            )
        return {
            **payload,
            "schema_version": "learning_worker_readiness.v2",
            "ok": operational,
            "state": "known" if operational else "stale" if not fresh else "error",
            "boot_status": boot_status,
            "updated_at": updated_at,
            "age_seconds": round(age_seconds, 3) if age_seconds is not None else None,
            "stale_after_seconds": stale_after_seconds,
            "fresh": fresh,
            "config_hash": worker_config_hash,
            "backend_config_hash": backend_config_hash,
            "config_hash_match": config_hash_match,
            "overlay_hash": worker_overlay_hash,
            "backend_overlay_hash": backend_overlay_hash,
            "overlay_hash_match": overlay_hash_match,
            "hash_match": bool(config_hash_match and overlay_hash_match),
            "observation_capability": observation,
            "research_capability": research,
            "mutation_capability": mutation,
        }

    @staticmethod
    def _build_readiness_dimensions(
        *,
        is_runtime_state_db: bool,
        global_blockers: list[dict[str, Any]],
        live_status: dict[str, Any],
        execution_semantics: dict[str, Any],
        startup_status: dict[str, Any],
        incident_control: dict[str, Any],
        runtime_weight_integrity: dict[str, Any],
        factor_blend_health: dict[str, Any],
        governance: dict[str, Any],
        config_runtime_drift: dict[str, Any],
        audit_health: dict[str, Any],
        replay: dict[str, Any],
        stability: dict[str, Any],
        learning_worker: dict[str, Any],
        risk_metrics: dict[str, Any],
    ) -> dict[str, Any]:
        """Build independent authorization-oriented readiness dimensions."""

        def blocker(component: str, reason: str, **extra: Any) -> dict[str, Any]:
            return {"component": component, "reason": reason, **extra}

        execution_blockers: list[dict[str, Any]] = []
        if not is_runtime_state_db:
            execution_blockers.append(blocker("state_store", "postgresql_required"))
        execution_blockers.extend(execution_semantics.get("blocking_components") or [])
        execution_blockers.extend(startup_status.get("blocking_components") or [])
        ctrader = dict(live_status.get("ctrader") or {})
        if str(ctrader.get("status") or "").lower() != "connected":
            execution_blockers.append(
                blocker("ctrader", "broker_not_connected", status=str(ctrader.get("status") or "unknown"))
            )
        incident_mode = str(incident_control.get("mode") or "unknown")
        if incident_mode != "normal":
            execution_blockers.append(
                blocker("incident_control", "new_risk_not_allowed", mode=incident_mode)
            )
        loop = dict(live_status.get("loop") or {})
        loop_readiness = dict(live_status.get("readiness") or {})
        if bool(loop.get("running")) and loop_readiness.get("ok") is False:
            execution_blockers.append(
                blocker("live_loop", "running_loop_not_ready", details=loop_readiness)
            )
        if bool(loop.get("running")) and loop.get("accepting_new_risk") is False:
            execution_blockers.append(blocker("live_loop", "not_accepting_new_risk"))
        if is_runtime_state_db and risk_metrics.get("ok") is not True:
            execution_blockers.append(
                blocker(
                    "risk_metrics",
                    "canonical_forward_var_not_ready",
                    status=str(risk_metrics.get("status") or "unknown"),
                    var_status=str(
                        risk_metrics.get("var_status") or "unknown"
                    ),
                )
            )

        live_alpha_blockers = list(execution_blockers)
        if runtime_weight_integrity.get("ok") is not True:
            live_alpha_blockers.append(
                blocker("runtime_weight_integrity", "factor_weights_not_authoritative")
            )
        blend_status = str(factor_blend_health.get("status") or "").lower()
        if factor_blend_health.get("ok") is False or blend_status in {"error", "critical"}:
            live_alpha_blockers.append(
                blocker("factor_blend_health", "live_factor_blend_unhealthy", status=blend_status or "unknown")
            )

        mutation_blockers: list[dict[str, Any]] = []
        from config.runtime_config import governance_expansion_is_paused

        if governance_expansion_is_paused():
            mutation_blockers.append(
                blocker("governance_expansion", "operator_pause_active")
            )
        mutation = dict(learning_worker.get("mutation_capability") or {})
        if mutation.get("available") is not True:
            mutation_blockers.append(
                blocker(
                    "learning_worker_mutation",
                    "mutation_capability_unavailable",
                    status=str(mutation.get("status") or "unknown"),
                )
            )
        governance_runtime = dict(governance.get("factor_governance_runtime") or {})
        if governance_runtime.get("enabled", True) and governance_runtime.get("ok") is not True:
            mutation_blockers.append(
                blocker(
                    "factor_governance_runtime",
                    "governance_runtime_not_ready",
                    status=str(governance_runtime.get("status") or "unknown"),
                )
            )
        if bool(config_runtime_drift.get("drift")) or bool(config_runtime_drift.get("semantic_drift")):
            mutation_blockers.append(blocker("runtime_config", "config_drift"))
        overlay = dict(stability.get("runtime_config_overlay") or {})
        if bool(overlay.get("suspicious")):
            mutation_blockers.append(blocker("runtime_config_overlay", "suspicious_overlay"))

        release_blockers: list[dict[str, Any]] = []
        snapshot = dict(stability.get("runtime_config_snapshot") or {})
        if snapshot.get("ok") is not True:
            release_blockers.append(blocker("runtime_config_snapshot", "snapshot_missing"))
        if replay.get("ok") is not True:
            release_blockers.append(blocker("replay", "release_replay_not_ready"))
        if bool(config_runtime_drift.get("drift")) or bool(config_runtime_drift.get("semantic_drift")):
            release_blockers.append(blocker("runtime_config", "config_drift"))
        if audit_health.get("ok") is False:
            release_blockers.append(blocker("audit_health", "audit_unhealthy"))
        if learning_worker.get("ok") is not True:
            release_blockers.append(
                blocker("learning_worker", "worker_boot_or_hash_not_ready")
            )
        release_blockers.extend(startup_status.get("blocking_components") or [])

        return {
            "schema_version": "readiness_dimensions.v2",
            # Frontend rendering is intentionally independent from trading and
            # governance authorization.  The compatibility bool remains top-level.
            "ready_for_frontend": not global_blockers,
            "ready_for_live_execution": not execution_blockers,
            "ready_for_live_alpha": not live_alpha_blockers,
            "ready_for_autonomous_mutation": not mutation_blockers,
            "ready_for_release": not release_blockers,
            "blockers": {
                "frontend": list(global_blockers),
                "live_execution": execution_blockers,
                "live_alpha": live_alpha_blockers,
                "autonomous_mutation": mutation_blockers,
                "release": release_blockers,
            },
            "authorization_boundary": {
                "frontend_readiness_authorizes_controls": False,
                "frontend_readiness_authorizes_release": False,
                "live_execution_uses": "ready_for_live_execution",
                "live_alpha_uses": "ready_for_live_alpha",
                "autonomous_mutation_uses": "ready_for_autonomous_mutation",
                "release_uses": "ready_for_release",
            },
        }

    def _runtime_config_snapshot_status(self) -> dict[str, Any]:
        try:
            from backend.services.evolution_ledger import current_runtime_config_snapshot

            snapshot = current_runtime_config_snapshot(db_path=self.db_path, create_if_missing=False)
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if not snapshot or not str(snapshot.get("config_hash") or ""):
            return {"ok": False, "status": "missing_snapshot"}
        created_at = _safe_float(snapshot.get("created_at"))
        age_sec = max(0.0, time.time() - created_at) if created_at > 0 else None
        return {
            "ok": True,
            "status": "available",
            "config_hash": str(snapshot.get("config_hash") or ""),
            "source": str(snapshot.get("source") or ""),
            "created_at": created_at,
            "age_seconds": round(age_sec, 3) if age_sec is not None else None,
        }

    def _runtime_config_overlay_status(self) -> dict[str, Any]:
        try:
            from backend.services.runtime_config_overlay import RuntimeConfigOverlayService

            return RuntimeConfigOverlayService(self.db_path).status()
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def _replay_status(self) -> dict[str, Any]:
        try:
            from backend.services.replay_harness import ReplayHarnessService

            return ReplayHarnessService(self.db_path).status()
        except Exception as exc:
            return {
                "ok": False,
                "schema_version": "replay_readiness.v1",
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _incident_control_status(self) -> dict[str, Any]:
        try:
            from backend.services.incident_controls import RuntimeIncidentControlService

            return RuntimeIncidentControlService(self.db_path).status()
        except Exception as exc:
            return {
                "schema_version": "runtime_incident_control.v1",
                "mode": "unknown",
                "valid_modes": [],
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _release_status(self) -> dict[str, Any]:
        try:
            from backend.services.release_control import ReleaseControlService

            latest = ReleaseControlService(self.db_path).latest_release()
            return {
                "schema_version": "release_readiness.v1",
                "ok": bool(latest.get("run_id")),
                "latest_release": latest,
            }
        except Exception as exc:
            return {
                "schema_version": "release_readiness.v1",
                "ok": False,
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }

    @staticmethod
    def _v15_phase0_status(readiness: dict[str, Any]) -> dict[str, Any]:
        try:
            from backend.services.v15_phase0 import V15Phase0CompletionService

            return V15Phase0CompletionService().build(readiness=readiness)
        except Exception as exc:
            return {
                "schema_version": "v15_phase0_completion.v1",
                "implementation_complete": False,
                "operationally_ready": False,
                "status": "error",
                "blockers": ["phase0_completion_error"],
                "error": f"{type(exc).__name__}: {exc}",
                "read_only": True,
                "updated_at": time.time(),
            }

    def _autonomy_health_status(
        self,
        *,
        live_status: dict[str, Any],
        system_health: dict[str, Any],
        governance: dict[str, Any],
        stability: dict[str, Any],
        replay: dict[str, Any],
        governance_freshness: dict[str, Any],
        model_status: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            from backend.services.autonomy_health import AutonomyHealthService

            return AutonomyHealthService(self.db_path).build(
                live_status=live_status,
                system_health=system_health,
                governance=governance,
                stability=stability,
                replay_status=replay,
                governance_freshness=governance_freshness,
                model_status=model_status,
            )
        except Exception as exc:
            return {
                "schema_version": "autonomy_health.v1",
                "score": 0.0,
                "posture": "frozen",
                "blockers": ["autonomy_health_error"],
                "error": f"{type(exc).__name__}: {exc}",
                "updated_at": time.time(),
                "read_only": True,
            }

    def _brain_state_status(self, readiness: dict[str, Any]) -> dict[str, Any]:
        try:
            from backend.services.v16_brain_snapshot import BrainStateService

            snapshot = BrainStateService(self.db_path).build(
                readiness=readiness,
                persist=True,
                source="backend_readiness",
            )
            return {
                "ok": True,
                "schema_version": "brain_state_readiness.v1",
                "status": "available",
                "snapshot_id": snapshot.get("snapshot_id"),
                "strategy_posture": snapshot.get("world_model", {}).get("strategy_posture", "unknown"),
                "hypothesis_count": len(snapshot.get("hypotheses") or []),
                "critic_verdict": snapshot.get("critic", {}).get("verdict", "unknown"),
                "read_only": True,
                "affects_trading": False,
                "latest_snapshot": snapshot,
            }
        except Exception as exc:
            return {
                "ok": False,
                "schema_version": "brain_state_readiness.v1",
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "read_only": True,
                "affects_trading": False,
            }

    def _brain_action_plan_status(self) -> dict[str, Any]:
        try:
            from backend.services.v16_brain_planning import BrainActionPlannerService

            status = BrainActionPlannerService(self.db_path).status(limit=20)
            status.setdefault("read_only", True)
            status.setdefault("affects_trading", False)
            return status
        except Exception as exc:
            return {
                "ok": False,
                "schema_version": "brain_action_plan_readiness.v1",
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "read_only": True,
                "affects_trading": False,
            }

    def _brain_action_plan_eval_status(self) -> dict[str, Any]:
        try:
            from backend.services.v16_brain_planning import BrainActionPlanEvaluatorService

            status = BrainActionPlanEvaluatorService(self.db_path).status(limit=20)
            status.setdefault("read_only", True)
            status.setdefault("affects_trading", False)
            return status
        except Exception as exc:
            return {
                "ok": False,
                "schema_version": "brain_action_plan_eval_readiness.v1",
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "read_only": True,
                "affects_trading": False,
            }

    def _brain_low_impact_execution_status(self) -> dict[str, Any]:
        try:
            from backend.services.v16_brain_planning import BrainLowImpactExecutorService

            status = BrainLowImpactExecutorService(self.db_path).status(limit=20)
            status.setdefault("low_impact_only", True)
            return status
        except Exception as exc:
            return {
                "ok": False,
                "schema_version": "brain_low_impact_execution_readiness.v1",
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "low_impact_only": True,
            }

    def _brain_medium_impact_governance_status(self) -> dict[str, Any]:
        try:
            from backend.services.v16_brain_planning import BrainMediumImpactGovernanceService

            status = BrainMediumImpactGovernanceService(self.db_path).status(limit=20)
            status.setdefault("medium_impact_governance", True)
            return status
        except Exception as exc:
            return {
                "ok": False,
                "schema_version": "brain_medium_impact_governance_readiness.v1",
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "medium_impact_governance": True,
            }

    def _brain_governance_candidate_status(self) -> dict[str, Any]:
        try:
            from backend.services.brain_governance_candidates import BrainGovernanceCandidateService

            status = BrainGovernanceCandidateService(self.db_path).status(limit=50)
            status.setdefault("candidate_lane_isolated", True)
            status.setdefault("policy_suggestion_bridge_manual_only", True)
            return status
        except Exception as exc:
            return {
                "ok": False,
                "schema_version": "brain_governance_candidate_readiness.v1",
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "candidate_lane_isolated": True,
                "policy_suggestion_bridge_manual_only": True,
            }

    def _v16_brain_orchestration_status(self) -> dict[str, Any]:
        try:
            from backend.services.v16_brain_orchestrator import V16BrainOrchestratorService

            return V16BrainOrchestratorService(self.db_path).status(limit=50)
        except Exception as exc:
            return {
                "ok": False,
                "schema_version": "v16_brain_orchestration_status.v1",
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "posterior_to_brain_closed": False,
                "command_to_candidate_closed": False,
                "boundary": {
                    "meta_brain_command_only": True,
                    "direct_mutation": False,
                },
            }

    def _entry_quality_governance_status(self) -> dict[str, Any]:
        try:
            from backend.services.entry_quality_governance import EntryQualityGovernanceService

            return EntryQualityGovernanceService(self.db_path).status()
        except Exception as exc:
            return {
                "ok": False,
                "schema_version": "entry_quality_governance_status.v1",
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _candidate_generation_context_coverage_status(self) -> dict[str, Any]:
        try:
            from backend.services.brain_governance_candidates import BrainGovernanceCandidateService

            return BrainGovernanceCandidateService(self.db_path).generation_context_coverage(limit=200)
        except Exception as exc:
            return {
                "ok": False,
                "schema_version": "candidate_generation_context_coverage.v1",
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "boundary": {
                    "read_only_generation_context_audit": True,
                    "does_not_create_candidates": True,
                    "does_not_modify_candidates": True,
                },
            }

    def _factor_pruning_governance_status(self) -> dict[str, Any]:
        try:
            from backend.services.factor_pruning_governance import FactorPruningGovernanceService

            return FactorPruningGovernanceService(self.db_path).status(limit=50)
        except Exception as exc:
            return {
                "ok": False,
                "schema_version": "factor_pruning_governance_status.v1",
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "boundary": {
                    "materializes_governance_candidates_only": True,
                    "does_not_write_policy_suggestion_directly": True,
                    "does_not_apply_factor_weights": True,
                },
            }

    def _factor_governance_effect_status(self) -> dict[str, Any]:
        try:
            from backend.services.factor_governance_effect_tracker import FactorGovernanceEffectTrackerService

            return FactorGovernanceEffectTrackerService(self.db_path).status(limit=50)
        except Exception as exc:
            return {
                "ok": False,
                "schema_version": "factor_governance_effect_tracker.v1",
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "boundary": {
                    "read_status_is_read_only": True,
                    "reconcile_uses_existing_governor": True,
                    "does_not_apply_factor_weights": True,
                },
            }

    def _learning_effect_quality_status(self) -> dict[str, Any]:
        from backend.services.learning_effect_quality import LearningEffectQualityService

        try:
            return LearningEffectQualityService(self.db_path).status(limit=1000)
        except Exception as exc:
            return {
                "ok": False,
                "schema_version": "learning_effect_quality.v1",
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "boundary": LearningEffectQualityService.boundary(),
            }

    @staticmethod
    def _runtime_factor_budget_status() -> dict[str, Any]:
        try:
            from alpha.runtime_factor_selection import runtime_factor_budget_status
            from config import runtime_config

            return runtime_factor_budget_status(runtime_config.shared().factor_signal_config)
        except Exception as exc:
            return {
                "ok": False,
                "schema_version": "runtime_factor_budget.v1",
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }

    @staticmethod
    def _runtime_health_projection_status() -> dict[str, Any]:
        try:
            from backend.services.runtime_health_projection import RuntimeHealthProjectionService

            return RuntimeHealthProjectionService().latest(max_age_seconds=180.0)
        except Exception as exc:
            return {
                "ok": False,
                "schema_version": "runtime_health_projection.v1",
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _brain_governance_candidate_review_status(self) -> dict[str, Any]:
        try:
            from backend.services.brain_governance_candidate_review import BrainGovernanceCandidateReviewService

            status = BrainGovernanceCandidateReviewService(self.db_path).status(limit=50)
            status.setdefault("review_only", True)
            status.setdefault("bridge_preview_only", True)
            return status
        except Exception as exc:
            return {
                "ok": False,
                "schema_version": "brain_governance_candidate_review_readiness.v1",
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "review_only": True,
                "bridge_preview_only": True,
            }

    def _candidate_bridge_review_coverage_status(self) -> dict[str, Any]:
        try:
            from backend.services.brain_governance_candidate_review import BrainGovernanceCandidateReviewService

            return BrainGovernanceCandidateReviewService(self.db_path).bridge_review_coverage(limit=200)
        except Exception as exc:
            return {
                "ok": False,
                "schema_version": "candidate_bridge_review_coverage.v1",
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "boundary": {
                    "read_only_bridge_coverage_audit": True,
                    "does_not_modify_policy_suggestion": True,
                    "does_not_submit_candidates": True,
                },
            }

    def _brain_live_ready_guardrail_status(self) -> dict[str, Any]:
        try:
            from backend.services.v16_brain_planning import BrainLiveReadyGuardrailService

            status = BrainLiveReadyGuardrailService(self.db_path).status(limit=20)
            status.setdefault("live_ready_guardrails", True)
            status.setdefault("tightening_only", True)
            return status
        except Exception as exc:
            return {
                "ok": False,
                "schema_version": "brain_live_ready_guardrail_readiness.v1",
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "live_ready_guardrails": True,
                "tightening_only": True,
            }

    def _proposal_registry_status(self) -> dict[str, Any]:
        try:
            return ProposalRegistryService(self.db_path).status()
        except Exception as exc:
            return {
                "ok": False,
                "schema_version": "proposal_registry_status.v1",
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _proposal_generation_context_coverage_status(self) -> dict[str, Any]:
        try:
            return ProposalRegistryService(self.db_path).generation_context_coverage(limit=500)
        except Exception as exc:
            return {
                "ok": False,
                "schema_version": "proposal_generation_context_coverage.v1",
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "boundary": {
                    "read_only_generation_context_audit": True,
                    "does_not_modify_policy_suggestion": True,
                    "does_not_apply_proposals": True,
                },
            }

    def _agent_authority_status(self) -> dict[str, Any]:
        try:
            return AgentAuthorityRegistryService().status(db_path=self.db_path)
        except Exception as exc:
            return {
                "ok": False,
                "schema_version": "agent_authority_status.v1",
                "status": "error",
                "registered_agents": 0,
                "unknown_sources": [],
                "contract_violations": [],
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _agent_scorecard_status(self) -> dict[str, Any]:
        try:
            scorecard = AgentScorecardService(self.db_path).scorecard(limit=300)
            return {
                "ok": bool(scorecard.get("ok")),
                "schema_version": "agent_scorecard_readiness.v1",
                "status": "available" if scorecard.get("items") else "empty",
                "summary": scorecard.get("summary") or {},
                "top_agents": (scorecard.get("items") or [])[:6],
                "boundary": scorecard.get("boundary") or AgentScorecardService.boundary(),
            }
        except Exception as exc:
            return {
                "ok": False,
                "schema_version": "agent_scorecard_readiness.v1",
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _agent_briefing_status(self) -> dict[str, Any]:
        try:
            briefing = AgentBriefingContextService(self.db_path).build(limit=20)
            return {
                "ok": bool(briefing.get("ok")),
                "schema_version": "agent_briefing_readiness.v1",
                "status": "available",
                "chain_health": briefing.get("chain_health") or {},
                "proposal_flow": briefing.get("proposal_flow") or {},
                "agent_scorecard": briefing.get("agent_scorecard") or {},
                "review_rules": briefing.get("review_rules") or {},
                "boundary": briefing.get("boundary") or AgentBriefingContextService.boundary(),
            }
        except Exception as exc:
            return {
                "ok": False,
                "schema_version": "agent_briefing_readiness.v1",
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _agent_chain_health_status(self) -> dict[str, Any]:
        try:
            return AgentScorecardService(self.db_path).chain_health(limit=300)
        except Exception as exc:
            return {
                "ok": False,
                "schema_version": "agent_chain_health.v1",
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }

    @staticmethod
    def _autonomous_blueprint_status(readiness: dict[str, Any]) -> dict[str, Any]:
        governance = dict(readiness.get("governance") or {})
        boundaries = dict(((readiness.get("v16") or {}).get("control_plane_boundaries") or {}))
        agent_authority = dict(readiness.get("agent_authority") or {})
        proposal_context = dict(readiness.get("proposal_generation_context_coverage") or {})
        candidate_context = dict(readiness.get("candidate_generation_context_coverage") or {})
        candidate_review = dict(readiness.get("candidate_bridge_review_coverage") or {})
        proposal_registry = dict(readiness.get("proposal_registry") or {})
        chain = dict(readiness.get("agent_chain_health") or {})
        live_ready = dict(readiness.get("brain_live_ready_guardrails") or {})
        checks = [
            {
                "component": "demo_nursery_learning_scope",
                "status": "ok" if governance.get("autonomy_mode") in {"demo_nursery", "demo_autonomous"} else "attention",
                "ok": governance.get("autonomy_mode") in {"demo_nursery", "demo_autonomous"},
                "autonomy_mode": governance.get("autonomy_mode"),
                "automatic_execution_enabled": bool(governance.get("automatic_execution_enabled")),
            },
            {
                "component": "agent_authority_contract",
                "status": agent_authority.get("status", "unknown"),
                "ok": bool(agent_authority.get("ok")) and not (agent_authority.get("unknown_sources") or []) and not (agent_authority.get("contract_violations") or []),
                "registered_agents": agent_authority.get("registered_agents", 0),
            },
            {
                "component": "proposal_generation_context",
                "status": proposal_context.get("status", "unknown"),
                "ok": proposal_context.get("status") == "ok" and int(proposal_context.get("missing_required_context_count") or 0) == 0,
                "legacy_missing_context_count": proposal_context.get("legacy_missing_context_count", 0),
            },
            {
                "component": "candidate_generation_context",
                "status": candidate_context.get("status", "unknown"),
                "ok": candidate_context.get("status") == "ok" and int(candidate_context.get("missing_required_context_count") or 0) == 0,
                "legacy_missing_context_count": candidate_context.get("legacy_missing_context_count", 0),
            },
            {
                "component": "candidate_bridge_review",
                "status": candidate_review.get("status", "unknown"),
                "ok": candidate_review.get("status") == "ok" and int(candidate_review.get("missing_required_review_count") or 0) == 0,
                "legacy_unreviewed_count": candidate_review.get("legacy_unreviewed_count", 0),
            },
            {
                "component": "proposal_registry_read_model",
                "status": "ok" if proposal_registry.get("ok") else "attention",
                "ok": bool(proposal_registry.get("ok")),
                "proposal_count": proposal_registry.get("proposal_count", 0),
                "conflict_count": proposal_registry.get("conflict_count", 0),
            },
            {
                "component": "memory_and_scorecard_feedback",
                "status": chain.get("status", "unknown"),
                "ok": chain.get("status") == "ok",
                "trade_feedback_summary": chain.get("trade_feedback_summary") or {},
            },
            {
                "component": "single_execution_boundary",
                "status": "ok" if all(
                    bool(boundaries.get(key))
                    for key in [
                        "risk_policy_service_required_for_future_actions",
                        "decision_policy_required_for_future_weight_writes",
                        "runtime_overlay_snapshot_required_for_future_mutations",
                        "proposal_registry_review_only",
                        "models_shadow_or_advisory_only",
                    ]
                ) else "attention",
                "ok": all(
                    bool(boundaries.get(key))
                    for key in [
                        "risk_policy_service_required_for_future_actions",
                        "decision_policy_required_for_future_weight_writes",
                        "runtime_overlay_snapshot_required_for_future_mutations",
                        "proposal_registry_review_only",
                        "models_shadow_or_advisory_only",
                    ]
                ),
            },
            {
                "component": "live_ready_guardrails",
                "status": live_ready.get("status", "unknown"),
                "ok": bool(live_ready.get("live_ready_guardrails")) and bool(live_ready.get("tightening_only", True)),
            },
        ]
        blockers = [item for item in checks if not item.get("ok")]
        status = "ok" if not blockers else "partial"
        return {
            "ok": status == "ok",
            "schema_version": "autonomous_trading_blueprint_status.v1",
            "status": status,
            "checks": checks,
            "blockers": blockers,
            "deviation_guard": {
                "does_not_expand_agent_authority": True,
                "does_not_bypass_risk_policy": True,
                "does_not_bypass_decision_policy": True,
                "does_not_create_second_execution_path": True,
                "readiness_observable": True,
            },
            "boundary": {
                "read_only_alignment_status": True,
                "does_not_execute_actions": True,
                "does_not_modify_runtime": True,
                "does_not_approve_proposals": True,
            },
        }

    def _live_autonomy_status(self, readiness: dict[str, Any]) -> dict[str, Any]:
        try:
            from backend.services.live_autonomy import LiveAutonomyService

            return LiveAutonomyService(self.db_path).status(readiness=readiness, refresh_proposals=False)
        except Exception as exc:
            return {
                "ok": False,
                "schema_version": "live_autonomy_status.v1",
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _autonomous_evolution_cycle_status(self, readiness: dict[str, Any]) -> dict[str, Any]:
        try:
            return AutonomousEvolutionCycleService(self.db_path).status(
                readiness=readiness,
                refresh_proposals=False,
            )
        except Exception as exc:
            return {
                "ok": False,
                "schema_version": "autonomous_evolution_cycle.v1",
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "boundary": AutonomousEvolutionCycleService.boundary(),
            }

    @staticmethod
    def _freshness_watchdog_status(
        *,
        governance_freshness: dict[str, Any],
        model_status: dict[str, Any],
    ) -> dict[str, Any]:
        tables = dict(governance_freshness.get("tables") or {})
        stale_tables = [
            name for name, item in tables.items()
            if str((item or {}).get("status") or "") != "fresh"
        ]
        max_age = 0.0
        for item in tables.values():
            age = item.get("age_seconds") if isinstance(item, dict) else None
            if isinstance(age, (int, float)):
                max_age = max(max_age, float(age))
        meta_report = (((model_status.get("meta_lightgbm") or {}).get("report") or {}))
        evaluated_count = int(meta_report.get("evaluated_count") or 0)
        status = "ok" if not stale_tables else "degraded"
        if evaluated_count <= 0:
            status = "degraded"
        return {
            "status": status,
            "stale_table_count": len(stale_tables),
            "stale_tables": stale_tables,
            "max_table_age_seconds": round(max_age, 3),
            "meta_shadow_evaluated_count": evaluated_count,
            "blocks_live_model_permission": True,
            "advisory_only": True,
        }

    @staticmethod
    def _runtime_weight_integrity_status() -> dict[str, Any]:
        try:
            from config.runtime_config import shared as runtime_config

            cfg = runtime_config()
            weights = dict(getattr(cfg, "factor_portfolio_weights", {}) or {})
            signal_cfg = dict(getattr(cfg, "factor_signal_config", {}) or {})
            from alpha.portfolio_compositor import resolve_factor_role

            alpha_signal_names = {
                name for name, item in signal_cfg.items()
                if resolve_factor_role(name, item if isinstance(item, dict) else None) == "alpha"
            }
            missing_weight = sorted(alpha_signal_names - set(weights))
            orphan_weight = sorted(set(weights) - set(signal_cfg))
            return {
                "ok": bool(weights),
                "weight_count": len(weights),
                "signal_config_count": len(signal_cfg),
                "alpha_signal_config_count": len(alpha_signal_names),
                "signal_without_weight": missing_weight[:50],
                "weight_without_signal_config": orphan_weight[:50],
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _factor_blend_health_status(self) -> dict[str, Any]:
        try:
            from backend.services.factor_blend_health import FactorBlendHealthService

            return FactorBlendHealthService(self.db_path).build()
        except Exception as exc:
            return {"ok": False, "schema_version": "factor_blend_health.v1", "status": "error", "error": str(exc)}

    def _factor_pruning_candidates_status(self) -> dict[str, Any]:
        try:
            from backend.services.factor_pruning_candidates import FactorPruningCandidateService

            return FactorPruningCandidateService(self.db_path).build()
        except Exception as exc:
            return {"ok": False, "schema_version": "factor_pruning_candidates.v1", "status": "error", "error": str(exc)}

    def _high_load_status(self, market_session: dict[str, Any]) -> dict[str, Any]:
        latest = self._latest_offmarket_audit()
        return {
            "allowed_now": bool(market_session.get("high_load_allowed")),
            "profile": str(market_session.get("high_load_profile") or "disabled"),
            "session_status": str(market_session.get("status") or ""),
            "can_run_training_with_positions": str(market_session.get("high_load_profile") or "") == "limited_with_positions",
            "requires_closed_confirmation": True,
            "latest_audit": latest,
        }

    def _latest_offmarket_audit(self) -> dict[str, Any]:
        conn = _connect_state(self.db_path)
        try:
            if not _table_exists(conn, "offmarket_high_load_job_audit"):
                return {}
            row = _execute(
                conn,
                """
                SELECT *
                FROM offmarket_high_load_job_audit
                ORDER BY started_at DESC
                LIMIT 1
                """
            ).fetchone()
            if not row:
                return {}
            return {
                "audit_id": str(row["audit_id"] or ""),
                "job_name": str(row["job_name"] or ""),
                "status": str(row["status"] or ""),
                "session_status": str(row["session_status"] or ""),
                "high_load_profile": str(row["high_load_profile"] or ""),
                "error": str(row["error"] or ""),
                "started_at": _safe_float(row["started_at"]),
                "finished_at": _safe_float(row["finished_at"]),
                "result": _loads(row["result_json"], {}),
            }
        finally:
            conn.close()

    @staticmethod
    def _system_health() -> dict[str, Any]:
        line = BackendReadinessService._latest_system_health_line()
        if not line:
            return {
                "overall": "unknown",
                "display_overall": "unknown",
                "score": 0.0,
                "components": {},
                "blocking_components": [],
                "known_observations": [],
            }
        components = BackendReadinessService._parse_components(line)
        overall = BackendReadinessService._parse_token_after(line, "overall=") or "unknown"
        score = _safe_float(BackendReadinessService._parse_token_after(line, "score="))
        blocking = []
        observations = []
        for name, status in components.items():
            status_text = str(status)
            if status_text not in {"degraded", "critical"}:
                continue
            if name in BLOCKING_COMPONENTS and status_text == "critical":
                blocking.append({"component": name, "status": status_text})
            else:
                observations.append(
                    {
                        "component": name,
                        "status": status_text,
                        "classification": KNOWN_OBSERVATION_COMPONENTS.get(name, "non_blocking_observation"),
                    }
                )
        display_overall = "critical" if blocking else "degraded" if observations else overall
        return {
            "overall": overall,
            "display_overall": display_overall,
            "score": score,
            "components": components,
            "blocking_components": blocking,
            "known_observations": observations,
            "source": str(LOG_PATH),
        }

    @staticmethod
    def _metrics_status() -> dict[str, Any]:
        try:
            from monitor.metrics import metrics_backend_status

            return metrics_backend_status()
        except Exception as exc:
            return {
                "ok": False,
                "status": "degraded",
                "metrics_backend": "fallback",
                "prometheus_available": False,
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _risk_metrics_status(self) -> dict[str, Any]:
        from backend.risk.metrics_snapshot import SNAPSHOT_KEY

        try:
            conn = _connect_state(self.db_path)
            try:
                if not _table_exists(conn, "runtime_kv"):
                    return {
                        "ok": False,
                        "status": "missing",
                        "var_status": "unknown",
                        "source": f"runtime_kv:{SNAPSHOT_KEY}",
                    }
                row = _execute(
                    conn,
                    "SELECT value_json FROM runtime_kv WHERE key=? LIMIT 1",
                    (SNAPSHOT_KEY,),
                ).fetchone()
            finally:
                conn.close()
        except Exception as exc:
            return {
                "ok": False,
                "status": "error",
                "var_status": "unknown",
                "source": f"runtime_kv:{SNAPSHOT_KEY}",
                "error": f"{type(exc).__name__}: {exc}",
            }
        if not row:
            return {
                "ok": False,
                "status": "missing",
                "var_status": "unknown",
                "source": f"runtime_kv:{SNAPSHOT_KEY}",
            }
        try:
            raw = row["value_json"]
        except (KeyError, TypeError):
            raw = row[0]
        snapshot = _loads(raw, {})
        components = dict(snapshot.get("components") or {})
        var = dict(components.get("var") or {})
        status = str(snapshot.get("status") or "unknown")
        var_status = str(var.get("status") or "unknown")
        contract_ok = snapshot.get("schema_version") == SNAPSHOT_KEY
        return {
            "ok": bool(
                contract_ok
                and status not in {"unknown", "stale", "error"}
                and var_status == "known"
            ),
            "status": status,
            "var_status": var_status,
            "schema_version": str(snapshot.get("schema_version") or ""),
            "as_of": snapshot.get("as_of"),
            "input_fingerprint": str(
                snapshot.get("input_fingerprint") or ""
            ),
            "source_window_start": str(
                snapshot.get("source_window_start") or ""
            ),
            "source_window_end": str(
                snapshot.get("source_window_end") or ""
            ),
            "sample_count": int(snapshot.get("sample_count") or 0),
            "current_var": var,
            "shadow_var_99": dict(components.get("var_shadow_99") or {}),
            "source": f"runtime_kv:{SNAPSHOT_KEY}",
            "read_only": True,
        }

    @staticmethod
    def _latest_system_health_line() -> str:
        if not LOG_PATH.exists():
            return ""
        try:
            with LOG_PATH.open("rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(size - 250000, 0))
                lines = f.read().decode("utf-8", errors="replace").splitlines()
        except Exception:
            return ""
        for line in reversed(lines):
            if "[system_health]" in line and "components=" in line:
                return line
        return ""

    @staticmethod
    def _parse_components(line: str) -> dict[str, str]:
        marker = "components="
        if marker not in line:
            return {}
        after = line.split(marker, 1)[1]
        raw = after.split(" errors=", 1)[0].strip()
        try:
            parsed = ast.literal_eval(raw)
            if isinstance(parsed, dict):
                return {str(k): str(v) for k, v in parsed.items()}
        except Exception:
            return {}
        return {}

    @staticmethod
    def _parse_token_after(line: str, marker: str) -> str:
        if marker not in line:
            return ""
        after = line.split(marker, 1)[1]
        return after.split()[0].strip()
