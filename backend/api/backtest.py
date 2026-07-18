"""POST /api/backtest/run → 202 {job_id}; GET /api/backtest/:id → job status."""
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.core.auth import RequireUser
from backend.jobs import get_job_manager
from backend.services.backtest_service import legacy_backtest_contract, run_backtest
from backend.services.research_evidence import enforce_legacy_backtest_contract

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


class BacktestRequest(BaseModel):
    symbol: str = "XAUUSD+"
    timeframe: str = "M15"
    risk_per_trade_pct: float | None = None
    enable_circuit: bool = False


def _legacy_job_payload(job) -> dict:
    payload = dict(job.to_dict())
    result = payload.get("result")
    if isinstance(result, dict):
        payload["result"] = enforce_legacy_backtest_contract(result)
    return enforce_legacy_backtest_contract(payload)


def _legacy_report_payload(report) -> dict | None:
    """Force the boundary on both report metadata and parsed JSON content."""

    if not isinstance(report, dict):
        return report
    payload = dict(report)
    content = payload.get("content")
    if isinstance(content, dict):
        payload["content"] = enforce_legacy_backtest_contract(content)
    return enforce_legacy_backtest_contract(payload)


@router.post("/run")
def run(_user: RequireUser, req: BacktestRequest) -> dict:
    """Submit a backtest job. Returns 202 with job_id (sync API for now)."""
    mgr = get_job_manager()
    # Bind params via closure: JobManager.submit calls fn(progress_cb) with
    # no params, so the service function must capture them itself.
    params = req.model_dump()
    fn = lambda cb: run_backtest(params, cb)
    js = mgr.submit("backtest", params, fn)
    return {"job_id": js.id, "status": js.status, **legacy_backtest_contract()}


@router.get("/{job_id}")
def get_job(_user: RequireUser, job_id: str) -> dict:
    mgr = get_job_manager()
    js = mgr.get(job_id)
    if js is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _legacy_job_payload(js)


@router.get("/")
def list_jobs(_user: RequireUser, status: str | None = None) -> dict:
    mgr = get_job_manager()
    jobs = mgr.list(kind="backtest", status=status)
    return {"jobs": [_legacy_job_payload(j) for j in jobs], **legacy_backtest_contract()}


# (audit v7-fix-5: FastAPI routes GET "/" on a prefix="/api/backtest" router
# to the path "/api/backtest/" only. Add a no-path alias that maps to the
# same handler so the canonical /api/backtest?status=done query string works.)
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
        return {
            "job_id": job_id,
            "status": js.status,
            "report": None,
            **legacy_backtest_contract(),
        }
    # Try to read latest report from data/charts
    try:
        from backend.services.report_service import list_reports, read_report

        result = js.result if isinstance(js.result, dict) else {}
        result_report_name = Path(str(result.get("report_path") or "")).name
        reports = [
            item
            for item in list_reports(kind="all")
            if str(item.get("name") or "").startswith("backtest_")
            and str(item.get("kind") or "") in {"txt", "json"}
        ]
        target = next(
            (item for item in reports if item.get("name") == result_report_name),
            None,
        )
        if target is None and reports:
            target = max(reports, key=lambda item: str(item.get("modified_at") or ""))
        if target:
            content = read_report(target["name"])
            return {
                "job_id": job_id,
                "status": js.status,
                "report": _legacy_report_payload(content),
                **legacy_backtest_contract(),
            }
    except Exception:
        pass
    return {
        "job_id": job_id,
        "status": js.status,
        "report": None,
        **legacy_backtest_contract(),
    }


@router.get("/report/latest")
def get_latest_report(_user: RequireUser) -> dict:
    """Fetch the most recent backtest report."""
    try:
        from backend.services.report_service import list_reports, read_report
        reports = [
            item
            for item in list_reports(kind="all")
            if str(item.get("name") or "").startswith("backtest_")
            and str(item.get("kind") or "") in {"txt", "json"}
        ]
        if not reports:
            return {"report": None, **legacy_backtest_contract()}
        target = max(reports, key=lambda item: str(item.get("modified_at") or ""))
        content = read_report(target["name"])
        return {
            "report": _legacy_report_payload(content),
            **legacy_backtest_contract(),
        }
    except Exception:
        return {"report": None, **legacy_backtest_contract()}
