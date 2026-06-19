"""POST /api/backtest/run → 202 {job_id}; GET /api/backtest/:id → job status."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.core.auth import RequireUser
from backend.jobs import get_job_manager
from backend.services.backtest_service import run_backtest

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


class BacktestRequest(BaseModel):
    symbol: str = "XAUUSD+"
    timeframe: str = "M15"
    risk_per_trade_pct: float | None = None
    enable_circuit: bool = False


@router.post("/run")
def run(_user: RequireUser, req: BacktestRequest) -> dict:
    """Submit a backtest job. Returns 202 with job_id (sync API for now)."""
    mgr = get_job_manager()
    # Bind params via closure: JobManager.submit calls fn(progress_cb) with
    # no params, so the service function must capture them itself.
    params = req.model_dump()
    fn = lambda cb: run_backtest(params, cb)
    js = mgr.submit("backtest", params, fn)
    return {"job_id": js.id, "status": js.status}


@router.get("/{job_id}")
def get_job(_user: RequireUser, job_id: str) -> dict:
    mgr = get_job_manager()
    js = mgr.get(job_id)
    if js is None:
        raise HTTPException(status_code=404, detail="job not found")
    return js.to_dict()


@router.get("/")
def list_jobs(_user: RequireUser, status: str | None = None) -> dict:
    mgr = get_job_manager()
    jobs = mgr.list(kind="backtest", status=status)
    return {"jobs": [j.to_dict() for j in jobs]}


# (audit v7-fix-5: FastAPI routes GET "/" on a prefix="/api/backtest" router
# to the path "/api/backtest/" only. The frontend calls "/api/backtest"
# (no trailing slash) and "/api/backtest/<id>", which are different
# routes. Add a no-path alias that maps to the same handler so the
# canonical /api/backtest?status=done query string reaches the backend.
# Otherwise next.config rewrites pass it through, FastAPI 404s, and the
# frontend's fetch().json() chokes on the HTML 404 body.)
@router.get("")
def list_jobs_noslash(_user: RequireUser, status: str | None = None) -> dict:
    return list_jobs(_user=_user, status=status)


@router.get("/{job_id}/report")
def get_job_report(_user: RequireUser, job_id: str) -> dict:
    """Fetch backtest report for a completed job."""
    mgr = get_job_manager()
    js = mgr.get(job_id)
    if js is None:
        raise HTTPException(status_code=404, detail="job not found")
    if js.status != "done":
        return {"job_id": job_id, "status": js.status, "report": None}
    # Try to read latest report from data/charts
    try:
        from backend.services.report_service import list_reports
        reports = list_reports(kind="json")
        # Match by job_id prefix or return most recent
        matched = [r for r in reports if job_id[:8] in r["name"]]
        target = matched[0] if matched else (reports[0] if reports else None)
        if target:
            from backend.services.report_service import read_report
            content = read_report(target["name"])
            return {"job_id": job_id, "status": js.status, "report": content}
    except Exception:
        pass
    return {"job_id": job_id, "status": js.status, "report": None}


@router.get("/report/latest")
def get_latest_report(_user: RequireUser) -> dict:
    """Fetch the most recent backtest report."""
    try:
        from backend.services.report_service import list_reports, read_report
        reports = list_reports(kind="json")
        if not reports:
            return {"report": None}
        target = reports[0]
        content = read_report(target["name"])
        return {"report": content}
    except Exception:
        return {"report": None}
