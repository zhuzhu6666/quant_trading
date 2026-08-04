#!/usr/bin/env python3
"""Dedicated learning/evolution worker.

This process is meant to run outside quant-backend.service so heavy training
and evolution jobs can use their own CPU affinity without contending directly
with the live trading API process.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loguru import logger

from backend.services.learning_worker_capability import (
    LearningWorkerCapability,
    guarded_mutation_job,
    mutation_stage_allowed,
)


_worker_capability = LearningWorkerCapability()
_factor_health_catchup_stop = threading.Event()
_factor_health_catchup_thread: threading.Thread | None = None


def _env_enabled(name: str, default: str = "1") -> bool:
    value = str(os.getenv(name, default) or "").strip().lower()
    return value not in {"0", "false", "no", "off", "disabled"}


def _apply_cpu_affinity(raw: str) -> None:
    raw = str(raw or "").strip()
    if not raw or not hasattr(os, "sched_setaffinity"):
        return
    cpus = {int(item) for item in raw.replace(",", " ").split() if item.strip()}
    if not cpus:
        return
    os.sched_setaffinity(0, cpus)
    logger.info("[learning_worker] CPU affinity set to {}", sorted(cpus))


def _publish_boot_failure(
    capability: LearningWorkerCapability,
    *,
    stage: str,
    error: BaseException,
) -> None:
    capability.mark_boot_failed(stage=stage, error=error)
    try:
        capability.publish()
    except Exception as publish_exc:
        logger.error(
            "[learning_worker] failed to publish boot failure stage={} error={}",
            stage,
            publish_exc,
        )


def _bootstrap_runtime(
    capability: LearningWorkerCapability | None = None,
) -> LearningWorkerCapability:
    capability = capability or _worker_capability
    from backend.core.logging import setup_logging

    setup_logging()
    try:
        from backend.core.db import init_all

        init_all()
        logger.info("[learning_worker] databases initialized")
    except Exception as exc:
        _publish_boot_failure(capability, stage="database_or_schema", error=exc)
        logger.error("[learning_worker] critical database/schema boot failure: {}", exc)
        raise

    try:
        from backend.services.evolution_ledger import expire_stale_evolution_runs
        from backend.services.governance_startup_recovery import (
            GovernanceStartupRecoveryService,
        )

        expired = expire_stale_evolution_runs(max_age_sec=3600.0)
        if expired.get("expired_count"):
            logger.info("[learning_worker] expired stale interrupted runs: {}", expired)
        governance_recovery = GovernanceStartupRecoveryService().run(
            process_role="learning_worker"
        )
        if governance_recovery.get("ok") is not True:
            raise RuntimeError(
                f"governance_startup_recovery_failed:{governance_recovery}"
            )
        if (
            governance_recovery.get("aborted_intent_count")
            or governance_recovery.get("released_claim_count")
        ):
            logger.info(
                "[learning_worker] governance crash recovery: {}",
                governance_recovery,
            )
        from backend.services.learning_application_state import LearningApplicationStateService

        recovery = LearningApplicationStateService().recover_prepared()
        if recovery.get("ok") is not True:
            raise RuntimeError(f"learning_application_recovery_failed:{recovery}")
        if recovery.get("checked"):
            logger.info("[learning_worker] governed weight application recovery: {}", recovery)
    except Exception as exc:
        _publish_boot_failure(capability, stage="recovery", error=exc)
        logger.error("[learning_worker] critical recovery boot failure: {}", exc)
        raise

    try:
        from backend.services.runtime_config_startup import (
            load_yaml_runtime_config,
            restore_runtime_config_on_startup,
        )

        base_cfg, _yaml_cfg = load_yaml_runtime_config()
    except Exception as exc:
        _publish_boot_failure(capability, stage="yaml_config", error=exc)
        logger.error("[learning_worker] critical YAML config boot failure: {}", exc)
        raise

    try:
        restored = restore_runtime_config_on_startup(
            base_cfg,
            snapshot_source="learning_worker_startup",
        )
        if restored.get("ok") is not True:
            raise RuntimeError(f"runtime_config_restore_failed:{restored}")
        overlay = restored.get("overlay") or {}
        if overlay.get("ok") is not True:
            raise RuntimeError(f"runtime_config_overlay_unavailable:{overlay}")
        if overlay.get("restored"):
            logger.info(
                "[learning_worker] RuntimeConfig autonomous overlay restored hash={}",
                overlay.get("overlay_hash", ""),
            )
    except Exception as exc:
        _publish_boot_failure(capability, stage="runtime_overlay", error=exc)
        logger.error("[learning_worker] critical runtime overlay boot failure: {}", exc)
        raise

    snapshot = dict(restored.get("snapshot") or {})
    capability.mark_ready(
        config_hash=str(snapshot.get("config_hash") or ""),
        overlay_hash=str(overlay.get("overlay_hash") or ""),
        recovery_status="complete",
    )
    # A worker that cannot publish its capability/config hashes is not safe to
    # start mutation schedulers because backend readiness cannot detect drift.
    capability.publish()
    logger.info(
        "[learning_worker] RuntimeConfig loaded config_hash={} overlay_hash={}",
        str(snapshot.get("config_hash") or "")[:12],
        str(overlay.get("overlay_hash") or "")[:12],
    )
    return capability


def _coordinated_mutation_job(name: str, fn: Callable[[], object]) -> Callable[[], object]:
    from backend.services.evolution_work_coordinator import coordinated_job

    guarded = guarded_mutation_job(
        _worker_capability,
        name,
        coordinated_job(name, fn),
    )
    # Preserve the established scheduler diagnostics and tests.
    guarded.__name__ = f"coordinated_{name}"
    return guarded


def _add_job(scheduler, name: str, cron_expr: str, fn: Callable[[], object]) -> None:
    if scheduler.add_job(name, cron_expr, fn):
        logger.info("[learning_worker] scheduled {} ({})", name, cron_expr)


def _register_heavy_jobs(*, include_system_health: bool) -> None:
    from backend.runtime.evolution_orchestrator import (
        scheduled_evolution_with_governance_handoff,
    )
    from backend.runtime.factor_governance_orchestrator import run_autonomous_factor_governance_cycle
    from backend.runtime.scheduler import InProcessScheduler
    from backend.services.autonomous_evolution_runner import AutonomousEvolutionNurseryRunner
    from backend.services.evolution_work_coordinator import coordinated_job
    from config.runtime_config import (
        effective_factor_governance_cron,
        shared as _runtime_shared,
    )
    from backend.services.learning_research_jobs import (
        run_feature_engineering_job,
        run_offmarket_position_quality_job,
    )
    from backend.services.supervisor_learning_scheduler import (
        run_supervisor_learning_cycle,
    )
    from backend.services.autonomous_learning import (
        run_watermark_gated_autonomous_learning_cycle,
    )

    scheduler = InProcessScheduler()
    _add_job(
        scheduler,
        "evolution_hourly",
        "23,53,58 * * * *",
        _coordinated_mutation_job(
            "evolution_hourly",
            scheduled_evolution_with_governance_handoff,
        ),
    )
    governance_cron = effective_factor_governance_cron()
    _add_job(
        scheduler,
        "factor_governance_autonomous",
        governance_cron,
        _coordinated_mutation_job(
            "factor_governance_autonomous",
            run_autonomous_factor_governance_cycle,
        ),
    )
    if _env_enabled("QUANT_AUTONOMOUS_EVOLUTION_NURSERY_RUNNER", "1"):
        nursery_cron = str(getattr(_runtime_shared(), "autonomous_evolution_nursery_cron", "7,22,37,52 * * * *") or "7,22,37,52 * * * *")

        def _run_nursery_cycle() -> None:
            result = AutonomousEvolutionNurseryRunner().run_once(
                replay_if_stale=_env_enabled("QUANT_AUTONOMOUS_EVOLUTION_NURSERY_REPLAY_IF_STALE", "1"),
                # The learning worker is the system owner of demo_nursery.
                # It performs review, bridge, governed apply and reconciliation
                # without waiting for an operator confirmation.  Live unlock
                # remains outside this path and still requires the live gate.
                automatic_demo=True,
                full_learning_cycle=False,
                consume_recommended_step=True,
                recommended_step_limit=int(os.getenv("QUANT_AUTONOMOUS_EVOLUTION_NURSERY_STEP_LIMIT", "5") or "5"),
            )
            logger.info(
                "[learning_worker] autonomous evolution nursery result: {}",
                {
                    "status": result.get("status"),
                    "initial": (result.get("initial_cycle") or {}).get("status"),
                    "repaired": (result.get("repaired_cycle") or {}).get("status"),
                    "final": (result.get("final_cycle") or {}).get("status"),
                    "actions": [item.get("action") for item in result.get("actions") or []],
                },
            )

        _add_job(
            scheduler,
            "autonomous_evolution_nursery",
            nursery_cron,
            _coordinated_mutation_job(
                "autonomous_evolution_nursery",
                _run_nursery_cycle,
            ),
        )
    _add_job(
        scheduler,
        "feature_eng",
        "5 3 * * *",
        coordinated_job("feature_eng", run_feature_engineering_job),
    )
    _add_job(
        scheduler,
        "supervisor_learning",
        "9,39 * * * *",
        coordinated_job(
            "supervisor_learning",
            lambda: run_supervisor_learning_cycle(limit=200),
        ),
    )
    _add_job(
        scheduler,
        "autonomous_learning",
        "12,42 * * * *",
        _coordinated_mutation_job(
            "autonomous_learning",
            lambda: run_watermark_gated_autonomous_learning_cycle(
                sample_limit=500,
                recommendation_limit=20,
                mutation_capability=_worker_capability.mutation_allowed(),
            ),
        ),
    )
    _add_job(
        scheduler,
        "offmarket_position_quality_lightgbm",
        "20 * * * *",
        coordinated_job(
            "offmarket_position_quality_lightgbm",
            run_offmarket_position_quality_job,
        ),
    )
    if include_system_health:
        try:
            from monitor.alerter import Alerter
            from monitor.system_health import shared as system_health

            health = system_health()
            health.set_alerter(Alerter({"log_file": "logs/alerts.log", "min_level": "WARNING"}).send)
            _add_job(scheduler, "learning_worker_system_health", "* * * * *", health.run)
        except Exception as exc:
            logger.warning("[learning_worker] system_health registration failed: {}", exc)
    scheduler.start()


def _start_learning_schedulers() -> None:
    from backend.services.learning_backfill import schedule_learning_backfill

    schedule_learning_backfill(delay_sec=30.0, limit=100, allow_partial=False, rebuild_learning=True)
    logger.info(
        "[learning_worker] startup backfill scheduled; recurring learning "
        "uses fixed UTC cron jobs"
    )


def _latest_factor_health_age_seconds() -> float | None:
    from backend.core.db import get_state_pg_conn

    conn = get_state_pg_conn(read_only=True)
    try:
        row = conn.execute(
            "SELECT MAX(updated_at) AS updated_at FROM factor_health"
        ).fetchone()
        updated_at = float((row or {}).get("updated_at") or 0.0)
        return max(0.0, time.time() - updated_at) if updated_at > 0.0 else None
    finally:
        conn.close()


def _schedule_factor_health_catchup(
    *,
    delay_sec: float = 180.0,
    stale_after_sec: float | None = None,
) -> bool:
    """Run one watermark-gated health/evolution catch-up after a restart."""

    global _factor_health_catchup_thread
    if (
        _factor_health_catchup_thread is not None
        and _factor_health_catchup_thread.is_alive()
    ):
        return False
    _factor_health_catchup_stop.clear()

    def _worker() -> None:
        if _factor_health_catchup_stop.wait(max(0.0, delay_sec)):
            return
        try:
            from backend.runtime.factor_governance_orchestrator import (
                factor_governance_health_max_age_seconds,
            )

            freshness_limit = (
                factor_governance_health_max_age_seconds()
                if stale_after_sec is None
                else max(0.0, float(stale_after_sec))
            )
            age = _latest_factor_health_age_seconds()
            if age is not None and age <= freshness_limit:
                logger.info(
                    "[learning_worker] factor health catch-up skipped: "
                    "current age={:.1f}s freshness_limit={:.1f}s",
                    age,
                    freshness_limit,
                )
                return
            from backend.runtime.evolution_orchestrator import (
                scheduled_evolution_with_governance_handoff,
            )

            result = _coordinated_mutation_job(
                "factor_health_startup_catchup",
                scheduled_evolution_with_governance_handoff,
            )()
            logger.info(
                "[learning_worker] factor health catch-up result: {}",
                result.to_dict() if hasattr(result, "to_dict") else result,
            )
        except Exception as exc:
            logger.warning(
                "[learning_worker] factor health catch-up failed: {}",
                exc,
            )

    _factor_health_catchup_thread = threading.Thread(
        target=_worker,
        name="factor_health_startup_catchup",
        daemon=True,
    )
    _factor_health_catchup_thread.start()
    return True


def _stop_schedulers() -> None:
    _factor_health_catchup_stop.set()
    try:
        from backend.services.learning_backfill import stop_learning_backfill
        from backend.services.supervisor_learning_scheduler import stop_supervisor_learning
        from backend.services.autonomous_learning import stop_autonomous_learning

        stop_learning_backfill()
        stop_supervisor_learning()
        stop_autonomous_learning()
    except Exception as exc:
        logger.warning("[learning_worker] learning scheduler stop failed: {}", exc)
    try:
        from backend.runtime.scheduler import InProcessScheduler

        InProcessScheduler().stop(wait=False)
    except Exception as exc:
        logger.warning("[learning_worker] in-process scheduler stop failed: {}", exc)


def _run_once(
    capability: LearningWorkerCapability | None = None,
) -> None:
    capability = capability or _worker_capability
    from backend.runtime.evolution_orchestrator import (
        scheduled_evolution_with_governance_handoff,
    )
    from backend.services.autonomous_evolution_runner import AutonomousEvolutionNurseryRunner
    from backend.services.autonomous_learning import run_autonomous_learning_cycle
    from backend.services.supervisor_learning_scheduler import run_supervisor_learning_cycle

    logger.info("[learning_worker] run-once supervisor learning")
    logger.info("[learning_worker] supervisor result: {}", run_supervisor_learning_cycle(limit=200))
    logger.info("[learning_worker] run-once autonomous learning")
    logger.info(
        "[learning_worker] autonomous result: {}",
        run_autonomous_learning_cycle(
            sample_limit=500,
            recommendation_limit=20,
            mutation_capability=mutation_stage_allowed(capability),
        ),
    )
    logger.info("[learning_worker] run-once evolution cycle")
    report = guarded_mutation_job(
        capability,
        "evolution_run_once",
        scheduled_evolution_with_governance_handoff,
    )()
    logger.info("[learning_worker] evolution result: {}", report.to_dict() if hasattr(report, "to_dict") else report)
    logger.info("[learning_worker] run-once autonomous evolution nursery")
    logger.info(
        "[learning_worker] autonomous evolution nursery result: {}",
        guarded_mutation_job(
            capability,
            "autonomous_evolution_nursery_run_once",
            lambda: AutonomousEvolutionNurseryRunner().run_once(
                automatic_demo=True,
                apply_when_ready=True,
                # The explicit run-once path already executed the full learning
                # cycle above; keep the nursery pass orchestration-only.
                full_learning_cycle=False,
                consume_recommended_step=True,
                recommended_step_limit=int(os.getenv("QUANT_AUTONOMOUS_EVOLUTION_NURSERY_STEP_LIMIT", "5") or "5"),
            ),
        )(),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run isolated learning/evolution schedulers.")
    parser.add_argument("--run-once", action="store_true", help="Run one supervisor/autonomous/evolution cycle and exit.")
    parser.add_argument("--no-learning-schedulers", action="store_true", help="Only schedule heavy evolution/training jobs.")
    parser.add_argument("--with-system-health", action="store_true", help="Register worker-local system health. Usually leave this off because live_loop and cTrader live in quant-backend.service.")
    args = parser.parse_args()

    _apply_cpu_affinity(os.getenv("QUANT_LEARNING_WORKER_CPU_AFFINITY", ""))
    _bootstrap_runtime(_worker_capability)

    if args.run_once:
        _run_once()
        return 0

    stop = False

    def _stop(_signum, _frame) -> None:
        nonlocal stop
        stop = True
        logger.info("[learning_worker] stopping")

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    _register_heavy_jobs(include_system_health=args.with_system_health)
    _schedule_factor_health_catchup()
    if not args.no_learning_schedulers and _env_enabled("QUANT_WORKER_LEARNING_SCHEDULERS", "1"):
        _start_learning_schedulers()
    else:
        logger.info("[learning_worker] learning schedulers disabled")

    logger.info("[learning_worker] started")
    last_heartbeat = 0.0
    while not stop:
        now = time.monotonic()
        if now - last_heartbeat >= 30.0:
            try:
                _worker_capability.refresh_and_publish_heartbeat()
            except Exception as exc:
                logger.warning("[learning_worker] capability heartbeat publish failed: {}", exc)
            last_heartbeat = now
        time.sleep(5.0)
    _stop_schedulers()
    _worker_capability.mark_stopped()
    try:
        _worker_capability.publish()
    except Exception as exc:
        logger.warning("[learning_worker] stopped-state publish failed: {}", exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
