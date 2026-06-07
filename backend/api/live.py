"""POST /api/live/{start,stop,emergency-close} + GET /api/live/status."""
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from backend.services.live_service import emergency_close, get_status

router = APIRouter(prefix="/api/live", tags=["live"])


class EmergencyCloseRequest(BaseModel):
    broker: str  # "mt5" | "ctrader"
    symbol: str | None = None


@router.get("/status")
def status() -> dict:
    return get_status()


@router.post("/emergency-close")
def emergency(req: EmergencyCloseRequest, x_confirm: str | None = Header(default=None)) -> dict:
    if x_confirm != "emergency":
        raise HTTPException(status_code=403, detail={"error": "missing_x_confirm", "msg": "send X-Confirm: emergency header"})
    return emergency_close(req.broker, req.symbol)


@router.post("/start")
def start() -> dict:
    """Start live trading loop. Not implemented in v1 — placeholder."""
    return {"ok": False, "error": "live trading loop not started via web UI in v1 (use python main.py --mode live)"}


@router.post("/stop")
def stop() -> dict:
    """Stop live trading loop. Not implemented in v1 — placeholder."""
    return {"ok": False, "error": "live trading loop not stopped via web UI in v1 (kill python main.py --mode live)"}
