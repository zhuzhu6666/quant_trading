"""FastAPI app factory — API-only backend (frontend removed, WeChat mini-program replaces it).

Usage:
  python -m backend            # uvicorn on :8000
  uvicorn backend.app:app      # direct
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.api import ALL_ROUTERS
from backend.core.logging import setup_logging
from backend.jobs import get_job_manager
from backend.ws.endpoints import router as ws_router
from monitor.metrics import install_into_runtime_state
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
    get_job_manager().bind_loop(asyncio.get_running_loop())

    # Restore shadow/discovered factors from lifecycle log
    try:
        from alpha.persistent_registry import restore_from_log
        restored = restore_from_log(verbose=False)
        if restored:
            _lg.info(f"[lifespan] restored {restored} shadow/discovered factors from lifecycle log")
    except Exception as e:
        _lg.warning(f"[lifespan] restore_from_log failed (non-fatal): {e}")

    # Pre-warm DataStore to avoid race on first access
    try:
        from data.store import DataStore
        DataStore()
        _lg.info("[lifespan] DataStore warmed up")
    except Exception as e:
        _lg.warning(f"[lifespan] DataStore warmup failed (non-fatal): {e}")

    # Background warm-up cTrader bridge
    try:
        from backend.services.live_service import warmup_ctrader
        warmup_ctrader(timeout_sec=0.0)
    except Exception as e:
        _lg.warning(f"[lifespan] cTrader warmup failed (non-fatal): {e}")

    yield

    # Stop scheduler on shutdown
    try:
        if hasattr(app.state, "_evolution_scheduler"):
            app.state._evolution_scheduler.stop()
            _lg.info("[lifespan] InProcessScheduler stopped")
    except Exception as e:
        _lg.warning(f"[lifespan] InProcessScheduler stop failed: {e}")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Quant Trading API",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Register all API routers
    for r in ALL_ROUTERS:
        app.include_router(r)
    app.include_router(ws_router)

    return app


app = create_app()
