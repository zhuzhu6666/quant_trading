"""GET /api/sync/status, POST /api/sync/once, POST /api/sync/daemon/start|stop."""
from fastapi import APIRouter
from backend.core.auth import RequireUser
from pydantic import BaseModel

from backend.jobs import get_job_manager
from backend.services.sync_service import get_status, run_sync_once

router = APIRouter(prefix="/api/sync", tags=["sync"])


class OnceRequest(BaseModel):
    timeframes: list[str] = ["M15", "H1", "D1"]
    type: str = "incremental"


class DaemonStartRequest(BaseModel):
    interval_seconds: int = 300
    timeframes: list[str] = ["M15", "H1", "D1"]


@router.get("/status")
def status(_user: RequireUser) -> dict:
    return get_status()


@router.post("/once")
def once(_user: RequireUser, req: OnceRequest) -> dict:
    mgr = get_job_manager()
    params = req.model_dump()
    fn = lambda cb: run_sync_once(params, cb)
    js = mgr.submit("sync", params, fn)
    return {"job_id": js.id, "status": js.status}


@router.post("/daemon/start")
def daemon_start(_user: RequireUser, req: DaemonStartRequest) -> dict:
    """已弃用 — 活盘用 scheduler 代替 daemon"""
    return {"ok": False, "msg": "daemon 已弃用, 改用 scheduler 自动同步"}


@router.post("/daemon/stop")
def daemon_stop(_user: RequireUser) -> dict:
    """已弃用"""
    return {"ok": False, "msg": "daemon 已弃用"}
