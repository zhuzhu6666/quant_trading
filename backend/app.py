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

    # Load RuntimeConfig from settings.yaml so live execution honors ctrader.send_orders.
    try:
        from config.runtime_config import RuntimeConfig, replace as rc_replace
        from backend.services.config_service import get_config
        yaml_cfg = get_config()["parsed"]
        rc = RuntimeConfig.from_yaml(yaml_cfg)
        rc_replace(rc)
        _lg.info("[lifespan] RuntimeConfig loaded from config/settings.yaml")
    except Exception as e:
        _lg.warning(f"[lifespan] RuntimeConfig load failed (non-fatal): {e}")

    _init_observability()

    # 初始化统一数据库
    try:
        from backend.core.db import init_all
        init_all()
        _lg.info("[lifespan] databases initialized")
    except Exception as e:
        _lg.warning(f"[lifespan] db init failed (non-fatal): {e}")

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
        from backend.services.live_service import schedule_auto_resume_loop, warmup_ctrader
        warmup_ctrader(timeout_sec=0.0)
        if schedule_auto_resume_loop():
            _lg.info("[lifespan] auto-resume loop scheduled from persisted desired state")
    except Exception as e:
        _lg.warning(f"[lifespan] cTrader warmup failed (non-fatal): {e}")

    # Delayed learning backfill: repair restart gaps using ctrader_deals + decision_ledger.
    try:
        from backend.services.learning_backfill import schedule_learning_backfill
        if schedule_learning_backfill(delay_sec=180.0, limit=100, allow_partial=False, rebuild_learning=True):
            _lg.info("[lifespan] learning backfill scheduled")
    except Exception as e:
        _lg.warning(f"[lifespan] learning backfill schedule failed (non-fatal): {e}")

    # Background warm-up db-health cache (避免首次请求阻塞线程池 20s)
    try:
        from backend.api.db_health import _on_startup as _warm_db_health
        _warm_db_health()
        _lg.info("[lifespan] db-health cache warmup scheduled")
    except Exception as e:
        _lg.warning(f"[lifespan] db-health warmup failed (non-fatal): {e}")

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
