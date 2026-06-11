# Self-Evolution Full System 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans

**Goal:** 完整实现自进化量化框架 5 支柱闭环

**Architecture:** 基于 Phase 1 基础设施，先补接线缺失，再依次实现 GP 状态化、OOS 评估、Canary 部署、退役调度、可观测收尾

**Tech Stack:** Python 3.11+, FastAPI, asyncio, apscheduler, prometheus_client

---

## Phase 0: 补接线

### Task 0.1: app.py lifespan 接入结构化日志 + Metrics 钩子

**Files:** Modify `backend/app.py`

```python
from monitor.structured_log import setup_structured_logging
from monitor.metrics import Metrics

def _init_observability():
    try:
        run_id = setup_structured_logging(logging.INFO)
        logger.info("Observability started", extra={"event": "observability_start"})
    except Exception as e:
        logger.warning("Structured logging init failed", extra={"error": str(e)})
    try:
        Metrics.install_into_runtime_state()
    except Exception as e:
        logger.warning("Metrics hook install failed", extra={"error": str(e)})
_init_observability()
```

### Task 0.2: DataQualityGate 接入 sync loop

**Files:** Modify `backend/services/sync_service.py`

在 _do_one_sync bars 插入后添加:
```python
from data.live_sync.quality_gate import DataQualityGate
try:
    report = DataQualityGate().check(symbol, timeframe, df)
    if report.bad_ratio > 0.05:
        logger.warning("Data quality issue", extra={"bad_ratio": report.bad_ratio})
except Exception as e:
    logger.error("Quality check failed", extra={"error": str(e)})
```

### Task 0.3: 运行 Phase 1 测试

```bash
pytest tests/test_loop_host.py tests/test_loop_host_lifecycle.py \
       tests/test_runtime_config.py tests/test_structured_log.py \
       tests/test_sync_health.py tests/test_data_quality_gate.py \
       tests/test_metrics_endpoint.py -v
```

---

## Phase 2.0: RuntimeConfig 订阅 + 指标

### Task 2.0.1: multi_factor_m15.py 接 subscribe

Add to __init__:
```python
from config.runtime_config import shared as rc_shared, subscribe as rc_subscribe
self._config_version = 0
rc_subscribe(self._on_runtime_config_change)
```

### Task 2.0.2: factor_health emit health_score

### Task 2.0.3: ic_tracker emit factor_ic

---

## Phase 2.1: GP 状态化

### Task 2.1.1: EliteArchive
### Task 2.1.2: OperatorRegistry
### Task 2.1.3: MAP-Elites
### Task 2.1.4: strategy_search
### Task 2.1.5: blend_search
### Task 2.1.6: factor_search_gp 重构
### Task 2.1.7: factor_dsl 改注册表

---

## Phase 2.2: OOS 隔离评估

### Task 2.2.1: EvaluationContext
### Task 2.2.2: PurgedWalkForward
### Task 2.2.3: BootstrapCI
### Task 2.2.4: CausalCheck
### Task 2.2.5: Attribution
### Task 2.2.6: factor_health 接 ctx
### Task 2.2.7: registry_adapter retire/unretire
### Task 2.2.8: walkforward 改调

---

## Phase 2.3: Canary

### Task 2.3.1: WeightPolicy
### Task 2.3.2: CanaryDirector
### Task 2.3.3: RiskRebalancer
### Task 2.3.4: multi_factor_m15 vote 段
### Task 2.3.5: promote_shadow wrapper

---

## Phase 2.4: 退役 + Scheduler

### Task 2.4.1: InProcessScheduler
### Task 2.4.2: retirement_check
### Task 2.4.3: retire/unretire 实现
### Task 2.4.4: cron 迁移

---

## Phase 3: 可观测

### Task 3.1-3.5: daily_report, panels, alerts, state snapshot, attribution
