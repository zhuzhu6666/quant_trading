"""WebSocket routes: /ws/state, /ws/alerts, /ws/jobs/:id, /ws/logs.

audit v9: 前端永远显示 cTrader 数据。live 在跑→实时更新; 停止→数据冻结。
不再显示 paper 模拟盘数据。
"""
import asyncio
import contextlib
import json
from datetime import datetime, timezone

from pathlib import Path
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend.core.logging import setup_logging
from backend.services.api_fact_views import state_snapshot_fact_payload
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
        'current_price_state': getattr(p, 'current_price_state', ''),
        'current_price_source': getattr(p, 'current_price_source', ''),
        'current_price_observed_at': getattr(p, 'current_price_observed_at', 0.0),
        'current_price_reason_code': getattr(p, 'current_price_reason_code', ''),
        'pnl_state': getattr(p, 'pnl_state', ''),
        'pnl_source': getattr(p, 'pnl_source', ''),
        'pnl_observed_at': getattr(p, 'pnl_observed_at', 0.0),
        'pnl_reason_code': getattr(p, 'pnl_reason_code', ''),
        'commission': p.commission,
        'swap': p.swap,
        'symbol': p.symbol,
        'open_time': p.open_timestamp,
    }


def _read_closed_loop_status(
    state: dict | None = None,
    attribution: object | None = None,
) -> dict:
    """Return compact closed-loop health summary for all pipeline nodes.

    Reads the locked live projection and the in-process attribution owner when
    the live loop is running. A stopped process falls back to one read-only
    state-store query for the durable attribution summary; this function never
    calls the broker.
    """
    from backend.services.live_service import _live_state_snapshot

    live_state = state if state is not None else _live_state_snapshot()

    # ── Attribution ──
    attr_status = "no_data"
    attr_trades = 0
    if attribution is not None:
        try:
            stats = attribution.get_all_factor_stats()
            attr_trades = sum(int(getattr(item, "n_trades", 0) or 0) for item in stats.values())
            attr_status = "active" if attr_trades > 10 else "cold_start"
        except Exception:
            attr_status = "error"
    else:
        try:
            from backend.core.db import get_state_pg_conn

            conn = get_state_pg_conn(read_only=True)
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
    strategy = live_state.get("loop_strategy")
    loop_running = live_state.get("loop_running")
    pipeline_status = "inactive"
    if strategy and loop_running is True:
        pipeline_status = "running"
    if live_state.get("circuit_breaker"):
        pipeline_status = "circuit_breaker"

    # ── Sync ──
    sync_age = live_state.get("sync_age_seconds")
    sync_status = "ok"
    if sync_age is not None:
        if sync_age > 300:
            sync_status = "stale"
        if sync_age > 3600:
            sync_status = "stale_critical"

    # 管道停了 → data_sync / risk 也显示待机
    ds_status = sync_status if pipeline_status == "running" else "inactive"
    risk_status = ("active" if live_state.get("circuit_breaker") is not None else "ok") if pipeline_status == "running" else "inactive"

    return {
        "nodes": {
            "data_sync": {"status": ds_status, "age_seconds": sync_age},
            "factor_engine": {"status": "running" if pipeline_status == "running" else "inactive"},
            "signal_normalizer": {"status": "running" if pipeline_status == "running" else "inactive"},
            "portfolio_compositor": {"status": "running" if pipeline_status == "running" else "inactive"},
            "execution_gate": {"status": pipeline_status},
            "execution": {
                "status": "running" if (live_state.get("loop_started_at") and loop_running is True) else "inactive",
                "n_positions": live_state.get("n_positions", 0),
            },
            "attribution": {"status": attr_status, "n_trades_attributed": attr_trades},
            "adaptive_weight": {"status": "initialized" if live_state.get("awe_adapted_at") else "waiting"},
            "risk": {
                "status": risk_status,
                "circuit_breaker": bool(live_state.get("circuit_breaker", False)),
            },
        },
        "pipeline_active": pipeline_status == "running",
        "all_green": pipeline_status == "running",
    }


def _read_state_snapshot() -> dict:
    """返回 cTrader 实时快照。live 在跑→实时; 停止→冻结最后数据。

    只有从未连接过 cTrader 时才返回全零占位。
    """
    from backend.services.live_service import (
        _factor_pipeline,
        _live_state_snapshot,
        _should_send_orders,
    )

    live_state = _live_state_snapshot()
    loop = _live_loop_status()
    live_running = loop.get("running", False)

    acct = live_state.get("account") or {}
    positions = live_state.get("positions") or []
    live_broker = live_state.get("broker") or loop.get("broker") or "ctrader"
    account_updated_at = live_state.get("account_updated_at")
    positions_updated_at = live_state.get("positions_updated_at")
    positions_component_facts = live_state.get("positions_component_facts") or {}
    diagnostic_ts = (live_state.get("_diag") or {}).get("ts")
    spot_quote = live_state.get("spot_quote")
    market_session = live_state.get("market_session") or {}

    # The browser live console consumes this projection through /ws/state only.
    # Keep it read-only: factor calculation, Safety and order admission remain
    # owned by the live loop and its existing authorities.
    pipeline = _factor_pipeline or {}
    engine = pipeline.get("engine")
    attribution = pipeline.get("attribution")
    awe = pipeline.get("awe")
    try:
        send_orders = bool(_should_send_orders(live_broker, log_blocking=False)) if live_running else False
    except Exception:
        send_orders = False
    accepting_new_risk = bool(loop.get("accepting_new_risk", live_state.get("accepting_new_risk", False)))
    safety_blockers = list(dict.fromkeys(
        [str(item).strip() for item in (loop.get("blockers") or []) if str(item).strip()]
        + [str(item).strip() for item in (live_state.get("new_risk_reconcile_blockers") or []) if str(item).strip()]
    ))
    strategy_status = {
        "running": live_running,
        "broker": live_broker,
        "strategy": "factor_pipeline_v4",
        "mode": "LIVE" if live_running else "STOPPED",
        "execution_mode": "LIVE" if live_running else "STOPPED",
        "send_orders": send_orders,
        "dry_run": not send_orders,
        "accepting_new_risk": accepting_new_risk,
        "safety_blockers": safety_blockers,
        "new_risk_reconcile_blockers": list(live_state.get("new_risk_reconcile_blockers") or []),
        "safety_heartbeat_at": loop.get("safety_heartbeat_at"),
        "reason": str((live_state.get("last_composite") or {}).get("gate_reason") or ""),
        "v4_status": {
            "pipeline_active": bool(pipeline),
            "engine_warm": bool(engine and getattr(engine, "is_warm", False)),
            "buffer_size": int(getattr(engine, "buffer_size", 0) or 0) if engine else 0,
            "n_attribution_trades": sum(
                int(getattr(item, "n_trades", 0) or 0)
                for item in attribution.get_all_factor_stats().values()
            ) if attribution else 0,
            "awe_conviction": round(float(awe.composite_conviction()), 3) if awe else 0.0,
        },
        "factor_votes": live_state.get("last_factor_votes") or {},
        "last_composite": live_state.get("last_composite") or {},
        "_diag": live_state.get("_diag") or {},
    }

    # 从未连接过 cTrader → 全零占位
    if not acct and not live_state.get("loop_started_at"):
        payload = {
            "source": "none",
            "broker": None,
            "equity": 0.0, "balance": 0.0, "pnl_today": 0.0,
            "position": {"dir": "FLAT", "entry": 0.0, "size": 0.0, "unrealized": 0.0},
            "daily": {"trades": 0, "win": 0, "loss": 0, "pnl": 0.0, "drawdown_pct": 0.0},
            "risk": {"circuit_breaker": False, "consecutive_loss": 0},
            "risk_health": {},
            "margin": 0.0, "margin_free": 0.0,
            "leverage": None, "currency": None, "n_positions": 0,
            "current_price": None,
            "active_strategy": {"id": None, "mode": "single", "source": "none"},
            "strategy_status": {},
            "market_session": {},
            "closed_loop": _read_closed_loop_status(live_state, attribution),
            "server_time": datetime.now(timezone.utc).isoformat(),
        }
        return state_snapshot_fact_payload(
            payload,
            account=acct,
            account_updated_at=account_updated_at,
            positions_updated_at=positions_updated_at,
            diagnostic_ts=diagnostic_ts,
            loop_status=loop,
            spot_quote=spot_quote,
            positions_component_facts=positions_component_facts,
        )

    # 有数据 → 显示 cTrader 状态 (live 或 frozen)
    session_pnl = float(live_state.get("session_pnl", 0.0))
    session_trades = int(live_state.get("session_trades", 0))

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

    # ── 所有持仓列表 (供前端持仓卡片) ──
    positions_list: list[dict] = []
    try:
        raw_list = positions.get("positions") if isinstance(positions, dict) else positions if isinstance(positions, list) else []
        if raw_list:
            for p_raw in raw_list:
                p_dict = _position_to_dict(p_raw)
                positions_list.append({
                    "symbol": p_dict.get("symbol") or "",
                    "type": p_dict.get("type") or "buy",
                    "direction": p_dict.get("direction") or 0,
                    "volume": p_dict.get("api_volume", p_dict.get("volume", 0.0)),
                    "price_open": p_dict.get("price_open", 0.0),
                    "current_price": p_dict.get("current_price", 0.0),
                    "pnl": p_dict.get("pnl") or p_dict.get("profit") or 0.0,
                    "sl": p_dict.get("sl") or 0.0,
                    "tp": p_dict.get("tp") or 0.0,
                    "position_id": p_dict.get("position_id") or p_dict.get("ticket") or 0,
                    "open_time": p_dict.get("open_time") or 0,
                })
    except Exception:
        pass

    source = "live" if live_running else "frozen"

    current_price = _live_get_latest_price()
    payload = {
        "source": source,
        "broker": live_broker,
        "equity": float(acct.get("equity") or 0.0),
        "balance": float(acct.get("balance") or 0.0),
        "pnl_today": round(session_pnl, 2),
        "position": pos_data,
        "daily": {
            "trades": session_trades,
            "win": int(live_state.get("session_winning", 0)),
            "loss": int(live_state.get("session_losing", 0)),
            "pnl": round(session_pnl, 2),
            "drawdown_pct": round(float(live_state.get("session_max_drawdown_pct", 0.0)), 2),
        },
        "risk": {
            "circuit_breaker": bool(live_state.get("circuit_breaker", False)),
            "consecutive_loss": int(live_state.get("session_consecutive_loss", 0)),
            # 风险模块自动计算结果 (VaR / Kelly / Stress / Concentration)
            **live_state.get("risk", {}),
        },
        "risk_health": {
            "trading_blocked": bool(live_state.get("circuit_breaker", False)),
            "circuit_breaker": bool(live_state.get("circuit_breaker", False)),
            "reason": str(live_state.get("circuit_reason") or ""),
        },
        "margin": float(acct.get("margin") or 0.0),
        "margin_free": float(acct.get("margin_free") or 0.0),
        "leverage": acct.get("leverage"),
        "currency": acct.get("currency"),
        "n_positions": n_positions,
        "positions_list": positions_list,
        "current_price": current_price,
        "spot_quote": spot_quote,
        "accepting_new_risk": accepting_new_risk,
        "safety_blockers": safety_blockers,
        "new_risk_reconcile_blockers": list(live_state.get("new_risk_reconcile_blockers") or []),
        "active_strategy": {
            "id": live_state.get("loop_strategy") or loop.get("strategy_name"),
            "mode": "single",
            "source": "live" if live_running else "stopped",
        },
        "strategy_status": strategy_status,
        "market_session": market_session,
        "closed_loop": _read_closed_loop_status(live_state, attribution),
        "server_time": datetime.now(timezone.utc).isoformat(),
    }
    return state_snapshot_fact_payload(
        payload,
        account=acct,
        account_updated_at=account_updated_at,
        positions_updated_at=positions_updated_at,
        diagnostic_ts=diagnostic_ts,
        loop_status=loop,
        spot_quote=spot_quote,
        positions_component_facts=positions_component_facts,
    )


@router.websocket("/ws/state")
async def ws_state(ws: WebSocket) -> None:
    """Push state snapshots when the canonical live state changes.

    A bearer token in the WebSocket subprotocol remains supported. URL JWTs
    require the explicit migration switch ``QUANT_AUTH_ALLOW_URL_JWT``.
    """
    import logging
    import os
    _ws_log = logging.getLogger("ws.auth")
    from backend.core.auth import consume_ws_ticket, decode_access_token

    token = ""
    ticket = ""
    accepted_subprotocol = None
    try:
        params = getattr(ws, 'query_params', None)
        if params is not None:
            ticket = params.get("ticket", "")
        protocol_header = ws.headers.get("sec-websocket-protocol", "")
        if protocol_header:
            token = protocol_header.split(",", 1)[0].strip()
            accepted_subprotocol = token
        allow_url_jwt = (os.environ.get("QUANT_AUTH_ALLOW_URL_JWT") or "").strip().lower() in {
            "1", "true", "yes", "on",
        }
        if not ticket and not token and allow_url_jwt and params is not None:
            token = params.get("token", "")
    except Exception:
        pass

    if not ticket and not token:
        _ws_log.warning("WS /ws/state rejected: missing ticket")
        await ws.accept()
        await ws.close(code=4001, reason="missing auth ticket")
        return
    try:
        if ticket:
            consume_ws_ticket(ticket)
        else:
            decode_access_token(token)
    except Exception as e:
        _ws_log.warning("WS /ws/state rejected: invalid auth (%s)", e)
        await ws.accept()
        await ws.close(code=4001, reason="invalid auth")
        return
    _ws_log.info("WS /ws/state connected OK")
    mgr = get_connection_manager()
    channel = "state"
    await mgr.connect(ws, channel, subprotocol=accepted_subprotocol)
    disconnect_task: asyncio.Task | None = None
    change_task: asyncio.Task | None = None
    try:
        generation = mgr.current_generation(channel)
        await ws.send_text(json.dumps(_read_state_snapshot(), default=str))
        # Keep one receive waiter so a client that closes while the state is
        # quiet is removed without requiring a fake heartbeat/poll message.
        disconnect_task = asyncio.create_task(ws.receive())
        while True:
            change_task = asyncio.create_task(
                mgr.wait_for_change(channel, generation)
            )
            done, _ = await asyncio.wait(
                {change_task, disconnect_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if disconnect_task in done:
                message = disconnect_task.result()
                if message.get("type") == "websocket.disconnect":
                    break
                disconnect_task = asyncio.create_task(ws.receive())
            if change_task in done:
                generation = change_task.result()
                # State writers are coalesced by the manager's generation
                # event.  Serialize one complete snapshot per change batch;
                # there is no per-client timer and no broker/API read here.
                await ws.send_text(json.dumps(_read_state_snapshot(), default=str))
            else:
                change_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await change_task
    except (WebSocketDisconnect, ConnectionError, RuntimeError):
        pass
    finally:
        for task in (change_task, disconnect_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await mgr.disconnect(ws, channel)
