# 分层架构映射 (Layer Map)

```
  Interface Layer          backend/ (FastAPI), 微信小程序
       │
  Governance Layer         risk/governor.py, backend/runtime/evolution_kernel.py
       │                   research/evolution_experiment.py, research/model_registry.py
       │                   deployment/canary.py, monitor/system_health.py
       │
  Execution Layer          backend/services/live_service.py
       │                   alpha/execution_gate.py
       │                   execution/ (cTrader bridge, OMS)
       │
  Decision Layer           alpha/decision_policy.py
       │                   alpha/adaptive_weight_engine.py
       │                   deployment/weight_policy.py
       │
  Evaluation Layer         alpha/evaluation/ (result.py, attribution.py, bootstrap_ci.py, ...)
       │                   alpha/attribution_engine.py
       │                   alpha/backtest/vectorized.py
       │                   alpha/shadow_trader.py
       │
  Alpha Layer              alpha/ (streaming_factor_engine, signal_normalizer,
       │                           portfolio_compositor, registry, factor_health,
       │                           ic_tracker, gp_classifier, ml/, features/, search/)
       │
  Data Layer               data/ (quality_gate.py, store.py, live_sync/,
                                   news_cache.py, charts/)
                           config/ (runtime_config.py, settings.yaml)
```

## 现有模块 → 层映射

| 层 | 模块 | 路径 |
|----|------|------|
| **Data** | Bar/Tick 存储 | `data/store.py` |
| | 外部数据 | `data/live_sync/`, `scripts/refresh_external_data.py` |
| | 数据质量门控 | `data/quality_gate.py` |
| | 运行时配置 | `config/runtime_config.py` |
| **Alpha** | 因子引擎 | `alpha/streaming_factor_engine.py` |
| | 信号归一化 | `alpha/signal_normalizer.py` |
| | 组合器 | `alpha/portfolio_compositor.py` |
| | 因子注册表 | `alpha/registry.py`, `alpha/registry_adapter.py` |
| | 因子健康 | `alpha/factor_health.py`, `alpha/ic_tracker.py` |
| | GP 分类器 | `alpha/gp_classifier.py` |
| | ML 预测 | `alpha/ml/` |
| | 特征工程 | `alpha/features/` |
| | 因子搜索 | `alpha/search/` |
| **Evaluation** | 归因引擎 | `alpha/attribution_engine.py` |
| | OOS 评估 | `alpha/evaluation/` (attribution, bootstrap_ci, causal_check, ...) |
| | 统一评价 | `alpha/evaluation/result.py` |
| | 影子交易 | `alpha/shadow_trader.py` |
| | 回测引擎 | `alpha/backtest/vectorized.py` |
| **Decision** | 权重决策 | `alpha/decision_policy.py` |
| | AWE | `alpha/adaptive_weight_engine.py` |
| | WeightPolicy | `deployment/weight_policy.py` |
| | 金丝雀 | `deployment/canary.py` |
| **Execution** | 实盘循环 | `backend/services/live_service.py` |
| | 执行闸门 | `alpha/execution_gate.py` |
| | cTrader 桥 | `execution/ctrader_bridge.py` |
| | OMS | `execution/oms.py` |
| | 执行质量 | `execution/analytics.py` |
| | 风控模块 | `risk/` (var, kelly, circuit, concentration, stress_test, ...) |
| **Governance** | Gov裁决 | `risk/governor.py` |
| | 进化中枢 | `backend/runtime/evolution_kernel.py` |
| | 进化编排 | `backend/runtime/evolution_orchestrator.py` |
| | 实验注册 | `research/evolution_experiment.py` |
| | 模型注册 | `research/model_registry.py` |
| | 系统健康 | `monitor/system_health.py` |
| | 告警 | `monitor/alerter.py` |
| **Interface** | FastAPI | `backend/app.py`, `backend/api/` |
| | WS | `backend/ws/` |
| | Scheduler | `backend/runtime/scheduler.py` |
| | 前端 | `backend/static/`, `miniprogram/` |
