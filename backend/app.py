"""FastAPI app factory — API-only backend (frontend removed, WeChat mini-program replaces it).

Usage:
  ./.venv/bin/python -m backend # uvicorn on :8000
  uvicorn backend.app:app      # direct
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api import ALL_ROUTERS
from backend.core.logging import setup_logging
from backend.jobs import get_job_manager
from backend.services.backend_runtime_lifecycle import BackendRuntimeLifecycle
from backend.ws.endpoints import router as ws_router
from monitor.metrics import install_into_runtime_state
from monitor.structured_log import setup_structured_logging


_DEFAULT_FRONTEND_CORS_ORIGINS = (
    "https://www.zhuzhu666.icu",
    "http://tauri.localhost",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)


def _frontend_cors_origins() -> list[str]:
    configured = [
        item.strip()
        for item in os.getenv("QUANT_FRONTEND_CORS_ORIGINS", "").split(",")
        if item.strip()
    ]
    origins = configured or list(_DEFAULT_FRONTEND_CORS_ORIGINS)
    if "*" in origins:
        raise RuntimeError(
            "QUANT_FRONTEND_CORS_ORIGINS must enumerate trusted origins; wildcard CORS is not allowed"
        )
    return list(dict.fromkeys(origins))


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


def _state_db_failure_is_blocking(exc: Exception, execution_semantics) -> bool:
    """Schema-version mismatches block every backend mode, including dry-run."""
    from backend.core.state_schema_migrations import StateSchemaVersionError

    return isinstance(exc, StateSchemaVersionError) or bool(
        execution_semantics and execution_semantics.effective_send_orders
    )


def _fail_closed_governance_authority(
    *,
    component: str,
    error: Exception,
    record_startup_issue,
    logger,
) -> None:
    """Preserve the loaded config but block new risk on authority ambiguity.

    Startup must keep the loop available for close/reduce/tighten operations,
    so an unverifiable legacy projection is not a process-fatal error.  It is,
    however, a governance fact-source failure and therefore installs the same
    durable local admission latch used by execution recovery.  The latch
    implementation also fails closed in-process when its append-only ledger
    cannot be written.
    """

    message = f"{type(error).__name__}: {error}"
    record_startup_issue(component, "critical", message, blocking=True)
    try:
        from backend.services.live_safety_state import activate_no_new_risk_latch

        activate_no_new_risk_latch(
            reason="legacy_governance_restore_unverified",
            actor="system:backend_lifespan",
            metadata={
                "component": component,
                "error_type": type(error).__name__,
                "error": str(error)[:500],
            },
            cause="governance_authority",
            cause_id=f"legacy_restore:{component}",
        )
    except Exception as latch_exc:
        logger.error(
            "[lifespan] governance restore latch persistence failed; "
            "new risk remains blocked in-process: %s",
            latch_exc,
        )
    logger.error(
        "[lifespan] %s failed authority validation; preserving loaded config "
        "and blocking new risk: %s",
        component,
        error,
    )


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
        _lg.info("[lifespan] RuntimeConfig base loaded from config/settings.yaml")
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
        from backend.services.learning_application_state import LearningApplicationStateService

        recovery = LearningApplicationStateService().recover_prepared()
        if recovery.get("checked"):
            _lg.info(f"[lifespan] governed weight application recovery: {recovery}")
    except Exception as e:
        blocking_state_db = _state_db_failure_is_blocking(e, execution_semantics)
        record_startup_issue(
            "state_db",
            "critical" if blocking_state_db else "degraded",
            str(e),
            blocking=blocking_state_db,
        )
        if blocking_state_db:
            _lg.error(f"[lifespan] blocking state db initialization failure: {e}")
            raise
        _lg.warning(f"[lifespan] db init failed (dry-run degraded): {e}")

    # The schema gate above must run before overlay restore because restore may
    # ensure tables and persist a startup snapshot.  A process with stale code
    # or a stale database must not perform DDL/DML before compatibility is
    # established.
    try:
        startup_restore = restore_runtime_config_on_startup(
            rc,
            snapshot_source="backend_lifespan_startup",
        )
        overlay_restore = startup_restore.get("overlay") or {}
        rc = startup_restore["config"]
        if overlay_restore.get("restored"):
            from config.runtime_config import (
                release_recovered_overlay_authority_latches,
            )

            if not release_recovered_overlay_authority_latches(overlay_restore):
                record_startup_issue(
                    "runtime_config_overlay_latch_release",
                    "critical",
                    "verified overlay restored but authority latch release failed",
                    blocking=True,
                )
            _lg.info(
                "[lifespan] RuntimeConfig autonomous overlay restored hash=%s",
                overlay_restore.get("overlay_hash", ""),
            )
    except Exception as overlay_exc:
        # An unbound/dangling overlay is a governance fact-source failure, not
        # permission to terminate risk reduction or silently reset session
        # truth.  Keep the release/YAML projection, durably block new risk, and
        # continue startup so close/reduce/tighten remain available.
        _fail_closed_governance_authority(
            component="runtime_config_overlay",
            error=overlay_exc,
            record_startup_issue=record_startup_issue,
            logger=_lg,
        )
        from config import runtime_config as _runtime_config

        quarantined_config = getattr(overlay_exc, "quarantined_config", None)
        if quarantined_config is not None:
            rc = quarantined_config
            _lg.error(
                "[lifespan] unverified overlay retained as read-only quarantine; "
                "new risk remains latched"
            )
        _runtime_config.replace(rc)
    _lg.info("[lifespan] RuntimeConfig loaded from config/settings.yaml and state overlay")

    get_job_manager().bind_loop(asyncio.get_running_loop())

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
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_frontend_cors_origins(),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Accept", "Authorization", "Content-Type", "X-Confirm"],
    )

    # Register all API routers
    for r in ALL_ROUTERS:
        app.include_router(r)
    app.include_router(ws_router)

    return app


app = create_app()
