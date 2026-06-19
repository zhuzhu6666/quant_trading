"""FastAPI app factory + lifespan + CORS + router registration.

Dev mode (start.bat): Vite dev on :5173 proxies /api/* to uvicorn on :8000.
Prod mode (start-prod.bat): static Vite frontend is served by uvicorn on :8000 directly.

CORS: in prod, allow same-origin (the static dir is mounted at /) plus localhost:5173
for dev convenience.
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from backend.api import ALL_ROUTERS
from backend.core.logging import setup_logging
from backend.core.paths import BACKEND_DIR
from backend.jobs import get_job_manager
from backend.ws.endpoints import router as ws_router
from monitor.metrics import Metrics, install_into_runtime_state
from monitor.structured_log import setup_structured_logging


def _init_observability() -> None:
    """Setup structured JSON logging + wire Metrics into RuntimeState.

    Both wrapped in try/except so a failure doesn't crash lifespan startup.
    """
    from loguru import logger as _lg
    try:
        setup_structured_logging(logging.INFO)
        _lg.info("[lifespan] structured logging initialized")
    except Exception as e:
        _lg.warning(f"[lifespan] setup_structured_logging failed (non-fatal): {e}")
    try:
        install_into_runtime_state()
        _lg.info("[lifespan] Metrics installed into RuntimeState")
    except Exception as e:
        _lg.warning(f"[lifespan] Metrics.install_into_runtime_state failed (non-fatal): {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    from loguru import logger as _lg
    setup_logging()
    _init_observability()
    # Bind the running loop so JobManager.submit() works when called from
    # a threadpool thread (e.g. FastAPI sync handler) that has no loop of its own.
    get_job_manager().bind_loop(asyncio.get_running_loop())
    # 恢复 shadow/discovered 因子 (audit 2026-06-08: 之前没调, 启动后
    # /factors 评估永远只有 22 个 builtin, discovered 因子全看不见).
    # 用 try/except 兜住 — restore 失败不应阻塞 backend 启动.
    try:
        from alpha.persistent_registry import restore_from_log
        restored = restore_from_log(verbose=False)
        if restored:
            _lg.info(f"[lifespan] restored {restored} shadow/discovered factors from lifecycle log")
    except Exception as e:
        _lg.warning(f"[lifespan] restore_from_log failed (non-fatal): {e}")
    # ⚠️ 审计 2026-06-09: 预热 SQLite DataStore, 避免 live loop 线程抢锁初始化
    # 导致并发 API 请求排队 5-12s. 在 lifespan 完成, 所有 _init_db DDL 都在这里跑完.
    try:
        from data.store import DataStore
        DataStore()
        _lg.info("[lifespan] DataStore warmed up")
    except Exception as e:
        _lg.warning(f"[lifespan] DataStore warmup failed (non-fatal): {e}")
    # audit 2026-06-10: 后台预热 cTrader bridge (切 Live tab 不阻塞).
    # 同步 0s, 后台线程继续做真 connect (1-10s).
    try:
        from backend.services.live_service import warmup_ctrader
        warmup_ctrader(timeout_sec=0.0)
    except Exception as e:
        _lg.warning(f"[lifespan] cTrader warmup failed (non-fatal): {e}")

    yield

    # ── 关停 Scheduler (若有残留) ──
    try:
        if hasattr(app.state, "_evolution_scheduler"):
            app.state._evolution_scheduler.stop()
            _lg.info("[lifespan] InProcessScheduler stopped")
    except Exception as e:
        _lg.warning(f"[lifespan] InProcessScheduler stop failed: {e}")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Quant Trading Web Console",
        version="0.1.0",
        lifespan=lifespan,
    )
    # CORS allow_origins: env-driven (QUANT_CORS_ALLOWED_ORIGINS) so the
    # same image can be used for localhost dev, 192.168.x.x LAN dev, and
    # prod behind a reverse proxy. Format: comma-separated origins, e.g.
    #   QUANT_CORS_ALLOWED_ORIGINS=http://localhost:5173,http://192.168.1.5:5173
    # Default: localhost + 127.0.0.1 on :5173 (the dev-mode Vite server).
    import os as _os
    _cors_env = _os.environ.get("QUANT_CORS_ALLOWED_ORIGINS", "").strip()
    if _cors_env:
        _cors_origins = [o.strip() for o in _cors_env.split(",") if o.strip()]
    else:
        _cors_origins = ["http://localhost:5173", "http://127.0.0.1:5173"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    for r in ALL_ROUTERS:
        app.include_router(r)
    app.include_router(ws_router)  # WS routes don't use prefix

    # Serve static Vite frontend if built (prod mode).
    # In dev, /static is not mounted; Vite dev server handles the frontend on :5173.
    static_dir = BACKEND_DIR / "static"
    if static_dir.exists():
        # Serve static assets (Vite puts them in /assets/ subdirectory)
        app.mount("/assets", StaticFiles(directory=str(static_dir / "assets")), name="static-assets")

        # SPA fallback: any non-/api / non-/ws GET serves index.html
        # (so /market, /factors, etc. all load index.html which React hydrates)
        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa_fallback(full_path: str) -> Response:
            # If the request path matches a real file in static_dir, serve it
            candidate = static_dir / full_path
            if candidate.is_file():
                return FileResponse(str(candidate))
            # Otherwise serve index.html for client-side routing
            index_html = static_dir / "index.html"
            if index_html.exists():
                return FileResponse(str(index_html), media_type="text/html")
            return Response("Frontend not built. Run start-prod.bat first.", status_code=503)

    return app


app = create_app()
