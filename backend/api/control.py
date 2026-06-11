"""GET /api/control/scheduler — 自进化 Scheduler 状态 + 启停."""
from fastapi import APIRouter
from backend.core.auth import RequireUser

router = APIRouter(prefix="/api/control", tags=["control"])


@router.get("/scheduler")
def scheduler_status(_user: RequireUser) -> dict:
    """返回 InProcessScheduler 的当前状态和所有 job 信息."""
    from backend.runtime.scheduler import InProcessScheduler
    try:
        sched = InProcessScheduler._instance
        if sched is None:
            return {"running": False, "jobs": [], "error": "scheduler not initialized"}
        running = sched._started if hasattr(sched, "_started") else False
        jobs = []
        try:
            jobs = [{
                "name": j.name,
                "cron_expr": j.cron_expr,
                "running": j.running,
                "run_count": j.run_count,
                "error_count": j.error_count,
                "last_error": j.last_error or "",
                "next_run_time": j.next_run_time,
            } for j in (sched.list_jobs() if hasattr(sched, "list_jobs") else [])]
        except Exception:
            pass
        return {"running": running, "jobs": jobs}
    except Exception as e:
        return {"running": False, "jobs": [], "error": str(e)}


@router.get("/evolution/latest")
def evolution_latest(_user: RequireUser) -> dict:
    """返回最近的 evolution_story 事件."""
    try:
        from monitor.evolution_story import EvolutionStory
        story = EvolutionStory.shared()
        events = story.query(limit=5, event_type="cycle_complete")
        if events:
            return dict(events[0])
        return {"event_type": "none", "ts_iso": ""}
    except Exception as e:
        return {"event_type": "error", "error": str(e), "ts_iso": ""}
