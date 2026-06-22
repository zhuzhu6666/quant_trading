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
import re

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
def stop(_user: RequireUser) -> dict:
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
    """当前因子管道状态、持仓、最近信号及闸门判定。"""
    from pathlib import Path as _Path
    from backend.services.live_service import _live_state, loop_status as _ls, _factor_pipeline

    loop = _ls()
    acct = _live_state.get("account") or {}
    positions = _live_state.get("positions") or []
    cb = _live_state.get("circuit_breaker", False)
    cb_reason = _live_state.get("circuit_reason", "")

    pos_list = positions.get("positions", []) if isinstance(positions, dict) else positions
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

    # ── v4 因子管道状态 ──
    pipeline = _factor_pipeline or {}
    engine = pipeline.get("engine")
    attr = pipeline.get("attribution")
    awe = pipeline.get("awe")

    v4_status: dict = {
        "pipeline_active": bool(pipeline),
        "engine_warm": engine.is_warm if engine else False,
        "buffer_size": engine.buffer_size if engine else 0,
        "n_attribution_trades": sum(s.n_trades for s in attr.get_all_factor_stats().values()) if attr else 0,
        "awe_conviction": round(awe.composite_conviction(), 3) if awe else 0.5,
    }

    # 最近信号: 从 live_loop.log 读取 v4 格式
    recent_signals: list[dict] = []
    logs_dir = _Path(__file__).resolve().parent.parent.parent / "logs"
    for log_name in ["live_loop.log"]:
        log_path = logs_dir / log_name
        if not log_path.exists():
            continue
        try:
            with open(log_path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(size - 8000, 0))
                raw = f.read().decode("utf-8", errors="replace")
            lines = raw.splitlines()
            if size > 8000:
                lines = lines[1:]
            for line in lines[-30:]:
                m = re.search(
                    r"signal=(LONG|SHORT)\s+score=([\d.\-]+)\s+"
                    r"tactical=([\d.\-]+)\s+macro=([\d.\-]+)\s+"
                    r"n=(\d+)\s+gate=(\S+)",
                    line,
                )
                if m:
                    recent_signals.append({
                        "direction": m.group(1),
                        "score": float(m.group(2)),
                        "tactical_score": float(m.group(3)),
                        "macro_score": float(m.group(4)),
                        "n_active_factors": int(m.group(5)),
                        "gate_reason": m.group(6),
                    })
        except Exception:
            pass

    # 原因
    reason = ""
    if cb:
        reason = f"熔断激活: {cb_reason}" if cb_reason else "熔断激活"
    elif not loop.get("running"):
        reason = "实盘未启动"
    elif not v4_status.get("pipeline_active"):
        reason = "因子管道未初始化"
    elif not v4_status.get("engine_warm"):
        reason = f"因子引擎预热中 ({v4_status.get('buffer_size', 0)}/50 bars)"
    elif pos_dir != "FLAT":
        reason = f"已有 {pos_dir} 持仓 @ {pos_entry:.2f}"
    elif recent_signals:
        last = recent_signals[-1]
        if last.get("gate_reason", "").startswith("passed"):
            reason = f"最近信号: {last['direction']} score={last['score']:.3f} (闸门通过)"
        else:
            reason = f"最近信号: {last['direction']} score={last['score']:.3f} (闸门: {last.get('gate_reason', '?')})"
    elif not _should_send_orders("ctrader"):
        reason = "DRY-RUN 模式 (不发实单)"
    else:
        reason = "等待因子信号"

    # ── 因子投票快照 (每 tick 更新, 前端「因子投票」面板) ──
    factor_votes = _live_state.get("last_factor_votes") or {}
    last_composite = _live_state.get("last_composite") or {}
    # 诊断信息
    diag = _live_state.get("_diag") or {}

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
        "v4_status": v4_status,
        "factor_votes": factor_votes,
        "last_composite": last_composite,
        "_diag": diag,
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
        "consecutive_loss": int(_live_state.get("session_consecutive_loss", 0)),
    }
