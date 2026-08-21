from __future__ import annotations

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
from backend.core.db_helpers import (
    conn_is_pg as _conn_is_pg,
    execute as _execute,
    pg_sql as _sql,
)
from backend.services.canonical_v2_reader import (
    iter_counterfactual_rows,
    iter_parameter_template_lifecycle_rows,
    iter_review_rows,
    iter_supervisor_trace_rows,
    review_row,
)
from backend.services.fact_envelope import DEFAULT_STALE_AFTER_SEC
from backend.services.review_contract import review_has_system_contamination
from backend.services.stability import measure, record_timing, timing_snapshot


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
        governance_freshness = self._timed_component("governance_freshness", self._governance_freshness_status)
        runtime_weight_integrity = self._timed_component("runtime_weight_integrity", self._runtime_weight_integrity_status)
        factor_blend_health = self._timed_component("factor_blend_health", self._factor_blend_health_status)
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
            "governance_freshness": governance_freshness,
            "runtime_weight_integrity": runtime_weight_integrity,
            "factor_blend_health": factor_blend_health,
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
                "offmarket_high_load_audits": "/api/learning/model/offmarket-high-load/audits",
                "must_not_call_live_mutation_from_model_pages": True,
            },
            "blockers": blockers,
            "known_observations": known_observations,
        }
        phase0 = self._v15_phase0_status(payload)
        payload["v15"]["phase0"] = phase0
        payload["v15_phase0"] = phase0
        v16_blockers = list(
            readiness_dimensions.get("blockers", {}).get("autonomous_mutation") or []
        )
        payload["v16"] = {
            "schema_version": "v16_readiness_contract.v1",
            "phase": "phase5_live_ready_guardrails",
            "ok": not v16_blockers,
            "status": "ready" if not v16_blockers else "blocked",
            "blocker_count": len(v16_blockers),
            "control_plane_boundaries": {
                "read_only": True,
                "affects_trading": False,
                "does_not_expand_agent_authority": True,
                "does_not_bypass_risk_policy": True,
                "does_not_create_second_execution_path": True,
                "meta_brain_command_only": True,
                "v16_command_owner": True,
                "v16_direct_policy_suggestion_write": False,
                "v16_direct_runtime_mutation": False,
                "v16_posterior_must_dispatch_to_specialist": True,
                "candidate_review_llm_advisory_only": True,
                "candidate_bridge_requires_review": True,
                "risk_policy_service_required_for_future_actions": True,
                "decision_policy_required_for_future_weight_writes": True,
                "runtime_overlay_snapshot_required_for_future_mutations": True,
                "models_shadow_or_advisory_only": True,
                "proposal_registry_review_only": True,
                "agent_authority_registry_is_source_of_truth": True,
                "live_autonomy_requires_manual_unlock": True,
            },
            "detail_endpoints": {
                "brain_state": "/api/ops/brain/state",
                "brain_memory": "/api/ops/brain/memory",
                "brain_commands": "/api/ops/brain/commands",
                "action_plans": "/api/ops/brain/action-plans",
                "action_plan_evals": "/api/ops/brain/action-plan-evals",
                "low_impact_executions": "/api/ops/brain/low-impact-executions",
                "medium_impact_governance": "/api/ops/brain/medium-impact-governance",
                "governance_candidates": "/api/ops/brain/governance-candidates",
                "governance_candidate_reviews": "/api/ops/brain/governance-candidate-reviews",
                "live_ready_guardrails": "/api/ops/brain/live-ready-guardrails",
                "proposal_registry": "/api/ops/autonomy/proposals",
                "agent_authority": "/api/ops/agent-authority",
                "agent_scorecard": "/api/ops/agent-scorecard",
                "agent_briefing": "/api/ops/agent-briefing",
                "agent_chain_health": "/api/ops/agent-chain-health",
                "live_autonomy": "/api/ops/autonomy/live-status",
                "autonomous_evolution_cycle": "/api/ops/autonomy/evolution-cycle",
            },
        }
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
        # This is an advisory projection only.  Memory integrity must be
        # visible beside learning readiness, but it must not become a second
        # readiness or trading-authority gate.
        from backend.services.memory_integrity import MemoryIntegrityReportService

        memory_integrity = MemoryIntegrityReportService(self.db_path).build()
        try:
            conn = _connect_state(self.db_path)
        except Exception as exc:
            return {
                **result,
                "ok": False,
                "reason": f"state_unavailable:{exc}",
                "memory_integrity": memory_integrity,
            }
        try:
            review_map = {
                str(row.get("review_id") or ""): row
                for row in iter_review_rows(conn, limit=0)
            }
            maturity_rows = []
            for item in iter_counterfactual_rows(conn, limit=0, reverse=True):
                value = dict(item)
                review_id = str(value.get("review_id") or "")
                review = review_map.get(review_id)
                value["source_review_id"] = review_id if review is not None else ""
                value["source_review_json"] = (review or {}).get("review_json") or {}
                maturity_rows.append(value)
            canary_started_at = 0.0
            canary_suggestion_id = ""
            canary_template_id = ""
            shadow_position_ids: set[str] = set()
            if _table_exists(conn, "policy_suggestion"):
                from backend.services.position_supervisor_governance import (
                    list_position_supervisor_canary_candidates,
                )

                candidates = list_position_supervisor_canary_candidates(conn, limit=100)
                candidate = next(
                    (item for item in candidates if item.get("status") == "applied"),
                    candidates[0] if candidates else None,
                )
                canary_started_at = _safe_float(candidate.get("created_at")) if candidate else 0.0
                canary_suggestion_id = str(candidate.get("suggestion_id") or "") if candidate else ""
                canary_template_id = str(candidate.get("scope_key") or "") if candidate else ""
            if canary_template_id:
                shadow_position_ids = {
                    str(row.get("position_id") or "")
                    for row in iter_supervisor_trace_rows(
                        conn,
                        limit=0,
                        stage="learning_shadow",
                        reverse=False,
                    )
                    if str(row.get("template_id") or "") == canary_template_id
                    and str(row.get("execution_status") or "") == "observation_only"
                    and str(row.get("trace_integrity") or "") == "recovered"
                    and str(row.get("execution_reason") or "")
                    == f"learning_worker_candidate_replay:{canary_suggestion_id}"
                    and _safe_float(row.get("event_ts")) >= canary_started_at
                }
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
                    or review_has_system_contamination(item.get("source_review_json") or {})
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
            try:
                from backend.services.learning_application_store import (
                    LearningApplicationStore,
                )

                for eff in LearningApplicationStore(self.db_path).iter_effects():
                    if str(eff.get("status") or "") in (
                        "prepared",
                        "observing",
                        "mixed",
                    ):
                        active_effects += 1
                        effect_ages.append(
                            max(0.0, now - _safe_float(eff.get("updated_at")))
                        )
            except Exception:
                active_effects = 0
                effect_ages = []

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
            "memory_integrity": memory_integrity,
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

        artifact_services = {
            "open_quality_lightgbm": OpenQualityLightGBMService(db_path=self.db_path),
            "position_quality_lightgbm": PositionQualityLightGBMService(db_path=self.db_path),
            "factor_governance_lightgbm": FactorGovernanceLightGBMService(db_path=self.db_path),
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
            "model_count": len(artifact_services),
            "promotion_gates": promotion_gates,
            "influence": influence_status,
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
            cfg = None
            try:
                from config.runtime_config import shared as runtime_config

                cfg = runtime_config()
                autonomy_mode = str(getattr(cfg, "autonomy_mode", "") or "manual")
                demo_auto_apply = bool(getattr(cfg, "autonomy_demo_auto_apply", False))
                send_orders = bool(getattr(cfg, "ctrader_send_orders", False))
            except Exception:
                autonomy_mode = "unknown"
                demo_auto_apply = False
                send_orders = False
            try:
                from backend.services.position_supervisor_templates import (
                    normalize_position_supervisor_template,
                )

                active_template = normalize_position_supervisor_template(
                    getattr(cfg, "position_supervisor_template_id", "")
                )
            except Exception as exc:
                active_template = {
                    "template_id": "",
                    "status": "invalid",
                    "risk_boundary": {},
                    "error": f"{type(exc).__name__}: {exc}",
                }
            active_template_id = str(active_template.get("template_id") or "")
            active_template_status = str(active_template.get("status") or "")
            active_boundary = dict(active_template.get("risk_boundary") or {})
            active_execution_mode = str(
                active_boundary.get("adaptive_execution_mode") or ""
            ).strip().lower()
            execution_intent_table_available = bool(
                _table_exists(conn, "broker_execution_intent")
            )
            execution_schema_status: dict[str, Any] = {
                "ok": execution_intent_table_available,
                "current_version": None,
                "minimum_version": None,
                "reason": (
                    "broker_execution_intent_table_missing"
                    if not execution_intent_table_available
                    else ""
                ),
            }
            if _conn_is_pg(conn):
                try:
                    from backend.core.state_schema_migrations import (
                        STATE_SCHEMA_MIN_VERSION,
                        state_schema_status,
                    )

                    state_status = state_schema_status(
                        conn,
                        minimum_version=STATE_SCHEMA_MIN_VERSION,
                    )
                    execution_schema_status.update(
                        {
                            "ok": bool(
                                execution_intent_table_available
                                and state_status.get("ok")
                            ),
                            "current_version": state_status.get("current_version"),
                            "minimum_version": state_status.get("minimum_version"),
                            "missing_required_versions": list(
                                state_status.get("missing_required_versions") or []
                            ),
                            "reason": (
                                "state_schema_below_runtime_minimum"
                                if not bool(state_status.get("ok"))
                                else execution_schema_status["reason"]
                            ),
                        }
                    )
                except Exception as exc:
                    execution_schema_status.update(
                        {
                            "ok": False,
                            "reason": f"state_schema_status_unavailable:{type(exc).__name__}",
                        }
                    )
            supervisor_execution_authorized = bool(
                active_template_status == "active"
                and active_execution_mode == "governed_execute"
                and send_orders
                and execution_schema_status["ok"]
            )
            if not execution_schema_status["ok"]:
                authority_source = "broker_execution_intent_schema_unavailable"
            elif not send_orders:
                authority_source = "broker_orders_disabled"
            elif active_template_id and active_execution_mode == "governed_execute":
                authority_source = f"position_supervisor_template:{active_template_id}"
            else:
                authority_source = "position_supervisor_authority_unavailable"
            recent_execution = {"status": "none"}
            trace_row = next(
                (
                    row
                    for row in iter_supervisor_trace_rows(conn, limit=0, reverse=True)
                    if str(row.get("action") or "").strip().lower() in {"close", "reduce", "tighten"}
                ),
                None,
            )
            if trace_row:
                recent_execution = {
                    "status": str(trace_row.get("outcome") or trace_row.get("execution_status") or "unknown"),
                    "position_id": str(trace_row.get("position_id") or ""),
                    "decision_id": str(trace_row.get("decision_id") or ""),
                    "stage": str(trace_row.get("stage") or ""),
                    "outcome": str(trace_row.get("outcome") or ""),
                    "execution_status": str(trace_row.get("execution_status") or ""),
                    "execution_reason": str(trace_row.get("execution_reason") or ""),
                    "event_ts": _safe_float(trace_row.get("event_ts")),
                }
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
            return {
                "policy_suggestion_counts": counts,
                "policy_suggestion_counts_raw": counts,
                "policy_suggestion_counts_normalized": normalized_counts,
                "pending_review_count": int(counts.get("proposed", 0)) + int(counts.get("pending_review", 0)),
                "autonomous_pending_count": int(normalized_counts.get("proposed", 0)),
                "autonomy_mode": autonomy_mode,
                "autonomy_demo_auto_apply": demo_auto_apply,
                "supervisor_execution_authorized": supervisor_execution_authorized,
                "authority_source": authority_source,
                "active_template_id": active_template_id,
                "active_template_mode": active_execution_mode,
                "execution_schema": execution_schema_status,
                "broker_execution_enabled": send_orders,
                "recent_supervisor_execution": recent_execution,
                "factor_governance_runtime": self._factor_governance_runtime_status(),
            }
        finally:
            conn.close()

    def _factor_governance_runtime_status(self) -> dict[str, Any]:
        try:
            from config.runtime_config import (
                effective_factor_governance_cron,
                shared as runtime_config,
            )

            cfg = runtime_config()
            enabled = bool(getattr(cfg, "factor_governance_enabled", True))
            cron = effective_factor_governance_cron(cfg)
            stale_after_sec = float(getattr(cfg, "factor_governance_stale_after_sec", 7200.0) or 7200.0)
        except Exception:
            enabled = True
            cron = "15,30,45 * * * *"
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
        elif str(latest_run.get("status") or "").lower().startswith("blocked"):
            status = str(latest_run.get("status") or "blocked")
            ok = False
            stale = False
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
            "factor_governance_shadow_audit",
            "position_quality_shadow_audit",
            "shadow_factor_perf",
            "factor_health",
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
            lifecycle_rows = iter_parameter_template_lifecycle_rows(
                conn,
                limit=1,
                reverse=True,
            )
            latest_lifecycle = (
                _safe_float(lifecycle_rows[0].get("timestamp"))
                if lifecycle_rows
                else 0.0
            )
            lifecycle_age = max(0.0, now - latest_lifecycle) if latest_lifecycle > 0 else None
            freshness["canonical.parameter_template_lifecycle"] = {
                "latest_ts": latest_lifecycle,
                "age_seconds": round(lifecycle_age, 3) if lifecycle_age is not None else None,
                "status": (
                    "fresh"
                    if lifecycle_age is not None and lifecycle_age <= 3 * 86400
                    else "stale_or_empty"
                ),
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
        if bool(loop.get("running")):
            # The loop/readiness pair describes one lifecycle authority.  Do
            # not publish both ``running_loop_not_ready`` and
            # ``not_accepting_new_risk`` for the same failed admission.
            if loop_readiness.get("ok") is False:
                execution_blockers.append(
                    blocker("live_loop", "not_ready", details=loop_readiness)
                )
            elif loop.get("accepting_new_risk") is False:
                execution_blockers.append(
                    blocker(
                        "live_loop",
                        "not_accepting_new_risk",
                        blockers=sorted(
                            {
                                str(item)
                                for item in list(loop.get("blockers") or [])
                                if str(item)
                            }
                        ),
                    )
                )
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
            directional_guard = dict(
                factor_blend_health.get("directional_portfolio_guard") or {}
            )
            projection_status = str(
                factor_blend_health.get("projection_status") or ""
            ).lower()
            live_alpha_blockers.append(
                blocker(
                    "factor_blend_health",
                    (
                        "runtime_factor_selection_projection_unavailable"
                        if projection_status in {"missing", "stale", "unavailable"}
                        else "directional_portfolio_degraded"
                        if str(directional_guard.get("status") or "")
                        in {"degraded", "unavailable"}
                        else "live_factor_blend_unhealthy"
                    ),
                    status=blend_status or "unknown",
                    directional_portfolio_guard=directional_guard,
                )
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














    @staticmethod
    def _runtime_health_projection_status() -> dict[str, Any]:
        try:
            from backend.services.runtime_health_projection import RuntimeHealthProjectionService
            from backend.services.postgres_backup_health import PostgresBackupHealthService

            projection = RuntimeHealthProjectionService().latest(max_age_seconds=180.0)
            # The Windows client receipt recorder is the canonical writer of
            # this external observation. Readiness only carries it alongside
            # the existing runtime projection; it does not turn backup
            # freshness into a trading or release verdict.
            return {
                **projection,
                "postgres_backup": PostgresBackupHealthService().latest(),
            }
        except Exception as exc:
            return {
                "ok": False,
                "schema_version": "runtime_health_projection.v1",
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
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
        status = "ok" if not stale_tables else "degraded"
        return {
            "status": status,
            "stale_table_count": len(stale_tables),
            "stale_tables": stale_tables,
            "max_table_age_seconds": round(max_age, 3),
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
            from backend.services.runtime_factor_selection_projection import (
                RuntimeFactorSelectionProjectionService,
            )

            projection = RuntimeFactorSelectionProjectionService(
                self.db_path
            ).latest(max_age_seconds=900.0)
            projection_status = str(projection.get("status") or "unavailable")
            directional_guard = dict(
                projection.get("directional_portfolio_guard") or {}
            )
            guard_status = str(
                directional_guard.get("status") or "unavailable"
            ).lower()
            healthy = bool(projection.get("ok")) and guard_status == "healthy"
            return {
                "ok": healthy,
                "schema_version": "factor_blend_health.v1",
                "status": "healthy" if healthy else "critical",
                "source": "runtime_kv:runtime_factor_selection.v1",
                "projection_status": projection_status,
                "reason_code": (
                    None
                    if healthy
                    else "runtime_factor_selection_projection_unavailable"
                    if projection_status in {"missing", "stale", "unavailable"}
                    else "directional_portfolio_degraded"
                ),
                "age_seconds": projection.get("age_seconds"),
                "stale_after_seconds": 900.0,
                "config_version": projection.get("config_version"),
                "config_hash": str(projection.get("config_hash") or ""),
                "selection_fingerprint": str(
                    projection.get("selection_fingerprint") or ""
                ),
                "selected_factor_count": len(
                    list(projection.get("selected_factor_ids") or [])
                ),
                "alpha_voter_count": int(
                    projection.get("alpha_voter_count") or 0
                ),
                "directional_portfolio_guard": directional_guard,
            }
        except Exception as exc:
            return {
                "ok": False,
                "schema_version": "factor_blend_health.v1",
                "status": "critical",
                "source": "runtime_kv:runtime_factor_selection.v1",
                "projection_status": "unavailable",
                "reason_code": "runtime_factor_selection_projection_unavailable",
                "directional_portfolio_guard": {},
                "error": str(exc),
            }

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
        # monitor.system_health is the only producer. Log lines remain
        # diagnostic artifacts and must never become a second readiness source.
        source = "monitor.system_health.shared()"
        stale_after_seconds = DEFAULT_STALE_AFTER_SEC["system_health"]
        try:
            from monitor.system_health import shared as _system_health_shared

            report = _system_health_shared().get_last_report()
        except Exception as exc:
            return {
                "overall": "unknown",
                "display_overall": "unknown",
                "score": 0.0,
                "components": {},
                "blocking_components": [
                    {
                        "component": "system_health",
                        "status": "error",
                        "reason": "system_health_source_error",
                    }
                ],
                "known_observations": [],
                "source": source,
                "status": "error",
                "reason_code": "system_health_source_error",
                "error": f"{type(exc).__name__}: {exc}",
                "observed_at": 0.0,
                "age_seconds": None,
                "stale_after_seconds": stale_after_seconds,
            }

        observed_at = (
            _safe_float(getattr(report, "ts", 0.0)) if report is not None else 0.0
        )
        age_seconds = (
            max(0.0, time.time() - observed_at) if observed_at > 0 else None
        )
        if report is None or observed_at <= 0:
            reason_code = (
                "system_health_snapshot_unavailable"
                if report is None
                else "system_health_timestamp_unknown"
            )
            return {
                "overall": "unknown",
                "display_overall": "unknown",
                "score": 0.0,
                "components": {},
                "blocking_components": [
                    {
                        "component": "system_health",
                        "status": "unknown",
                        "reason": reason_code,
                    }
                ],
                "known_observations": [],
                "source": source,
                "status": "unknown",
                "reason_code": reason_code,
                "observed_at": observed_at or None,
                "age_seconds": age_seconds,
                "stale_after_seconds": stale_after_seconds,
            }

        raw_components = getattr(report, "components", None) or {}
        components = {
            str(name): str(getattr(component, "status", "") or "")
            for name, component in raw_components.items()
        }
        overall = str(getattr(report, "overall", "") or "") or "unknown"
        score = round(_safe_float(getattr(report, "overall_score", 0.0)), 2)
        blocking = []
        observations = []
        for name, status_text in components.items():
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
        stale = age_seconds is not None and age_seconds > stale_after_seconds
        if stale:
            blocking.append(
                {
                    "component": "system_health",
                    "status": "stale",
                    "reason": "system_health_snapshot_stale",
                }
            )
        display_overall = (
            "stale"
            if stale
            else "critical"
            if blocking
            else "degraded"
            if observations
            else overall
        )
        return {
            "overall": overall,
            "display_overall": display_overall,
            "score": score,
            "components": components,
            "blocking_components": blocking,
            "known_observations": observations,
            "source": source,
            "status": "stale" if stale else "known",
            "reason_code": "system_health_snapshot_stale" if stale else None,
            "observed_at": observed_at,
            "age_seconds": age_seconds,
            "stale_after_seconds": stale_after_seconds,
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
