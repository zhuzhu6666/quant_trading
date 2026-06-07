"""WebSocket routes: /ws/state, /ws/alerts, /ws/jobs/:id, /ws/logs.

In Phase 1 only /ws/state is implemented; it broadcasts a 1s snapshot
read from core.state (or, if paper isn't running, a placeholder zero-state).
"""
import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend.core.logging import setup_logging
from core.state import state
from backend.ws.manager import get_connection_manager

router = APIRouter()
setup_logging()


def _read_state_snapshot() -> dict:
    """Read current state for snapshot. Falls back to zeros if no paper running."""
    try:
        pos = state.position
        daily = state.daily
        snapshot = {
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
            "server_time": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        # StateContainer may not be fully initialized in Phase 1
        snapshot = {
            "equity": 0.0, "balance": 0.0, "pnl_today": 0.0,
            "position": {"dir": "FLAT", "entry": 0.0, "size": 0.0, "unrealized": 0.0},
            "daily": {"trades": 0, "win": 0, "loss": 0, "pnl": 0.0, "drawdown_pct": 0.0},
            "risk": {"circuit_breaker": False, "consecutive_loss": 0},
            "server_time": datetime.now(timezone.utc).isoformat(),
            "warning": f"state not initialized: {type(e).__name__}",
        }
    return snapshot


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
