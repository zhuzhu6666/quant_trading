"""POST /api/discover (start job) + GET /api/discover/{id} (status)."""
from fastapi import APIRouter
from backend.core.auth import RequireUser
from pydantic import BaseModel

from backend.jobs import get_job_manager
from backend.services.discover_service import run_discovery

router = APIRouter(prefix="/api/discover", tags=["discover"])


class DiscoverRequest(BaseModel):
    engine: str = "gp"  # "gp" | "random"
    n_candidates: int = 1000
    top_k: int = 50
    forward_periods: list[int] = [1, 5, 20]
    auto_register: bool = True
    gp_pop: int = 100
    gp_gen: int = 20


@router.post("")
def start(_user: RequireUser, req: DiscoverRequest)-> dict:
    mgr = get_job_manager()
    params = req.model_dump()
    fn = lambda cb: run_discovery(params, cb)
    js = mgr.submit("discover", params, fn)
    return {"job_id": js.id, "status": js.status}


@router.get("/{job_id}")
def get_job(_user: RequireUser, job_id: str)-> dict:
    from backend.jobs import get_job_manager
    from fastapi import HTTPException
    js = get_job_manager().get(job_id)
    if js is None:
        raise HTTPException(status_code=404, detail="job not found")
    d = js.to_dict()
    d["top_factors"] = (d.get("result") or {}).get("top_factors", [])
    return d
