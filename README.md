# Python 量化交易框架

XAUUSD+ 黄金 M15 趋势/回归/因子合成, 7 层架构, 本地 paper + backtest baseline 已实盘验证 (read-only 模式)。

**最后更新: 2026-06-02**

---

## 当前状态 (2026-06-02 验证)

### 代码层进度: 41/41 (P0+P1+P3) + T1-T16 集成层

- ✅ **P0 (1-7)**: 因子库 22 个 / PCA / IC 监控 / XGBoost / Walk-Forward / 元学习
- ✅ **P1 (A/B/C/D/E/F)**: MT5 整合 / 智能路由 / 数据拉取 / 影子 / A/B / 紧急平仓
- ✅ **P3** circuit 调优: 5% → 10% 默认
- ✅ **T1-T13 集成层**: MAB 多策略 + SelfLearningScheduler + WeightedScorer + ProbabilityCalibrator + MetaLearnerMonitor + FactorMonitor + Alerter + RetrainScheduler + SharedEventFilter (T13 业务关键)
- ✅ **T14 L1 因子生命周期**: FactorHealth 5 维评分 + RegistryAdapter 动态 register/unregister
- ✅ **T15 L2 因子 DSL**: parser + AST + 20+ 算子 + 搜索 + 自动发现 + 持久化
- ✅ **T16 实时数据同步**: MT5 → db 正增长 (增量拉取 + 多 timeframe + Windows Task Scheduler)

### PnL 数字

| 路径 | PnL | Trades | Sharpe | DD | 备注 |
|---|---|---|---|---|---|
| **main.py baseline** | **+407.51%** | 738 | 1.807 | 39.77% | 单策略 + 事件 skip + circuit 关闭 |
| **MAB T1-T10** (无 T13) | +20.53% | 841 | -0.436 | 169.11% | 4 策略共享, 无事件 skip → OOH 跳爆仓 |
| **MAB T1-T10 + T13** | **+120.75%** | 639 | **0.894** | 64% | SharedEventFilter 是业务层关键修复 |
| MAB + circuit 10% | -34.47% | 54 | -0.838 | 38.38% | T13 后 circuit 冗余, 阻止开仓 |
| mab_paper 修后 | +380.58% | 596 | 1.452 | — | 老脚本, MAB router + 事件 skip |
| paper w/ circuit 10% | -9.54% | 123 | -0.105 | — | P3 调优, baseline 路径 |

### MT5 真值 (2026-06-02 验证)

- 账户: 9823690 / Bybit-Live-2 / **leverage=500x**
- XAUUSD+: **contract_size=100 oz/lot**, volume_min=0.01, step=0.01
- 0.01 lot = **1 oz**, 3 ATR SL ≈ **$25 = 5% 账户** (跟 P0 原则一致)
- 当前金价: **4529 USD/oz**, ATR14 mean ≈ **$8.42** (M15)
- MT5 账户 balance=0, **不能 live trade**, 全 read+paper 模式

### 因子健康 (T14.1 评估)

22 builtin 因子在 50K M15 bar 上的健康分: **0 HEALTHY / 2 WATCH / 20 DECAYING**
- WATCH: keltner_width (41.0, |IC|=0.024), atr_ratio (40.9, |IC|=0.024)
- 关键 dxy_corr_20: IC=-0.038 (跟 PROJECT_MAP 标的 0.034 一致)

### L2 DSL 发现 (T15.5)

1000 候选随机搜索 (50K M15 真实数据, 132.9s):
- 956/1000 有效, 148 WATCH, 281 DECAYING
- 去重后 1-5 个独立候选 (跟 close 等基础因子 |corr| < 0.5)
- cross-validation promoted: 4-7 个 shadow factor

### T13 EventFilter (MAB 业务层关键修复)

跳过 19906 bars (40%), 把 DD 从 169% 降到 64%, PnL 从 +20% 升到 +121%
- NFP: 87 天窗口, FOMC: 19 个, CPI: 29 个, GVZ: 603 天

### T16 数据同步 (db 正增长)

| timeframe | db bars | 最新 bar |
|---|---|---|
| M5 | 200199 | 2026-06-02 13:40 |
| M15 | 50182 | 2026-06-02 13:45 |
| H1 | 18045 | 2026-06-02 13:00 |
| D1 | 500 | 2026-05-29 |

---

## 目录结构

```
quant_trading/
├── main.py                  # 入口 (backtest/paper/live/dashboard)
├── README.md                # 本文件 (用户文档)
├── ROADMAP.md               # 单源待办 (P0/P1/P2/P3/Tier1-4 + T1-T16)
├── PROJECT_MAP.md           # 框架索引 + 真状态数字 + 文件路径速查
├── requirements.txt         # Python 3.12 依赖
│
├── config/                  # settings.yaml + instruments.yaml + __init__.py shim
├── core/                    # event_bus / clock / state
├── db/                      # schema / store (analytics)
├── factors/                 # aroon / cci / mfi / williams_r (老接口 4 因子)
├── strategies/              # 7 策略: multi_factor_m15 ★ / trend_following /
│                            # mean_reversion / breakout / ma_cross_h4 /
│                            # macd_bb / gold_momentum
├── strategy/                # base / signal_bus / registry / portfolio /
│                            # mab_router ★ / scheduler / scorer / retrain_scheduler
├── alpha/                   # 22 因子 registry / factor_engine / ic_tracker /
│                            # regime_classifier / probability_calibrator /
│                            # factor_health ★ / registry_adapter ★ /
│                            # factor_dsl ★ / factor_score_evaluator ★ /
│                            # factor_search ★ / factor_discovery ★ /
│                            # persistent_registry ★
├── execution/               # oms / router / paper_trader ★ / mt5_bridge ★ /
│                            # algos (TWAP/VWAP/POV/IS) / slippage / impact /
│                            # match_replay / latency / order_retry /
│                            # mab_paper_runner ★ / event_filter ★
├── risk/                    # circuit (P3 调优 10%) / pre_trade / position / regime
├── monitor/                 # dashboard / alerter / alerts
├── live/                    # factor_monitor (P0-4) / meta_learner_monitor (P0-7)
├── modules/                 # 老兼容 shim (1 文件, 包 data.store.DataStore)
├── data/
│   ├── store.py             # DataStore (50K+ bars M15)
│   ├── bar_builder.py       # tick → bar
│   ├── feed.py              # bar 流喂入
│   ├── external_loader.py   # 跨资产/事件/ETF 对齐
│   ├── news_cache.py        # 事件日历 / GVZ / NFP 读
│   ├── tick_generator.py    # Brownian bridge tick 生成
│   ├── live_sync/           # ★ T16: 实时数据同步
│   │   ├── mt5_puller.py    # MT5 实时 bar 拉取
│   │   ├── bar_filter.py    # 去重 / 当前 bar skip / 完整性检查
│   │   ├── db_inserter.py   # DataStore 包装 + 重试 + 状态持久化
│   │   ├── orchestrator.py  # full_sync / incremental_sync 编排
│   │   └── daemon.py        # 后台守护进程
│   ├── market_data.db       # 100MB+ M15 bars + macro/events/etf
│   ├── analytics.db         # strategy_perf + decision_log
│   └── charts/              # 30 报告 + discovery runs + factor_health
├── scripts/                 # 35+ 脚本 (测试 + 工具 + 脚本入口)
│   ├── live_sync.py         # ★ T16: 实时数据同步 CLI
│   ├── discover_factors.py  # ★ L2: 因子发现 CLI
│   ├── test_*.py            # 17 单测
│   └── ...
├── memory/                  # selflearning-scheduler 笔记
└── logs/                    # 运行日志
```

---

## 安装

```bash
# Python 3.12 (hermes venv 3.11 缺 backtrader)
C:\Users\zhu\AppData\Local\Programs\Python\Python312\python.exe -m pip install -r requirements.txt
```

---

## 运行

```bash
# 回测 (默认 M15, 扫描 SL/TP/CD 12 组合)
python main.py --mode backtest --timeframe M15

# 模拟盘 — 单一策略 baseline, +407.51% / 738t / Sharpe 1.807
python main.py --mode paper --timeframe M15

# 模拟盘 — MAB 多策略全栈 (T1-T16)
python main.py --mode paper --timeframe M15 \
  --use-router --use-scheduler --use-calibrator \
  --use-meta-monitor --use-factor-monitor --use-alerter \
  --use-retrain --retrain-every-n 300 --use-event-filter

# 因子健康评估 (T14.1)
python main.py --mode paper --timeframe M15 --factor-health-report

# L2 因子发现 (DSL 搜索 + 自动 register)
python scripts/discover_factors.py --n-candidates 1000 --top-k 50 \
  --forward-periods 1,5,20 --auto-register

# 实时数据同步 (T16)
python scripts/live_sync.py --mode once --type incremental --timeframes M15,H1,D1
python scripts/live_sync.py --mode status  # 查看 db bar 数

# 实盘 (stub, 需配 MT5 — 当前 balance=0 阻塞)
python main.py --mode live

# 监控面板
python main.py --mode dashboard --port 8050
```

---

## 文档导航

| 想了解 | 看 |
|---|---|
| 任务清单 / 优先级 / 待办 | `ROADMAP.md` |
| 框架索引 / 文件路径 / 真 PnL 数字 | `PROJECT_MAP.md` |
| 自学习调度器细节 | `memory/selflearning-scheduler.md` |
| 历史路线图 (旧版规划) | git log `ROADMAP.py` 删除前 |

---

## 核心原则

1. **先回测, 后实盘** — 至少 2K 根 bar, 样本外衰减<150% 才算过验证
2. **风控第一** — 单笔风险 4-6% 账户, $500+0.01 lot (1 oz XAUUSD, contract_size=100) + 3 ATR SL ≈ $25 = 5% 账户
3. **参数不贪** — 12 组合全过, 过拟合=未来函数=假
4. **数据质量** — MT5 真实 tick, 不用 Yahoo Finance
5. **本地代理** — claude CLI 走 `ANTHROPIC_BASE_URL=http://127.0.0.1:15721` → deepseek-v4-flash
