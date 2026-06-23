"""WebSocket routes: /ws/state, /ws/alerts, /ws/jobs/:id, /ws/logs.

audit v9: 前端永远显示 cTrader 数据。live 在跑→实时更新; 停止→数据冻结。
不再显示 paper 模拟盘数据。
"""
import asyncio
import json
from datetime import datetime, timezone

from pathlib import Path
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend.core.logging import setup_logging
from backend.services.live_service import (
    loop_status as _live_loop_status,
    get_latest_price as _live_get_latest_price,
)
from backend.ws.manager import get_connection_manager

router = APIRouter()
setup_logging()


def _position_to_dict(p: object) -> dict:
    """统一 PositionInfo dataclass 或 dict → 字典 (含所有字段, 兼容旧 key 名)"""
    if isinstance(p, dict):
        return p
    # PositionInfo dataclass — 同时暴露 dataclass 字段 + 前端兼容名
    _volume = getattr(p, "volume", 0.0)
    return {
        "type": "buy" if p.direction == 1 else "sell",
        "price_open": p.entry_price,
        "volume": _volume,
        "api_volume": _volume,
        "profit": p.pnl,
        # 原始字段 (兼容代码中 p.get("position_id") / p.get("open_price") 等)
        "position_id": p.position_id,
        "ticket": p.position_id,
        "symbol_id": getattr(p, "symbol_id", 0),
        "direction": p.direction,
        "entry_price": p.entry_price,
        "open_price": p.entry_price,
        "current_price": p.current_price,
        "sl": p.sl,
        "tp": p.tp,
        'pnl': p.pnl,
        'commission': p.commission,
        'swap': p.swap,
        'symbol': p.symbol,
        'open_time': p.open_timestamp,
    }


def _read_closed_loop_status() -> dict:
    """Return compact closed-loop health summary for all pipeline nodes.

    Reads from _live_state + attribution snapshot. No blocking I/O on hot path
    except a small cached file read (1s interval is fine for JSON < 100KB).
    """
    from backend.services.live_service import _live_state

    # ── Attribution ──
    attr_status = "no_data"
    attr_trades = 0
    try:
        from backend.core.db import get_state_conn
        conn = get_state_conn()
        try:
            rows = conn.execute(
                "SELECT data_json FROM attribution_snapshot"
            ).fetchall()
            for r in rows:
                d = json.loads(r["data_json"]) if isinstance(r["data_json"], str) else r["data_json"]
                attr_trades += d.get("n_trades", 0)
            if rows:
                attr_status = "active" if attr_trades > 10 else "cold_start"
        finally:
            conn.close()
    except Exception:
        attr_status = "error"

    # ── Pipeline ──
    strategy = _live_state.get("loop_strategy")
    loop_running = _live_state.get("loop_running")
    pipeline_status = "inactive"
    if strategy and loop_running is True:
        pipeline_status = "running"
    if _live_state.get("circuit_breaker"):
        pipeline_status = "circuit_breaker"

    # ── Sync ──
    sync_age = _live_state.get("sync_age_seconds")
    sync_status = "ok"
    if sync_age is not None:
        if sync_age > 300:
            sync_status = "stale"
        if sync_age > 3600:
            sync_status = "stale_critical"

    # 管道停了 → data_sync / risk 也显示待机
    ds_status = sync_status if pipeline_status == "running" else "inactive"
    risk_status = ("active" if _live_state.get("circuit_breaker") is not None else "ok") if pipeline_status == "running" else "inactive"

    return {
        "nodes": {
            "data_sync": {"status": ds_status, "age_seconds": sync_age},
            "factor_engine": {"status": "running" if pipeline_status == "running" else "inactive"},
            "signal_normalizer": {"status": "running" if pipeline_status == "running" else "inactive"},
            "portfolio_compositor": {"status": "running" if pipeline_status == "running" else "inactive"},
            "execution_gate": {"status": pipeline_status},
            "execution": {
                "status": "running" if (_live_state.get("loop_started_at") and loop_running is True) else "inactive",
                "n_positions": _live_state.get("n_positions", 0),
            },
            "attribution": {"status": attr_status, "n_trades_attributed": attr_trades},
            "adaptive_weight": {"status": "initialized" if _live_state.get("awe_adapted_at") else "waiting"},
            "risk": {
                "status": risk_status,
                "circuit_breaker": bool(_live_state.get("circuit_breaker", False)),
            },
        },
        "pipeline_active": pipeline_status == "running",
        "all_green": pipeline_status == "running",
    }


def _read_state_snapshot() -> dict:
    """返回 cTrader 实时快照。live 在跑→实时; 停止→冻结最后数据。

    只有从未连接过 cTrader 时才返回全零占位。
    """
    from backend.services.live_service import _live_state

    loop = _live_loop_status()
    live_running = loop.get("running", False)

    acct = _live_state.get("account") or {}
    positions = _live_state.get("positions") or []
    live_broker = _live_state.get("broker") or loop.get("broker") or "ctrader"

    # 从未连接过 cTrader → 全零占位
    if not acct and not _live_state.get("loop_started_at"):
        return {
            "source": "none",
            "broker": None,
            "equity": 0.0, "balance": 0.0, "pnl_today": 0.0,
            "position": {"dir": "FLAT", "entry": 0.0, "size": 0.0, "unrealized": 0.0},
            "daily": {"trades": 0, "win": 0, "loss": 0, "pnl": 0.0, "drawdown_pct": 0.0},
            "risk": {"circuit_breaker": False, "consecutive_loss": 0},
            "margin": 0.0, "margin_free": 0.0,
            "leverage": None, "currency": None, "n_positions": 0,
            "current_price": None,
            "active_strategy": {"id": None, "mode": "single", "source": "none"},
            "closed_loop": _read_closed_loop_status(),
            "server_time": datetime.now(timezone.utc).isoformat(),
        }

    # 有数据 → 显示 cTrader 状态 (live 或 frozen)
    session_pnl = float(_live_state.get("session_pnl", 0.0))
    session_trades = int(_live_state.get("session_trades", 0))

    # position 数据 — 兼容 dict 或 PositionInfo dataclass
    pos_data = {"dir": "FLAT", "entry": 0.0, "size": 0.0, "unrealized": 0.0}
    try:
        if isinstance(positions, dict) and positions.get("ok"):
            pos_list = positions.get("positions") or []
            if pos_list:
                p = _position_to_dict(pos_list[0])
                pos_data = {
                    "dir": "LONG" if p.get("type") == "buy" else "SHORT",
                    "entry": p.get("price_open", 0.0),
                    "size": p.get("api_volume", p.get("volume", 0.0)),
                    "unrealized": p.get("profit") or 0.0,
                }
        elif isinstance(positions, list) and positions:
            p = _position_to_dict(positions[0])
            pos_data = {
                "dir": "LONG" if p.get("type") == "buy" else "SHORT",
                "entry": p.get("price_open", 0.0),
                "size": p.get("api_volume", p.get("volume", 0.0)),
                "unrealized": p.get("profit") or 0.0,
            }
    except Exception as exc:
        import logging as _lg
        _lg.getLogger("ws.debug").warning(
            "snapshot pos_data error: %s | positions type=%s len=%s",
            exc, type(positions).__name__,
            len(positions) if isinstance(positions, (list, dict)) else "?",
        )

    n_positions = len(positions.get("positions") or []) if isinstance(positions, dict) else len(positions) if isinstance(positions, list) else 0

    # ── 所有持仓列表 (供前端持仓卡片, 每 WS 推送更新) ──
    positions_list: list[dict] = []
    spot_price = _live_get_latest_price()
    try:
        raw_list = positions.get("positions") if isinstance(positions, dict) else positions if isinstance(positions, list) else []
        if raw_list:
            for p_raw in raw_list:
                p_dict = _position_to_dict(p_raw)
                entry = p_dict.get("price_open", 0.0) or 0.0
                vol = p_dict.get("api_volume", p_dict.get("volume", 0.0)) or 0.0
                direction = p_dict.get("direction") or 0
                if direction == 0:
                    direction = 1 if p_dict.get("type") == "buy" else -1
                # 浮动盈亏: 每 WS 推送用实时 spot 计算 (桥接层 PnL 受 15s 缓存限制)
                pnl_val = (spot_price - entry) * direction * vol * 100.0 if (spot_price and entry) else 0.0
                positions_list.append({
                    "symbol": p_dict.get("symbol") or "",
                    "type": p_dict.get("type") or "buy",
                    "direction": direction,
                    "volume": vol,
                    "price_open": entry,
                    "current_price": spot_price or 0.0,
                    "pnl": round(pnl_val, 2),
                    "sl": p_dict.get("sl") or 0.0,
                    "tp": p_dict.get("tp") or 0.0,
                    "position_id": p_dict.get("position_id") or p_dict.get("ticket") or 0,
                    "open_time": p_dict.get("open_time") or 0,
                })
    except Exception:
        pass

    source = "live" if live_running else "frozen"

    return {
        "source": source,
        "broker": live_broker,
        "equity": float(acct.get("equity") or 0.0),
        "balance": float(acct.get("balance") or 0.0),
        "pnl_today": round(session_pnl, 2),
        "position": pos_data,
        "daily": {
            "trades": session_trades,
            "win": int(_live_state.get("session_winning", 0)),
            "loss": int(_live_state.get("session_losing", 0)),
            "pnl": round(session_pnl, 2),
            "drawdown_pct": round(float(_live_state.get("session_max_drawdown_pct", 0.0)), 2),
        },
        "risk": {
            "circuit_breaker": bool(_live_state.get("circuit_breaker", False)),
            "consecutive_loss": int(_live_state.get("session_losing", 0)),
            # 风险模块自动计算结果 (VaR / Kelly / Stress / Concentration)
            **_live_state.get("risk", {}),
        },
        "margin": float(acct.get("margin") or 0.0),
        "margin_free": float(acct.get("margin_free") or 0.0),
        "leverage": acct.get("leverage"),
        "currency": acct.get("currency"),
        "n_positions": n_positions,
        "positions_list": positions_list,
        "current_price": _live_get_latest_price(),
        "active_strategy": {
            "id": _live_state.get("loop_strategy") or loop.get("strategy_name"),
            "mode": "single",
            "source": "live" if live_running else "stopped",
        },
        "closed_loop": _read_closed_loop_status(),
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


@router.websocket("/ws/state")
async def ws_state(ws: WebSocket) -> None:
    """Push 1s state snapshot. Auth via ?token= query param or WS subprotocol."""
    import jwt as _jwt
    import logging
    _ws_log = logging.getLogger("ws.auth")
    from backend.core.auth import JWT_SECRET, JWT_ALGORITHM
    # 手动从 query string 取 token (FastAPI WebSocket 不自动解析 query params)
    # 也用 subprotocol 做备选 (避免 token 出现在 proxy log)
    token = ""
    try:
        # Starlette WebSocket 提供 query_params
        params = getattr(ws, 'query_params', None)
        if params is not None:
            token = params.get("token", "")
        # 备选: subprotocol
        if not token and hasattr(ws, 'subprotocols') and ws.subprotocols:
            token = ws.subprotocols[0]
    except Exception:
        pass
    if not token:
        _ws_log.warning("WS /ws/state rejected: missing token")
        await ws.accept()
        await ws.close(code=4001, reason="missing token")
        return
    try:
        _jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except _jwt.ExpiredSignatureError:
        _ws_log.warning("WS /ws/state rejected: token expired")
        await ws.accept()
        await ws.close(code=4001, reason="token expired")
        return
    except Exception as e:
        _ws_log.warning("WS /ws/state rejected: invalid token (%s)", e)
        await ws.accept()
        await ws.close(code=4001, reason="invalid token")
        return
    _ws_log.info("WS /ws/state connected OK")
    mgr = get_connection_manager()
    channel = "state"
    await mgr.connect(ws, channel, subprotocol=token if hasattr(ws, 'subprotocols') else None)
    try:
        await ws.send_text(json.dumps(_read_state_snapshot(), default=str))
        while True:
            await asyncio.sleep(1.0)
            # Windows ProactorEventLoop 可能抛 ConnectionResetError (WinError 10054)
            # RuntimeError 来自 starlette: 客户端已发送 close 后不能再 send
            for _retry in range(5):
                try:
                    await ws.send_text(json.dumps(_read_state_snapshot(), default=str))
                    break
                except (WebSocketDisconnect, ConnectionError, RuntimeError) as e:
                    _ws_log.debug("WS /ws/state send failed (attempt %d): %s", _retry + 1, e)
                    await asyncio.sleep(0.5)
            else:
                _ws_log.debug("WS /ws/state send failed after 5 attempts, closing")
                break
    except (WebSocketDisconnect, ConnectionError, RuntimeError):
        pass
    finally:
        await mgr.disconnect(ws, channel)
