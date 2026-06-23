"""POST /api/factor-health/run, GET /api/factor-health/latest."""
import json
from pathlib import Path

from fastapi import APIRouter
from backend.core.auth import RequireUser
from pydantic import BaseModel

from backend.core.paths import CHARTS_DIR
from backend.jobs import get_job_manager
from backend.services.factor_health_service import run_factor_health

router = APIRouter(prefix="/api/factor-health", tags=["factor-health"])


def _is_internal_factor_name(name: str) -> bool:
    return name.startswith("dsl_") or name.startswith("pca_")


class RunRequest(BaseModel):
    threshold: float = 0.04
    bar_count: int = 50000
    sync_run: bool = False


@router.post("/run")
def run(_user: RequireUser, req: RunRequest)-> dict:
    mgr = get_job_manager()
    params = req.model_dump()
    fn = lambda cb: run_factor_health(params, cb)
    js = mgr.submit("factor_health", params, fn)
    return {"job_id": js.id, "status": js.status}


@router.get("/latest")
def latest(_user: RequireUser)-> dict:
    """Read the last-written factor_health_report.json. Returns 404 if not present."""
    p = CHARTS_DIR / "factor_health_report.json"
    if not p.exists():
        return {"error": "no_report_yet", "report": None}

    report = json.loads(p.read_text(encoding="utf-8"))
    factors = report.get("factors", []) if isinstance(report, dict) else []
    visible = [f for f in factors if not _is_internal_factor_name(str(f.get("factor", "")))]
    visible.sort(key=lambda f: float(f.get("score", 0.0) or 0.0), reverse=True)

    if isinstance(report, dict):
        report = dict(report)
        report["factors"] = visible
        report["total"] = len(visible)
        report["healthy"] = sum(1 for f in visible if f.get("status") == "HEALTHY")
        report["watch"] = sum(1 for f in visible if f.get("status") == "WATCH")
        report["decaying"] = sum(1 for f in visible if f.get("status") == "DECAYING")
        report["dead"] = sum(1 for f in visible if f.get("status") == "DEAD")
        report["unknown"] = sum(1 for f in visible if f.get("status") == "UNKNOWN")

    return {"report": report, "report_path": str(p)}
