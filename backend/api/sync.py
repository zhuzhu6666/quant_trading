"""GET /api/sync/status, POST /api/sync/once."""
from fastapi import APIRouter
from backend.core.auth import RequireUser
from pydantic import BaseModel

from backend.jobs import get_job_manager
from backend.services.sync_service import get_status, run_sync_once

router = APIRouter(prefix="/api/sync", tags=["sync"])


class OnceRequest(BaseModel):
    timeframes: list[str] = ["M15", "H1", "D1"]
    type: str = "incremental"


@router.get("/status")
def status(_user: RequireUser)-> dict:
    return get_status()


@router.post("/once")
def once(_user: RequireUser, req: OnceRequest)-> dict:
    mgr = get_job_manager()
    params = req.model_dump()
    fn = lambda cb: run_sync_once(params, cb)
    js = mgr.submit("sync", params, fn)
    return {"job_id": js.id, "status": js.status}
