"""Ops API endpoints: alerts, auto-recovery, weekly reports, experiments."""
from fastapi import APIRouter
import time
from typing import Any

from pydantic import BaseModel, Field

from backend.core.auth import RequireUser
from backend.services.autonomy_health import AutonomyHealthService
from backend.services.backend_readiness import BackendReadinessService
from backend.services.incident_controls import RuntimeIncidentControlService
from backend.services.release_control import ReleaseControlService
from backend.services.replay_harness import ReplayHarnessService
from backend.services.stability import TimedCache, measure
from backend.services.v15_phase0 import V15Phase0CompletionService
from monitor.auto_recovery import AutoRecovery
from research.report_generator import WeeklyReport
from research.experiment_tracker import ExperimentTracker

router = APIRouter(prefix="/api/ops", tags=["ops"])

# Singletons (lazy init)
_auto_recovery: AutoRecovery | None = None
_report_gen: WeeklyReport | None = None
_READINESS_CACHE = TimedCache()


class IncidentControlRequest(BaseModel):
    mode: str
    reason: str = ""
    confirm_thaw: bool = False


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
    return {
        "status": "Healthy",
        "rules_active": 6,
        "rules": [
            {"name": "权益回撤 > 5%", "threshold": "5%", "active": True},
            {"name": "连续亏损 3 次", "threshold": "3", "active": True},
            {"name": "单因子权重 > 40%", "threshold": "40%", "active": True},
            {"name": "cTrader 断开 > 30s", "threshold": "30s", "active": True},
            {"name": "数据同步延迟 > 30min", "threshold": "30min", "active": True},
            {"name": "VaR 95% > 账户 5%", "threshold": "5%", "active": True},
        ],
    }


# ── Auto Recovery ──
@router.get("/recovery")
def get_recovery_status(_user: RequireUser) -> dict[str, Any]:
    """
    获取 AutoRecovery 当前状态。
    """
    ar = _get_auto_recovery()
    return ar.health_status()


@router.get("/recovery/history")
def get_recovery_history(_user: RequireUser) -> dict[str, Any]:
    """
    获取恢复历史记录 (占位)。
    """
    return {
        "history": [],
        "note": "待实现持久化",
    }


@router.get("/backend-readiness")
def get_backend_readiness(_user: RequireUser) -> dict[str, Any]:
    """前端交接用的后端统一状态合约。"""
    def _compute() -> dict[str, Any]:
        with measure("api.ops.backend_readiness"):
            payload = BackendReadinessService().build()
            return payload

    cache_key = "backend-readiness"
    payload = _READINESS_CACHE.get(cache_key)
    if payload is not None:
        payload.setdefault("cache", {})
        payload["cache"].update({"source": "cache", "ttl_sec": 10.0})
        return payload

    with _READINESS_CACHE.compute_lock(cache_key):
        payload = _READINESS_CACHE.get(cache_key)
        if payload is not None:
            payload.setdefault("cache", {})
            payload["cache"].update({"source": "cache", "ttl_sec": 10.0})
            return payload
        try:
            payload = _compute()
            payload.setdefault("cache", {})
            payload["cache"].update({"source": "computed", "ttl_sec": 10.0})
            return _READINESS_CACHE.set(cache_key, payload, ttl_sec=10.0)
        except Exception:
            fallback = _READINESS_CACHE.last_good(cache_key)
            if not fallback:
                raise
            created_at, payload = fallback
            payload.setdefault("cache", {})
            payload["cache"].update({
                "source": "stale",
                "ttl_sec": 10.0,
                "stale_reason": "compute_error",
                "last_good_age_sec": round(max(0.0, time.time() - created_at), 3),
            })
            return payload


@router.get("/replay/latest")
def get_latest_replay_report(_user: RequireUser) -> dict[str, Any]:
    """Return latest V15 replay report metadata."""
    report = ReplayHarnessService().latest_report()
    return {
        "ok": bool(report.get("replay_run_id")) and not report.get("replay_error"),
        "schema_version": "ops_replay_latest.v1",
        "report": report,
    }


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
    return {
        "ok": not bool(report.get("replay_error")),
        "schema_version": "ops_replay_run.v1",
        "report": report,
    }


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
    return {
        "ok": not bool(report.get("replay_error")),
        "schema_version": "ops_replay_bar_run.v1",
        "report": report,
    }


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
    return {
        "ok": not bool(report.get("replay_error")),
        "schema_version": "ops_replay_bar_preview.v1",
        "report": report,
    }


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
    return {
        "ok": True,
        "schema_version": "ops_replay_bar_decisions.v1",
        "choices": choices,
        "items": choices.get("items", []),
    }


@router.get("/incident-control")
def get_incident_control(_user: RequireUser) -> dict[str, Any]:
    """Return current V15 runtime incident control mode."""
    status = RuntimeIncidentControlService().status()
    return {
        "ok": True,
        "schema_version": "ops_incident_control.v1",
        "incident_control": status,
    }


@router.post("/incident-control")
def set_incident_control(req: IncidentControlRequest, _user: RequireUser) -> dict[str, Any]:
    """Set V15 incident control mode through RiskPolicyService + runtime overlay."""
    result = RuntimeIncidentControlService().set_mode(
        req.mode,
        reason=req.reason,
        actor="api:ops.incident_control",
        confirm_thaw=req.confirm_thaw,
    )
    _READINESS_CACHE.invalidate("backend-readiness")
    return {
        "ok": bool(result.get("ok")),
        "schema_version": "ops_incident_control.v1",
        "result": result,
    }


@router.get("/incident-playbook/latest")
def get_latest_incident_playbook(_user: RequireUser) -> dict[str, Any]:
    """Return latest V15 incident playbook plan."""
    playbook = RuntimeIncidentControlService().latest_playbook()
    return {
        "ok": bool(playbook.get("ok")),
        "schema_version": "ops_incident_playbook_latest.v1",
        "playbook": playbook,
    }


@router.post("/incident-playbook/run")
def run_incident_playbook(req: IncidentPlaybookRequest, _user: RequireUser) -> dict[str, Any]:
    """Build and persist a V15 incident playbook plan without applying runtime changes."""
    playbook = RuntimeIncidentControlService().build_playbook(
        scenario=req.scenario,
        severity=req.severity,
        release_run_id=req.release_run_id,
        created_by=req.created_by,
    )
    return {
        "ok": bool(playbook.get("ok")),
        "schema_version": "ops_incident_playbook_run.v1",
        "playbook": playbook,
    }


@router.get("/incident-playbook/{playbook_id}/events")
def get_incident_playbook_events(playbook_id: str, _user: RequireUser, limit: int = 100) -> dict[str, Any]:
    """Return audit events bound to a V15 incident playbook plan."""
    trail = RuntimeIncidentControlService().playbook_events(playbook_id, limit=limit)
    return {
        "ok": bool(trail.get("ok")),
        "schema_version": "ops_incident_playbook_events.v1",
        "event_trail": trail,
    }


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
    return {
        "ok": bool(event.get("ok")),
        "schema_version": "ops_incident_playbook_event.v1",
        "event": event,
    }


@router.get("/autonomy-health/scope-approvals/latest")
def get_latest_autonomy_scope_approval(_user: RequireUser) -> dict[str, Any]:
    """Return latest V15 autonomy health scope approval audit event."""
    event = AutonomyHealthService().latest_scope_approval()
    return {
        "ok": bool(event.get("ok")),
        "schema_version": "ops_autonomy_scope_approval_latest.v1",
        "approval_event": event,
    }


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
    return {
        "ok": bool(event.get("ok")),
        "schema_version": "ops_autonomy_scope_approval_event.v1",
        "approval_event": event,
        "readiness_generated_at": readiness.get("generated_at"),
    }


@router.get("/autonomy-health/scope-enforcements/latest")
def get_latest_autonomy_scope_enforcement(_user: RequireUser) -> dict[str, Any]:
    """Return latest V15 autonomy health scope enforcement event."""
    event = AutonomyHealthService().latest_scope_enforcement()
    return {
        "ok": bool(event.get("ok")),
        "schema_version": "ops_autonomy_scope_enforcement_latest.v1",
        "enforcement_event": event,
    }


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
    return {
        "ok": bool(event.get("ok")),
        "schema_version": "ops_autonomy_scope_enforcement_event.v1",
        "enforcement_event": event,
        "readiness_generated_at": readiness.get("generated_at"),
    }


@router.get("/v15/phase0")
def get_v15_phase0_completion(_user: RequireUser) -> dict[str, Any]:
    """Return the machine-readable V15 Phase 0 completion gate."""
    readiness = BackendReadinessService().build()
    phase0 = V15Phase0CompletionService().build(readiness=readiness)
    return {
        "ok": bool(phase0.get("implementation_complete")),
        "schema_version": "ops_v15_phase0_completion.v1",
        "phase0": phase0,
        "readiness_generated_at": readiness.get("generated_at"),
    }


@router.get("/release/latest")
def get_latest_release_run(_user: RequireUser) -> dict[str, Any]:
    """Return latest V15 release run ledger row."""
    release = ReleaseControlService().latest_release()
    return {
        "ok": bool(release.get("run_id")),
        "schema_version": "ops_release_latest.v1",
        "release": release,
    }


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
    return {
        "ok": bool(release.get("run_id")),
        "schema_version": "ops_release_start.v1",
        "release": release,
    }


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
    return {
        "ok": bool(release.get("ok")) and bool(release.get("run_id")),
        "schema_version": "ops_release_finish.v1",
        "release": release,
    }


@router.get("/release/{run_id}/approvals")
def get_release_approval_trail(run_id: str, _user: RequireUser) -> dict[str, Any]:
    """Return V15 release approval audit events for a release run."""
    trail = ReleaseControlService().approval_trail(run_id)
    return {
        "ok": bool(trail.get("ok")),
        "schema_version": "ops_release_approval_trail.v1",
        "approval_trail": trail,
    }


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
    return {
        "ok": bool(event.get("ok")),
        "schema_version": "ops_release_approval_event.v1",
        "approval_event": event,
    }


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
        return {
            "reports": reports,
            "count": len(reports),
        }
    except Exception:
        return {"reports": [], "count": 0}


@router.post("/reports/weekly/generate")
def generate_weekly_report(_user: RequireUser) -> dict[str, Any]:
    """
    触发周报生成 (占位)。
    """
    return {
        "status": "queued",
        "note": "周报生成需 ReportGenerator 接口支持",
    }
