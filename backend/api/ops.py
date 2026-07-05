"""Ops API endpoints: alerts, auto-recovery, weekly reports, experiments."""
from fastapi import APIRouter
import time
from typing import Any

from backend.core.auth import RequireUser
from backend.services.backend_readiness import BackendReadinessService
from backend.services.stability import TimedCache, measure
from monitor.auto_recovery import AutoRecovery
from research.report_generator import WeeklyReport
from research.experiment_tracker import ExperimentTracker

router = APIRouter(prefix="/api/ops", tags=["ops"])

# Singletons (lazy init)
_auto_recovery: AutoRecovery | None = None
_report_gen: WeeklyReport | None = None
_READINESS_CACHE = TimedCache()


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
