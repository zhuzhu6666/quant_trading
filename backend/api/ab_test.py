"""POST /api/ab/run + GET /api/ab/{id}."""
from fastapi import APIRouter, HTTPException
from backend.core.auth import RequireUser
from pydantic import BaseModel

from backend.jobs import get_job_manager
from backend.services.ab_service import run_ab

router = APIRouter(prefix="/api/ab", tags=["ab"])


class ABRequest(BaseModel):
    path_a: str = "baseline"
    path_b: str = "reverse"
    n_bars: int = 5000


@router.post("/run")
def run(_user: RequireUser, req: ABRequest)-> dict:
    mgr = get_job_manager()
    params = req.model_dump()
    fn = lambda cb: run_ab(params, cb)
    js = mgr.submit("ab_test", params, fn)
    return {"job_id": js.id, "status": js.status}


@router.get("/{job_id}")
def get_job(_user: RequireUser, job_id: str)-> dict:
    js = get_job_manager().get(job_id)
    if js is None:
        raise HTTPException(status_code=404, detail="job not found")
    return js.to_dict()
