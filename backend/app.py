"""FastAPI app factory — API-only backend (frontend removed, WeChat mini-program replaces it).

Usage:
  python -m backend            # uvicorn on :8000
  uvicorn backend.app:app      # direct
"""

import asyncio
import logging
import os
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


def _env_enabled(name: str, default: str = "1") -> bool:
    value = str(os.getenv(name, default) or "").strip().lower()
    return value not in {"0", "false", "no", "off", "disabled"}


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
        try:
            from backend.services.evolution_ledger import persist_runtime_config_snapshot

            persist_runtime_config_snapshot(rc, source="backend_lifespan_startup")
        except Exception as snap_exc:
            _lg.warning(f"[lifespan] RuntimeConfig snapshot failed (non-fatal): {snap_exc}")
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

    try:
        from backend.services.parameter_templates import ParameterTemplateService
        ParameterTemplateService().sync_runtime_config()
        _lg.info("[lifespan] active parameter templates synced into RuntimeConfig")
    except Exception as e:
        _lg.warning(f"[lifespan] parameter template runtime sync failed (non-fatal): {e}")

    try:
        from backend.core.db import STATE_DB
        from backend.services.evolution_ledger import persist_runtime_config_snapshot
        from backend.services.position_supervisor_templates import latest_applied_position_supervisor_template_id
        from config.runtime_config import patch as rc_patch
        from config.runtime_config import shared as rc_shared

        active_template_id = latest_applied_position_supervisor_template_id(db_path=STATE_DB)
        current_template_id = str(getattr(rc_shared(), "position_supervisor_template_id", "") or "")
        if active_template_id and active_template_id != current_template_id:
            rc_patch({"position_supervisor_template_id": active_template_id})
            persist_runtime_config_snapshot(
                rc_shared(),
                source="position_supervisor_template_restore",
                db_path=STATE_DB,
            )
            _lg.info(f"[lifespan] restored position supervisor template: {active_template_id}")
    except Exception as e:
        _lg.warning(f"[lifespan] position supervisor template restore failed (non-fatal): {e}")

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

    if _env_enabled("QUANT_BACKEND_LEARNING_SCHEDULERS", "0"):
        # Delayed learning backfill: repair restart gaps using ctrader_deals + decision_ledger.
        try:
            from backend.services.learning_backfill import schedule_learning_backfill
            if schedule_learning_backfill(delay_sec=180.0, limit=100, allow_partial=False, rebuild_learning=True):
                _lg.info("[lifespan] learning backfill scheduled")
        except Exception as e:
            _lg.warning(f"[lifespan] learning backfill schedule failed (non-fatal): {e}")

        # Supervisor counterfactual/advisory materialization.
        try:
            from backend.services.supervisor_learning_scheduler import schedule_supervisor_learning
            if schedule_supervisor_learning(delay_sec=300.0, interval_sec=1800.0, limit=200):
                _lg.info("[lifespan] supervisor learning scheduled")
        except Exception as e:
            _lg.warning(f"[lifespan] supervisor learning schedule failed (non-fatal): {e}")

        # Autonomous learning factory: samples + governed suggestion materialization only.
        try:
            from backend.services.autonomous_learning import schedule_autonomous_learning
            if schedule_autonomous_learning(delay_sec=420.0, interval_sec=1800.0, sample_limit=500, recommendation_limit=20):
                _lg.info("[lifespan] autonomous learning scheduled")
        except Exception as e:
            _lg.warning(f"[lifespan] autonomous learning schedule failed (non-fatal): {e}")
    else:
        _lg.info("[lifespan] backend learning schedulers disabled by QUANT_BACKEND_LEARNING_SCHEDULERS")

    # Background warm-up db-health cache (避免首次请求阻塞线程池 20s)
    try:
        from backend.api.db_health import _on_startup as _warm_db_health
        _warm_db_health()
        _lg.info("[lifespan] db-health cache warmup scheduled")
    except Exception as e:
        _lg.warning(f"[lifespan] db-health warmup failed (non-fatal): {e}")

    _lg.info("[lifespan] PostgreSQL state store active; legacy state dual-write worker not started")

    yield

    # Stop scheduler on shutdown
    try:
        from backend.services.learning_backfill import stop_learning_backfill
        stop_learning_backfill()
    except Exception as e:
        _lg.warning(f"[lifespan] learning backfill stop failed: {e}")

    try:
        from backend.services.supervisor_learning_scheduler import stop_supervisor_learning
        stop_supervisor_learning()
    except Exception as e:
        _lg.warning(f"[lifespan] supervisor learning stop failed: {e}")

    try:
        from backend.services.autonomous_learning import stop_autonomous_learning
        stop_autonomous_learning()
    except Exception as e:
        _lg.warning(f"[lifespan] autonomous learning stop failed: {e}")

    try:
        from backend.services.live_service import _stop_live_scheduler
        _stop_live_scheduler()
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
