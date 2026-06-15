# Quant Trading — Factor Takeover v4 Alpha Factory

XAUUSD 黄金 M5 量化交易系统，**Factor Takeover v4** 架构：因子计算 → 连续信号 → 组合优化 → 执行 → 归因 → 自适应全闭环。
以因子系统彻底取代旧 `multi_factor_m15` 投票策略。cTrader demo 为唯一执行通道。

**最后更新**: 2026-06-15 | **架构**: Factor Takeover v4 | **Phase 0-7**: ✅ 全部完成

---

## 状态速览

| 维度 | 状态 |
|------|------|
| **Factor Takeover v4** | ✅ Phase 0-7 全部完成（详见 `docs/UPGRADE_BLUEPRINT.md`） |
| **因子库** | 39 builtin + 26 GP DSL shadow/discovered，实时 39 因子参与组合信号 |
| **决策管道** | ✅ StreamingFactorEngine → SignalNormalizer → PortfolioCompositor → ExecutionGate |
| **归因** | ✅ AttributionEngine（线性 MC + Gram-Schmidt 正交 + MTM，NW-HAC Sharpe 三层窗口） |
| **权重自适应** | ✅ AdaptiveWeightEngine（exp(k×score)，DSR+健康分退役，CausalCheck 启用，多样性约束）
| **GP 分类器** | ✅ GPClassifier（AST 表达式 → 类型标签，接入 SignalNormalizer + PortfolioCompositor）
| **cTrader** | ✅ demo 真发单 (Pepperstone, Open API)
| **Web UI** | ✅ Vite + React 19, 5 面板, 权重/归因卡片
| **Scheduler** | ✅ 9 任务 (evolution/canary/retire/sync/data_pull/awe_adapt/ml_retrain/feature_eng/ml_drift)
| **MT5 数据** | ✅ 定时拉 K 线填充 DataStore
| **回测引擎** | ✅ 向量化 (alpha/backtest/vectorized.py, 202K bar 实测)
| **ML 预测** | ✅ XGBoost 方向预测器注册为因子 + 概念漂移检测
| **特征工程** | ✅ FeatureDeriver (200+ 特征) + PCA/KPCA 压缩 + FeatureSelector
| **执行层** | ✅ BaseBrokerBridge 统一接口 (PaperBridge + CTraderBridge), VWAP/TWAP, 执行质量分析
| **风控** | ✅ VaR/CVaR 引擎 + Kelly 仓位 + 压力测试 + 因子暴露集中度监控
| **DuckDB** | ✅ data/duckdb_store.py (列式存储, 向量化查询)
| **Tick 管道** | ✅ data/tick_pipeline/ (MT5→DuckDB→TickBarBuilder)
| **多品种** | ✅ 并行管道 (XAUUSD+ + EURUSD)
| **平台/运维** | ✅ ExperimentTracker + FactorLibrary + WeeklyReport + Docker + AutoRecovery + AlertRules

---

## 快速启动

```bash
cd C:\Users\zhu\quant_trading
python start-all.py              # 后端 :8000 + 前端 :5173
python start-all.py --refresh-data  # 启动前刷新外部数据
```

启动后打开 `http://localhost:5173` → 点"启动 cTrader 实盘"，自动创建因子全管道。

---

## 核心架构

```
每根 M5 bar
    │
    ▼
StreamingFactorEngine.append_bar(bar)     ← 39 因子增量计算 (deque 200)
    │
    ▼
SignalNormalizer.normalize(values)        ← zscore_tanh / rank / discrete → [-1, +1]
    │
    ▼
PortfolioCompositor.compose(signals)      ← Tactical 70% + Macro 30% 两层组合
    │                                         tags_breakdown 标签分解
    ▼
ExecutionGate.filter(composite, ...)      ← 信号强度 / MACD 反向 / 冷却 / NFP 事件
    │
    ▼
market_buy/market_sell                   ← cTrader demo (factor_dry_run=False)
    │
    ▼ (平仓时)
AttributionEngine.record_close(...)       ← 线性 MC / Gram-Schmidt 正交
    │                                        每笔写入 factor_trades.jsonl
    ▼ (每 30 分钟 / 50 笔交易)
AdaptiveWeightEngine.adapt(...)           ← NW-HAC Sharpe → exp(k×score)
                                              锚点回归 + 限幅 [0.1, 3.0]
                                              DSR 多重检验 / 健康分 / CausalCheck 退役
                                              多样性约束 ≤ 40%/类型
                                              写入 factor_weight_history.jsonl
    │
    ▼
RuntimeConfig 热更新 → 下一 tick 生效
```

### Broker 分工
- **cTrader** — 唯一执行通道（Pepperstone demo, Open API）
- **MT5** — 仅数据源（scheduler data_pull 每 10 分钟拉 K 线填 SQLite DataStore）

---

## 审计状态

详见 `PROJECT_AUDIT_v10.md`（2026-06-14，9 P0 + 24 P1 + 18 P2）。全部 P0 及主要 P1 已修复，剩余技术债务见 `TODO.md` 和蓝图 Appendix C.1。

---

## 启动实盘后的自动流程

1. `_run_loop` 创建因子管道（engine→normalizer→compositor→gate→attribution→AWE）
2. 预热 200 根历史 M5 bar → 预热 normalizer 滚动窗口
3. 进入 60s 主循环，每根新 M5 bar 走完因子全闭环
4. Scheduler 9 个后台任务并行运转

---

## 一键切换 / 回退

- `python start-all.py` — 正常启动
- cTrader demo 虚拟钱，默认真发单
- 无回退（旧策略已删除）

---

## 关键配置 (RuntimeConfig)

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `factor_tactical_alpha` | 0.7 | 战术层权重 |
| `factor_signal_threshold` | 0.4 | 开仓信号阈值 |
| `awe_sensitivity` | 0.5 | 自适应敏感度 |
| `awe_anchor_pull` | 0.15 | 锚点回归系数 |
| `factor_dry_run` | False | 是否只算信号不下单 |
| `ctrader_send_orders` | True | 是否发单到 cTrader |

---

## 数据持久化

| 文件 | 说明 |
|------|------|
| `data/charts/factor_trades.jsonl` | 逐笔归因明细 |
| `data/charts/factor_weight_history.jsonl` | 权重变更记录 |
| `data/charts/factor_attribution.json` | 归因快照（原子更新，重启恢复） |
| `data/market_data.duckdb` | DuckDB 列式存储（13 表，结构化市场数据） |

---

## 技术栈

- **后端**: Python 3.11, FastAPI, uvicorn
- **前端**: React 19, TypeScript, Vite, Tailwind CSS
- **因子计算**: numpy, pandas, scipy, xgboost, lightgbm
- **执行**: cTrader Open API (Twisted), BaseBrokerBridge 统一接口
- **数据**: DuckDB (market_data.duckdb) + SQLite (market_data.db), MT5 数据拉取
- **调度**: APScheduler / threading.Timer
- **回测**: 向量化引擎 (alpha/backtest/vectorized.py)
- **风控**: VaR/CVaR, Kelly, 压力测试, 集中度监控
- **ML**: XGBoost 方向预测器, 概念漂移检测
- **特征工程**: FeatureDeriver, PCA/KPCA, FeatureSelector
- **平台**: ExperimentTracker, FactorLibrary, WeeklyReport, Docker, AutoRecovery
- **测试**: pytest 497 tests
