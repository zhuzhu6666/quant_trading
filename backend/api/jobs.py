"""Generic jobs endpoints: list all / cancel any."""
from fastapi import APIRouter, HTTPException

from backend.core.auth import RequireUser
from backend.jobs import get_job_manager
from backend.services.mutation_audit import record_api_mutation

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("")
def list_jobs(_user: RequireUser, kind: str | None = None, status: str | None = None) -> dict:
    mgr = get_job_manager()
    jobs = mgr.list(kind=kind, status=status)
    return {"jobs": [j.to_dict() for j in jobs]}


@router.get("/{job_id}")
def get_job(_user: RequireUser, job_id: str) -> dict:
    js = get_job_manager().get(job_id)
    if js is None:
        raise HTTPException(status_code=404, detail="job not found")
    return js.to_dict()


@router.post("/{job_id}/cancel")
def cancel_job(_user: RequireUser, job_id: str) -> dict:
    ok = get_job_manager().cancel(job_id)
    if not ok:
        record_api_mutation(
            user=_user,
            endpoint="/api/jobs/{job_id}/cancel",
            action="cancel_job",
            status="blocked",
            result={"job_id": job_id, "reason": "job not running or not found"},
        )
        raise HTTPException(status_code=400, detail="job not running or not found")
    result = {"ok": True, "job_id": job_id}
    record_api_mutation(
        user=_user,
        endpoint="/api/jobs/{job_id}/cancel",
        action="cancel_job",
        status="applied",
        result=result,
    )
    return result
