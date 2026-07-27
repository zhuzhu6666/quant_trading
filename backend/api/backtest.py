"""Historical parity backtest job API."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.core.auth import RequireUser
from backend.jobs import get_job_manager
from backend.services.backtest_service import run_backtest

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


class BacktestRequest(BaseModel):
    symbol: str = "XAUUSD+"
    timeframe: str = "M5"
    start: str | float | None = None
    end: str | float | None = None
    max_bars: int = Field(default=5000, ge=2, le=20_000)
    warmup_bars: int = Field(default=150, ge=0, le=1000)
    initial_equity: float = Field(default=10_000.0, gt=0)
    volume_lots: float = Field(default=0.01, gt=0)
    contract_size: float = Field(default=100.0, gt=0)
    commission_per_lot_round_turn: float = Field(default=6.0, ge=0)
    slippage_bps: float = Field(default=0.0, ge=0)


def _job_payload(job: Any) -> dict[str, Any]:
    return dict(job.to_dict())


@router.post("/run")
def run(_user: RequireUser, req: BacktestRequest) -> dict[str, Any]:
    mgr = get_job_manager()
    active = [
        job
        for status in ("queued", "pending", "running", "retry_wait")
        for job in mgr.list(kind="backtest", status=status)
    ]
    if active:
        raise HTTPException(
            status_code=409,
            detail={"error": "backtest_already_running", "job_id": active[0].id},
        )
    params = req.model_dump()
    job = mgr.submit("backtest", params, lambda cb: run_backtest(params, cb))
    return {"job_id": job.id, "status": job.status, "engine": "live_parity_replay_v1"}


@router.get("/{job_id}")
def get_job(_user: RequireUser, job_id: str) -> dict[str, Any]:
    job = get_job_manager().get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _job_payload(job)


@router.get("/")
def list_jobs(_user: RequireUser, status: str | None = None) -> dict[str, Any]:
    jobs = get_job_manager().list(kind="backtest", status=status)
    return {"jobs": [_job_payload(job) for job in jobs]}


@router.get("")
def list_jobs_noslash(_user: RequireUser, status: str | None = None) -> dict[str, Any]:
    return list_jobs(_user=_user, status=status)


@router.get("/{job_id}/report")
def get_job_report(_user: RequireUser, job_id: str) -> dict[str, Any]:
    job = get_job_manager().get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return {
        "job_id": job_id,
        "status": job.status,
        "report": job.result if job.status == "done" else None,
    }


@router.post("/{job_id}/cancel")
def cancel_job(_user: RequireUser, job_id: str) -> dict[str, Any]:
    manager = get_job_manager()
    if manager.get(job_id) is None:
        raise HTTPException(status_code=404, detail="job not found")
    return {"job_id": job_id, "cancelled": bool(manager.cancel(job_id))}


@router.get("/report/latest")
def get_latest_report(_user: RequireUser) -> dict[str, Any]:
    jobs = get_job_manager().list(kind="backtest", status="done")
    if not jobs:
        return {"report": None}
    latest = max(jobs, key=lambda job: float(getattr(job, "updated_at", 0.0) or 0.0))
    return {"report": latest.result}
