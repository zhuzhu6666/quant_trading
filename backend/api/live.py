"""Live trading API.

Endpoints (all JWT-protected except /api/auth/login; /api/health):
  GET  /api/live/status           — broker status + loop status
  GET  /api/live/loop-status      — just the loop subprocess status
  GET  /api/live/account          — real broker account info (balance/equity/margin)
  GET  /api/live/positions        — real open positions
  POST /api/live/start            — spawn `python main.py --mode live` subprocess
  POST /api/live/stop             — terminate the loop subprocess (SIGTERM, then SIGKILL)
  POST /api/live/emergency-close  — flatten all positions; requires X-Confirm: emergency

(audit 2026-06-08: previously /start and /stop were placeholders. v8 added
real subprocess management so the Web 总览 can drive the loop from the
browser. /account and /positions expose the real MT5/cTrader state via the
existing bridge.account_info() / get_positions() methods.)
"""
from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel

from backend.core.auth import RequireUser
from backend.services.live_service import (
    emergency_close,
    get_account,
    get_positions,
    get_status,
    loop_status,
    start_loop,
    stop_loop,
)

router = APIRouter(prefix="/api/live", tags=["live"])


class StartRequest(BaseModel):
    broker: str = "mt5"  # "mt5" | "ctrader"
    strategy_name: str = "v1_minimal_ma_cross"  # audit 2026-06-08: v1 loop 没真装 strategy, 仅记录名


class EmergencyCloseRequest(BaseModel):
    broker: str  # "mt5" | "ctrader"
    symbol: str | None = None


@router.get("/status")
def status(_user: RequireUser) -> dict:
    return get_status()


@router.get("/loop-status")
def get_loop_status(_user: RequireUser) -> dict:
    return loop_status()


@router.get("/account")
def get_account_endpoint(_user: RequireUser, broker: str = Query("mt5")) -> dict:
    return get_account(broker)


@router.get("/positions")
def get_positions_endpoint(
    _user: RequireUser,
    broker: str = Query("mt5"),
    symbol: str | None = Query(None),
) -> dict:
    return get_positions(broker, symbol)


@router.post("/start")
def start(_user: RequireUser, req: StartRequest) -> dict:
    """Spawn `python main.py --mode live` as a background subprocess.
    Refuses if a loop is already running. Requires the broker to be reachable
    (verified via get_account first)."""
    return start_loop(req.broker, strategy_name=req.strategy_name)


@router.post("/stop")
def stop(_user: RequireUser) -> dict:
    """Terminate the loop subprocess. Idempotent — returns ok=True with
    was_running=False if no loop is running."""
    return stop_loop()


@router.post("/emergency-close")
def emergency(
    req: EmergencyCloseRequest,
    x_confirm: str | None = Header(default=None),
) -> dict:
    if x_confirm != "emergency":
        raise HTTPException(
            status_code=403,
            detail={"error": "missing_x_confirm", "msg": "send X-Confirm: emergency header"},
        )
    return emergency_close(req.broker, req.symbol)
