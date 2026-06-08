"""POST /api/paper/start|stop|emergency-stop, GET /api/paper/status."""
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException
from backend.core.auth import RequireUser
from pydantic import BaseModel

from backend.services.paper_service import get_paper_service

router = APIRouter(prefix="/api/paper", tags=["paper"])


class PaperStartRequest(BaseModel):
    symbol: str = "XAUUSD+"
    timeframe: str = "M15"
    use_router: bool = False
    use_scheduler: bool = False
    use_calibrator: bool = False
    use_meta_monitor: bool = False
    use_factor_monitor: bool = False
    use_alerter: bool = False
    use_retrain: bool = False
    retrain_every_n: int = 200
    use_event_filter: bool = False
    risk_per_trade_pct: float | None = None
    max_daily_loss_pct: float | None = None
    single_risk_usd: float | None = None
    include_shadow_factors: bool = False
    shadow_top_k: int = 3


class PaperStopRequest(BaseModel):
    close_positions: bool = False


@router.post("/start")
def start(_user: RequireUser, req: PaperStartRequest)-> dict:
    svc = get_paper_service()
    try:
        st = svc.start(req.model_dump())
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail={"error": "already_running", "msg": str(e)})
    return {"status": st.status, "started_at": st.started_at, "pid": st.pid}


@router.post("/stop")
def stop(_user: RequireUser, req: PaperStopRequest)-> dict:
    svc = get_paper_service()
    st = svc.stop(req.close_positions)
    return {"status": st.status, "closed_positions": int(req.close_positions)}


@router.post("/emergency-stop")
def emergency_stop(
    body: PaperStopRequest = PaperStopRequest(close_positions=True),
    x_confirm: Annotated[str | None, Header()] = None,
) -> dict:
    """Emergency stop. Requires `X-Confirm: emergency` header (v1 defense)."""
    if x_confirm != "emergency":
        raise HTTPException(status_code=403, detail={"error": "missing_x_confirm", "msg": "send X-Confirm: emergency header"})
    svc = get_paper_service()
    st = svc.stop(close_positions=True)
    return {"status": st.status, "emergency": True, "closed_positions": 1}


@router.get("/status")
def status(_user: RequireUser)-> dict:
    svc = get_paper_service()
    st = svc.status()
    return {
        "status": st.status,
        "started_at": st.started_at,
        "pid": st.pid,
        "config": st.config,
        "last_error": st.last_error,
    }
