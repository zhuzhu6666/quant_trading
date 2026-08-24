"""Ops API endpoints: alerts, auto-recovery, weekly reports, experiments."""
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException
import time
import uuid
from typing import Annotated, Any

from pydantic import BaseModel, Field

from backend.core.auth import RequireRecentStepUp, RequireUser, require_recent_step_up
from backend.services.agent_authority import AgentAuthorityRegistryService
from backend.services.agent_scorecard import AgentScorecardService
from backend.services.agent_briefing import AgentBriefingContextService
from backend.services.autonomous_demo_apply_stepper import AutonomousDemoApplyStepper
from backend.services.autonomous_evolution_cycle import AutonomousEvolutionCycleService
from backend.services.autonomous_evolution_runner import AutonomousEvolutionNurseryRunner
from backend.services.autonomy_health import AutonomyHealthService
from backend.services.backend_readiness import BackendReadinessService
from backend.services.backend_readiness_snapshot import BackendReadinessSnapshotService
from backend.services.api_fact_views import (
    alerts_fact_payload,
    live_autonomy_evaluation_fact_payload,
    live_autonomy_status_fact_payload,
    readiness_fact_payload,
    recovery_fact_payload,
)
from backend.services.fact_envelope import DEFAULT_STALE_AFTER_SEC, attach_fact
from backend.services.ops_governance_fact_views import (
    autonomy_scope_enforcement_fact_payload,
    governance_mutation_fact_payload,
    incident_control_status_fact_payload,
    ledger_read_fact_payload,
    persisted_record_fact_payload,
    proposal_refresh_fact_payload,
    release_approval_trail_fact_payload,
    runtime_config_projection_observation,
    scope_approval_fact_payload,
    scope_enforcement_read_fact_payload,
    unverified_compat_fact_payload,
    v15_phase0_fact_payload,
)
from backend.services.brain_governance_candidates import BrainGovernanceCandidateService
from backend.services.brain_governance_candidate_review import BrainGovernanceCandidateReviewService
from backend.services.factor_governance_effect_tracker import FactorGovernanceEffectTrackerService
from backend.services.factor_pruning_governance import FactorPruningGovernanceService
from backend.services.governance_expansion_control import GovernanceExpansionControlService
from backend.services.incident_controls import RuntimeIncidentControlService
from backend.services.live_autonomy import LiveAutonomyService
from backend.services.memory_integrity import MemoryIntegrityReportService
from backend.services.proposal_registry import ProposalRegistryService
from backend.services.release_control import ReleaseControlService
from backend.services.replay_harness import ReplayHarnessService
from backend.services.stability import TimedCache
from backend.services.v15_phase0 import V15Phase0CompletionService
from backend.services.v16_brain_snapshot import BrainStateService, BrainMemoryService
from backend.services.v16_brain_orchestrator import V16BrainOrchestratorService
from backend.services.v16_brain_planning import (
    BrainActionPlannerService, BrainActionPlanEvaluatorService,
    BrainLowImpactExecutorService, BrainMediumImpactGovernanceService,
    BrainLiveReadyGuardrailService,
)
from monitor.auto_recovery import AutoRecovery
from research.report_generator import WeeklyReport
from research.experiment_tracker import ExperimentTracker
from risk.policy_service import INCIDENT_MODE_RANK
router = APIRouter(prefix="/api/ops", tags=["ops"])

# Singletons (lazy init)
_auto_recovery: AutoRecovery | None = None
_report_gen: WeeklyReport | None = None
_READINESS_CACHE = TimedCache()
_BACKEND_READINESS_TTL_SEC = 180.0


class IncidentControlRequest(BaseModel):
    mode: str
    reason: str = ""
    confirm_thaw: bool = False
    v16_command_id: str = ""
    v16_claim_token: str = ""


class IncidentPlaybookRequest(BaseModel):
    scenario: str = "unknown"
    severity: str = "medium"
    release_run_id: str = ""
    created_by: str = "api:ops.incident_playbook"


class IncidentPlaybookEventRequest(BaseModel):
    event_type: str = "evidence_linked"
    actor: str = "api:ops.incident_playbook"
    status: str = "recorded"
    evidence_refs: dict[str, Any] | list[Any] = Field(default_factory=dict)
    notes: str = ""


class AutonomyScopeApprovalRequest(BaseModel):
    actor: str = "api:ops.autonomy_health"
    decision: str = "recorded"
    reason: str = ""
    snapshot_id: str = ""


class AutonomyScopeEnforcementRequest(BaseModel):
    actor: str = "api:ops.autonomy_health"
    reason: str = ""
    snapshot_id: str = ""


class ReleaseRunStartRequest(BaseModel):
    release_class: str = "daily_autonomous_mutation"
    summary: dict[str, Any] = Field(default_factory=dict)
    tests: list[dict[str, Any]] = Field(default_factory=list)
    rollback_ref: dict[str, Any] = Field(default_factory=dict)
    created_by: str = "api:ops.release"


class ReleaseRunFinishRequest(BaseModel):
    status: str = "completed"
    summary: dict[str, Any] | None = None
    tests: list[dict[str, Any]] | None = None
    rollback_ref: dict[str, Any] | None = None


class ReleaseApprovalEventRequest(BaseModel):
    action: str = "approval_decision"
    actor: str = "api:ops.release"
    decision: str = "recorded"
    reason: str = ""
    evidence_refs: dict[str, Any] | list[Any] = Field(default_factory=dict)


class BrainLowImpactExecutionRequest(BaseModel):
    limit: int = 1
    allow_tighten: bool = False
    replay_lookback_days: float = 1.0
    replay_limit: int = 100


class BrainRefreshRequest(BaseModel):
    limit: int = Field(default=50, ge=1, le=200)
    idempotency_key: str = Field(default="", max_length=160)


class BrainMediumImpactGovernanceRequest(BaseModel):
    limit: int = 4
    allow_tighten_low_health: bool = False


class FactorPruningGovernanceRequest(BaseModel):
    limit: int = 50
    min_priority: float = 0.75


class FactorPruningGovernancePromoteRequest(BaseModel):
    limit: int = 50
    min_evidence_score: float = 0.9
    require_weak_health: bool = True


class FactorPruningGovernanceBridgeRequest(BaseModel):
    limit: int = 5
    require_demo_nursery: bool = True
    actor: str = "api:ops.factor_pruning_governance.bridge_ready"


class FactorGovernanceEffectReconcileRequest(BaseModel):
    limit: int = 50


class BrainGovernanceCandidateSubmitRequest(BaseModel):
    actor: str = "api:ops.brain.governance_candidate"
    require_review: bool = True


class BrainGovernanceCandidateReviewRequest(BaseModel):
    limit: int = 20
    run_llm: bool = False
    llm_dry_run: bool = True


class AutonomousEvolutionNurseryRunRequest(BaseModel):
    replay_if_stale: bool = True
    reconcile_effects: bool = True
    refresh_proposals: bool = True
    review_candidates: bool = True
    create_release_evidence: bool = True
    consume_recommended_step: bool = False
    apply_when_ready: bool = False
    confirm_blocking_apply: bool = False
    full_learning_cycle: bool = False
    replay_lookback_days: float = 7.0
    replay_limit: int = 80
    review_limit: int = 50
    effect_limit: int = 50
    sample_limit: int = 500
    recommendation_limit: int = 20
    suggestion_limit: int = 20
    recommended_step_limit: int = 1
    recommended_step_allowlist: list[str] = Field(default_factory=list)


class AutonomousDemoApplyStepRequest(BaseModel):
    step: str
    limit: int = 0
    confirm_step: bool = False
    actor: str = "api:ops.autonomous_demo_apply_step"
    run_async: bool = False


class BrainLiveReadyGuardrailEvaluateRequest(BaseModel):
    source: str = "api:ops.brain.live_ready_guardrails"


class BrainLiveReadyGuardrailTightenRequest(BaseModel):
    target_mode: str = "no_new_risk"
    reason: str = ""
    actor: str = "api:ops.brain.live_ready_guardrails"


class ProposalReviewRequest(BaseModel):
    actor: str = "api:ops.autonomy.proposals"
    decision: str = "reviewed"
    route: str = ""
    notes: str = ""


class LiveAutonomyUnlockRequest(BaseModel):
    actor: str = "api:ops.autonomy.live_unlock"
    reason: str = ""
    confirm: bool = False
    v16_command_id: str = ""
    v16_claim_token: str = ""


class LiveAutonomyRevokeRequest(BaseModel):
    actor: str = "api:ops.autonomy.live_unlock"
    reason: str = ""


class GovernanceExpansionControlRequest(BaseModel):
    paused: bool
    reason: str = ""
    confirm_resume: bool = False
    v16_command_id: str = ""
    v16_claim_token: str = ""


def _get_auto_recovery() -> AutoRecovery:
    global _auto_recovery
    if _auto_recovery is None:
        _auto_recovery = AutoRecovery(check_interval=30.0, max_failures=2, max_restart_attempts=3)
    return _auto_recovery


def _get_report_gen() -> WeeklyReport:
    global _report_gen
    if _report_gen is None:
        _report_gen = WeeklyReport()
    return _report_gen


# ── Alerts (static rules config) ──
@router.get("/alerts")
def get_alert_rules(_user: RequireUser) -> dict[str, Any]:
    """
    获取告警规则配置和状态。
    """
    return alerts_fact_payload({
        "status": "Healthy",
        "config_status": "configured",
        "rules_active": 6,
        "rules": [
            {"name": "权益回撤 > 5%", "threshold": "5%", "active": True},
            {"name": "连续亏损 3 次", "threshold": "3", "active": True},
            {"name": "单因子权重 > 40%", "threshold": "40%", "active": True},
            {"name": "cTrader 断开 > 30s", "threshold": "30s", "active": True},
            {"name": "数据同步延迟 > 30min", "threshold": "30min", "active": True},
            {"name": "VaR 95% > 账户 5%", "threshold": "5%", "active": True},
        ],
        "delivery": {
            "status": "not_registered",
            "registered": False,
            "source": "not_registered",
        },
    })


# ── Auto Recovery ──
@router.get("/recovery")
def get_recovery_status(_user: RequireUser) -> dict[str, Any]:
    """
    获取 AutoRecovery 当前状态。
    """
    ar = _auto_recovery
    if ar is None:
        return recovery_fact_payload(
            {
                "status": "not_registered",
                "registered": False,
                "running": False,
                "loop_healthy": None,
                "scheduler_healthy": None,
                "failures": 0,
                "last_check": 0.0,
                "restart_attempts": 0,
            },
            registered=False,
        )
    registered = bool(
        getattr(ar, "_loop_check_fn", None) is not None
        or getattr(ar, "_scheduler_check_fn", None) is not None
    )
    return recovery_fact_payload(ar.health_status(), registered=registered)


@router.get("/recovery/history")
def get_recovery_history(_user: RequireUser) -> dict[str, Any]:
    """Return recovery history without constructing or starting a monitor."""
    payload = {
        "history": [],
        "note": "AutoRecovery 未注册，尚无权威恢复历史源",
        "status": "not_registered",
        "registered": False,
    }
    return dict(
        attach_fact(
            payload,
            contract="ops.auto-recovery-history.v2",
            source="not_registered",
            observed_at=None,
            stale_after_sec=DEFAULT_STALE_AFTER_SEC["recovery"],
            reason_code="auto_recovery_history_not_registered",
        )
    )


@router.get("/backend-readiness")
def get_backend_readiness(_user: RequireUser) -> dict[str, Any]:
    """前端交接用的后端统一状态合约。"""
    cache_key = "backend-readiness"
    payload = _READINESS_CACHE.get(cache_key)
    if payload is not None:
        payload = dict(payload)
        payload["cache"] = dict(payload.get("cache") or {})
        payload["cache"].update({"source": "cache", "ttl_sec": _BACKEND_READINESS_TTL_SEC})
        return readiness_fact_payload(payload)

    snapshots = BackendReadinessSnapshotService()
    current = snapshots.latest()
    refresh = snapshots.refresh_async(max_age_seconds=_BACKEND_READINESS_TTL_SEC)
    if current.get("ok"):
        payload = dict(current.get("payload") or {})
        payload.setdefault("cache", {})
        payload["cache"].update({
            "source": "persistent_snapshot",
            "ttl_sec": _BACKEND_READINESS_TTL_SEC,
            "age_sec": round(float(current.get("age_seconds") or 0.0), 3),
            "refresh_status": refresh.get("status"),
        })
        cached = _READINESS_CACHE.set(cache_key, payload, ttl_sec=30.0)
        return readiness_fact_payload(cached)
    return readiness_fact_payload({
        "ok": True,
        "schema_version": "backend_readiness.v1",
        "generated_at": time.time(),
        "ready_for_frontend": False,
        "ready_for_live_execution": False,
        "ready_for_live_alpha": False,
        "ready_for_autonomous_mutation": False,
        "ready_for_release": False,
        "status": "warming_snapshot",
        "blockers": [],
        "known_observations": [{
            "component": "backend_readiness_snapshot",
            "status": "warming",
            "classification": "projection_initializing",
        }],
        "cache": {"source": "warming", "refresh_status": refresh.get("status")},
        "v16": {},
    })


@router.get("/agent-authority")
def get_agent_authority(_user: RequireUser) -> dict[str, Any]:
    """Return the machine-readable authority contract for autonomous agents."""
    registry = AgentAuthorityRegistryService()
    return unverified_compat_fact_payload({
        "ok": True,
        "schema_version": "ops_agent_authority.v1",
        "agent_authority": registry.list_agents(),
        "status": registry.status(),
    }, contract="ops.agent-authority.v2",
       reason_code="agent_authority_observation_not_exposed")


@router.get("/agent-scorecard")
def get_agent_scorecard(_user: RequireUser, limit: int = 500) -> dict[str, Any]:
    """Return read-only quality and reliability metrics for autonomous agents."""
    scorecard = AgentScorecardService().scorecard(limit=max(1, min(int(limit), 2000)))
    return ledger_read_fact_payload({
        "ok": bool(scorecard.get("ok")),
        "schema_version": "ops_agent_scorecard.v1",
        "scorecard": scorecard,
    }, contract="ops.agent-scorecard.v2",
       source="runtime.agent-governance-ledgers",
       entity_path=("scorecard",),
       observed_paths=(),
       item_paths=(("items",),),
       item_timestamp_fields=("latest_activity_at",),
       reason_code="agent_scorecard_observation_missing")


@router.get("/agent-briefing")
def get_agent_briefing(_user: RequireUser, limit: int = 50) -> dict[str, Any]:
    """Return shared read-only briefing context for autonomous agent review."""
    briefing = AgentBriefingContextService().build(limit=max(1, min(int(limit), 200)))
    return ledger_read_fact_payload({
        "ok": bool(briefing.get("ok")),
        "schema_version": "ops_agent_briefing.v1",
        "briefing": briefing,
    }, contract="ops.agent-briefing.v2",
       source="runtime.agent-governance-ledgers",
       entity_path=("briefing",),
       observed_paths=(),
       item_paths=(("agent_scorecard", "top_agents"), ("relevant_experience",)),
       item_timestamp_fields=("latest_activity_at", "created_at"),
       reason_code="agent_briefing_observation_missing")


@router.get("/agent-trade-attribution")
def get_agent_trade_attribution(
    _user: RequireUser,
    limit: int = 50,
    include_external_links: bool = False,
) -> dict[str, Any]:
    """Return read-only trade outcome feedback linked back to source agents."""
    attribution = AgentScorecardService().latest_trade_attributions(
        limit=max(1, min(int(limit), 200)),
        include_external_links=bool(include_external_links),
    )
    return ledger_read_fact_payload({
        "ok": bool(attribution.get("ok")),
        "schema_version": "ops_agent_trade_attribution.v1",
        "trade_attribution": attribution,
    }, contract="ops.agent-trade-attribution.v2",
       source="canonical_v2.trade_review",
       entity_path=("trade_attribution",),
       observed_paths=(),
       item_paths=(("items",),),
       item_timestamp_fields=("created_at",),
       reason_code="agent_trade_attribution_observation_missing")


@router.get("/agent-chain-health")
def get_agent_chain_health(_user: RequireUser, limit: int = 300) -> dict[str, Any]:
    """Return agent authority, proposal flow, scorecard, and feedback health."""
    health = AgentScorecardService().chain_health(limit=max(1, min(int(limit), 1000)))
    return unverified_compat_fact_payload({
        "ok": bool(health.get("ok")),
        "schema_version": "ops_agent_chain_health.v1",
        "agent_chain_health": health,
    }, contract="ops.agent-chain-health.v2",
       reason_code="agent_chain_health_observation_not_exposed")


@router.get("/autonomy/evolution-cycle")
def get_autonomous_evolution_cycle(
    _user: RequireUser,
    refresh_proposals: bool = False,
    full_readiness: bool = False,
) -> dict[str, Any]:
    """Return the read-only self-evolution cycle state for demo nursery."""
    readiness = (
        BackendReadinessService().build()
        if bool(full_readiness)
        else AutonomousEvolutionNurseryRunner().build_light_readiness()
    )
    cycle = AutonomousEvolutionCycleService().status(
        readiness=readiness,
        refresh_proposals=bool(refresh_proposals),
        include_chain_health=bool(full_readiness),
    )
    if refresh_proposals:
        _READINESS_CACHE.invalidate("backend-readiness")
    return ledger_read_fact_payload({
        "ok": bool(cycle.get("ok")),
        "schema_version": "ops_autonomous_evolution_cycle.v1",
        "cycle": cycle,
        "readiness_generated_at": readiness.get("generated_at"),
        "readiness_mode": "full" if bool(full_readiness) else "light",
    }, contract="ops.autonomous-evolution-cycle.v2",
       source="runtime.autonomous-evolution-evidence",
       entity_path=("cycle",),
       observed_paths=(),
       item_paths=(("evidence", "tables"), ("effect_monitor", "tables")),
       item_timestamp_fields=("latest_created_at",),
       reason_code="autonomous_evolution_cycle_observation_missing")


@router.post("/autonomy/evolution-cycle/run")
def run_autonomous_evolution_nursery_cycle(req: AutonomousEvolutionNurseryRunRequest, _user: RequireUser) -> dict[str, Any]:
    """Run one guarded demo-nursery self-evolution coordination cycle."""
    if bool(req.apply_when_ready) and not bool(req.confirm_blocking_apply):
        raise HTTPException(
            status_code=409,
            detail={
                "error": "blocking_apply_requires_confirmation",
                "msg": "Set confirm_blocking_apply=true to run the currently blocking demo apply path.",
                "recommended": "Run with apply_when_ready=false to repair freshness and inspect the guarded apply window.",
            },
        )
    result = AutonomousEvolutionNurseryRunner().run_once(
        replay_if_stale=bool(req.replay_if_stale),
        reconcile_effects=bool(req.reconcile_effects),
        refresh_proposals=bool(req.refresh_proposals),
        review_candidates=bool(req.review_candidates),
        create_release_evidence=bool(req.create_release_evidence),
        consume_recommended_step=bool(req.consume_recommended_step),
        apply_when_ready=bool(req.apply_when_ready),
        full_learning_cycle=bool(req.full_learning_cycle),
        replay_lookback_days=max(0.0, min(float(req.replay_lookback_days), 30.0)),
        replay_limit=max(1, min(int(req.replay_limit), 1000)),
        review_limit=max(1, min(int(req.review_limit), 200)),
        effect_limit=max(1, min(int(req.effect_limit), 200)),
        sample_limit=max(1, min(int(req.sample_limit), 2000)),
        recommendation_limit=max(1, min(int(req.recommendation_limit), 100)),
        suggestion_limit=max(1, min(int(req.suggestion_limit), 100)),
        recommended_step_limit=max(1, min(int(req.recommended_step_limit), 20)),
        recommended_step_allowlist=list(req.recommended_step_allowlist or []),
    )
    _READINESS_CACHE.invalidate("backend-readiness")
    return unverified_compat_fact_payload({
        "ok": bool(result.get("ok")),
        "schema_version": "ops_autonomous_evolution_nursery_run.v1",
        "run": result,
    }, contract="ops.autonomous-evolution-nursery-run.v2",
       reason_code="nursery_run_commit_not_reconciled")


@router.get("/autonomy/demo-apply-plan")
def get_autonomous_demo_apply_plan(_user: RequireUser) -> dict[str, Any]:
    """Return explicit single-step demo apply plan without mutating state."""
    plan = AutonomousDemoApplyStepper().plan()
    return unverified_compat_fact_payload({
        "ok": bool(plan.get("ok")),
        "schema_version": "ops_autonomous_demo_apply_plan.v1",
        "plan": plan,
    }, contract="ops.autonomous-demo-apply-plan.v2",
       reason_code="demo_apply_plan_observation_not_exposed")


@router.post("/autonomy/demo-apply-step")
def run_autonomous_demo_apply_step(
    req: AutonomousDemoApplyStepRequest,
    background_tasks: BackgroundTasks,
    _user: RequireUser,
) -> dict[str, Any]:
    """Run one confirmed demo apply step through existing guarded services."""
    service = AutonomousDemoApplyStepper()
    if bool(req.run_async):
        result = service.start_background_step(
            req.step,
            limit=int(req.limit or 0) if req.limit else None,
            confirm_step=bool(req.confirm_step),
            actor=req.actor,
        )
        if str(result.get("status") or "") == "accepted":
            background_tasks.add_task(
                service.run_accepted_step,
                step=str(result.get("step") or req.step),
                experiment_id=str(result.get("experiment_id") or ""),
                run_id=str(result.get("run_id") or ""),
                limit=int(result.get("limit") or req.limit or 1),
            )
    else:
        result = service.run_step(
            req.step,
            limit=int(req.limit or 0) if req.limit else None,
            confirm_step=bool(req.confirm_step),
            actor=req.actor,
        )
    if bool(result.get("ok")):
        _READINESS_CACHE.invalidate("backend-readiness")
    status = str(result.get("status") or "")
    if status == "confirmation_required":
        raise HTTPException(status_code=409, detail=result)
    if not bool(result.get("ok")):
        raise HTTPException(status_code=400, detail=result)
    return persisted_record_fact_payload({
        "ok": True,
        "schema_version": "ops_autonomous_demo_apply_step.v1",
        "step_result": result,
    }, contract="ops.autonomous-demo-apply-step.v2",
       source="runtime.evolution_run",
       record_path=("step_result",),
       id_fields=("run_id",),
       observed_paths=(("finished_at",), ("started_at",)))


@router.get("/autonomy/proposals")
def get_autonomy_proposals(
    _user: RequireUser,
    limit: int = 100,
    status: str = "",
    refresh: bool = False,
) -> dict[str, Any]:
    """Return the unified autonomous proposal registry read model."""
    proposals = ProposalRegistryService().latest(
        limit=max(1, min(int(limit), 500)),
        status=str(status or ""),
        refresh=bool(refresh),
    )
    return ledger_read_fact_payload({
        "ok": bool(proposals.get("ok")),
        "schema_version": "ops_autonomy_proposals.v1",
        "proposals": proposals,
    }, contract="ops.autonomy-proposals.v2",
       source="runtime.proposal_registry",
       entity_path=("proposals",),
       observed_paths=(),
       item_paths=(("items",),),
       query_observed_at=(
           time.time()
           if proposals.get("ok") is not False
           else None
       ),
       reason_code="proposal_registry_observation_missing")


@router.get("/autonomy/proposals/{proposal_id}")
def get_autonomy_proposal(proposal_id: str, _user: RequireUser) -> dict[str, Any]:
    """Return one proposal registry item."""
    proposal = ProposalRegistryService().get(proposal_id)
    return ledger_read_fact_payload({
        "ok": bool(proposal.get("ok")),
        "schema_version": "ops_autonomy_proposal.v1",
        "proposal": proposal,
    }, contract="ops.autonomy-proposal.v2",
       source="runtime.proposal_registry",
       entity_path=("proposal",),
       observed_paths=(("proposal", "updated_at"), ("proposal", "created_at")),
       reason_code="proposal_observation_missing")


@router.post("/autonomy/proposals/refresh")
def refresh_autonomy_proposals(_user: RequireUser, limit: int = 500) -> dict[str, Any]:
    """Refresh the proposal registry from existing governance ledgers."""
    service = ProposalRegistryService()
    result = service.refresh(limit=max(1, min(int(limit), 5000)))
    _READINESS_CACHE.invalidate("backend-readiness")
    return proposal_refresh_fact_payload({
        "ok": bool(result.get("ok")),
        "schema_version": "ops_autonomy_proposals_refresh.v1",
        "refresh": result,
    }, reconciled_projection=None)


@router.post("/autonomy/proposals/{proposal_id}/review")
def review_autonomy_proposal(proposal_id: str, req: ProposalReviewRequest, _user: RequireUser) -> dict[str, Any]:
    """Record a proposal review without authorizing or applying the source action."""
    result = ProposalRegistryService().review(
        proposal_id,
        actor=str(req.actor or "api:ops.autonomy.proposals"),
        decision=str(req.decision or "reviewed"),
        route=str(req.route or ""),
        notes=str(req.notes or ""),
    )
    _READINESS_CACHE.invalidate("backend-readiness")
    return persisted_record_fact_payload({
        "ok": bool(result.get("ok")),
        "schema_version": "ops_autonomy_proposal_review.v1",
        "review": result,
    }, contract="ops.autonomy-proposal-review.v2",
       source="runtime.proposal_registry",
       record_path=("review",),
       id_fields=("proposal_id",),
       observed_paths=(("review", "reviewed_at"),))


@router.get("/autonomy/live-status")
def get_live_autonomy_status(_user: RequireUser, refresh_proposals: bool = False) -> dict[str, Any]:
    """Return governed live-autonomy unlock status."""
    readiness = BackendReadinessService().build()
    status = LiveAutonomyService().status(
        readiness=readiness,
        refresh_proposals=bool(refresh_proposals),
    )
    return live_autonomy_status_fact_payload({
        "ok": bool(status.get("ok")),
        "schema_version": "ops_live_autonomy_status.v1",
        "live_autonomy": status,
        "readiness_generated_at": readiness.get("generated_at"),
    })


@router.post("/autonomy/live-unlock/evaluate")
def evaluate_live_autonomy_unlock(req: LiveAutonomyUnlockRequest, _user: RequireUser) -> dict[str, Any]:
    """Evaluate live-autonomous unlock requirements without changing runtime mode."""
    readiness = BackendReadinessService().build()
    evaluation = LiveAutonomyService().evaluate(
        readiness=readiness,
        refresh_proposals=True,
        persist=True,
        actor=str(req.actor or "api:ops.autonomy.live_unlock"),
        reason=str(req.reason or ""),
    )
    _READINESS_CACHE.invalidate("backend-readiness")
    return live_autonomy_evaluation_fact_payload({
        "ok": bool(evaluation.get("ok")),
        "schema_version": "ops_live_autonomy_unlock_evaluate.v1",
        "evaluation": evaluation,
        "readiness_generated_at": readiness.get("generated_at"),
    })


@router.post("/autonomy/live-unlock")
def unlock_live_autonomy(req: LiveAutonomyUnlockRequest, _user: RequireRecentStepUp) -> dict[str, Any]:
    """Manually unlock live-autonomous mode after evidence gates pass."""
    readiness = BackendReadinessService().build()
    result = LiveAutonomyService().unlock(
        actor="api:ops.autonomy.live_unlock",
        reason=str(req.reason or ""),
        confirm=bool(req.confirm),
        readiness=readiness,
        v16_command_id=str(req.v16_command_id or ""),
        v16_claim_token=str(req.v16_claim_token or ""),
    )
    _READINESS_CACHE.invalidate("backend-readiness")
    return governance_mutation_fact_payload({
        "ok": bool(result.get("ok")),
        "schema_version": "ops_live_autonomy_unlock.v1",
        "unlock": result,
        "readiness_generated_at": readiness.get("generated_at"),
    }, contract="ops.live-autonomy-unlock.v2",
       result_path=("unlock",))


@router.get("/autonomy/governance-expansion-control")
def get_governance_expansion_control(_user: RequireUser) -> dict[str, Any]:
    """Return the operator-owned all-mode governance expansion kill switch."""
    status = GovernanceExpansionControlService().status()
    return unverified_compat_fact_payload({
        "ok": True,
        "schema_version": "ops_governance_expansion_control.v1",
        "governance_expansion_paused": bool(
            status.get("governance_expansion_paused")
        ),
        "control": status,
    }, contract="ops.governance-expansion-control.v2",
       reason_code="governance_expansion_projection_observation_missing")


@router.post("/autonomy/governance-expansion-control")
def set_governance_expansion_control(
    req: GovernanceExpansionControlRequest,
    _user: RequireRecentStepUp,
) -> dict[str, Any]:
    """Pause or governably resume all-mode autonomous expansion."""
    result = GovernanceExpansionControlService().set_paused(
        bool(req.paused),
        actor="api:ops.governance_expansion_control",
        reason=str(req.reason or ""),
        confirm_resume=bool(req.confirm_resume),
        v16_command_id=str(req.v16_command_id or ""),
        v16_claim_token=str(req.v16_claim_token or ""),
    )
    _READINESS_CACHE.invalidate("backend-readiness")
    return governance_mutation_fact_payload({
        "ok": bool(result.get("ok")),
        "schema_version": "ops_governance_expansion_control.v1",
        "governance_expansion_paused": bool(
            result.get("governance_expansion_paused")
        ),
        "result": result,
    }, contract="ops.governance-expansion-control-mutation.v2",
       result_path=("result",))


@router.post("/autonomy/live-unlock/revoke")
def revoke_live_autonomy(req: LiveAutonomyRevokeRequest, _user: RequireUser) -> dict[str, Any]:
    """Revoke live-autonomous mode back to live_candidate through runtime overlay."""
    result = LiveAutonomyService().revoke(
        actor="api:ops.autonomy.live_unlock",
        reason=str(req.reason or ""),
    )
    _READINESS_CACHE.invalidate("backend-readiness")
    return governance_mutation_fact_payload({
        "ok": bool(result.get("ok")),
        "schema_version": "ops_live_autonomy_revoke.v1",
        "revoke": result,
    }, contract="ops.live-autonomy-revoke.v2",
       result_path=("revoke",))


@router.get("/brain/state")
def get_brain_state(_user: RequireUser, refresh: bool = False) -> dict[str, Any]:
    """Return the V16 Phase 1 read-only brain state snapshot."""
    service = BrainStateService()
    if refresh:
        readiness = BackendReadinessService().build()
        snapshot = (readiness.get("brain_state") or {}).get("latest_snapshot") or {}
        if not snapshot.get("snapshot_id"):
            snapshot = service.build(
                readiness=readiness,
                persist=True,
                source="api:ops.brain_state",
            )
        _READINESS_CACHE.invalidate("backend-readiness")
    else:
        snapshot = service.latest_snapshot()
        if not snapshot.get("snapshot_id"):
            readiness = BackendReadinessService().build()
            snapshot = (readiness.get("brain_state") or {}).get("latest_snapshot") or {}
            if not snapshot.get("snapshot_id"):
                snapshot = service.build(
                    readiness=readiness,
                    persist=True,
                    source="api:ops.brain_state",
                )
    return ledger_read_fact_payload({
        "ok": bool(snapshot.get("ok")),
        "schema_version": "ops_brain_state.v1",
        "brain_state": snapshot,
    }, contract="ops.v16-brain-state.v2",
       source="runtime.brain_state_snapshot",
       entity_path=("brain_state",),
       observed_paths=(("created_at",),),
       reason_code="brain_state_snapshot_missing")


@router.get("/brain/memory")
def get_brain_memory(_user: RequireUser, refresh: bool = False, limit: int = 50) -> dict[str, Any]:
    """Return V16 Phase 1 read-only memory retrieval/index metadata."""
    service = BrainMemoryService()
    if refresh:
        readiness = BackendReadinessService().build()
        snapshot = (readiness.get("brain_state") or {}).get("latest_snapshot") or {}
        memory = snapshot.get("memory") or service.retrieve(
            world_model=snapshot.get("world_model") or {},
            hypotheses=snapshot.get("hypotheses") or [],
            limit=max(1, min(int(limit), 50)),
            persist=True,
        )
        _READINESS_CACHE.invalidate("backend-readiness")
    else:
        memory = service.latest_indexed(limit=max(1, min(int(limit), 200)))
    memory = {
        **memory,
        "integrity": MemoryIntegrityReportService().build(),
    }
    return ledger_read_fact_payload({
        "ok": bool(memory.get("ok")),
        "schema_version": "ops_brain_memory.v1",
        "memory": memory,
    }, contract="ops.v16-brain-memory.v2",
       source="runtime.brain_memory",
       entity_path=("memory",),
       observed_paths=(),
       item_paths=(("items",),),
       item_timestamp_fields=("last_used_at", "created_at"),
       reason_code="brain_memory_observation_missing")


@router.get("/brain/commands")
def get_brain_commands(_user: RequireUser, limit: int = 50) -> dict[str, Any]:
    """Return V16 decisions and specialist delegations.

    This endpoint is intentionally read-only.  A command can point to a
    governance candidate, but the mutation remains in the downstream agent
    and its existing RiskPolicy/DecisionPolicy gates.
    """
    commands = V16BrainOrchestratorService().latest_commands(limit=max(1, min(int(limit), 200)))
    return ledger_read_fact_payload({
        "ok": bool(commands.get("ok")),
        "schema_version": "ops_v16_brain_commands.v1",
        "commands": commands,
    }, contract="ops.v16-brain-commands.v2",
       source="runtime.v16_brain_command",
       entity_path=("commands",),
       observed_paths=(),
       item_paths=(("commands",),),
       item_timestamp_fields=("created_at",),
       reason_code="v16_brain_command_observation_missing")


@router.get("/brain/action-plans")
def get_brain_action_plans(_user: RequireUser, refresh: bool = False, limit: int = 50) -> dict[str, Any]:
    """Return V16 Phase 2 shadow action plans without writing state.

    ``refresh`` remains accepted for client compatibility but is deliberately
    ignored.  Use the explicit POST refresh route for a stateful operation.
    """
    planner = BrainActionPlannerService()
    limit = max(1, min(int(limit), 200))
    del refresh
    action_plans = planner.latest_plans(limit=limit)
    return ledger_read_fact_payload({
        "ok": bool(action_plans.get("ok")),
        "schema_version": "ops_brain_action_plans.v1",
        "action_plans": action_plans,
    }, contract="ops.v16-action-plans.v2",
       source="runtime.brain_action_plan",
       entity_path=("action_plans",),
       observed_paths=(("created_at",),),
       item_paths=(("plans",),),
       reason_code="brain_action_plan_observation_missing")


@router.get("/brain/action-plan-evals")
def get_brain_action_plan_evals(_user: RequireUser, refresh: bool = False, limit: int = 50) -> dict[str, Any]:
    """Return V16 Phase 2 comparisons without writing state."""
    evaluator = BrainActionPlanEvaluatorService()
    limit = max(1, min(int(limit), 200))
    del refresh
    evals = evaluator.latest_evals(limit=limit)
    return ledger_read_fact_payload({
        "ok": bool(evals.get("ok")),
        "schema_version": "ops_brain_action_plan_evals.v1",
        "action_plan_evals": evals,
    }, contract="ops.v16-action-plan-evals.v2",
       source="runtime.brain_action_plan_eval",
       entity_path=("action_plan_evals",),
       observed_paths=(("created_at",),),
       item_paths=(("evals",),),
       reason_code="brain_action_plan_eval_observation_missing")


@router.post("/brain/action-plans/refresh")
def refresh_brain_action_plans(req: BrainRefreshRequest, _user: RequireUser) -> dict[str, Any]:
    """Explicitly rebuild and persist V16 shadow action plans."""
    planner = BrainActionPlannerService()
    readiness = BackendReadinessService().build()
    snapshot = (readiness.get("brain_state") or {}).get("latest_snapshot") or {}
    if not snapshot.get("snapshot_id"):
        snapshot = BrainStateService().build(
            readiness=readiness,
            persist=True,
            source="api:ops.brain_action_plans.refresh",
        )
    action_plans = planner.build_plans(
        brain_state=snapshot,
        persist=True,
        source="api:ops.brain_action_plans.refresh",
    )
    _READINESS_CACHE.invalidate("backend-readiness")
    return persisted_record_fact_payload(
        {
            "ok": bool(action_plans.get("ok")),
            "schema_version": "ops_brain_action_plans.v1",
            "action_plans": action_plans,
        },
        contract="ops.v16-action-plans-refresh.v1",
        source="runtime.brain_action_plan",
        record_path=("action_plans",),
        id_fields=(),
        observed_paths=(("created_at",),),
        item_paths=(("plans",),),
        item_id_fields=("plan_id",),
    )


@router.post("/brain/action-plan-evals/refresh")
def refresh_brain_action_plan_evals(req: BrainRefreshRequest, _user: RequireUser) -> dict[str, Any]:
    """Explicitly evaluate and persist V16 action-plan comparisons."""
    planner = BrainActionPlannerService()
    latest_plans = planner.latest_plans(limit=max(1, min(int(req.limit), 200)))
    if not latest_plans.get("plans"):
        readiness = BackendReadinessService().build()
        snapshot = (readiness.get("brain_state") or {}).get("latest_snapshot") or {}
        if not snapshot.get("snapshot_id"):
            snapshot = BrainStateService().build(
                readiness=readiness,
                persist=True,
                source="api:ops.brain_action_plan_evals.refresh",
            )
        planner.build_plans(
            brain_state=snapshot,
            persist=True,
            source="api:ops.brain_action_plan_evals.refresh",
        )
    evaluation_run_id = str(req.idempotency_key or f"api_eval_{uuid.uuid4().hex}")
    evals = BrainActionPlanEvaluatorService().evaluate_latest_plans(
        limit=max(1, min(int(req.limit), 200)),
        persist=True,
        evaluation_run_id=evaluation_run_id,
    )
    _READINESS_CACHE.invalidate("backend-readiness")
    return persisted_record_fact_payload(
        {
            "ok": bool(evals.get("ok")),
            "schema_version": "ops_brain_action_plan_evals.v1",
            "action_plan_evals": evals,
        },
        contract="ops.v16-action-plan-evals-refresh.v1",
        source="runtime.brain_action_plan_eval",
        record_path=("action_plan_evals",),
        id_fields=(),
        observed_paths=(("created_at",),),
        item_paths=(("evals",),),
        item_id_fields=("eval_id",),
    )


@router.get("/brain/low-impact-executions")
def get_brain_low_impact_executions(_user: RequireUser, limit: int = 50) -> dict[str, Any]:
    """Return V16 Phase 3 low-impact execution ledger."""
    executions = BrainLowImpactExecutorService().latest_executions(limit=max(1, min(int(limit), 200)))
    return ledger_read_fact_payload({
        "ok": bool(executions.get("ok")),
        "schema_version": "ops_brain_low_impact_executions.v1",
        "low_impact_executions": executions,
    }, contract="ops.v16-low-impact-executions.v2",
       source="runtime.brain_low_impact_execution",
       entity_path=("low_impact_executions",),
       observed_paths=(),
       item_paths=(("executions",),),
       reason_code="low_impact_execution_observation_missing")


@router.post("/brain/low-impact-executions/run")
def run_brain_low_impact_execution(req: BrainLowImpactExecutionRequest, _user: RequireUser) -> dict[str, Any]:
    """Run V16 Phase 3 low-impact autonomous actions through backend boundaries."""
    result = BrainLowImpactExecutorService().execute_latest(
        limit=max(1, min(int(req.limit), 20)),
        allow_tighten=bool(req.allow_tighten),
        replay_lookback_days=max(0.0, min(float(req.replay_lookback_days), 7.0)),
        replay_limit=max(1, min(int(req.replay_limit), 500)),
        persist=True,
    )
    _READINESS_CACHE.invalidate("backend-readiness")
    return persisted_record_fact_payload({
        "ok": bool(result.get("ok")),
        "schema_version": "ops_brain_low_impact_execution_run.v1",
        "execution_run": result,
    }, contract="ops.v16-low-impact-execution-run.v2",
       source="runtime.brain_low_impact_execution",
       record_path=("execution_run",),
       id_fields=(),
       observed_paths=(("created_at",),),
       item_paths=(("executions",),),
       item_id_fields=("execution_id",))


@router.get("/brain/medium-impact-governance")
def get_brain_medium_impact_governance(_user: RequireUser, limit: int = 50) -> dict[str, Any]:
    """Return V16 Phase 4 medium-impact governance candidate ledger."""
    governance = BrainMediumImpactGovernanceService().latest_governance(limit=max(1, min(int(limit), 200)))
    return ledger_read_fact_payload({
        "ok": bool(governance.get("ok")),
        "schema_version": "ops_brain_medium_impact_governance.v1",
        "medium_impact_governance": governance,
    }, contract="ops.v16-medium-impact-governance.v2",
       source="runtime.brain_medium_impact_governance",
       entity_path=("medium_impact_governance",),
       observed_paths=(),
       item_paths=(("items",),),
       reason_code="medium_impact_governance_observation_missing")


@router.post("/brain/medium-impact-governance/materialize")
def materialize_brain_medium_impact_governance(req: BrainMediumImpactGovernanceRequest, _user: RequireUser) -> dict[str, Any]:
    """Materialize V16 Phase 4 medium-impact governance candidates only."""
    readiness = BackendReadinessService().build()
    result = BrainMediumImpactGovernanceService().materialize_latest(
        limit=max(1, min(int(req.limit), 20)),
        allow_tighten_low_health=bool(req.allow_tighten_low_health),
        readiness=readiness,
        persist=True,
    )
    _READINESS_CACHE.invalidate("backend-readiness")
    return persisted_record_fact_payload({
        "ok": bool(result.get("ok")),
        "schema_version": "ops_brain_medium_impact_governance_materialize.v1",
        "governance_run": result,
    }, contract="ops.v16-medium-impact-governance-materialize.v2",
       source="runtime.brain_medium_impact_governance",
       record_path=("governance_run",),
       id_fields=(),
       observed_paths=(("created_at",),),
       item_paths=(("items",),),
       item_id_fields=("governance_id",))


@router.post("/factor/pruning-governance/materialize")
def materialize_factor_pruning_governance(req: FactorPruningGovernanceRequest, _user: RequireUser) -> dict[str, Any]:
    """Materialize factor pruning candidates into the isolated governance candidate lane."""
    result = FactorPruningGovernanceService().materialize_latest(
        limit=max(1, min(int(req.limit), 100)),
        min_priority=max(0.0, min(float(req.min_priority), 1.0)),
        persist=True,
    )
    _READINESS_CACHE.invalidate("backend-readiness")
    return unverified_compat_fact_payload({
        "ok": bool(result.get("ok")),
        "schema_version": "ops_factor_pruning_governance_materialize.v1",
        "governance_run": result,
    }, contract="ops.factor-pruning-governance-materialize.v2",
       reason_code="factor_pruning_materialization_not_reconciled")


@router.post("/factor/pruning-governance/promote-ready")
def promote_factor_pruning_governance(req: FactorPruningGovernancePromoteRequest, _user: RequireUser) -> dict[str, Any]:
    """Promote strong factor pruning candidates to governance_ready without submitting policy suggestions."""
    result = FactorPruningGovernanceService().promote_ready(
        limit=max(1, min(int(req.limit), 100)),
        min_evidence_score=max(0.0, min(float(req.min_evidence_score), 1.0)),
        require_weak_health=bool(req.require_weak_health),
    )
    _READINESS_CACHE.invalidate("backend-readiness")
    return unverified_compat_fact_payload({
        "ok": bool(result.get("ok")),
        "schema_version": "ops_factor_pruning_governance_promote_ready.v1",
        "promote_run": result,
    }, contract="ops.factor-pruning-governance-promote-ready.v2",
       reason_code="factor_pruning_promotion_not_reconciled")


@router.post("/factor/pruning-governance/bridge-ready")
def bridge_factor_pruning_governance(req: FactorPruningGovernanceBridgeRequest, _user: RequireUser) -> dict[str, Any]:
    """Bridge governance-ready factor pruning candidates into policy_suggestion without applying weights."""
    result = FactorPruningGovernanceService().bridge_ready_candidates(
        limit=max(1, min(int(req.limit), 20)),
        require_demo_nursery=bool(req.require_demo_nursery),
        actor=str(req.actor or "api:ops.factor_pruning_governance.bridge_ready"),
    )
    _READINESS_CACHE.invalidate("backend-readiness")
    return unverified_compat_fact_payload({
        "ok": bool(result.get("ok")),
        "schema_version": "ops_factor_pruning_governance_bridge_ready.v1",
        "bridge_run": result,
    }, contract="ops.factor-pruning-governance-bridge-ready.v2",
       reason_code="factor_pruning_bridge_not_reconciled")


@router.get("/factor/governance-effects")
def get_factor_governance_effects(_user: RequireUser, limit: int = 50) -> dict[str, Any]:
    """Return pruning governance application effects using existing learning_application facts."""
    result = FactorGovernanceEffectTrackerService().status(limit=max(1, min(int(limit), 200)))
    return ledger_read_fact_payload({
        "ok": bool(result.get("ok")),
        "schema_version": "ops_factor_governance_effects.v1",
        "effects": result,
    }, contract="ops.factor-governance-effects.v2",
       source="runtime.learning_application_effect",
       entity_path=("effects",),
       observed_paths=(),
       item_paths=(("items",),),
       item_timestamp_fields=("created_at",),
       reason_code="factor_governance_effect_observation_missing")


@router.post("/factor/governance-effects/reconcile")
def reconcile_factor_governance_effects(req: FactorGovernanceEffectReconcileRequest, _user: RequireUser) -> dict[str, Any]:
    """Reconcile pruning governance effects through the existing governor effect engine."""
    result = FactorGovernanceEffectTrackerService().reconcile(limit=max(1, min(int(req.limit), 200)))
    _READINESS_CACHE.invalidate("backend-readiness")
    return ledger_read_fact_payload({
        "ok": bool(result.get("ok")),
        "schema_version": "ops_factor_governance_effects_reconcile.v1",
        "effects": result,
    }, contract="ops.factor-governance-effects-reconcile.v2",
       source="runtime.learning_application_effect",
       entity_path=("effects",),
       observed_paths=(),
       item_paths=(("effect_status", "items"),),
       item_timestamp_fields=("created_at",),
       reason_code="factor_governance_effect_reconcile_observation_missing")


@router.get("/brain/governance-candidates")
def get_brain_governance_candidates(_user: RequireUser, limit: int = 50, status: str = "") -> dict[str, Any]:
    """Return isolated V16 governance candidates before policy_suggestion submission."""
    candidates = BrainGovernanceCandidateService().latest_candidates(
        limit=max(1, min(int(limit), 200)),
        status=str(status or ""),
    )
    return ledger_read_fact_payload({
        "ok": bool(candidates.get("ok")),
        "schema_version": "ops_brain_governance_candidates.v1",
        "governance_candidates": candidates,
    }, contract="ops.v16-governance-candidates.v2",
       source="runtime.brain_governance_candidate",
       entity_path=("governance_candidates",),
       observed_paths=(),
       item_paths=(("items",),),
       query_observed_at=(
           time.time()
           if str(candidates.get("status") or "") in {"available", "missing_candidates"}
           else None
       ),
       reason_code="governance_candidate_observation_missing")


@router.post("/brain/governance-candidates/{candidate_id}/submit")
def submit_brain_governance_candidate(candidate_id: str, req: BrainGovernanceCandidateSubmitRequest, _user: RequireUser) -> dict[str, Any]:
    """Manually bridge a compatible V16 candidate into legacy policy_suggestion review."""
    if bool(req.require_review):
        review_result = BrainGovernanceCandidateReviewService().review_candidate(
            candidate_id,
            run_llm=False,
            llm_dry_run=True,
            persist=True,
        )
        review = dict(review_result.get("review") or {})
        if not bool(review.get("bridge_ready")):
            _READINESS_CACHE.invalidate("backend-readiness")
            return unverified_compat_fact_payload({
                "ok": False,
                "schema_version": "ops_brain_governance_candidate_submit.v1",
                "submit_result": {
                    "ok": False,
                    "schema_version": "brain_governance_candidate_submit.v1",
                    "status": "blocked_candidate_review",
                    "candidate_id": candidate_id,
                    "review_status": review.get("review_status", review_result.get("status", "")),
                    "evidence_gaps": review.get("evidence_gaps") or [],
                    "review": review,
                    "boundary": BrainGovernanceCandidateReviewService.boundary(),
                },
            }, contract="ops.v16-governance-candidate-submit.v2",
               reason_code="governance_candidate_submit_blocked")
    result = BrainGovernanceCandidateService().submit_candidate_to_policy_suggestion(
        candidate_id,
        actor=str(req.actor or "api:ops.brain.governance_candidate"),
    )
    _READINESS_CACHE.invalidate("backend-readiness")
    return persisted_record_fact_payload({
        "ok": bool(result.get("ok")),
        "schema_version": "ops_brain_governance_candidate_submit.v1",
        "submit_result": result,
    }, contract="ops.v16-governance-candidate-submit.v2",
       source="runtime.policy_suggestion",
       record_path=("submit_result",),
       id_fields=("suggestion_id",),
       observed_paths=(("candidate", "updated_at"), ("candidate", "submitted_at")))


@router.get("/brain/governance-candidate-reviews")
def get_brain_governance_candidate_reviews(_user: RequireUser, limit: int = 50) -> dict[str, Any]:
    """Return latest V16 governance candidate protocol reviews."""
    reviews = BrainGovernanceCandidateReviewService().latest_reviews(limit=max(1, min(int(limit), 200)))
    return ledger_read_fact_payload({
        "ok": bool(reviews.get("ok")),
        "schema_version": "ops_brain_governance_candidate_reviews.v1",
        "candidate_reviews": reviews,
    }, contract="ops.v16-governance-candidate-reviews.v2",
       source="runtime.brain_governance_candidate_review",
       entity_path=("candidate_reviews",),
       observed_paths=(),
       item_paths=(("items",),),
       item_timestamp_fields=("created_at",),
       query_observed_at=(
           time.time()
           if str(reviews.get("status") or "") in {"available", "missing_reviews"}
           else None
       ),
       reason_code="governance_candidate_review_observation_missing")


@router.post("/brain/governance-candidates/review")
def review_brain_governance_candidates(req: BrainGovernanceCandidateReviewRequest, _user: RequireUser) -> dict[str, Any]:
    """Review candidate evidence, conflicts, bridge readiness, and optional LLM advisory."""
    result = BrainGovernanceCandidateReviewService().review_latest(
        limit=max(1, min(int(req.limit), 200)),
        run_llm=bool(req.run_llm),
        llm_dry_run=bool(req.llm_dry_run),
        persist=True,
    )
    _READINESS_CACHE.invalidate("backend-readiness")
    return persisted_record_fact_payload({
        "ok": bool(result.get("ok")),
        "schema_version": "ops_brain_governance_candidate_review_run.v1",
        "review_run": result,
    }, contract="ops.v16-governance-candidate-review-run.v2",
       source="runtime.brain_governance_candidate_review",
       record_path=("review_run",),
       id_fields=(),
       observed_paths=(("created_at",),),
       item_paths=(("items",),),
       item_id_fields=("review_id",),
       item_timestamp_fields=("created_at",))


@router.get("/brain/live-ready-guardrails")
def get_brain_live_ready_guardrails(_user: RequireUser, limit: int = 50) -> dict[str, Any]:
    """Return V16 Phase 5 live-ready guardrail ledger."""
    guardrails = BrainLiveReadyGuardrailService().latest_guardrails(limit=max(1, min(int(limit), 200)))
    return ledger_read_fact_payload({
        "ok": bool(guardrails.get("ok")),
        "schema_version": "ops_brain_live_ready_guardrails.v1",
        "live_ready_guardrails": guardrails,
    }, contract="ops.v16-live-ready-guardrails.v2",
       source="runtime.brain_live_ready_guardrail",
       entity_path=("live_ready_guardrails",),
       observed_paths=(),
       item_paths=(("items",),),
       reason_code="live_ready_guardrail_observation_missing")


@router.post("/brain/live-ready-guardrails/evaluate")
def evaluate_brain_live_ready_guardrail(req: BrainLiveReadyGuardrailEvaluateRequest, _user: RequireUser) -> dict[str, Any]:
    """Evaluate V16 Phase 5 live-ready guardrails without applying runtime mutations."""
    readiness = BackendReadinessService().build()
    guardrail = BrainLiveReadyGuardrailService().evaluate(
        readiness=readiness,
        persist=True,
        source=req.source,
    )
    _READINESS_CACHE.invalidate("backend-readiness")
    return persisted_record_fact_payload({
        "ok": bool(guardrail.get("guardrail_id")),
        "schema_version": "ops_brain_live_ready_guardrail_evaluate.v1",
        "guardrail": guardrail,
    }, contract="ops.v16-live-ready-guardrail-evaluate.v2",
       source="runtime.brain_live_ready_guardrail",
       record_path=("guardrail",),
       id_fields=("guardrail_id",),
       observed_paths=(("updated_at",), ("created_at",)))


@router.post("/brain/live-ready-guardrails/tighten")
def tighten_brain_live_ready_guardrail(req: BrainLiveReadyGuardrailTightenRequest, _user: RequireUser) -> dict[str, Any]:
    """Apply a V16 Phase 5 tightening-only guardrail through incident control."""
    readiness = BackendReadinessService().build()
    result = BrainLiveReadyGuardrailService().tighten(
        target_mode=req.target_mode,
        reason=req.reason,
        actor=req.actor,
        readiness=readiness,
    )
    _READINESS_CACHE.invalidate("backend-readiness")
    return governance_mutation_fact_payload({
        "ok": bool(result.get("ok")),
        "schema_version": "ops_brain_live_ready_guardrail_tighten.v1",
        "tighten_run": result,
    }, contract="ops.v16-live-ready-guardrail-tighten.v2",
       result_path=("tighten_run",),
       mutation_path=("incident_control_result", "mutation"))


@router.get("/replay/latest")
def get_latest_replay_report(_user: RequireUser) -> dict[str, Any]:
    """Return latest V15 replay report metadata."""
    report = ReplayHarnessService().latest_report()
    return ledger_read_fact_payload({
        "ok": bool(report.get("replay_run_id")) and not report.get("replay_error"),
        "schema_version": "ops_replay_latest.v1",
        "report": report,
    }, contract="ops.replay-latest.v2",
       source="runtime.replay_report",
       entity_path=("report",),
       observed_paths=(("created_at",),),
       reason_code="replay_report_observation_missing")


@router.post("/replay/run")
def run_replay_harness(
    _user: RequireUser,
    lookback_days: float = 7.0,
    limit: int = 500,
) -> dict[str, Any]:
    """Run V15 replay harness v1 for factor/gate/risk ledger alignment."""
    report = ReplayHarnessService().run_factor_gate_risk_replay(
        lookback_days=max(0.0, min(float(lookback_days), 90.0)),
        limit=max(1, min(int(limit), 5000)),
    )
    _READINESS_CACHE.invalidate("backend-readiness")
    return persisted_record_fact_payload({
        "ok": not bool(report.get("replay_error")),
        "schema_version": "ops_replay_run.v1",
        "report": report,
    }, contract="ops.replay-run.v2",
       source="runtime.replay_report",
       record_path=("report",),
       id_fields=("replay_run_id",),
       observed_paths=(("created_at",),))


@router.post("/replay/bar-run")
def run_bar_replay_harness(
    _user: RequireUser,
    lookback_days: float = 7.0,
    limit: int = 200,
    warmup_bars: int = 80,
    post_bars: int = 1,
) -> dict[str, Any]:
    """Run V15 P1 bar replay evidence for decision/bar alignment."""
    report = ReplayHarnessService().run_bar_replay_evidence(
        lookback_days=max(0.0, min(float(lookback_days), 90.0)),
        limit=max(1, min(int(limit), 2000)),
        warmup_bars=max(1, min(int(warmup_bars), 500)),
        post_bars=max(0, min(int(post_bars), 20)),
    )
    _READINESS_CACHE.invalidate("backend-readiness")
    return persisted_record_fact_payload({
        "ok": not bool(report.get("replay_error")),
        "schema_version": "ops_replay_bar_run.v1",
        "report": report,
    }, contract="ops.replay-bar-run.v2",
       source="runtime.replay_report",
       record_path=("report",),
       id_fields=("replay_run_id",),
       observed_paths=(("created_at",),))


@router.post("/replay/bar-preview")
def run_bar_replay_preview(
    _user: RequireUser,
    lookback_days: float = 1.0,
    limit: int = 1,
    warmup_bars: int = 40,
    post_bars: int = 24,
    decision_id: str = "",
) -> dict[str, Any]:
    """Run a fast read-only K-line window preview for the V15 cockpit."""
    report = ReplayHarnessService().run_bar_window_preview(
        lookback_days=max(0.0, min(float(lookback_days), 7.0)),
        limit=max(1, min(int(limit), 5)),
        warmup_bars=max(1, min(int(warmup_bars), 120)),
        post_bars=max(0, min(int(post_bars), 48)),
        decision_id=str(decision_id or "").strip(),
    )
    return unverified_compat_fact_payload({
        "ok": not bool(report.get("replay_error")),
        "schema_version": "ops_replay_bar_preview.v1",
        "report": report,
    }, contract="ops.replay-bar-preview.v2",
       reason_code="replay_preview_is_not_persisted")


@router.get("/replay/bar-decisions")
def list_bar_replay_decisions(
    _user: RequireUser,
    lookback_days: float = 7.0,
    limit: int = 30,
    offset: int = 0,
) -> dict[str, Any]:
    """Return selectable historical decisions for the V15 K-line replay preview."""
    choices = ReplayHarnessService().list_bar_preview_decisions(
        lookback_days=max(0.0, min(float(lookback_days), 30.0)),
        limit=max(1, min(int(limit), 100)),
        offset=max(0, int(offset)),
    )
    return ledger_read_fact_payload({
        "ok": True,
        "schema_version": "ops_replay_bar_decisions.v1",
        "choices": choices,
        "items": choices.get("items", []),
    }, contract="ops.replay-bar-decisions.v2",
       source="canonical_v2.risk_decision",
       entity_path=("choices",),
       observed_paths=(),
       item_paths=(("items",),),
       item_timestamp_fields=("decision_ts",),
       reason_code="replay_decision_observation_missing")


@router.get("/incident-control")
def get_incident_control(_user: RequireUser) -> dict[str, Any]:
    """Return current V15 runtime incident control mode."""
    status = RuntimeIncidentControlService().status()
    projection = runtime_config_projection_observation()
    return incident_control_status_fact_payload({
        "ok": True,
        "schema_version": "ops_incident_control.v1",
        "incident_control": status,
    }, projection_observed_at=projection.get("observed_at"),
       projection_error=projection.get("error"))


@router.post("/incident-control")
def set_incident_control(
    req: IncidentControlRequest,
    _user: RequireUser,
    authorization: Annotated[str | None, Header(include_in_schema=False)] = None,
) -> dict[str, Any]:
    """Set V15 incident control mode through RiskPolicyService + runtime overlay."""
    service = RuntimeIncidentControlService()
    current_mode = str(service.status().get("mode") or "frozen")
    target_mode = str(req.mode or "").strip().lower()
    if INCIDENT_MODE_RANK.get(target_mode, -1) < INCIDENT_MODE_RANK.get(
        current_mode, -1
    ):
        # Tightening must stay available with local JWT verification only.
        # Thawing unlocks risk and additionally requires a fresh server-side
        # Auth v2 session, failing closed when session authority is unavailable.
        require_recent_step_up(authorization)
    result = service.set_mode(
        req.mode,
        reason=req.reason,
        actor="api:ops.incident_control",
        confirm_thaw=req.confirm_thaw,
        v16_command_id=str(req.v16_command_id or ""),
        v16_claim_token=str(req.v16_claim_token or ""),
    )
    _READINESS_CACHE.invalidate("backend-readiness")
    return governance_mutation_fact_payload({
        "ok": bool(result.get("ok")),
        "schema_version": "ops_incident_control.v1",
        "result": result,
    }, contract="ops.incident-control-mutation.v2",
       result_path=("result",))


@router.get("/incident-playbook/latest")
def get_latest_incident_playbook(_user: RequireUser) -> dict[str, Any]:
    """Return latest V15 incident playbook plan."""
    playbook = RuntimeIncidentControlService().latest_playbook()
    return ledger_read_fact_payload({
        "ok": bool(playbook.get("ok")),
        "schema_version": "ops_incident_playbook_latest.v1",
        "playbook": playbook,
    }, contract="ops.incident-playbook-latest.v2",
       source="runtime.incident_playbook_run",
       entity_path=("playbook",),
       observed_paths=(("created_at",),),
       reason_code="incident_playbook_observation_missing")


@router.post("/incident-playbook/run")
def run_incident_playbook(req: IncidentPlaybookRequest, _user: RequireUser) -> dict[str, Any]:
    """Build and persist a V15 incident playbook plan without applying runtime changes."""
    playbook = RuntimeIncidentControlService().build_playbook(
        scenario=req.scenario,
        severity=req.severity,
        release_run_id=req.release_run_id,
        created_by=req.created_by,
    )
    return persisted_record_fact_payload({
        "ok": bool(playbook.get("ok")),
        "schema_version": "ops_incident_playbook_run.v1",
        "playbook": playbook,
    }, contract="ops.incident-playbook-run.v2",
       source="runtime.incident_playbook_run",
       record_path=("playbook",),
       id_fields=("playbook_id",),
       observed_paths=(("created_at",),))


@router.get("/incident-playbook/{playbook_id}/events")
def get_incident_playbook_events(playbook_id: str, _user: RequireUser, limit: int = 100) -> dict[str, Any]:
    """Return audit events bound to a V15 incident playbook plan."""
    trail = RuntimeIncidentControlService().playbook_events(playbook_id, limit=limit)
    return ledger_read_fact_payload({
        "ok": bool(trail.get("ok")),
        "schema_version": "ops_incident_playbook_events.v1",
        "event_trail": trail,
    }, contract="ops.incident-playbook-events.v2",
       source="runtime.incident_playbook_event",
       entity_path=("event_trail",),
       observed_paths=(),
       item_paths=(("events",),),
       item_timestamp_fields=("created_at",),
       reason_code="incident_playbook_event_observation_missing")


@router.post("/incident-playbook/{playbook_id}/events")
def record_incident_playbook_event(
    playbook_id: str,
    req: IncidentPlaybookEventRequest,
    _user: RequireUser,
) -> dict[str, Any]:
    """Bind evidence or operator notes to a V15 incident playbook without applying runtime changes."""
    event = RuntimeIncidentControlService().record_playbook_event(
        playbook_id,
        event_type=req.event_type,
        actor=req.actor,
        status=req.status,
        evidence_refs=req.evidence_refs,
        notes=req.notes,
    )
    return persisted_record_fact_payload({
        "ok": bool(event.get("ok")),
        "schema_version": "ops_incident_playbook_event.v1",
        "event": event,
    }, contract="ops.incident-playbook-event.v2",
       source="runtime.incident_playbook_event",
       record_path=("event",),
       id_fields=("event_id",),
       observed_paths=(("created_at",),))


@router.get("/autonomy-health/scope-approvals/latest")
def get_latest_autonomy_scope_approval(_user: RequireUser) -> dict[str, Any]:
    """Return latest V15 autonomy health scope approval audit event."""
    event = AutonomyHealthService().latest_scope_approval()
    return scope_approval_fact_payload({
        "ok": bool(event.get("ok")),
        "schema_version": "ops_autonomy_scope_approval_latest.v1",
        "approval_event": event,
    }, mutation=False)


@router.post("/autonomy-health/scope-approvals")
def record_autonomy_scope_approval(req: AutonomyScopeApprovalRequest, _user: RequireUser) -> dict[str, Any]:
    """Record an autonomy scope approval audit event without applying permissions."""
    readiness = BackendReadinessService().build()
    health = readiness.get("autonomy_health") or {}
    event = AutonomyHealthService().record_scope_approval(
        health=health,
        snapshot_id=req.snapshot_id,
        actor=req.actor,
        decision=req.decision,
        reason=req.reason,
    )
    return scope_approval_fact_payload({
        "ok": bool(event.get("ok")),
        "schema_version": "ops_autonomy_scope_approval_event.v1",
        "approval_event": event,
        "readiness_generated_at": readiness.get("generated_at"),
    }, mutation=True)


@router.get("/autonomy-health/scope-enforcements/latest")
def get_latest_autonomy_scope_enforcement(_user: RequireUser) -> dict[str, Any]:
    """Return latest V15 autonomy health scope enforcement event."""
    event = AutonomyHealthService().latest_scope_enforcement()
    return scope_enforcement_read_fact_payload({
        "ok": bool(event.get("ok")),
        "schema_version": "ops_autonomy_scope_enforcement_latest.v1",
        "enforcement_event": event,
    })


@router.post("/autonomy-health/scope-enforcements")
def enforce_autonomy_scope(req: AutonomyScopeEnforcementRequest, _user: RequireUser) -> dict[str, Any]:
    """Apply a tightening-only autonomy scope recommendation through incident control."""
    readiness = BackendReadinessService().build()
    health = readiness.get("autonomy_health") or {}
    event = AutonomyHealthService().enforce_scope_recommendation(
        health=health,
        snapshot_id=req.snapshot_id,
        actor=req.actor,
        reason=req.reason,
    )
    _READINESS_CACHE.invalidate("backend-readiness")
    return autonomy_scope_enforcement_fact_payload({
        "ok": bool(event.get("ok")),
        "schema_version": "ops_autonomy_scope_enforcement_event.v1",
        "enforcement_event": event,
        "readiness_generated_at": readiness.get("generated_at"),
    })


@router.get("/v15/phase0")
def get_v15_phase0_completion(_user: RequireUser) -> dict[str, Any]:
    """Return the machine-readable V15 Phase 0 completion gate."""
    readiness = BackendReadinessService().build()
    phase0 = V15Phase0CompletionService().build(readiness=readiness)
    return v15_phase0_fact_payload({
        "ok": bool(phase0.get("implementation_complete")),
        "schema_version": "ops_v15_phase0_completion.v1",
        "phase0": phase0,
        "readiness_generated_at": readiness.get("generated_at"),
    })


@router.get("/release/latest")
def get_latest_release_run(_user: RequireUser) -> dict[str, Any]:
    """Return latest V15 release run ledger row."""
    release = ReleaseControlService().latest_release()
    return ledger_read_fact_payload({
        "ok": bool(release.get("run_id")),
        "schema_version": "ops_release_latest.v1",
        "release": release,
    }, contract="ops.release-latest.v2",
       source="runtime.release_run",
       entity_path=("release",),
       query_observed_at=(time.time() if release.get("run_id") else None),
       reason_code="release_run_observation_missing")


@router.post("/release/start")
def start_release_run(req: ReleaseRunStartRequest, _user: RequireUser) -> dict[str, Any]:
    """Start a V15 release run ledger row with current readiness evidence."""
    readiness = BackendReadinessService().build()
    release = ReleaseControlService().start_release(
        release_class=req.release_class,
        summary=req.summary,
        tests=req.tests,
        rollback_ref=req.rollback_ref,
        created_by=req.created_by,
        readiness=readiness,
    )
    _READINESS_CACHE.invalidate("backend-readiness")
    return persisted_record_fact_payload({
        "ok": bool(release.get("run_id")),
        "schema_version": "ops_release_start.v1",
        "release": release,
    }, contract="ops.release-start.v2",
       source="runtime.release_run",
       record_path=("release",),
       id_fields=("run_id",))


@router.post("/release/{run_id}/finish")
def finish_release_run(run_id: str, req: ReleaseRunFinishRequest, _user: RequireUser) -> dict[str, Any]:
    """Finish a V15 release run ledger row with current readiness evidence."""
    readiness = BackendReadinessService().build()
    release = ReleaseControlService().finish_release(
        run_id,
        status=req.status,
        summary=req.summary,
        tests=req.tests,
        rollback_ref=req.rollback_ref,
        readiness=readiness,
    )
    _READINESS_CACHE.invalidate("backend-readiness")
    return persisted_record_fact_payload({
        "ok": bool(release.get("ok")) and bool(release.get("run_id")),
        "schema_version": "ops_release_finish.v1",
        "release": release,
    }, contract="ops.release-finish.v2",
       source="runtime.release_run",
       record_path=("release",),
       id_fields=("run_id",))


@router.get("/release/{run_id}/approvals")
def get_release_approval_trail(run_id: str, _user: RequireUser) -> dict[str, Any]:
    """Return V15 release approval audit events for a release run."""
    service = ReleaseControlService()
    trail = service.approval_trail(run_id)
    release = service.get_release(run_id)
    return release_approval_trail_fact_payload({
        "ok": bool(trail.get("ok")),
        "schema_version": "ops_release_approval_trail.v1",
        "approval_trail": trail,
    }, release=release)


@router.post("/release/{run_id}/approvals")
def record_release_approval_event(
    run_id: str, req: ReleaseApprovalEventRequest, _user: RequireUser
) -> dict[str, Any]:
    """Record a V15 release approval audit event without executing release actions."""
    event = ReleaseControlService().record_approval_event(
        run_id,
        action=req.action,
        actor=req.actor,
        decision=req.decision,
        reason=req.reason,
        evidence_refs=req.evidence_refs,
    )
    return persisted_record_fact_payload({
        "ok": bool(event.get("ok")),
        "schema_version": "ops_release_approval_event.v1",
        "approval_event": event,
    }, contract="ops.release-approval-event.v2",
       source="runtime.release_approval_event",
       record_path=("approval_event",),
       id_fields=("event_id",),
       observed_paths=(("created_at",),))


# ── Weekly Reports ──
@router.get("/reports/weekly")
def get_weekly_reports(_user: RequireUser) -> dict[str, Any]:
    """
    获取已生成的周报列表。
    """
    try:
        gen = _get_report_gen()
        # 周报在 data/charts/ 下以 weekly_*.html 形式存储
        from backend.core.paths import CHARTS_DIR
        import re
        reports = []
        if CHARTS_DIR.exists():
            for p in sorted(CHARTS_DIR.iterdir()):
                if p.is_file() and re.match(r"weekly_.*\.(html|txt|json)", p.name):
                    reports.append({
                        "name": p.name,
                        "modified_at": p.stat().st_mtime,
                    })
        return ledger_read_fact_payload({
            "reports": reports,
            "count": len(reports),
        }, contract="ops.weekly-reports.v2",
           source="filesystem:data/charts",
           entity_path=(),
           observed_paths=(),
           item_paths=(("reports",),),
           item_timestamp_fields=("modified_at",),
           reason_code="weekly_report_observation_missing")
    except Exception as exc:
        return unverified_compat_fact_payload(
            {
                "reports": [],
                "count": 0,
                "error": f"{type(exc).__name__}: {exc}",
            },
            contract="ops.weekly-reports.v2",
            reason_code="weekly_report_source_failed",
        )


@router.post("/reports/weekly/generate")
def generate_weekly_report(_user: RequireUser) -> dict[str, Any]:
    """
    触发周报生成 (占位)。
    """
    return unverified_compat_fact_payload({
        "status": "queued",
        "note": "周报生成需 ReportGenerator 接口支持",
    }, contract="ops.weekly-report-generate.v2",
       reason_code="weekly_report_generator_not_registered")
