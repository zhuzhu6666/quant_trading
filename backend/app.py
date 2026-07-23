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


def _state_db_failure_is_blocking(exc: Exception, execution_semantics) -> bool:
    """Schema-version mismatches block every backend mode, including dry-run."""
    from backend.core.state_schema_migrations import StateSchemaVersionError

    return isinstance(exc, StateSchemaVersionError) or bool(
        execution_semantics and execution_semantics.effective_send_orders
    )


def _fail_closed_legacy_governance_restore(
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
        _fail_closed_legacy_governance_restore(
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

    # In coordinator modes the committed overlay is the only startup
    # projection source.  Replaying application/registry rows directly here
    # could republish an approved-but-uncommitted control or overwrite a
    # coordinator recovery decision.  The legacy restore remains available
    # only while the static rollout flag is explicitly off.
    try:
        from backend.services.governance_control_plans import governance_coordinator_mode

        coordinator_mode = governance_coordinator_mode()
    except Exception as e:
        record_startup_issue("governance_authority", "critical", str(e), blocking=True)
        _lg.error(f"[lifespan] governance authority mode invalid: {e}")
        raise

    legacy_governance_restore_blocked = False
    if coordinator_mode == "off":
        try:
            from backend.services.parameter_templates import ParameterTemplateService

            ParameterTemplateService().sync_runtime_config(restore_only=True)
            _lg.info("[lifespan] active parameter templates synced into RuntimeConfig (legacy off mode)")
        except Exception as e:
            legacy_governance_restore_blocked = True
            _fail_closed_legacy_governance_restore(
                component="parameter_template_restore",
                error=e,
                record_startup_issue=record_startup_issue,
                logger=_lg,
            )

        try:
            from backend.core.db import STATE_DB
            from backend.services.evolution_ledger import persist_runtime_config_snapshot
            from backend.services.position_supervisor_templates import latest_applied_position_supervisor_template_id
            from config.runtime_config import patch as rc_patch
            from config.runtime_config import autonomy_expansion_freeze_applies, shared as rc_shared

            active_template_id = latest_applied_position_supervisor_template_id(
                db_path=STATE_DB,
                require_authority=True,
            )
            current_template_id = str(getattr(rc_shared(), "position_supervisor_template_id", "") or "")
            expansion_frozen = autonomy_expansion_freeze_applies(rc_shared())
            if expansion_frozen:
                active_template_id = ""
                _lg.info("[lifespan] supervisor template restore skipped: autonomy expansion frozen")
            if active_template_id and active_template_id != current_template_id:
                rc_patch({"position_supervisor_template_id": active_template_id})
                persist_runtime_config_snapshot(
                    rc_shared(),
                    source="position_supervisor_template_restore",
                    db_path=STATE_DB,
                )
                _lg.info(f"[lifespan] restored position supervisor template: {active_template_id}")
        except Exception as e:
            legacy_governance_restore_blocked = True
            _fail_closed_legacy_governance_restore(
                component="position_supervisor_template_restore",
                error=e,
                record_startup_issue=record_startup_issue,
                logger=_lg,
            )
    else:
        _lg.info(
            "[lifespan] coordinator mode=%s; parameter/supervisor startup projection "
            "is owned by committed overlay recovery",
            coordinator_mode,
        )

    get_job_manager().bind_loop(asyncio.get_running_loop())

    # The legacy event log has no committed mutation binding.  Keep its
    # one-release restore only in off mode; coordinator modes rebuild Registry
    # from factor_lifecycle_state in BackendRuntimeLifecycle.start().
    if coordinator_mode == "off":
        if legacy_governance_restore_blocked:
            _lg.error(
                "[lifespan] legacy Registry restore skipped because governance "
                "authority validation failed; safety loop may run but new risk is latched"
            )
        else:
            try:
                from alpha.persistent_registry import restore_from_log

                configured_factor_names = {
                    str(name)
                    for name, factor_cfg in dict(getattr(rc, "factor_signal_config", {}) or {}).items()
                    if not isinstance(factor_cfg, dict) or factor_cfg.get("enabled") is not False
                }
                restored = restore_from_log(
                    verbose=False,
                    preferred_names=configured_factor_names,
                    discovered_budget=None,
                )
                if restored:
                    _lg.info(f"[lifespan] restored {restored} configured/budgeted runtime factors from lifecycle log")
            except Exception as e:
                _lg.warning(f"[lifespan] restore_from_log failed (non-fatal): {e}")
    else:
        _lg.info(
            "[lifespan] coordinator mode=%s; Registry restore requires committed lifecycle state",
            coordinator_mode,
        )

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
