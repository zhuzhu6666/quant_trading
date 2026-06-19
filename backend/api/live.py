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
browser. /account and /positions expose the real cTrader state via the
existing bridge.account_info() / get_positions() methods.)
"""
from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel

from backend.core.auth import RequireUser
from backend.services.live_service import (
    _should_send_orders,
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
    broker: str = "ctrader"  # 唯一执行通道
    strategy_name: str = "v1_minimal_ma_cross"  # audit 2026-06-08: v1 loop 没真装 strategy, 仅记录名


class EmergencyCloseRequest(BaseModel):
    broker: str  # "ctrader"
    symbol: str | None = None


@router.get("/status")
def status(_user: RequireUser) -> dict:
    return get_status()


@router.get("/loop-status")
def get_loop_status(_user: RequireUser) -> dict:
    return loop_status()


@router.get("/account")
def get_account_endpoint(_user: RequireUser, broker: str = Query("ctrader")) -> dict:
    return get_account(broker)


@router.get("/positions")
def get_positions_endpoint(
    _user: RequireUser,
    broker: str = Query("ctrader"),
    symbol: str | None = Query(None),
) -> dict:
    return get_positions(broker, symbol)


@router.post("/start")
def start(_user: RequireUser, req: StartRequest) -> dict:
    """Spawn live loop as background thread."""
    return start_loop(req.broker, strategy_name=req.strategy_name)


@router.post("/stop")
def stop() -> dict:
    """Terminate the loop subprocess."""
    return stop_loop()


@router.post("/emergency-close")
def emergency(
    _user: RequireUser,
    req: EmergencyCloseRequest,
    x_confirm: str | None = Header(default=None),
) -> dict:
    if x_confirm != "emergency":
        raise HTTPException(
            status_code=403,
            detail={"error": "missing_x_confirm", "msg": "send X-Confirm: emergency header"},
        )
    return emergency_close(req.broker, req.symbol)


@router.get("/strategy-status")
def strategy_status_endpoint(_user: RequireUser) -> dict:
    """当前策略、持仓、最近信号及开仓/不开仓原因。"""
    import os, re
    from pathlib import Path as _Path
    from backend.services.live_service import _live_state, loop_status as _ls

    loop = _ls()
    acct = _live_state.get("account") or {}
    positions = _live_state.get("positions") or []
    cb = _live_state.get("circuit_breaker", False)
    cb_reason = _live_state.get("circuit_reason", "")

    pos_list = positions.get("positions", []) if isinstance(positions, dict) else positions
    # ★ P0 fix: PositionInfo dataclass → dict
    if pos_list and hasattr(pos_list[0], '__dataclass_fields__'):
        from backend.ws.endpoints import _position_to_dict
        pos_list = [_position_to_dict(p) for p in pos_list]
    n_pos = len(pos_list)
    pos_dir = "FLAT"
    pos_entry = 0.0
    if pos_list:
        p = pos_list[0]
        pos_dir = "LONG" if p.get("type") == "buy" else "SHORT"
        pos_entry = float(p.get("price_open", 0))

    # 最近信号: 读 live_loop.log + backend.log 末尾
    recent_signals: list[dict] = []
    logs_dir = _Path(__file__).resolve().parent.parent.parent / "logs"
    for log_name in ["backend.log", "live_loop.log"]:
        log_path = logs_dir / log_name
        if not log_path.exists():
            continue
        try:
            with open(log_path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(size - 12000, 0))
                raw = f.read().decode("utf-8", errors="replace")
            lines = raw.splitlines()
            if size > 12000:
                lines = lines[1:]
            for line in lines[-40:]:
                m = re.search(r"signal=(\w+)", line)
                if not m:
                    continue
                direction = m.group(1)
                indicators = {}
                for key in ["rsi", "di", "stoch", "macd", "bb", "atr"]:
                    im = re.search(rf"{key}=([\d.\-]+)", line)
                    if im:
                        indicators[key] = float(im.group(1))
                vm = re.search(r"votes\(([\d.]+)/([\d.]+)\)", line)
                votes = None
                if vm:
                    votes = {"long": float(vm.group(1)), "short": float(vm.group(2))}
                recent_signals.append({
                    "direction": direction,
                    "indicators": indicators,
                    "votes": votes,
                    "dry_run": "dry-run" in line,
                })
        except Exception:
            pass

    # 原因
    reason = ""
    if cb:
        reason = f"熔断激活: {cb_reason}" if cb_reason else "熔断激活"
    elif not loop.get("running"):
        reason = "实盘未启动"
    elif pos_dir != "FLAT":
        reason = f"已有 {pos_dir} 持仓 @ {pos_entry:.2f}"
    elif not _should_send_orders("ctrader"):
        reason = "DRY-RUN 模式 (不发实单)"
    elif recent_signals:
        last = recent_signals[-1]
        reason = f"最近信号: {last['direction']}"
    else:
        reason = "等待策略信号"

    return {
        "running": loop.get("running", False),
        "broker": loop.get("broker") or "ctrader",
        "strategy": "factor_pipeline_v4",
        "mode": "DRY-RUN" if not _should_send_orders("ctrader") else "LIVE",
        "position": {"dir": pos_dir, "entry": round(pos_entry, 2), "count": n_pos},
        "circuit_breaker": cb,
        "circuit_reason": cb_reason,
        "reason": reason,
        "recent_signals": recent_signals[-5:],
    }


@router.get("/session-stats")
def session_stats_endpoint(_user: RequireUser) -> dict:
    """今日会话统计: 盈亏/交易笔数/胜率/回撤. HTTP 后备, 不依赖 WS."""
    from backend.services.live_service import _live_state
    return {
        "pnl_today": float(_live_state.get("session_pnl", 0)),
        "trades": int(_live_state.get("session_trades", 0)),
        "wins": int(_live_state.get("session_winning", 0)),
        "losses": int(_live_state.get("session_losing", 0)),
        "drawdown_pct": float(_live_state.get("session_max_drawdown_pct", 0)),
        "consecutive_loss": int(_live_state.get("session_losing", 0)),
    }
