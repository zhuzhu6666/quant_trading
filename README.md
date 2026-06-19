# Quant Trading — Factor Takeover v4 Alpha Factory

XAUUSD 黄金 M5 量化交易系统，**Factor Takeover v4** 架构：因子计算 → 连续信号 → 组合优化 → 执行 → 归因 → 自适应全闭环。
以因子系统彻底取代旧 `multi_factor_m15` 投票策略。cTrader demo 为唯一执行通道。

**最后更新**: 2026-06-19 | **架构**: Factor Takeover v4 | **Phase 0-7**: ✅ 全部完成

---

## 状态速览

| 维度 | 状态 |
|------|------|
| **Factor Takeover v4** | ✅ Phase 0-7 全部完成 |
| **因子库** | 39 builtin + GP 动态发现，实时参与组合信号 |
| **决策管道** | ✅ StreamingFactorEngine → SignalNormalizer → PortfolioCompositor → ExecutionGate |
| **归因** | ✅ AttributionEngine（线性 MC + Gram-Schmidt 正交 + MTM，NW-HAC Sharpe） |
| **权重自适应** | ✅ AdaptiveWeightEngine（exp(k×score)，DSR+健康分+CausalCheck 三重退役，多样性约束） |
| **cTrader** | ✅ demo 真发单 (Pepperstone, Open API) |
| **Web UI** | ✅ React 19 + Vite + Tailwind，三栏布局 + 日志全宽 |
| **Scheduler** | ✅ 8 任务，全自主运行 |
| **回测引擎** | ✅ 向量化 (alpha/backtest/vectorized.py, 202K bar 实测) |
| **ML 预测** | ✅ XGBoost 方向预测器注册为因子 + 概念漂移检测 |
| **特征工程** | ✅ FeatureDeriver (200+) + PCA/KPCA 压缩 + FeatureSelector |
| **执行层** | ✅ CTraderBridge, VWAP/TWAP, 执行质量分析 |
| **风控** | ✅ VaR/CVaR + Kelly + 压力测试 + 集中度监控 |
| **业务告警** | ✅ 连亏/回撤/熔断 每 tick 检查 → logs/alerts.log |
| **Tick 数据** | ✅ Dukascopy (~65M ticks) 每小时增量 |
| **L2 订单簿** | ✅ cTrader depth event 实时入库 |
| **开平仓记录** | ✅ trades.duckdb 自动记录 |
| **事件日历** | ✅ events.duckdb (NFP/FOMC/CPI) |

---

## 数据架构

```
data/
├── ctrader_data.duckdb   K线主库 (cTrader M5/M15/M30/H1/D1)
├── ticks.duckdb           Dukascopy tick (~65M bid/ask/volume)
├── l2.duckdb              L2 订单簿 (实时 depth event)
├── trades.duckdb          开平仓记录 (归因引擎自动写入)
└── events.duckdb          事件日历 (NFP/FOMC/CPI)
```

---

## 快速启动

```bash
cd C:\Users\zhu\quant_trading
python start-all.py              # 后端 :8000 + 前端 :5173
python start-all.py --refresh-data  # 启动前刷新外部数据
```

启动后打开 `http://localhost:5173` → 点「启动 cTrader 实盘」，自动创建因子全管道。

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
    ▼ (每 30 分钟)
AdaptiveWeightEngine.adapt(...)           ← NW-HAC Sharpe → exp(k×score)
                                              锚点回归 + 限幅 [0.1, 3.0]
                                              DSR + 健康分 + CausalCheck 三重退役
                                              多样性约束 ≤ 40%/类型
    │
    ▼
RuntimeConfig 热更新 → 下一 tick 生效
```

---

## Scheduler 任务 (8 jobs)

| 任务 | 频率 | 说明 |
|------|------|------|
| evolution_hourly | 每小时 | GP搜索 + OOS评估 + Canary晋升 + 退役 |
| data_sync | 每5分钟 | 检查各周期数据新鲜度，有缺口才补 |
| dukascopy_tick | 每小时 | Dukascopy tick 增量拉取 |
| awe_adapt | 每30分钟 | AWE 权重自适应 |
| ml_retrain | 每周日5am | XGBoost 方向预测器重训 |
| feature_eng | 每天3am | 特征衍生 + PCA压缩 + 特征筛选 |
| ml_drift_check | 每6小时 | ML 因子概念漂移检测 |
| system_health | 每分钟 | 桥/数据/调度器/磁盘/内存健康检查 |

---

## 启动实盘后的自动流程

1. `_run_loop` 创建因子管道（engine→normalizer→compositor→gate→attribution→AWE）
2. 预热 200 根历史 M5 bar → 预热 normalizer 滚动窗口
3. 进入 60s 主循环，每根新 M5 bar 走完因子全闭环
4. Scheduler 8 个后台任务并行运转
5. 业务告警每 tick 检查（连亏/回撤/熔断）

---

## 关键配置 (RuntimeConfig)

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `factor_tactical_alpha` | 0.7 | 战术层权重 |
| `factor_signal_threshold` | 0.3 | 开仓信号阈值 |
| `awe_sensitivity` | 0.5 | 自适应敏感度 |
| `awe_anchor_pull` | 0.15 | 锚点回归系数 |
| `factor_dry_run` | False | 是否只算信号不下单 |
| `ctrader_send_orders` | True | 是否发单到 cTrader |

---

## 审计

详见 `PROJECT_AUDIT_v14.md`（2026-06-19，P0 全部修复，454 tests pass）。

---

## 技术栈

- **后端**: Python 3.11, FastAPI, uvicorn
- **前端**: React 19, TypeScript, Vite, Tailwind CSS
- **因子计算**: numpy, pandas, scipy, xgboost
- **执行**: cTrader Open API (Twisted)
- **数据**: DuckDB (5 库: K线/tick/L2/开平仓/事件)
- **调度**: APScheduler / threading.Timer (InProcessScheduler)
- **回测**: 向量化引擎 (alpha/backtest/vectorized.py)
- **风控**: VaR/CVaR, Kelly, 压力测试, 集中度监控
- **ML**: XGBoost 方向预测器, 概念漂移检测
- **特征工程**: FeatureDeriver (200+), PCA/KPCA, FeatureSelector
- **平台**: ExperimentTracker, FactorLibrary, WeeklyReport, Docker, AutoRecovery
- **测试**: pytest 454 tests
