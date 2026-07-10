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
import time
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loguru import logger


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


def _bootstrap_runtime() -> None:
    from backend.core.logging import setup_logging

    setup_logging()
    try:
        from backend.core.db import init_all

        init_all()
        logger.info("[learning_worker] databases initialized")
    except Exception as exc:
        logger.warning("[learning_worker] db init failed: {}", exc)

    try:
        from backend.services.runtime_config_startup import (
            load_yaml_runtime_config,
            restore_runtime_config_on_startup,
        )

        base_cfg, _yaml_cfg = load_yaml_runtime_config()
        try:
            restored = restore_runtime_config_on_startup(
                base_cfg,
                snapshot_source="learning_worker_startup",
            )
            overlay = restored.get("overlay") or {}
            if overlay.get("restored"):
                logger.info(
                    "[learning_worker] RuntimeConfig autonomous overlay restored hash={}",
                    overlay.get("overlay_hash", ""),
                )
        except Exception as restore_exc:
            from config import runtime_config as _runtime_config

            _runtime_config.replace(base_cfg)
            logger.warning("[learning_worker] RuntimeConfig overlay restore failed, using YAML base: {}", restore_exc)
        logger.info("[learning_worker] RuntimeConfig loaded")
    except Exception as exc:
        logger.warning("[learning_worker] RuntimeConfig load failed: {}", exc)


def _add_job(scheduler, name: str, cron_expr: str, fn: Callable[[], object]) -> None:
    if scheduler.add_job(name, cron_expr, fn):
        logger.info("[learning_worker] scheduled {} ({})", name, cron_expr)


def _register_heavy_jobs(*, include_system_health: bool) -> None:
    from backend.runtime.evolution_orchestrator import scheduled_evolution_cycle
    from backend.runtime.factor_governance_orchestrator import run_autonomous_factor_governance_cycle
    from backend.runtime.scheduler import InProcessScheduler
    from backend.services.autonomous_evolution_runner import AutonomousEvolutionNurseryRunner
    from backend.services.evolution_work_coordinator import coordinated_job
    from config.runtime_config import shared as _runtime_shared
    from backend.services.live_service import (
        _scheduled_feature_engineering,
        _scheduled_offmarket_position_quality_lightgbm,
    )

    scheduler = InProcessScheduler()
    _add_job(
        scheduler,
        "evolution_hourly",
        "2 * * * *",
        coordinated_job("evolution_hourly", scheduled_evolution_cycle),
    )
    governance_cron = str(getattr(_runtime_shared(), "factor_governance_cron", "*/15 * * * *") or "*/15 * * * *")
    _add_job(
        scheduler,
        "factor_governance_autonomous",
        governance_cron,
        coordinated_job("factor_governance_autonomous", run_autonomous_factor_governance_cycle),
    )
    if _env_enabled("QUANT_AUTONOMOUS_EVOLUTION_NURSERY_RUNNER", "1"):
        nursery_cron = str(getattr(_runtime_shared(), "autonomous_evolution_nursery_cron", "7,22,37,52 * * * *") or "7,22,37,52 * * * *")

        def _run_nursery_cycle() -> None:
            result = AutonomousEvolutionNurseryRunner().run_once(
                replay_if_stale=False,
                apply_when_ready=False,
                consume_recommended_step=_env_enabled("QUANT_AUTONOMOUS_EVOLUTION_NURSERY_CONSUME_STEP", "1"),
                recommended_step_limit=int(os.getenv("QUANT_AUTONOMOUS_EVOLUTION_NURSERY_STEP_LIMIT", "1") or "1"),
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
            coordinated_job("autonomous_evolution_nursery", _run_nursery_cycle),
        )
    _add_job(
        scheduler,
        "feature_eng",
        "0 3 * * *",
        coordinated_job("feature_eng", _scheduled_feature_engineering),
    )
    _add_job(
        scheduler,
        "offmarket_position_quality_lightgbm",
        "20 * * * *",
        coordinated_job(
            "offmarket_position_quality_lightgbm",
            _scheduled_offmarket_position_quality_lightgbm,
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
    from backend.services.autonomous_learning import schedule_autonomous_learning
    from backend.services.learning_backfill import schedule_learning_backfill
    from backend.services.supervisor_learning_scheduler import schedule_supervisor_learning

    schedule_learning_backfill(delay_sec=30.0, limit=100, allow_partial=False, rebuild_learning=True)
    schedule_supervisor_learning(delay_sec=60.0, interval_sec=1800.0, limit=200)
    schedule_autonomous_learning(delay_sec=90.0, interval_sec=1800.0, sample_limit=500, recommendation_limit=20)
    logger.info("[learning_worker] learning schedulers started")


def _stop_schedulers() -> None:
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


def _run_once() -> None:
    from backend.runtime.evolution_orchestrator import scheduled_evolution_cycle
    from backend.runtime.factor_governance_orchestrator import run_autonomous_factor_governance_cycle
    from backend.services.autonomous_evolution_runner import AutonomousEvolutionNurseryRunner
    from backend.services.autonomous_learning import run_autonomous_learning_cycle
    from backend.services.supervisor_learning_scheduler import run_supervisor_learning_cycle

    logger.info("[learning_worker] run-once supervisor learning")
    logger.info("[learning_worker] supervisor result: {}", run_supervisor_learning_cycle(limit=200))
    logger.info("[learning_worker] run-once autonomous learning")
    logger.info("[learning_worker] autonomous result: {}", run_autonomous_learning_cycle(sample_limit=500, recommendation_limit=20))
    logger.info("[learning_worker] run-once evolution cycle")
    report = scheduled_evolution_cycle()
    logger.info("[learning_worker] evolution result: {}", report.to_dict() if hasattr(report, "to_dict") else report)
    logger.info("[learning_worker] run-once factor governance")
    logger.info("[learning_worker] factor governance result: {}", run_autonomous_factor_governance_cycle())
    logger.info("[learning_worker] run-once autonomous evolution nursery")
    logger.info("[learning_worker] autonomous evolution nursery result: {}", AutonomousEvolutionNurseryRunner().run_once())


def main() -> int:
    parser = argparse.ArgumentParser(description="Run isolated learning/evolution schedulers.")
    parser.add_argument("--run-once", action="store_true", help="Run one supervisor/autonomous/evolution cycle and exit.")
    parser.add_argument("--no-learning-schedulers", action="store_true", help="Only schedule heavy evolution/training jobs.")
    parser.add_argument("--with-system-health", action="store_true", help="Register worker-local system health. Usually leave this off because live_loop and cTrader live in quant-backend.service.")
    args = parser.parse_args()

    _apply_cpu_affinity(os.getenv("QUANT_LEARNING_WORKER_CPU_AFFINITY", ""))
    _bootstrap_runtime()

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
    if not args.no_learning_schedulers and _env_enabled("QUANT_WORKER_LEARNING_SCHEDULERS", "1"):
        _start_learning_schedulers()
    else:
        logger.info("[learning_worker] learning schedulers disabled")

    logger.info("[learning_worker] started")
    while not stop:
        time.sleep(5.0)
    _stop_schedulers()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
