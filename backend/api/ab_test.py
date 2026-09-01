"""POST /api/ab/run + GET /api/ab/{id} + GET /api/ab (B6 fix: 列表 endpoint)."""
from fastapi import APIRouter, HTTPException
from backend.core.auth import RequireUser
from pydantic import BaseModel

from backend.jobs import get_job_manager

router = APIRouter(prefix="/api/ab", tags=["ab"])


class ABRequest(BaseModel):
    path_a: str = "baseline"
    path_b: str = "reverse"
    n_bars: int = 5000


@router.post("/run")
def run(_user: RequireUser, req: ABRequest)-> dict:
    mgr = get_job_manager()
    params = req.model_dump()
    js = mgr.submit("ab_test", params)
    return {"job_id": js.id, "status": js.status}


@router.get("")  # B6 fix: 之前只有 /run + /{id}, 前端 useApi("/api/ab") 拿到 Vite fallback HTML 永远 loading
def list_jobs(_user: RequireUser) -> dict:
    """列出 ab_test 类型的 jobs (跟 /api/jobs?kind=ab_test 等价, 单独 endpoint 方便前端直接挂)."""
    mgr = get_job_manager()
    jobs = mgr.list(kind="ab_test")
    return {"jobs": [js.to_dict() for js in jobs]}


@router.get("/{job_id}")
def get_job(_user: RequireUser, job_id: str)-> dict:
    js = get_job_manager().get(job_id)
    if js is None:
        raise HTTPException(status_code=404, detail="job not found")
    return js.to_dict()
