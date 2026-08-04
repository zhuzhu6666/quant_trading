"""backend/runtime/evolution_kernel.py — 自进化中枢 (v1).

职责: 将 evolution、AWE、DecisionPolicy、QualityGate、Governor
从分散的定时任务集中到一个类管理。

Design:
  EvolutionKernel 是单例, 由 live_service 在启动时初始化。
  内部:
    - 创建 InProcessScheduler
    - 注册重型演化相关 cron jobs (AWE 由持有 live pipeline 的 backend 管理)
    - 每次进化 cycle 跑 QualityGate → Governor → evolution → DecisionPolicy
"""

from __future__ import annotations

import logging
import threading
import time as _time
from typing import Any, Callable

logger = logging.getLogger(__name__)


class EvolutionKernel:
    """自进化中枢单例.

    用法:
        kernel = EvolutionKernel.shared()
        kernel.start()
        # ...
        kernel.stop()
    """

    _instance: EvolutionKernel | None = None
    _lock = threading.Lock()

    def __init__(self):
        self._scheduler = None
        self._started = False
        self._pipeline_ref: dict | None = None

    @classmethod
    def shared(cls) -> EvolutionKernel:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls._instance = None

    # ── 生命周期 ────────────────────────────────────────────────────

    def start(self) -> None:
        """启动自进化中枢: 创建 scheduler + 注册 jobs."""
        if self._started:
            return
        try:
            from backend.runtime.scheduler import InProcessScheduler
            self._scheduler = InProcessScheduler()
            self._register_jobs()
            self._started = True
            logger.info("[EvolutionKernel] started with %d jobs",
                        len(self._scheduler._jobs) if hasattr(self._scheduler, '_jobs') else 0)
        except Exception as e:
            logger.warning("[EvolutionKernel] start failed: %s", e)

    def stop(self) -> None:
        """停止 scheduler."""
        self._started = False
        if self._scheduler:
            try:
                self._scheduler.stop()
            except Exception:
                pass
        logger.info("[EvolutionKernel] stopped")

    def set_pipeline(self, pipeline: dict | None) -> None:
        self._pipeline_ref = pipeline

    # ── Job 注册 ────────────────────────────────────────────────────

    def _register_jobs(self) -> None:
        """注册演化相关的 cron jobs (evolution_hourly, factor_governance, system_health).

        注意: InProcessScheduler 是单例, 此处注册的 job 与
        live_service._start_live_scheduler 中的数据 job 共享同一调度器.
        """
        sched = self._scheduler
        if sched is None:
            return

        # 每小时: 完整自进化循环 (GP + OOS + Canary + 退役 + 权重)
        from backend.runtime.evolution_orchestrator import (
            scheduled_evolution_with_governance_handoff,
        )
        from backend.services.evolution_work_coordinator import coordinated_job

        sched.add_job(
            "evolution_hourly",
            "2 * * * *",
            coordinated_job(
                "evolution_hourly",
                scheduled_evolution_with_governance_handoff,
            ),
        )

        # 非整点的 15 分钟节拍: 因子 V3 自治治理。整点由 minute 2
        # 的完整 health -> V16 -> governance 链拥有，避免 single-flight 抢占。
        try:
            from backend.runtime.factor_governance_orchestrator import run_autonomous_factor_governance_cycle
            from config.runtime_config import effective_factor_governance_cron

            cron = effective_factor_governance_cron()
            sched.add_job(
                "factor_governance_autonomous",
                cron,
                coordinated_job(
                    "factor_governance_autonomous",
                    run_autonomous_factor_governance_cycle,
                ),
            )
        except Exception as e:
            logger.warning("[EvolutionKernel] factor_governance registration failed: %s", e)

        # 每分钟: 系统总健康检查 (桥/数据/调度器/磁盘/内存)
        try:
            from monitor.system_health import shared as _sh_shared
            from monitor.alerter import Alerter
            _sys_health = _sh_shared()
            _sys_health.set_alerter(Alerter({
                "log_file": "logs/alerts.log",
                "min_level": "WARNING",
            }).send)
            sched.add_job("system_health", "* * * * *", _sys_health.run)
            logger.info("[EvolutionKernel] registered system_health job")
        except Exception as e:
            logger.warning("[EvolutionKernel] system_health registration failed: %s", e)

    def run_full_cycle(self, **kwargs) -> Any:
        """运行一次完整自进化循环 (含 QualityGate + Governor 检查)."""
        from backend.runtime.evolution_orchestrator import scheduled_evolution_cycle, EvolutionReport

        # QualityGate
        try:
            from data.quality_gate import run_quality_gate, evolution_guard
            report = run_quality_gate(
                symbol=kwargs.get("symbol", "XAUUSD+"),
            )
            if not evolution_guard(report):
                err_report = EvolutionReport()
                err_report.error = f"QualityGate blocked: {report.detail}"
                return err_report
        except Exception as e:
            logger.debug("[EvolutionKernel] quality gate skipped: %s", e)

        # Governor: allow_promotion / allow_new_factor
        try:
            from risk.governor import RiskGovernor, GovernorState
            gov = RiskGovernor.shared()
            prom_verdict = gov.allow_promotion()
            if not prom_verdict.allowed:
                logger.info("[EvolutionKernel] promotion blocked by Governor: %s", prom_verdict.reason)
        except Exception:
            pass

        # 运行 evolution cycle
        return scheduled_evolution_cycle(**kwargs)

    @property
    def is_running(self) -> bool:
        return self._started
