# Quant Trading System — CLAUDE.md

## Project Identity

A production-grade algorithmic trading system focused on gold (XAUUSD) futures, with a **Factor Takeover v4** closed-loop architecture — **Phase 0-7 ✅ all complete**:
因子计算 (StreamingFactorEngine) → 三域归一 (SignalNormalizer) → 两层组合 (PortfolioCompositor) → 执行闸门 (ExecutionGate) → 归因 (AttributionEngine) → 权重自适应 (AdaptiveWeightEngine)
取代了旧的 multi_factor_m15 投票策略。cTrader demo 为唯一执行通道，MT5 已完全移除。

## Codebase Map (Factor Takeover v4)

| Layer | Path | Purpose |
|-------|------|---------|
| **Factor Engine** | `alpha/streaming_factor_engine.py` | 流式因子计算，deque buffer 200，每 bar 增量计算 39+ 因子 |
| **Signal Normalizer** | `alpha/signal_normalizer.py` | 三域归一：zscore_tanh / rank_mapping / discrete → [-1, +1] |
| **Portfolio Compositor** | `alpha/portfolio_compositor.py` | Tactical/Macro 两层加权组合，tags_breakdown |
| **Execution Gate** | `alpha/execution_gate.py` | 开仓闸门：信号强度/MACD反向/冷却/NFP事件过滤 |
| **Attribution Engine** | `alpha/attribution_engine.py` | 实盘归因：线性 MC + Gram-Schmidt 正交，NW-HAC Sharpe |
| **Adaptive Weight** | `alpha/adaptive_weight_engine.py` | 权重自适应：exp(k×score)，锚点回归，DSR/健康分退役，复活 |
| **GP Classifier** | `alpha/gp_classifier.py` | AST 表达式 → 类型标签（量价/动量/均值回归/波动率/非线性） |
| Alpha mining | `alpha/search/` | GP/Random search, MAP-Elites, BlendSearch SLSQP |
| **Backend** | `backend/` | FastAPI app, REST/WS, scheduler, live loop |
| **Backtest** | `alpha/backtest/vectorized.py` | 向量化回测引擎 (202K bar 实测) |
| **ML** | `alpha/ml/` | XGBoost 方向预测器, 概念漂移检测 |
| **Features** | `alpha/features/` | FeatureDeriver(200+), PCA/KPCA, FeatureSelector |
| **Data** | `data/` | DuckDB 5 库: ctrader K线, Dukascopy tick, L2订单簿, 开平仓, 事件日历 |
| **Execution** | `execution/` | cTrader bridge, OMS, BaseBrokerBridge, VWAP/TWAP, 执行质量分析 |
| **Risk** | `risk/` | VaR/CVaR, Kelly, 压力测试, 集中度监控, 跨品种协方差 |
| **Platform** | `research/` | ExperimentTracker, FactorLibrary, WeeklyReport |
| **Ops** | `monitor/` | 业务告警(连亏/回撤/熔断, 每tick检查), AutoRecovery(心跳+重启), system_health |
| **Config** | `config/runtime_config.py` | 热更新配置：factor_signal_config, factor_portfolio_weights, awe_* |
| **Frontend** | `frontend-v2/` | React 19/TypeScript UI (Vite + Tailwind), 5 面板 |

## Architecture Flow

```
每根 M5 bar → StreamingFactorEngine.append_bar(bar)
    → SignalNormalizer.normalize(values)           # 39 因子 → [-1, +1]
    → PortfolioCompositor.compose(signals)          # Tactical 70% + Macro 30%
    → ExecutionGate.filter(composite, ...)          # 信号/MACD/冷却/事件
    → market_buy/sell (到 cTrader demo)
    ↓ (平仓时)
    AttributionEngine.record_close(...)             # 线性 MC / Gram-Schmidt
    ↓ (每 30 分钟 / 50 笔交易)
    AdaptiveWeightEngine.adapt(...)                 # NW-HAC Sharpe → exp(k×score)
                                                    # DSR + 健康分三重门控退役
                                                    # 多样性约束 ≤ 40%/类型
```

## Conventions

- **No legacy strategy**: `multi_factor_m15` 已删除，全部由因子管道驱动
- **cTrader 唯一执行通道**: `ctrader_send_orders=True` 默认发单到 demo
- **Data source**: cTrader 为唯一数据源 + 执行通道, Dukascopy 补充 tick 历史
- **Factor lifecycle**: DISCOVERED → SHADOW → ACTIVE（通过 evolution_orchestrator）
- **Factor health**: 5-dimension (mean_abs_ic 40%, ic_stability 20%, regime_consistency 20%, decay_rate 10%, independence 10%)
- **Scheduler**: 8 jobs (evolution_hourly, data_sync, dukascopy_tick, awe_adapt, ml_retrain, feature_eng, ml_drift_check, system_health)
- **Default symbol**: XAUUSD+, timeframe M5
- **Default weights**: 设计文档手拍值, 由 AWE 实盘自适应调优

## Key Config (RuntimeConfig)

- `factor_signal_config` — 每个因子的归一化模式/window/tags
- `factor_portfolio_weights` — 每个因子的初始权重
- `factor_tactical_alpha=0.7` — 战术层权重
- `factor_signal_threshold=0.4` — 开仓信号阈值
- `awe_sensitivity=0.5` / `awe_anchor_pull=0.15` — 自适应参数
- `factor_dry_run=False` — cTrader demo 默认真发单

## Testing

- `pytest tests/ -v` — 454 tests (alpha/backend/risk/research 全模块)
- `pytest tests/alpha/ -v` for Factor Takeover v4 module tests (305 alpha tests)
- `pytest tests/alpha/ -v -k <pattern>` for targeted tests
- Test files mirror source structure: `tests/alpha/`, `tests/execution/`, etc.
- Key test files: `test_streaming_factor_engine.py`, `test_signal_normalizer.py`, `test_portfolio_compositor.py`, `test_execution_gate.py`, `test_attribution_engine.py`, `test_adaptive_weight_engine.py`, `test_gp_classifier.py`

## Audit

- `PROJECT_AUDIT_v14.md` — 2026-06-19 全代码库审计 (196 files, 闭环验证 + bug hunt + 孤儿文件检测)
- 全部 P0 已修复
- `docs/UPGRADE_BLUEPRINT.md` — Phase 0-7 全部完成
- 剩余技术债务: `TODO.md` / 蓝图 Appendix C.1

## Startup

```bash
python start-all.py                    # 开发模式 (后端 :8000 + 前端 :5173)
python start-all.py --refresh-data     # 启动前刷新外部数据 (COT/Events/ETF)
```

## AI Behavior Rules

1. **Before editing a file, read it** — never assume content from memory
2. **Verify before claiming complete** — run the relevant test or command, show evidence
3. **When debugging, use systematic-debugging skill** — never trial-and-error
4. **Check git status before proposing changes** — don't clobber uncommitted work
5. **All new features must integrate into the factor pipeline** — no standalone scripts
6. **Respect alpha/factor_dsl.py AST conventions** — don't invent new expression formats
7. **Use RegistryAdapter for factor registration** — never write directly to SQLite
8. **Memory-first**: Save non-obvious project insights to `memory/` directory
9. **Before touching cTrader bridge**, check if connected (can block threadpool)
10. **Backend runs on FastAPI** — blocking calls go in `run_in_executor` or background tasks
11. **Database is DuckDB** — avoid writes in hot paths, use LIMIT on market data queries
12. **cTrader = 唯一数据源+执行通道**
