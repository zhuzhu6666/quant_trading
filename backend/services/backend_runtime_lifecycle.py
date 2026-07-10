"""Best-effort backend warmup and scheduler lifecycle orchestration.

This boundary deliberately preserves the existing FastAPI lifespan semantics:
all warmups and stops are independent, non-fatal steps, except that cTrader
warmup and live-loop auto-resume share one exception boundary.  It does not
stop the live loop or disconnect the broker.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import os
from typing import Any


def _env_enabled(name: str, default: str = "1") -> bool:
    value = str(os.getenv(name, default) or "").strip().lower()
    return value not in {"0", "false", "no", "off", "disabled"}


def _warm_data_store() -> None:
    from data.store import DataStore

    DataStore()


def _warmup_ctrader(*, timeout_sec: float) -> None:
    from backend.services.live_service import warmup_ctrader

    warmup_ctrader(timeout_sec=timeout_sec)


def _schedule_auto_resume_loop() -> bool:
    from backend.services.live_service import schedule_auto_resume_loop

    return schedule_auto_resume_loop()


def _schedule_learning_backfill(**kwargs: Any) -> bool:
    from backend.services.learning_backfill import schedule_learning_backfill

    return schedule_learning_backfill(**kwargs)


def _schedule_supervisor_learning(**kwargs: Any) -> bool:
    from backend.services.supervisor_learning_scheduler import schedule_supervisor_learning

    return schedule_supervisor_learning(**kwargs)


def _schedule_autonomous_learning(**kwargs: Any) -> bool:
    from backend.services.autonomous_learning import schedule_autonomous_learning

    return schedule_autonomous_learning(**kwargs)


def _warm_db_health() -> None:
    from backend.api.db_health import _on_startup

    _on_startup()


def _stop_learning_backfill() -> None:
    from backend.services.learning_backfill import stop_learning_backfill

    stop_learning_backfill()


def _stop_supervisor_learning() -> None:
    from backend.services.supervisor_learning_scheduler import stop_supervisor_learning

    stop_supervisor_learning()


def _stop_autonomous_learning() -> None:
    from backend.services.autonomous_learning import stop_autonomous_learning

    stop_autonomous_learning()


def _stop_live_loop_for_process_shutdown(*, timeout_sec: float) -> dict[str, Any]:
    from backend.services.live_service import stop_loop_for_process_shutdown

    return stop_loop_for_process_shutdown(timeout_sec=timeout_sec)


def _stop_live_scheduler() -> None:
    from backend.services.live_service import _stop_live_scheduler as stop_live_scheduler

    stop_live_scheduler()


@dataclass(frozen=True)
class BackendRuntimeLifecycleCallbacks:
    """Injectable runtime operations used to lock lifecycle behavior in tests."""

    warm_data_store: Callable[[], Any] = _warm_data_store
    warmup_ctrader: Callable[..., Any] = _warmup_ctrader
    schedule_auto_resume_loop: Callable[[], bool] = _schedule_auto_resume_loop
    schedule_learning_backfill: Callable[..., bool] = _schedule_learning_backfill
    schedule_supervisor_learning: Callable[..., bool] = _schedule_supervisor_learning
    schedule_autonomous_learning: Callable[..., bool] = _schedule_autonomous_learning
    warm_db_health: Callable[[], Any] = _warm_db_health
    stop_learning_backfill: Callable[[], Any] = _stop_learning_backfill
    stop_supervisor_learning: Callable[[], Any] = _stop_supervisor_learning
    stop_autonomous_learning: Callable[[], Any] = _stop_autonomous_learning
    stop_live_loop_for_process_shutdown: Callable[..., dict[str, Any]] = (
        _stop_live_loop_for_process_shutdown
    )
    stop_live_scheduler: Callable[[], Any] = _stop_live_scheduler


class BackendRuntimeLifecycle:
    """Run the non-blocking runtime steps formerly embedded in lifespan."""

    def __init__(
        self,
        callbacks: BackendRuntimeLifecycleCallbacks | None = None,
        *,
        env_enabled: Callable[[str, str], bool] = _env_enabled,
    ) -> None:
        self._callbacks = callbacks or BackendRuntimeLifecycleCallbacks()
        self._env_enabled = env_enabled

    def start(self, logger: Any) -> None:
        callbacks = self._callbacks

        # Pre-warm DataStore to avoid race on first access.
        try:
            callbacks.warm_data_store()
            logger.info("[lifespan] DataStore warmed up")
        except Exception as exc:
            logger.warning(f"[lifespan] DataStore warmup failed (non-fatal): {exc}")

        # Background warm-up cTrader bridge.  Auto-resume intentionally shares
        # this exception boundary with warmup to preserve current behavior.
        try:
            callbacks.warmup_ctrader(timeout_sec=0.0)
            if callbacks.schedule_auto_resume_loop():
                logger.info("[lifespan] auto-resume loop scheduled from persisted desired state")
        except Exception as exc:
            logger.warning(f"[lifespan] cTrader warmup failed (non-fatal): {exc}")

        if self._env_enabled("QUANT_BACKEND_LEARNING_SCHEDULERS", "0"):
            try:
                if callbacks.schedule_learning_backfill(
                    delay_sec=180.0,
                    limit=100,
                    allow_partial=False,
                    rebuild_learning=True,
                ):
                    logger.info("[lifespan] learning backfill scheduled")
            except Exception as exc:
                logger.warning(f"[lifespan] learning backfill schedule failed (non-fatal): {exc}")

            try:
                if callbacks.schedule_supervisor_learning(
                    delay_sec=300.0,
                    interval_sec=1800.0,
                    limit=200,
                ):
                    logger.info("[lifespan] supervisor learning scheduled")
            except Exception as exc:
                logger.warning(f"[lifespan] supervisor learning schedule failed (non-fatal): {exc}")

            try:
                if callbacks.schedule_autonomous_learning(
                    delay_sec=420.0,
                    interval_sec=1800.0,
                    sample_limit=500,
                    recommendation_limit=20,
                ):
                    logger.info("[lifespan] autonomous learning scheduled")
            except Exception as exc:
                logger.warning(f"[lifespan] autonomous learning schedule failed (non-fatal): {exc}")
        else:
            logger.info("[lifespan] backend learning schedulers disabled by QUANT_BACKEND_LEARNING_SCHEDULERS")

        # Background warm-up db-health cache (avoid blocking the first request).
        try:
            callbacks.warm_db_health()
            logger.info("[lifespan] db-health cache warmup scheduled")
        except Exception as exc:
            logger.warning(f"[lifespan] db-health warmup failed (non-fatal): {exc}")

    def stop(self, logger: Any) -> None:
        callbacks = self._callbacks

        try:
            live_result = callbacks.stop_live_loop_for_process_shutdown(timeout_sec=30.0)
            if live_result.get("status") == "timed_out":
                logger.warning(
                    "[lifespan] live loop process shutdown timed out; recovery required"
                )
            else:
                logger.info(
                    f"[lifespan] live loop process shutdown status={live_result.get('status', 'unknown')}"
                )
        except Exception as exc:
            logger.warning(f"[lifespan] live loop process shutdown failed: {exc}")

        try:
            callbacks.stop_learning_backfill()
        except Exception as exc:
            logger.warning(f"[lifespan] learning backfill stop failed: {exc}")

        try:
            callbacks.stop_supervisor_learning()
        except Exception as exc:
            logger.warning(f"[lifespan] supervisor learning stop failed: {exc}")

        try:
            callbacks.stop_autonomous_learning()
        except Exception as exc:
            logger.warning(f"[lifespan] autonomous learning stop failed: {exc}")

        try:
            callbacks.stop_live_scheduler()
        except Exception as exc:
            logger.warning(f"[lifespan] InProcessScheduler stop failed: {exc}")
