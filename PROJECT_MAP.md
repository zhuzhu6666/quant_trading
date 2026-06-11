# 项目框架总览 (PROJECT_MAP)

> 最后更新: 2026-06-11
> 项目状态: 39 因子 + 因子自进化全闭环 + cTrader 开平仓/SLTP 全流通过
> 当前 HEAD: daa7f54 (自进化闭环 + cTrader bridge 修复 + 因子评估框架)

---

## 1. 项目目录结构

```
quant_trading/
├── start-all.py                     # ★ 一键启动入口 (替代旧 8 个 start/stop 脚本)
├── main.py                          # CLI 入口 (backtest/paper/live/dashboard)
├── docs/planning/ROADMAP.md         # 路线图 (单源待办)
├── README.md                        # 用户文档
├── PROJECT_MAP.md                   # 本文件 — 完整索引
├── requirements.txt                 # Python 3.11+ 依赖
│
├── core/                            # 基础设施 (5 文件)
│   ├── clock.py                     # 时间 / 计时
│   ├── event_bus.py                 # 进程内事件总线
│   └── state.py                     # StateContainer 多账户
│
├── data/                            # 数据层 (10 文件)
│   ├── store.py                     # DataStore — SQLite 读写
│   ├── feed.py                      # bar 流喂入
│   ├── bar_builder.py               # tick → bar
│   ├── tick_generator.py            # Brownian bridge tick 生成
│   ├── tick_receiver.py             # 实盘 tick 接收
│   ├── news_cache.py                # 事件日历 / GVZ 读
│   ├── external_loader.py           # P0-3: 跨资产/事件/ETF 对齐
│   └── live_sync/                   # ★ T16: 实时数据同步
│       ├── mt5_puller.py            # MT5 bar 拉取
│       ├── bar_filter.py            # 去重 + 跳过 + 完整性检查
│       ├── db_inserter.py           # DataStore 包装 + 重试 + 状态持久化
│       ├── orchestrator.py          # full_sync / incremental_sync
│       └── daemon.py                # 后台守护进程
│
├── db/                              # analytics 库 (2 文件)
│   ├── schema.py                    # strategy_perf / decision_log DDL
│   └── store.py                     # AnalyticsStore
│
├── alpha/                           # 因子/ML/校准/DSL/评估 (25+ 文件)
│   ├── registry.py                  # 39 因子注册
│   ├── factor_engine.py             # 流式因子计算 + IC 分析
│   ├── ic_tracker.py                # 滚动 IC 追踪
│   ├── factor_attribution.py        # 边际 IC 归因
│   ├── regime_classifier.py         # sklearn LogReg
│   ├── probability_calibrator.py    # 桶级 + Platt 校准
│   ├── factor_health.py             # ★ T14.1: 5 维健康评分
│   ├── registry_adapter.py          # ★ T14.2: 动态 register/unregister
│   ├── persistent_registry.py       # ★ T15.5: 跨进程恢复 shadow
│   ├── factor_dsl.py                # ★ T15.1: DSL parser + AST + 20+ 算子
│   ├── factor_search_gp.py          # ★ T15.3: GP 引擎
│   ├── factor_score_evaluator.py    # ★ T15.2: DSL 候选 IC 评分
│   ├── factor_search.py             # ★ T15.3: 随机搜索
│   ├── factor_discovery.py          # ★ T15.4: orchestrator
│   ├── search/                      # ★ 搜索框架 (OperatorRegistry + StrategySearch + BlendSearch)
│   │   ├── __init__.py
│   │   ├── operator_registry.py
│   │   ├── strategy_search.py
│   │   └── blend_search.py
│   └── evaluation/                  # ★ 新: 因子评估框架
│       ├── __init__.py
│       ├── attribution.py           # 收益归因
│       ├── bootstrap_ci.py          # 自助法置信区间
│       ├── causal_check.py          # 因果检验
│       ├── evaluation_context.py    # 评估上下文
│       └── purged_walkforward.py    # 清洗前向验证
│
├── factors/                         # 老接口因子 (4 文件)
├── strategies/                      # 7 交易策略
│   ├── multi_factor_m15.py          # ★ 主策略 (含 shadow 因子投票)
│   ├── ma_cross_h4.py / macd_bb.py / gold_momentum.py
│   ├── trend_following.py / mean_reversion.py / breakout.py
│
├── strategy/                        # 策略框架 (9 文件)
│   ├── base.py / signal_bus.py / registry.py / portfolio.py
│   ├── mab_router.py                # ★ MAB Thompson sampling
│   ├── scheduler.py / scorer.py / retrain_scheduler.py
│
├── execution/                       # 14 文件
│   ├── oms.py                       # Order 状态机
│   ├── router.py                    # ExecutionRouter
│   ├── paper_engine.py              # PaperEngine
│   ├── paper_trader.py              # ★ PaperTrader
│   ├── _sharpe.py                   # Sharpe log returns + NW HAC
│   ├── mt5_bridge.py                # MT5 (仅数据源, 不交易)
│   ├── ctrader_bridge.py            # ★ cTrader Open API 桥接 (2026-06-11 修复 5 bug)
│   ├── algos.py                     # TWAP/VWAP/POV/IS
│   ├── mab_paper_runner.py          # MAB 多策略 paper
│   ├── event_filter.py              # SharedEventFilter
│   ├── slippage.py / market_impact.py / match_replay.py
│   ├── latency.py / order_retry.py
│
├── backend/                         # ★ Web 总控台后端 (FastAPI, 43 REST + 1 WS)
│   ├── app.py                       # FastAPI app + lifespan
│   ├── api/                         # 路由
│   ├── services/
│   │   ├── live_service.py          # ★ 实盘管理
│   │   └── paper_service.py         # ★ 模拟盘
│   └── runtime/
│       ├── evolution_orchestrator.py # ★ 自进化编排器
│       └── scheduler.py             # ★ InProcessScheduler (5-job cron)
│
├── frontend-v2/                     # ★ Web 总控台前端 (Vite + React 19 + Tailwind)
│   └── src/
│       ├── pages/MainDashboard.tsx
│       ├── components/panels/
│       │   ├── TradingPanel.tsx / FactorsPanel.tsx
│       │   ├── ExperimentsPanel.tsx / DataPanel.tsx / SystemPanel.tsx
│       └── ...
│
├── deployment/                      # ★ 新: 部署层
│   ├── __init__.py
│   ├── canary.py                    # ★ 金丝雀部署
│   ├── risk_rebalancer.py           # ★ 风险重平衡
│   └── weight_policy.py             # ★ 权重策略
│
├── risk/                            # 4 文件
│   ├── circuit.py                   # CircuitBreaker
│   ├── pre_trade.py                 # 前置风控
│   ├── position.py                  # 持仓监控
│   └── regime.py                    # Regime 标签
│
├── monitor/                         # 6+ 文件
│   ├── alerter.py / alerts.py / dashboard.py
│   └── evolution_story/             # ★ 新: 自进化事件日志
│       ├── __init__.py
│       └── report.py
│   └── panels/                      # ★ 新: 监控面板
│       ├── __init__.py
│       └── overview.py
│   └── prometheus_alerts.yaml       # ★ 新: 告警规则
│
├── scripts/                         # 30+ 脚本
│   ├── ctrader_poc.py / ctrader_oauth.py       # cTrader 工具
│   ├── test_ctrader_full_flow.py               # ★ 新: cTrader 全流测试
│   ├── validate_ctrader_token.py               # Token 验证
│   ├── live_sync.py                             # T16 数据同步
│   ├── discover_factors.py                      # L2 因子发现
│   └── ... (各种测试/工具脚本)
│
├── tests/                           # ★ 新: 正式测试目录
│   ├── alpha/evaluation/            # 评估框架测试 (5 文件)
│   └── deployment/                  # 部署测试
│
├── config/                          # settings.yaml + instruments.yaml
└── data/charts/                     # 落盘报告
```

---

## 2. 关键路径

### 启动
- **开发模式**: `python start-all.py` → 后 :8000 + 前 :5173 + 自动开浏览器
- **回测**: `python main.py --mode backtest`
- **模拟盘**: `python main.py --mode paper`
- **cTrader PoC**: `python scripts/ctrader_poc.py`

### 因子自进化 (闭环)
- 编排: `backend/runtime/evolution_orchestrator.py` → `GP→OOS→Canary→WeightPolicy→Retire`
- 调度: `backend/runtime/scheduler.py` (5 cron jobs: evolution_hourly, canary_fast, retire_hourly, sync_health, data_pull)
- 部署: `deployment/` (canary, weight_policy, risk_rebalancer)
- 可观测: `monitor/evolution_story/` + `monitor/panels/`

### cTrader 执行
- 桥接: `execution/ctrader_bridge.py`
- 注意: ProtoOAPosition.symbolId 在 `tradeData` 里; price 是 float 不除 digits
- broker: demo.ctraderapi.com:5035, account=47276606 (JPY 1000)
- 全流测试 `scripts/test_ctrader_full_flow.py`: market_buy → amend SLTP → close_position 已验证通过

### Web 总控台
- 后端: `backend/` (FastAPI, 43 REST + 1 WS)
- 前端: `frontend-v2/` (Vite + React 19 + Tailwind 3)
- 用户手册: `docs/user-guide/README.md`

---

## 3. 真状态数字

### PnL 对比 (multi_factor_m15, M15, 50K bar)

| 路径 | PnL | Trades | Sharpe | DD |
|---|---|---|---|---|
| baseline (无风控) | +407.51% | 738 | 1.807 | 39.77% |
| MAB T1-T13 全栈 | +120.75% | 639 | 0.894 | 64% |
| 调参后 (risk=1%, CB=15%) | +59.17% | 354 | 0.936 | 15.9% |

### 因子健康 (2026-06-06): 2 HEALTHY + 45 WATCH + 18 DECAYING
- HEALTHY: gld_tonnes_zscore_60d (95.2), cot_mm_net_pct_oi (83.8)
- 报告: `data/charts/factor_health_report.{txt,json}`

### 账户
- cTrader demo: 47276606 @ Pepperstone, 1000 JPY
- MT5: 9823690 @ Bybit-Live-2, balance=0 (阻塞)

---

## 4. 文档清单

| 文档 | 用途 |
|---|---|
| `README.md` | 用户文档, 安装 + 运行 |
| `docs/planning/ROADMAP.md` | 路线图 (单源待办) |
| `docs/planning/self-evolution-system.md` | 自进化设计文档 |
| `docs/CTRADER_INTEGRATION.md` | cTrader 接入设计 |
| `docs/startup.md` | 启动指南 |
| `docs/user-guide/README.md` | Web Console 用户指南 |
| `docs/frontend-architecture.md` | 前端架构 |
| `docs/design/product-spec.md` | 产品定位 |
| `docs/design/ui-design-tokens.md` | UI 设计 token |
| `docs/REALTIME_PAPER_DESIGN.md` | 实盘设计 draft |
| `PROJECT_MAP.md` | 本文件 |

---

**最后扫描**: 2026-06-11
**代码量**: ~10000+ 行 (核心路径 + 自进化 + cTrader + 评估框架)
**HEAD**: daa7f54
