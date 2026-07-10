"""FastAPI app factory — API-only backend (frontend removed, WeChat mini-program replaces it).

Usage:
  ./.venv/bin/python -m backend # uvicorn on :8000
  uvicorn backend.app:app      # direct
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.api import ALL_ROUTERS
from backend.core.logging import setup_logging
from backend.jobs import get_job_manager
from backend.services.backend_runtime_lifecycle import BackendRuntimeLifecycle
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
    from backend.services.startup_status import clear_startup_issues, record_startup_issue

    clear_startup_issues()

    try:
        from backend.core.auth import validate_auth_config
        validate_auth_config()
        _lg.info("[lifespan] auth configuration validated")
    except Exception as e:
        _lg.error(f"[lifespan] auth configuration invalid: {e}")
        raise

    # Load RuntimeConfig from settings.yaml so live execution honors ctrader.send_orders.
    execution_semantics = None
    try:
        from backend.services.execution_semantics import validate_execution_semantics
        from backend.services.runtime_config_startup import (
            load_yaml_runtime_config,
            restore_runtime_config_on_startup,
        )

        rc, yaml_cfg = load_yaml_runtime_config()
        execution_semantics = validate_execution_semantics(yaml_cfg, rc)
        try:
            startup_restore = restore_runtime_config_on_startup(
                rc,
                snapshot_source="backend_lifespan_startup",
            )
            overlay_restore = startup_restore.get("overlay") or {}
            rc = startup_restore["config"]
            if overlay_restore.get("restored"):
                _lg.info(
                    "[lifespan] RuntimeConfig autonomous overlay restored hash=%s",
                    overlay_restore.get("overlay_hash", ""),
                )
        except Exception as overlay_exc:
            if execution_semantics.effective_send_orders:
                record_startup_issue("runtime_config_overlay", "critical", str(overlay_exc), blocking=True)
                raise
            record_startup_issue("runtime_config_overlay", "degraded", str(overlay_exc), blocking=False)
            _lg.warning(f"[lifespan] RuntimeConfig autonomous overlay restore failed (non-fatal): {overlay_exc}")
            from config import runtime_config as _runtime_config

            _runtime_config.replace(rc)
        _lg.info("[lifespan] RuntimeConfig loaded from config/settings.yaml")
    except Exception as e:
        record_startup_issue("runtime_config", "critical", str(e), blocking=True)
        _lg.error(f"[lifespan] RuntimeConfig load failed: {e}")
        raise

    _init_observability()

    # 初始化统一数据库
    try:
        from backend.core.db import init_all
        init_all()
        _lg.info("[lifespan] databases initialized")
    except Exception as e:
        effective_send_orders = bool(execution_semantics and execution_semantics.effective_send_orders)
        record_startup_issue("state_db", "critical" if effective_send_orders else "degraded", str(e), blocking=effective_send_orders)
        if effective_send_orders:
            _lg.error(f"[lifespan] db init failed while effective send-orders is enabled: {e}")
            raise
        _lg.warning(f"[lifespan] db init failed (dry-run degraded): {e}")

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

    runtime_lifecycle = BackendRuntimeLifecycle()
    runtime_lifecycle.start(_lg)

    _lg.info("[lifespan] PostgreSQL state store active; legacy state dual-write worker not started")

    try:
        yield
    finally:
        runtime_lifecycle.stop(_lg)


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
