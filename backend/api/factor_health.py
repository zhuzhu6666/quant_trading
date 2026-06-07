"""POST /api/factor-health/run, GET /api/factor-health/latest."""
import json
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from backend.core.paths import CHARTS_DIR
from backend.jobs import get_job_manager
from backend.services.factor_health_service import run_factor_health

router = APIRouter(prefix="/api/factor-health", tags=["factor-health"])


class RunRequest(BaseModel):
    threshold: float = 0.04
    bar_count: int = 50000
    sync_run: bool = False


@router.post("/run")
def run(req: RunRequest) -> dict:
    mgr = get_job_manager()
    params = req.model_dump()
    fn = lambda cb: run_factor_health(params, cb)
    js = mgr.submit("factor_health", params, fn)
    return {"job_id": js.id, "status": js.status}


@router.get("/latest")
def latest() -> dict:
    """Read the last-written factor_health_report.json. Returns 404 if not present."""
    p = CHARTS_DIR / "factor_health_report.json"
    if not p.exists():
        return {"error": "no_report_yet", "report": None}
    return {"report": json.loads(p.read_text(encoding="utf-8")), "report_path": str(p)}
