"""GET /api/sync/status, POST /api/sync/once, POST /api/sync/daemon/start|stop."""
from fastapi import APIRouter
from backend.core.auth import RequireUser
from pydantic import BaseModel

from backend.jobs import get_job_manager
from backend.services.api_fact_views import sync_status_fact_payload
from backend.services.sync_service import get_status
from backend.services.mutation_audit import record_api_mutation

router = APIRouter(prefix="/api/sync", tags=["sync"])


class OnceRequest(BaseModel):
    timeframes: list[str] = ["M15", "H1", "D1"]
    type: str = "incremental"


class DaemonStartRequest(BaseModel):
    interval_seconds: int = 300
    timeframes: list[str] = ["M15", "H1", "D1"]


@router.get("/status")
def status(_user: RequireUser) -> dict:
    return sync_status_fact_payload(get_status())


@router.post("/once")
def once(_user: RequireUser, req: OnceRequest) -> dict:
    mgr = get_job_manager()
    params = req.model_dump()
    js = mgr.submit("sync", params)
    result = {"job_id": js.id, "status": js.status}
    record_api_mutation(
        user=_user,
        endpoint="/api/sync/once",
        action="sync_once",
        status="applied",
        result=result,
    )
    return result


@router.post("/daemon/start")
def daemon_start(_user: RequireUser, req: DaemonStartRequest) -> dict:
    """已弃用 — 活盘用 scheduler 代替 daemon"""
    result = {"ok": False, "msg": "daemon 已弃用, 改用 scheduler 自动同步"}
    record_api_mutation(
        user=_user,
        endpoint="/api/sync/daemon/start",
        action="sync_daemon_start",
        status="blocked",
        result=result,
    )
    return result


@router.post("/daemon/stop")
def daemon_stop(_user: RequireUser) -> dict:
    """已弃用"""
    result = {"ok": False, "msg": "daemon 已弃用"}
    record_api_mutation(
        user=_user,
        endpoint="/api/sync/daemon/stop",
        action="sync_daemon_stop",
        status="blocked",
        result=result,
    )
    return result
