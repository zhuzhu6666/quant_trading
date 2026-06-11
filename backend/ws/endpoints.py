"""WebSocket routes: /ws/state, /ws/alerts, /ws/jobs/:id, /ws/logs.

The /ws/state broadcaster pushes a 1s snapshot. v8: the snapshot now
includes a `source` field ("paper" | "live" | "none") and a `live` block
with the real broker account/position when the live loop is running. This
lets the frontend / 总览 page show real broker data without polling, and
keeps backward compatibility (paper-mode clients ignore the new fields).
"""
import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend.core.logging import setup_logging
from core.state import state
from backend.services.live_service import (
    get_account as _live_get_account,
    get_positions as _live_get_positions,
    loop_status as _live_loop_status,
    get_latest_price as _live_get_latest_price,  # audit 2026-06-08
)
from backend.services.paper_service import get_paper_service  # audit 2026-06-08
from backend.ws.manager import get_connection_manager

router = APIRouter()
setup_logging()


def _read_state_snapshot() -> dict:
    """Read current state for snapshot. Falls back to zeros if no paper running.

    v8: when the live loop is running, the `live` block is populated from
    the real broker; the `source` field tells the frontend which to use.
    When the live loop is stopped AND paper isn't running, source is
    "none" and equity/balance are zeros."""
    loop = _live_loop_status()
    live_running = loop.get("running", False)
    broker = loop.get("broker")

    # ── live block (if loop is running) ──
    # audit 2026-06-08: WS 推送不再调 broker, 只读 live loop 写的共享缓存.
    # audit 2026-06-09: 之前在 acct 缺失时会 fallback 同步调 _live_get_account /
    # _live_get_positions — 那是 Twisted reactor callFromThread + busy-wait,
    # 阻塞 asyncio 事件循环 50-200ms × N 次/秒, 直接卡住前端所有 HTTP 请求
    # 和 tab 切换. 修复: snapshot 永不调 broker, 缓存未就绪时用零值 + warning
    # 标志, 让前端显示 "warming up" 而不是卡死.
    live_block: dict | None = None
    live_source = "none"
    if live_running and broker:
        from backend.services.live_service import _live_state
        acct = _live_state.get("account") or {}
        positions = _live_state.get("positions") or []
        # ⚠️ audit 2026-06-09: 不再 fallback 调 broker. 如果缓存没准备好, snapshot
        # 用 0 占位 + warming_up 标志, 前端 UI 显示初始化中. 真正 broker 数据
        # 要等 live loop 的第一个 tick (60s 内). 之前 fallback 调 _live_get_account
        # 会同步阻塞 WS 推送 50-200ms, 这是启动 cTrader 后 tab 切换卡顿的根因.
        warming_up = not acct or acct.get("error")
        # Normalize: take first position as the "current position" (the v1
        # strategy uses at most one open position at a time)
        live_pos = None
        if (not warming_up) and isinstance(positions, dict) and positions.get("ok") and positions.get("positions"):
            p = positions["positions"][0]
            live_pos = {
                "dir": "LONG" if p.get("type") == "buy" else "SHORT",
                "entry": p.get("price_open", 0.0),
                "size": p.get("volume", 0.0),
                "unrealized": p.get("profit") or 0.0,
                "ticket": p.get("ticket"),
                "sl": p.get("sl", 0.0),
                "tp": p.get("tp", 0.0),
            }
        n_positions = len(positions.get("positions") or []) if isinstance(positions, dict) else 0
        live_block = {
            "broker": broker,
            "account": acct if (not warming_up and acct.get("ok")) else {
                "warming_up": True,
                "broker": broker,
            },
            "position": live_pos,
            "n_positions": n_positions,
        }
        live_source = "live"

    # ── paper block (always try; falls back to zeros if state uninit) ──
    try:
        pos = state.position
        daily = state.daily
        paper_block = {
            "equity": round(state.equity, 2),
            "balance": round(state.balance, 2),
            "pnl_today": round(daily.net_pnl, 2),
            "position": {
                "dir": "LONG" if pos.direction == 1 else "SHORT" if pos.direction == -1 else "FLAT",
                "entry": round(pos.entry_price, 2),
                "size": pos.volume,
                "unrealized": round(pos.unrealized_pnl, 2),
            },
            "daily": {
                "trades": daily.total_trades,
                "win": daily.winning_trades,
                "loss": daily.losing_trades,
                "pnl": round(daily.net_pnl, 2),
                "drawdown_pct": round(daily.max_drawdown_pct, 2),
            },
            "risk": {
                "circuit_breaker": state.is_circuit_breaker,
                "consecutive_loss": daily.consecutive_losses,
            },
        }
    except Exception as e:
        paper_block = {
            "equity": 0.0, "balance": 0.0, "pnl_today": 0.0,
            "position": {"dir": "FLAT", "entry": 0.0, "size": 0.0, "unrealized": 0.0},
            "daily": {"trades": 0, "win": 0, "loss": 0, "pnl": 0.0, "drawdown_pct": 0.0},
            "risk": {"circuit_breaker": False, "consecutive_loss": 0},
            "warning": f"state not initialized: {type(e).__name__}",
        }

    # When live is running, `source` is "live" and the top-level equity/
    # balance/pnL/position/risk fields are mirrored from the broker so
    # existing frontend code (which reads these flat fields) keeps working
    # without any changes. The paper fields stay under `paper` for code
    # that wants to see them.
    # audit 2026-06-09: in live mode, daily stats and pnl_today are
    # sourced from `_live_state.session_*` (the loop's session accumulators)
    # rather than the paper's `state.daily` — those represent a different
    # backtest and would be misleading. session_pnl starts at 0 and grows
    # as the loop records closed trades.
    if live_source == "live" and live_block is not None:
        from backend.services.live_service import _live_state
        a = live_block["account"]
        p = live_block["position"] or {"dir": "FLAT", "entry": 0.0, "size": 0.0, "unrealized": 0.0}
        loop_st = _live_loop_status()
        # Prefer session_pnl (closed trade accumulator). Fall back to
        # the open position's unrealized P&L only if the loop has been
        # running but hasn't recorded a closed trade yet (i.e. session
        # is still warming up — show a sensible number).
        session_pnl = float(_live_state.get("session_pnl", 0.0))
        live_daily = {
            "trades": int(_live_state.get("session_trades", 0)),
            "win": int(_live_state.get("session_winning", 0)),
            "loss": int(_live_state.get("session_losing", 0)),
            "pnl": round(session_pnl, 2),
            "drawdown_pct": round(float(_live_state.get("session_max_drawdown_pct", 0.0)), 2),
        }
        # ⚠️ cTrader bridge 可能返回 equity/margin=None (需另查 ProtoOAGetPositionUnrealizedPnLReq)
        # 用 or 0.0 兜底防 float(None) → TypeError 炸 WS 连接
        return {
            "source": "live",
            "broker": live_block["broker"],
            "equity": float(a.get("equity") or 0.0),
            "balance": float(a.get("balance") or 0.0),
            "pnl_today": round(session_pnl, 2),
            "position": p,
            "daily": live_daily,
            "risk": paper_block["risk"],
            "margin": float(a.get("margin") or 0.0),
            "margin_free": float(a.get("margin_free") or 0.0),
            "leverage": a.get("leverage"),
            "currency": a.get("currency"),
            "n_positions": live_block["n_positions"],
            "current_price": _live_get_latest_price(),  # audit 2026-06-08
            "active_strategy": {  # audit 2026-06-08
                "id": loop_st.get("strategy_name"),
                "mode": "single",
                "source": "live",
            },
            "live": live_block,
            "paper": paper_block,
            "server_time": datetime.now(timezone.utc).isoformat(),
        }

    return {
        "source": "paper",
        "broker": None,
        "equity": paper_block["equity"],
        "balance": paper_block["balance"],
        "pnl_today": paper_block["pnl_today"],
        "position": paper_block["position"],
        "daily": paper_block["daily"],
        "risk": paper_block["risk"],
        "active_strategy": {  # audit 2026-06-08
            "id": get_paper_service().status().strategy_id,
            "mode": "single",
            "source": "paper",
        },
        "live": None,
        "paper": paper_block,
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


@router.websocket("/ws/state")
async def ws_state(ws: WebSocket) -> None:
    """Push 1s state snapshot."""
    mgr = get_connection_manager()
    channel = "state"
    await mgr.connect(ws, channel)
    try:
        # Push initial snapshot immediately
        await ws.send_text(json.dumps(_read_state_snapshot(), default=str))
        # Then tick every 1s while client is connected
        while True:
            await asyncio.sleep(1.0)
            try:
                await ws.send_text(json.dumps(_read_state_snapshot(), default=str))
            except Exception:
                break
    except WebSocketDisconnect:
        pass
    finally:
        await mgr.disconnect(ws, channel)
