"""POST /api/tuning/run + GET /api/tuning/{id}."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.jobs import get_job_manager
from backend.services.tuning_service import run_tuning

router = APIRouter(prefix="/api/tuning", tags=["tuning"])


class TuningRequest(BaseModel):
    risk_pct_grid: list[float] = [0.5, 1.0, 1.5, 2.0]
    cb_pct_grid: list[float] = [5, 10, 15, 20]
    n_bars: int = 5000


@router.post("/run")
def run(req: TuningRequest) -> dict:
    mgr = get_job_manager()
    params = req.model_dump()
    fn = lambda cb: run_tuning(params, cb)
    js = mgr.submit("tuning", params, fn)
    return {"job_id": js.id, "status": js.status}


@router.get("/{job_id}")
def get_job(job_id: str) -> dict:
    js = get_job_manager().get(job_id)
    if js is None:
        raise HTTPException(status_code=404, detail="job not found")
    return js.to_dict()
