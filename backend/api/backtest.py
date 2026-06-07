"""POST /api/backtest/run → 202 {job_id}; GET /api/backtest/:id → job status."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.jobs import get_job_manager
from backend.services.backtest_service import run_backtest

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


class BacktestRequest(BaseModel):
    symbol: str = "XAUUSD+"
    timeframe: str = "M15"
    risk_per_trade_pct: float | None = None
    enable_circuit: bool = False


@router.post("/run")
def run(req: BacktestRequest) -> dict:
    """Submit a backtest job. Returns 202 with job_id (sync API for now)."""
    mgr = get_job_manager()
    # Bind params via closure: JobManager.submit calls fn(progress_cb) with
    # no params, so the service function must capture them itself.
    params = req.model_dump()
    fn = lambda cb: run_backtest(params, cb)
    js = mgr.submit("backtest", params, fn)
    return {"job_id": js.id, "status": js.status}


@router.get("/{job_id}")
def get_job(job_id: str) -> dict:
    mgr = get_job_manager()
    js = mgr.get(job_id)
    if js is None:
        raise HTTPException(status_code=404, detail="job not found")
    return js.to_dict()


@router.get("/")
def list_jobs(status: str | None = None) -> dict:
    mgr = get_job_manager()
    jobs = mgr.list(kind="backtest", status=status)
    return {"jobs": [j.to_dict() for j in jobs]}
