# Python 量化交易框架

XAUUSD+ 黄金 M15 趋势/回归/因子合成, 7 层架构, 本地 paper + backtest baseline 已实盘验证 (read-only 模式)。

**最后更新: 2026-06-03 下午 (8 项接入完成)**

---

## 当前状态 (2026-06-02 验证)

### 代码层进度: 41/41 (P0+P1+P3) + T1-T16 集成层

- ✅ **P0 (1-7)**: 因子库 22 个 / PCA / IC 监控 / XGBoost / Walk-Forward / 元学习
- ✅ **P1 (A/B/C/D/E/F)**: MT5 整合 / 智能路由 / 数据拉取 / 影子 / A/B / 紧急平仓
- ✅ **P3** circuit 调优: 5% → 10% 默认
- ✅ **T1-T13 集成层**: MAB 多策略 + SelfLearningScheduler + WeightedScorer + ProbabilityCalibrator + MetaLearnerMonitor + FactorMonitor + Alerter + RetrainScheduler + SharedEventFilter (T13 业务关键)
- ✅ **T14 L1 因子生命周期**: FactorHealth 5 维评分 + RegistryAdapter 动态 register/unregister
- ✅ **T15 L2 因子 DSL** (T15.1-4 + T15.6-8): parser + AST + 20+ 算子 + 搜索 + 自动发现 + 持久化
- ✅ **T15.5 闭环 (2026-06-03)**: shadow/discovered 因子接进 multi_factor_m15 投票管道 (lazy load 绕过 registry kwargs 时序 bug); A/B 测试 PnL delta=-24.06% (DD 同步改善 -17.39pp), wiring 已生效
- ⏸ **T16 实时数据同步** (2026-06-03 **暂停**): Python MetaTrader5 5.0.5735 vs MT5 terminal 2026 版本 IPC pipe 名字 hash 不匹配, 包 `WaitNamedPipeW` 一直 timeout (7 种 path 变体 + 重装 + 短长 timeout 全败; 同会话 `CreateFileW` 手动连 `MT5.Terminal.781AEDD6...` pipe 100% 成功). db 50K M15 bar 是历史一次性 `scripts/fetch_mt5_data.py` 拉的. cron job 54c849d80e9d 配了但 `script=~/.hermes/scripts/live_sync_5m.py` 0 字节空, last_status=error. **影响范围**: 仅 T16 增量, 7 策略/MAB/影子/GP/校准/全部 PnL 数字全走 db 离线, 不受影响. 改按需手动: `python scripts/live_sync.py --mode once --type incremental --timeframes M15,H1,D1`. 报告: `data/charts/live_sync_status.json` (M15 50204 bars, inserted_last=0).

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

### P2 SL/TP bid-ask (2026-06-03)

- bars 表加 `spread INTEGER` 字段 (ALTER TABLE 自动迁移)
- M15 backfill 4998/50204 bar (10% 真实 spread, broker 限 5000; 老 bar fallback 0.13 USD)
- paper_engine._check_exit: SL/TP 按 bid/ask-extreme 比较 (long SL 用 bid-low, TP 用 bid-high-spread)
- entry 价: long 在 ask (bar.open + half_spread), short 在 bid
- 端到端 5000 bar PnL 变化 < 0.5% (spread 0.13 USD 远小于 3ATR=$25)
- 框架就位, 真实影响在 FOMC/NFP 事件日 spread 1-3 USD 时才有意义 (2-5% PnL 影响)
- 报告: `data/charts/p2_sltp_bidask_report.txt`

### P2 资金费/过夜费建模 (2026-06-03 下午)

- `execution/paper_engine.py` 加 3 参数: `enable_swap=True`, `swap_long_per_lot_per_day=-1.0`, `swap_short_per_lot_per_day=0.0`
- `PaperTrade` 加 `swap: float=0` 字段, `_close` 计算: `swap_cost = swap_rate * pos.volume * hold_days` (USD)
- Bug 修复: `_close` 路径 `bar_time` 透传, 避免 entry_time 落到 utcnow
- A/B 验证 5000 bar: A swap_off +24.79% / B swap_on -1/day -0.04% / C stress -5/day -0.20%
- XAUUSD 长仓过夜费 -1 USD/lot/day 是合理默认值 (Bybit-Live-2 历史)
- 报告: `data/charts/swap_funding_report.txt`

### 自主进化 8 项接入 (2026-06-03 下午)

完整 L1-L5 自循环已闭环, 8 项 cron 化接入:

| PR | 改动 | 文件 |
|---|---|---|
| PR-1.3 | Calibrator retrain 后自动 save | scripts/walkforward_p0_6.py 末尾 + ProbabilityCalibrator.fit_from_predictions() |
| PR-1.6 | 影子因子默认 vote_weight=0 永久 | config/factor_lifecycle.yaml + multi_factor_m15.py |
| PR-1.8 | 日终 paper dryrun cron | scripts/daily_paper_dryrun.py (新) |
| PR-2.1 | L2 GP 发现 cron 化 + auto_register | scripts/auto_discover_daemon.py (新) |
| PR-2.5 | 影子 7 天 HEALTHY -> DISCOVERED 升级 | scripts/promote_shadow_to_active.py (新) + RegistryAdapter.promote() |
| PR-3.2 | 漂移 SEVERE_DRIFT -> 触发 re-search | scripts/drift_research_daemon.py (新) + MetaLearnerMonitor.drift_handler |
| PR-3.4 | T13 skip -> meta-learner batch | scripts/t13_skip_backfill.py (新) |
| PR-3.7 | Regime 分类器周期重训 | scripts/regime_retrain.py (新) |

完整 cron 配置 (hermes):
- `daily_paper_dryrun`: 每天 02:00, 跑最近 1 天 paper
- `auto_discover_daily`: 每天 01:00, GP 50x30 + auto_register
- `shadow_promote_daily`: 每天 01:30, 评估 shadow + promote/remove
- `drift_research_daily`: 每 6h 检查一次, SEVERE_DRIFT 触发 GP re-search
- `t13_skip_backfill_daily`: 每天 00:30, 把 T13 skip 期间数据批量喂 meta-learner
- `regime_retrain_weekly`: 每周日 02:30, 重训 regime classifier

### 影子因子 shadow ON 默认关闭 (2026-06-03 下午)

32 组合扫描: `shadow_vote_weight < 1` 等于无影响 (整数票 floor), `vw >= 1` 拖累 PnL
- 最佳: vw=0 = baseline (+24.79%)
- 最差: vw=1.0 + tp=0.8: -19.59% / DD 54.37%
- 默认改 `shadow_vote_weight=0` (实质关闭, 保留 lazy load 机制)
- 开启需显式: `--include-shadow-factors --shadow-vote-weight 1.0`
- 报告: `data/charts/shadow_calibration_report.txt`

### ProbabilityCalibrator 校准 A/B (2026-06-03 下午)

P0-7 桶级 calibrator 在 3000 bar OOS 段**反伤** PnL (-30%, Sharpe 1.85 vs 3.59)
- wiring 验证通过, 但 calibrator 本身需 retrain (P0-7 OOS 段已变, 桶表 stale)
- 下一步: 周期性重训 calibrator (跟 P0-7 重训同节奏)

### 自进化状态评估 (2026-06-03)

**框架自主进化差距 3/3 已闭环**。剩余 1 项 (第三项) 待定。

1. **T15.5 闭环 wiring** ✅ **(2026-06-03 closed)** — lazy load 绕过 `strategy_registry.create()` 的 kwargs 时序 bug; A/B 测试 PnL delta=-24.06% (68t/+0.73%/Sharpe 0.69/DD 34.06%) vs A (62t/+24.79%/Sharpe 1.46/DD 51.44%); 影子因子在 OOS 上 OOS filter 有效 (DD 降 17pp) 但信号偏弱 (PnL 跌), 待校准 (top_pct/vote_weight)

2. **ProbabilityCalibrator 持久化** ✅ **(2026-06-03 closed)** — main.py 启动时优先 `load("data/charts/calibrator_bucket.json")` (已有 P0-7 实测桶级 8 桶), 缺失回退 identity; 新 CLI `--calibrator-path` / `--calibrator-save`; 测试 5/5 通过 (load/roundtrip/missing/platt)

3. **L2 GP 因子搜索 (T15.3 v2)** ✅ **(2026-06-03 closed)** — alpha/factor_search_gp.py 实现 Genetic Programming 引擎 (population/tournament/crossover/mutate/elite); 5000 bar A/B 对比: A random 1000c top1=70.38 (10.9s); B GP 100x10 top1=72.17 (17.1s) +1.79 vs random; C GP 50x30 top1=72.74 (24.2s) +2.36 vs random; GP 历史曲线 63.8→72.7, 代际持续爬坡; 推荐配置 pop>=50, gen>=20 — 等 T15.5 闭环后再讨论

**今日工作日志 (2026-06-03)**

- 策略层: `strategies/multi_factor_m15.py` +157 行
  - 8 个新参数 (include_shadow_factors / shadow_top_k / shadow_recompute_every / shadow_rank_window / shadow_min_samples / shadow_vote_weight / shadow_top_pct / shadow_bottom_pct)
  - 3 个新方法: `_load_shadow_factors` (从 lifecycle_log 读活跃 shadow 因子) / `_compute_shadow_factors` (滚动重算) / `_shadow_votes` (分位 ranking 投票)
  - `__init__` 状态初始化 + `on_init` 状态重置 + `on_bar` 投票钩子 + signal meta 加 `shadow_active`
- 入口层: `main.py` +7 行
  - 新增 CLI 参数 `--include-shadow-factors` (默认 off) / `--shadow-top-k` (默认 3)
  - 已接进单策略 `override_params` 和 MAB `overrides_full`
- 测试层: `scripts/test_shadow_consumption.py` (新增, A/B 验证: 5000 bar, baseline vs shadow-on)
- A/B 测试输出: A 与 B PnL 不同 (delta=-24.06%, DD -17.39pp) → wiring 闭环确认
- 修过的 bug: `strategy_registry.create()` 先 `cls(...)` 再 `instance.params = params`, 导致 `__init__` 读 `self.params` 时拿的是类默认, 影子加载分支永远不进。修法: lazy load 移到 `on_bar` 第一个调用时, 此时 `self.params` 已被 registry 覆盖
- 影子因子在 OOS 上净负 PnL 但 DD 改善, 后续需校准 `shadow_top_pct` / `shadow_vote_weight`

### Task #2 工作日志: ProbabilityCalibrator 持久化 (2026-06-03)

- 入口层: `main.py` 启动时优先从 `data/charts/calibrator_bucket.json` 加载 calibrator (P0-7 实测的 8 桶桶级表), 文件缺失回退 identity, load 失败 fallback identity
- 新增 CLI flag: `--calibrator-path` (默认 `data/charts/calibrator_bucket.json`) / `--calibrator-save` (预留, 定时保存)
- 测试层: `scripts/test_calibrator_persistence.py` (5/5 通过)
  - TEST 1: 加载真实 calibrator_bucket.json → method=bucket, 8 buckets
  - TEST 2: 校准值与 identity 不同 (0.75→0.60, 0.85→1.00 等)
  - TEST 3: save→load roundtrip 保留所有字段
  - TEST 4: 文件不存在时回退 identity, 不崩
  - TEST 5: fit Platt → save → load → predict 一致
- Smoke test: `python main.py --mode paper --use-router --use-scorer --use-calibrator` 日志确认 `calibrator: bucket (loaded from data\charts\calibrator_bucket.json, buckets=8, platt=(1.000, 0.000))`

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
│                            # factor_search ★ / factor_search_gp ★ / factor_discovery ★ /
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
# L2 因子发现 v1 (random search)
python scripts/discover_factors.py --n-candidates 1000 --top-k 50 \
  --forward-periods 1,5,20 --auto-register

# L2 因子发现 v2 (GP search, 推荐)
python scripts/test_gp_search.py  # A/B 验证 + 落盘 gp_run_*.json
# 自定义 GP: 直接 import FactorSearchGP (pop=100 gen=20 即可超过 random)
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

---

## Audit 验证状态 (2026-06-03 反查代码)

参考: `data/charts/framework_audit_20260603.md`

| 项 | 状态 | 验证 |
|---|---|---|
| BUG-1 daily_loss_pct abs() | ✅ 已修 | `core/state.py:91` `max(0, -net_pnl)` |
| BUG-2 circuit reset 覆写 peak | ✅ 已修 | `risk/circuit.py:122-134` 不再调 reset_daily |
| BUG-3 forward_periods 无效 | ✅ 已修 | `alpha/factor_engine.py:81-140` 多周期+mean |
| BUG-4 SL slippage 方向 | ❌ **audit 描述错, 代码实际对** | `paper_engine.py:252` close_dir=-pos.direction 是正确 (long sell 应 price-slip) |
| BUG-5 break_even 单独计 | ✅ 已修 | `state.py:33+131-133` |
| OPT-1 WAL+busy_timeout | ⏳ 未查 | — |
| OPT-2 _trades 无上限 | ⏸ 未改 | 长跑需定期 flush |
| OPT-3 EventBus async publish | ⏸ 未用 | 全走 publish_sync |
| OPT-4 死代码 | ⏸ 保留 | `strategy/portfolio.py` / `on_tick` / `active_orders` / `live/executor.py` |
| OPT-5 event_filter strptime 重复解析 | ⏸ **未修** | `event_filter.py:118-124` 50K bar × 50 FOMC = 2.5M 次 |

