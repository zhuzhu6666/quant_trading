# Quant Trading System — CLAUDE.md

## Project Identity

A production-grade algorithmic trading system focused on gold (XAUUSD) futures, with a **Factor Takeover v4** closed-loop architecture — **Phase 0-7 ✅ all complete**:
因子计算 (StreamingFactorEngine) → 三域归一 (SignalNormalizer) → 两层组合 (PortfolioCompositor) → 执行闸门 (ExecutionGate) → 归因 (AttributionEngine) → 权重自适应 (AdaptiveWeightEngine)
取代了旧的 multi_factor_m15 投票策略。cTrader demo 为唯一执行通道，MT5 已完全移除。

## Codebase Map (Factor Takeover v4)

| Layer | Path | Purpose |
|-------|------|---------|
| **Factor Engine** | `alpha/streaming_factor_engine.py` | 流式因子计算，deque buffer 200，每 bar 增量计算 39+ 因子；shadow 因子被过滤不参与投票 |
| **Signal Normalizer** | `alpha/signal_normalizer.py` | 三域归一：zscore_tanh / rank_mapping / discrete → [-1, +1] |
| **Portfolio Compositor** | `alpha/portfolio_compositor.py` | Tactical/Macro 两层加权组合，tags_breakdown；支持 RuntimeConfig 热更新权重 |
| **Execution Gate** | `alpha/execution_gate.py` | 开仓闸门：信号强度/MACD反向/冷却/NFP事件过滤 |
| **Attribution Engine** | `alpha/attribution_engine.py` | 实盘归因：线性 MC + Gram-Schmidt 正交，NW-HAC Sharpe；快照双写 state.db；接受 cTrader 真实 PnL (gross/swap/commission) |
| **Adaptive Weight** | `alpha/adaptive_weight_engine.py` | 权重自适应：exp(k×score)，锚点回归，DSR/健康分退役，复活；权重历史写入 state.db |
| **GP Classifier** | `alpha/gp_classifier.py` | AST 表达式 → 类型标签（量价/动量/均值回归/波动率/非线性） |
| Alpha mining | `alpha/search/` | GP/Random search, MAP-Elites, BlendSearch SLSQP |
| **Backend** | `backend/` | FastAPI app, REST/WS, scheduler, live loop |
| **Registry Adapter** | `alpha/registry_adapter.py` | 因子注册表适配器 — 单例(shared)，动态 register/unregister/promote/retire，生命周期事件写 state.db |
| **Evolution Orchestrator** | `backend/runtime/evolution_orchestrator.py` | 自进化闭环：GP搜索→影子注册→Canary评估(持久化)→晋升执行→退役检查→权重更新 |
| **Backtest** | `alpha/backtest/vectorized.py` | 向量化回测引擎 (202K bar 实测) |
| **ML** | `alpha/ml/` | XGBoost 方向预测器, 概念漂移检测 |
| **Features** | `alpha/features/` | FeatureDeriver(200+), PCA/KPCA, FeatureSelector；PCA 因子注册为 SOURCE_SHADOW |
| **Data** | `data/` | DuckDB 5 库: ctrader K线, Dukascopy tick, L2订单簿, 开平仓, 事件日历 |
| **State DB** | `data/state.db` | SQLite 统一状态库 (15表): decision_log, lifecycle_events, weight_history, canary_state, factor_health, evolution_events, jobs, attribution_snapshot, param_tune, calibrator, sync_health, strategy_perf, ctrader_deals, live_trades(已废弃) |
| **Execution** | `execution/` | cTrader bridge, OMS, BaseBrokerBridge, VWAP/TWAP, 执行质量分析, deal_sync (成交同步模块) |
| **Risk** | `risk/` | VaR/CVaR, Kelly, 压力测试, 集中度监控, 跨品种协方差 |
| **Platform** | `research/` | ExperimentTracker, WeeklyReport |
| **Ops** | `monitor/` | 业务告警(连亏/回撤/熔断, 每tick检查), AutoRecovery(心跳+重启), system_health |
| **Config** | `config/runtime_config.py` | 热更新配置：factor_signal_config, factor_portfolio_weights, awe_*；支持 subscribe/patch |
| **DB Constants** | `backend/core/db.py` | 统一数据库路径常量 + DDL + 连接管理 |
| **Frontend** | 微信小程序 | WeChat Mini-Program (HTTPS API)，无 Web 前端 |

## Architecture Flow

```
每根 M5 bar → StreamingFactorEngine.append_bar(bar)
    → 过滤 shadow 因子 (仅 BUILTIN + DISCOVERED 参与投票)
    → SignalNormalizer.normalize(values)           # 39 因子 → [-1, +1]
    → PortfolioCompositor.compose(signals)          # Tactical 70% + Macro 30%
    → ExecutionGate.filter(composite, ...)          # 信号/MACD/冷却/事件
    → market_buy/sell (到 cTrader demo)
    ↓ (平仓时)
    live_service 检测 closed_pids → deal_sync 拉 cTrader 真实 PnL (gross/swap/commission)
    → AttributionEngine.record_close(..., real_pnl=...) # 真实 PnL 参与 MC 分解
    ↓ (每 30 分钟 / 50 笔交易)
    AdaptiveWeightEngine.adapt(...)                 # NW-HAC Sharpe → exp(k×score)
    → cfg.patch({"factor_portfolio_weights": ...})  # RuntimeConfig 广播
    → compositor.update_weights(...)                # 热更新，不需重启

Evolution Pipeline (每小时):
    GP搜索 → 注册shadow → Canary评估(持久化state.db) → 晋升执行(adapter.promote)
    → 退役检查 → 退役执行(adapter.retire) → 权重更新(WeightPolicy) → compositor热更新
```

## Conventions

- **No legacy strategy**: `multi_factor_m15` 已删除，全部由因子管道驱动
- **cTrader 唯一执行通道**: `ctrader_send_orders=True` 默认发单到 demo
- **Data source**: cTrader 为唯一数据源 + 执行通道, Dukascopy 补充 tick 历史
- **Factor lifecycle**: SHADOW → (Canary评估) → DISCOVERED → ACTIVE；退役 → DEAD
  - SHADOW: GP 或 PCA 新发现，注册到 factor_registry 但 StreamingFactorEngine 跳过不投票
  - DISCOVERED: 通过 Canary 晋升，开始参与交易
  - DEAD: 健康分过低或持续衰退，adapter.retire() 移除
- **Factor health**: 5-dimension (mean_abs_ic 40%, ic_stability 20%, regime_consistency 20%, decay_rate 10%, independence 10%)
- **Scheduler**: 11 jobs (evolution_hourly, data_sync, dukascopy_tick, events_sync, cot_sync, etf_sync, awe_adapt, ml_retrain, feature_eng, ml_drift_check, system_health)
- **Default symbol**: XAUUSD+, timeframe M5
- **Weight system**: AWE(实盘归因驱动)+WeightPolicy(健康分驱动)→同写 factor_portfolio_weights→compositor 热更新
- **Database**: 所有路径统一在 `backend/core/db.py`；DuckDB 存时序，SQLite(state.db) 存运行时状态
- **RegistryAdapter**: 使用 `RegistryAdapter.shared()` 单例，不要 `RegistryAdapter()`
- **_live_state**: 读写锁 `_LIVE_STATE_LOCK` 保护读-改-写操作
- **include_shadow_factors**: 已删除，影子因子隔离由 StreamingFactorEngine 实现

## Key Config (RuntimeConfig)

- `factor_signal_config` — 每个因子的归一化模式/window/tags
- `factor_portfolio_weights` — 每个因子的初始权重 (AWE 和 WeightPolicy 热更新此字段)
- `factor_tactical_alpha=0.7` — 战术层权重
- `factor_signal_threshold=0.4` — 开仓信号阈值
- `awe_sensitivity=0.5` / `awe_anchor_pull=0.15` — 自适应参数
- `factor_dry_run=False` — cTrader demo 默认真发单

## Database Architecture

```
DuckDB (时序市场数据):
  data/ctrader_data.duckdb — K线 bars + 外部数据(COT/ETF/GVZ)
  data/ticks.duckdb        — Dukascopy Tick
  data/l2.duckdb           — L2 订单簿深度
  data/trades.duckdb       — 交易记录(归因用)
  data/events.duckdb       — 经济事件日历

SQLite (运行时状态):
  data/state.db            — 统一状态库 (15 表, 含 ctrader_deals)
  data/experiments.db      — 实验记录

路径常量: backend/core/db.py (DUCKDB_BARS, STATE_DB, etc.)
旧 DB 备份: analytics.db.bak, decision_log.db.bak
```

## Testing

- `pytest tests/ -v` — 全量测试 (alpha/backend/risk/research 等模块)
- `pytest tests/alpha/ -v` for Factor Takeover v4 module tests
- `pytest tests/alpha/ -v -k <pattern>` for targeted tests
- Test files mirror source structure: `tests/alpha/`, `tests/execution/`, etc.
- Key test files: `test_streaming_factor_engine.py`, `test_signal_normalizer.py`, `test_portfolio_compositor.py`, `test_execution_gate.py`, `test_attribution_engine.py`, `test_adaptive_weight_engine.py`, `test_gp_classifier.py`

## Audit

- 2026-06-22: 全面修复 (进化闭环打通 / 数据库统一 / 权重热更新 / 影子隔离 / 并发锁 / 交易日志统一 / 死代码清理)
- 2026-06-23: 归因真实 PnL 改造 — `record_close` 新增 `real_pnl` 参数, `FactorAttributionStats` 新增 total_gross/swap/commission/net 累加器, `state.db` 新增 `ctrader_deals` 表存原始成交, `execution/deal_sync.py` 同步模块, `live_service` 平仓检测自动调 deal_sync 获取真实 PnL
- `docs/UPGRADE_BLUEPRINT.md` — Phase 0-7 全部完成
- 剩余技术债务: `TODO.md` / 蓝图 Appendix C.1

## Startup

```bash
python -m backend                     # FastAPI :8000
uvicorn backend.app:app --host 0.0.0.0 --port 8000  # 或直接 uvicorn
python scripts/refresh_external_data.py --once        # 刷新外部数据 (COT/Events/ETF)
.venv/bin/python scripts/backfill_ctrader_deals.py --days 30  # 回填历史成交到 ctrader_deals
```

## AI Behavior Rules

1. **Before editing a file, read it** — never assume content from memory
2. **Verify before claiming complete** — run the relevant test or command, show evidence
3. **When debugging, use systematic-debugging skill** — never trial-and-error
4. **Use RegistryAdapter.shared()** — singleton, not RegistryAdapter()
5. **Factor registration goes through RegistryAdapter** — never write directly to factor_registry._factors
6. **Database paths use backend/core/db.py constants** — no hardcoded "data/ctrader_data.duckdb"
7. **State goes to state.db** — don't create new JSON/JSONL files for runtime state
8. **RuntimeConfig.patch for hot updates** — compositor subscribes, no restart needed
9. **Before touching cTrader bridge**, check if connected (can block threadpool)
10. **Backend runs on FastAPI** — blocking calls go in `run_in_executor` or background tasks
11. **cTrader = 唯一数据源+执行通道**
12. **归因使用真实 PnL**: `record_close()` 的 `real_pnl` 参数由 `live_service` 通过 `deal_sync` 自动提供, 包含 gross/swap/commission; `FactorAttributionStats` 累加 `total_gross/swap/commission/net_pnl`; 手工测试需传 `real_pnl={\"gross\":...,\"swap\":...,\"commission\":...,\"net\":...}`
13. **ctrader_deals 表是原始数据锚点**: `execution/deal_sync.py` 负责从 cTrader 拉成交写入 state.db; `find_close_deal` 按 `gross_profit != 0` 判断平仓腿; 回填用 `scripts/backfill_ctrader_deals.py`
14. **_live_state RMW operations MUST use _LIVE_STATE_LOCK**
