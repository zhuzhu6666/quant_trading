"""
Dashboard — 实时Web监控面板

基于 FastAPI + WebSocket 的轻量监控：
- 实时权益曲线
- 当前持仓
- 当日统计
- 因子IC状态

用法:
    uvicorn monitor.dashboard:app --port 8050
"""

import json
import logging
import time
from datetime import datetime

from core.state import state
from core.event_bus import bus, EventType

logger = logging.getLogger(__name__)

try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.responses import HTMLResponse
    HAS_WEB = True
except ImportError:
    HAS_WEB = False
    logger.info("FastAPI not installed — web dashboard disabled")


if HAS_WEB:
    app = FastAPI(title="Quant Trading Dashboard", version="1.0")
    _ws_clients: set[WebSocket] = set()

    DASHBOARD_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Quant Trading Dashboard</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0d1117;color:#c9d1d9;font-family:monospace;padding:20px}
h1{color:#58a6ff;margin-bottom:10px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px}
.card h2{font-size:14px;color:#8b949e;margin-bottom:8px}
.val{font-size:28px;font-weight:bold}
.val.green{color:#3fb950}.val.red{color:#f85149}.val.amber{color:#d2991d}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #21262d}
th{color:#8b949e;font-weight:normal}
</style></head><body>
<h1>⚡ Quant Trading Dashboard</h1>
<p style="color:#8b949e;margin-bottom:16px" id="status">connecting...</p>
<div class="grid" id="grid"></div>
<script>
var ws = new WebSocket('ws://' + location.host + '/ws');
ws.onopen = ()=>document.getElementById('status').textContent='connected';
ws.onclose = ()=>document.getElementById('status').textContent='disconnected';
ws.onmessage = (e)=>{
    var d = JSON.parse(e.data);
    var html = '';
    for(var key in d){
        var v = d[key], color = '';
        if(typeof v==='number'){
            color = v>0?'green':v<0?'red':'';
        }
        html += '<div class="card"><h2>'+key+'</h2>';
        if(typeof v==='object'){
            html += '<table>';
            for(var k in v) html += '<tr><th>'+k+'</th><td>'+JSON.stringify(v[k])+'</td></tr>';
            html += '</table>';
        }else{
            html += '<div class="val '+color+'">'+(typeof v==='number'?v.toFixed(2):v)+'</div>';
        }
        html += '</div>';
    }
    document.getElementById('grid').innerHTML = html;
};
</script></body></html>"""

    @app.get("/")
    async def root():
        return HTMLResponse(DASHBOARD_HTML)

    # P11 (audit 2026-06-04 ARCH-7): 起 _broadcast_loop 后台任务
    # 之前 _broadcast() 定义了但从未启动, WebSocket 收不到任何数据
    @app.on_event("startup")
    async def _start_broadcast_loop():
        import asyncio
        from config import load_config, cfg_get
        cfg = load_config()
        interval = cfg_get(cfg, "monitor", "metrics_interval_seconds", default=1.0)
        async def loop():
            while True:
                try:
                    await _broadcast()
                except Exception as e:
                    logger.warning(f"broadcast failed: {e}")
                await asyncio.sleep(interval)
        asyncio.create_task(loop())

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        global _ws_clients  # P11: 避免闭包 UnboundLocalError
        await ws.accept()
        _ws_clients.add(ws)
        try:
            while True:
                await ws.receive_text()  # keep-alive
        except WebSocketDisconnect:
            _ws_clients.discard(ws)

    async def _broadcast():
        """推送当前状态到所有WebSocket客户端"""
        global _ws_clients  # P11: 同上
        if not _ws_clients:
            return

        pos = state.position
        daily = state.daily
        # ARCH-11 (audit 2026-06-04): pnl 用 state.daily.net_pnl, 不再硬编码 - 100
        # (原来假设 initial_balance=100, 但 main.py 用 500.0, 算出来差 $400)
        payload = {
            "equity": round(state.equity, 2),
            "balance": round(state.balance, 2),
            "pnl": round(daily.net_pnl, 2),
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

        dead = set()
        for ws in _ws_clients:
            try:
                import asyncio
                asyncio.create_task(ws.send_text(json.dumps(payload)))
            except Exception:
                dead.add(ws)
        _ws_clients -= dead
