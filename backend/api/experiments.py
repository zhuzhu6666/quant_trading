"""Experiments API endpoints — thin REST wrapper over ExperimentTracker."""
from fastapi import APIRouter
from typing import Any

from backend.core.auth import RequireUser
from research.experiment_tracker import ExperimentTracker

router = APIRouter(prefix="/api/experiments", tags=["experiments"])

_tracker: ExperimentTracker | None = None


def _get_tracker() -> ExperimentTracker:
    global _tracker
    if _tracker is None:
        _tracker = ExperimentTracker()
    return _tracker


@router.get("/")
def list_experiments(
    _user: RequireUser,
    exp_type: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """
    查询实验记录。
    """
    tracker = _get_tracker()
    runs = tracker.query(exp_type=exp_type, limit=limit)
    return {
        "experiments": [
            {
                "run_id": r.run_id,
                "timestamp": r.timestamp,
                "experiment_type": r.experiment_type,
                "params": r.params,
                "metrics": r.metrics,
                "tags": r.tags,
                "artifacts": r.artifacts,
                "status": r.status,
            }
            for r in runs
        ],
        "count": len(runs),
    }


@router.get("/stats")
def get_stats(_user: RequireUser) -> dict[str, Any]:
    """
    获取实验统计概览。
    """
    tracker = _get_tracker()
    return tracker.get_summary_stats()


@router.get("/{run_id}")
def get_experiment(_user: RequireUser, run_id: str) -> dict[str, Any] | None:
    """
    获取单个实验详情。
    """
    tracker = _get_tracker()
    r = tracker.get_run(run_id)
    if r is None:
        return None
    return {
        "run_id": r.run_id,
        "timestamp": r.timestamp,
        "experiment_type": r.experiment_type,
        "params": r.params,
        "metrics": r.metrics,
        "tags": r.tags,
        "artifacts": r.artifacts,
        "status": r.status,
    }


@router.post("/")
def create_experiment(
    _user: RequireUser,
    exp_type: str,
    params: dict[str, Any] | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """
    创建新实验。
    """
    tracker = _get_tracker()
    run_id = tracker.start_run(exp_type=exp_type, params=params or {}, tags=tags or [])
    return {"run_id": run_id, "status": "running"}


@router.patch("/{run_id}")
def update_experiment(
    _user: RequireUser,
    run_id: str,
    status: str | None = None,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    更新实验状态/指标。
    """
    tracker = _get_tracker()
    if status:
        tracker.finish_run(run_id, status)
    if metrics:
        for k, v in metrics.items():
            try:
                tracker.log_metric(run_id, k, float(v))
            except Exception:
                pass
    return {"run_id": run_id, "status": status or "updated"}
