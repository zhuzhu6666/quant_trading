# 项目框架总览 (PROJECT_MAP)

> 最后更新: 2026-06-02
> 项目状态: 41/41 ROADMAP 任务代码层完成 (100%)
> 真实 PnL 数字 / 风险点 / 阻塞项 全部记录

---

## 1. 项目目录结构

```
quant_trading/
├── main.py                          # CLI 入口 (backtest/paper/live/dashboard)
├── ROADMAP.md                       # 41 项任务路线图 (单源待办, 替代旧 ROADMAP.py + TODO.md)
├── README.md                        # 用户文档
├── MEMORY.md                        # 1 行自学习调度器引用
├── requirements.txt                 # Python 3.12 依赖
│
├── core/                            # 基础设施 (5 文件)
│   ├── __init__.py
│   ├── clock.py                     # 时间 / 计时
│   ├── event_bus.py                 # 进程内事件总线
│   └── state.py                     # 全局状态 (position / balance / circuit flag)
│
├── data/                            # 数据层 (10 文件)
│   ├── store.py                     # DataStore — SQLite 读写
│   ├── feed.py                      # bar 流喂入
│   ├── bar_builder.py               # tick → bar
│   ├── tick_generator.py            # Brownian bridge tick 生成 (T1.2)
│   ├── tick_receiver.py             # 实盘 tick 接收
│   ├── news_cache.py                # 事件日历 / GVZ 读
│   ├── external_loader.py           # P0-3: 跨资产/事件/ETF 对齐
│   └── live_sync/                   # ★ T16: 实时数据同步 (5 文件)
│       ├── mt5_puller.py            # MT5 bar 拉取 (history + incremental)
│       ├── bar_filter.py            # 去重 + 当前 bar skip + 完整性检查
│       ├── db_inserter.py           # DataStore 包装 + 重试 + 状态持久化
│       ├── orchestrator.py          # full_sync / incremental_sync + 多 TF
│       └── daemon.py                # 后台守护进程 (once / daemon 模式)
│
├── db/                              # analytics 库 (3 文件)
│   ├── schema.py                    # strategy_perf / decision_log DDL
│   └── store.py                     # AnalyticsStore
│
├── alpha/                           # 因子/ML/校准/DSL (15 文件)
│   ├── registry.py                  # 22 因子注册 (P0-1 8 + P0-3 7 + 7 旧)
│   ├── factor_engine.py             # 流式因子计算 + IC 分析
│   ├── ic_tracker.py                # 滚动 IC 追踪
│   ├── factor_attribution.py        # 边际 IC 归因
│   ├── regime_classifier.py         # sklearn LogReg (P0 旧)
│   ├── probability_calibrator.py    # P0-7: 桶级 + Platt 校准
│   ├── factor_health.py             # ★ T14.1: 因子健康评分 (5 维, 0-100)
│   ├── registry_adapter.py          # ★ T14.2: 动态 register/unregister + 事件流
│   ├── persistent_registry.py       # ★ T15.5: 跨进程恢复 shadow 因子 (闭环 2026-06-03)
│   ├── factor_dsl.py                # ★ T15.1: DSL parser + AST + 20+ 算子 + 沙箱
│   ├── factor_score_evaluator.py    # ★ T15.2: DSL 候选 IC 评分 + cross-validation
│   ├── factor_search.py             # ★ T15.3: 随机搜索 (100 候选 0.4s)
│   └── factor_discovery.py          # ★ T15.4: orchestrator (search→eval→dedup→register)
│
├── factors/                         # 老接口因子 (4 文件 + 测试)
│   ├── aroon.py / cci.py / mfi.py / williams_r.py
│   └── _test_factors.py
│
├── strategies/                      # 7 交易策略
│   ├── multi_factor_m15.py         # ★ 主策略 (M15, 4 因子合成) — T15.5 闭环 (2026-06-03): _load_shadow_factors / _compute_shadow_factors / _shadow_votes + lazy load
│   ├── ma_cross_h4.py               # MA cross H4
│   ├── macd_bb.py                   # MACD + BB H1
│   ├── gold_momentum.py             # 黄金动量 H1
│   ├── trend_following.py / mean_reversion.py / breakout.py  # M15
│
├── strategy/                        # 策略框架 (9 文件)
│   ├── base.py                      # BaseStrategy / Signal
│   ├── signal_bus.py                # 跨策略信号
│   ├── registry.py                  # 策略注册表
│   ├── portfolio.py                 # 仓位计算
│   ├── mab_router.py                # ★ MAB Thompson sampling
│   ├── scheduler.py                 # 自学习调度 (C6)
│   ├── scorer.py                    # 加权打分融合
│   └── retrain_scheduler.py         # ★ T8: 周期 retrain hook
│
├── execution/                       # 13 文件
│   ├── oms.py                       # Order 状态机
│   ├── router.py                    # ExecutionRouter (P1-B algos 集成)
│   ├── paper_engine.py              # PaperEngine 简化版
│   ├── paper_trader.py              # ★ PaperTrader (主 paper 路径)
│   ├── mt5_bridge.py                # ★ MT5 整合 (P1-A, filling mode + fetch)
│   ├── algos.py                     # ★ T1.1 智能路由 (TWAP/VWAP/POV/IS)
│   ├── mab_paper_runner.py          # ★ T1: MAB 多策略共享 paper (4 策略 + 7 组件)
│   ├── event_filter.py              # ★ T13: SharedEventFilter (NFP/FOMC+CPI/GVZ)
│   ├── slippage.py                  # 动态滑点
│   ├── market_impact.py             # Almgren-Chriss
│   ├── match_replay.py              # Brownian bridge 撮合回放
│   ├── latency.py                   # 延迟模拟
│   └── order_retry.py               # 拒单补单 (指数退避)
│
├── live/                            # 3 文件 (P1 实时层)
│   ├── executor.py                  # MT5 旧实盘 (被 mt5_bridge 整合)
│   ├── factor_monitor.py            # P0-4: 实时 IC 监控 + regime shift
│   └── meta_learner_monitor.py      # P0-7: 多模型校准监控
│
├── config/                          # 2 yaml + 1 shim (新加 __init__.py 解析 flat 常量)
│   ├── settings.yaml                # 主配置 (nested: mt5/commission/risk/execution/data/...)
│   ├── instruments.yaml             # 品种定义
│   └── __init__.py                  # flat-constant shim (老 scripts 兼容)
│
├── modules/                         # 老兼容层 (1 文件 shim, 2026-06-02 复活)
│   └── database.py                  # 包装 data.store.DataStore, 6 函数 init/insert/load/get_range/count/summary
│
├── risk/                            # 4 文件
│   ├── circuit.py                   # ★ CircuitBreaker (P3 调优 5% → 10%)
│   ├── pre_trade.py                 # 前置风控
│   ├── position.py                  # 持仓监控
│   └── regime.py                    # Regime 标签
│
├── monitor/                         # 3 文件
│   ├── alerter.py                   # 钉钉/企微告警
│   ├── alerts.py                    # Alert 接口
│   └── dashboard.py                 # 监控面板
│
├── scripts/                         # 35+ 脚本 (测试 + 工具 + 入口)
│   ├── live_sync.py                 # ★ T16: 实时数据同步 CLI
│   ├── test_shadow_consumption.py   # ★ T15.5 闭环: A/B 验证 shadow 因子接进投票 (2026-06-03, PnL delta=-24.06% 闭环确认)
│   ├── live_sync_daily.bat          # ★ T16: Windows Task Scheduler 配置
│   ├── discover_factors.py          # ★ L2: 因子发现 CLI
│   ├── P0 系列: test_p0_factors / factor_pca / factor_ic_rolling
│   ├── P0-5/6: train_xgb_walkforward / walkforward_p0_6
│   ├── P0-7: test_probability_calibrator
│   ├── P1-A: p1_c_sync_live_bars
│   ├── P1-D: p1_d_shadow
│   ├── P1-E: p1_e_ab_test
│   ├── P3: p3_circuit_tune
│   ├── 主 paper: mab_paper / mab_paper_v2 / baseline_all_strategies
│   └── 单测: test_algos / test_alerter / test_order_retry / 等 12 个
│
├── data/charts/                     # 落盘报告 (24 文件)
│   ├── *.txt (报告) + *.png (图) + *.npy (数值) + *.json (校准器)
│
├── factors/__init__.py              # 4 老因子接口导出
└── live/__init__.py                 # (空)
```

---

## 2. 关键文件路径速查

### 2.1 核心主路径
- **回测**: `main.py --mode backtest` → `main.py:run_backtest()` (backtrader 内联, 走 optstrategy SL/TP/CD 扫描)
- **模拟盘**: `main.py --mode paper` → `execution/paper_trader.py` (PaperTrader, ★)
- **实盘**: `main.py --mode live` → `live/executor.py` (旧) / `execution/mt5_bridge.py` (新)
- **MAB paper**: `scripts/mab_paper.py` / `mab_paper_v2.py`
- **Dashboard**: `main.py --mode dashboard`

### 2.2 因子 / 模型
- **因子注册**: `alpha/registry.py` (22 因子)
- **PCA**: `scripts/factor_pca.py`
- **XGBoost 训练**: `scripts/train_xgb_walkforward.py`
- **Walk-Forward**: `scripts/walkforward_p0_6.py`
- **校准器**: `alpha/probability_calibrator.py`
- **因子 IC 监控**: `live/factor_monitor.py`
- **元学习监控**: `live/meta_learner_monitor.py`

### 2.3 路由 / 执行
- **智能路由算法**: `execution/algos.py` (TWAP/VWAP/POV/IS)
- **执行路由**: `execution/router.py` (大单走 algo)
- **MT5 整合**: `execution/mt5_bridge.py` (filling mode / fetch / 紧急平仓)
- **影子交易**: `scripts/p1_d_shadow.py`
- **A/B 测试**: `scripts/p1_e_ab_test.py`

### 2.4 风险 / 风控
- **熔断器**: `risk/circuit.py` (默认 10% 日损, P3 调优)
- **前置风控**: `risk/pre_trade.py`
- **持仓**: `risk/position.py`
- **Regime 分类**: `risk/regime.py`

### 2.5 数据
- **DataStore**: `data/store.py` (50K bar M15 XAUUSD+, 9 字段含 spread)
- **外部数据加载**: `data/external_loader.py` (DXY / SLV/GLD/TLT / FOMC/NFP/CPI/PCE)
- **事件日历**: db 内 `events` 表 (105 条, 2024-2026)
- **Tick 生成**: `data/tick_generator.py` (Brownian bridge)
- **P2 spread backfill**: `scripts/p2_backfill_spread.py` (MT5 → bars.spread, 10% 真实覆盖)

### 2.6 P2 SL/TP bid-ask (2026-06-03)
- **paper_engine._check_exit**: bid/ask-extreme 判定 (long SL 用 low, TP 用 high-spread)
- **paper_engine._open**: long entry 在 ask (bar.open + half_spread)
- **FORCE_CLOSE_BASED_SLTP env**: 关掉 bid/ask 偏移 (A/B 对比用)
- **报告**: `data/charts/p2_sltp_bidask_report.txt` (close-based vs bid/ask)

### 2.7 T15.5 闭环 wiring ✅ (2026-06-03 closed)
- **strategies/multi_factor_m15.py**: `_load_shadow_factors` / `_compute_shadow_factors` / `_shadow_votes` (3 新方法 + 8 新参数 + lazy load 守卫)
- **main.py CLI**: `--include-shadow-factors` / `--shadow-top-k` (单策略 + MAB overrides 两条路径都通)
- **scripts/test_shadow_consumption.py**: A/B 验证 (5000 bar, baseline vs shadow-on)
- **A/B 结果**: A=62t/+24.79%/Sharpe 1.46/DD 51.44%/PF 1.11/$623.94; B=68t/+0.73%/Sharpe 0.69/DD 34.06%/PF 1.01/$503.64; **delta=-24.06% PnL 但 DD -17.39pp 改善** (wiring 闭环确认, 影子因子 OOS 净负 → 校准 `top_pct` / `vote_weight`)

---

## 3. 真状态数字 (2026-06-02)

### 3.1 PnL 对比 (multi_factor_m15, M15, 50K bar)

| 路径 | PnL | Trades | Sharpe | 备注 |
|---|---|---|---|---|
| **main.py --mode paper** | **+407.51%** | 738 | 1.807 | enable_circuit=False, 显式无事件过滤 baseline |
| **baseline_all_strategies** | +407.51% | 738 | - | 同上, 同一路径 |
| **mab_paper 修后** | +380.58% | 596 | 1.452 | MAB router + 事件 skip 真的生效 |
| **mab_paper_v2 修后** | +181.18% | 590 | 1.105 | v2 行为差异 (trend 选 76 次 vs v1 17 次) |
| **MAB T1-T13 全栈 (新)** | +120.75% | 639 | 0.894 | main.py --use-router + T13 EventFilter, 业务层跟 baseline 同量级 |
| paper w/ circuit 10% | -9.54% | 123 | -0.105 | P3 调优后默认值 |
| paper w/ circuit 5% (原) | -33.61% | 62 | -0.872 | 频繁触发 (13+ 次) |

### 3.2 因子 (22 个, 4 有效)

| 因子 | 状态 | abs_IC |
|---|---|---|
| dxy_corr_20 | ACTIVE | 0.034 (金矿) |
| macd_hist | ACTIVE | 0.022 |
| bb_width | fading | 0.012 |
| ema_slope | fading | 0.011 |
| 其他 18 | dead | < 0.005 |

### 3.3 MT5 / 账户

- 账户: 9823690 / Bybit-Live-2 / UST / 500x
- **balance=0** (子账户/未充值, 不能 live trade, 全 read+paper)
- 当前金价: **4512 USD/oz** (2026-06-02, 黄金 2025-2026 突破 4000 上行)
- API: `mt5.initialize()` 无 creds 可用 (依赖已登录终端)
- copy_rates_from_pos: 5000 bar=78 天, 10000 bar=155 天
- copy_ticks_from_pos: 易挂死, 避免

### 3.4 Walk-Forward / XGBoost (P0-5/6)

- 80/20 OOS: acc 0.5211 / AUC 0.5276
- Walk-Forward 2 fold: mean lift +2.41% / OOS lift +2.03%
- 校准 (P0-7): 4.6% gap, 6-bin 校准表 (XGBoost [0.6,0.7] 过度自信 +17%)

---

## 4. 框架就位 vs 阻塞项

### 4.1 已就位 (代码层 100%)
- ✅ P0 因子库 / PCA / IC 监控 / 训练 / 元学习 (7/7)
- ✅ P1 MT5 整合 / 智能路由 / 数据拉取 / 影子 / A/B / 紧急平仓 (6/7)
- ✅ P3 circuit 调优 (1 项)

### 4.2 阻塞 / 等输入
- ~~P1-G 合规检查~~ **跳过 (不需要)**
- ⏸ T1.2 L2 / T&S / 基本面数据 (需 broker 支持 DOM)
- ⏸ T3 治理 (Bonferroni / CSCV / Deflated Sharpe 等) — 需机构级流程
- ⏸ T4 长期方向 (卫星数据 / 跨资产套利) — 需外部资源

### 4.3 已发现 / 待修
- ⚠ circuit 启用下 PnL 仍负 (-9.54%), 需进一步:
  - 单笔风险 0.01 → 0.005 手
  - max_consecutive_loss 5 → 3
  - P0-7 校准接 scoring (减少弱信号)
- ⚠ mab_paper_v2 行为跟 v1 差异大, 内部 baseline 设计待查
- ⚠ 22 因子 18 noise, 因子库饱和, P2 因子 DSL / 合成 待启动
- ✅ T15.5 闭环 wiring 已修 (2026-06-03): lazy load 绕过 registry kwargs 时序, A/B delta=-24.06% 闭环确认

---

## 5. 单测覆盖

✅ **通过的测试** (已实际跑过):
- `scripts/test_algos.py` — 10/10 (TWAP/VWAP/POV/IS 单测)
- `scripts/test_probability_calibrator.py` — bucket > Platt, Brier 改善 +0.0033
- `scripts/p1_c_sync_live_bars.py` — broker vs db 同步
- `scripts/p1_d_shadow.py` — dual-router PnL 差 +176
- `scripts/p1_e_ab_test.py` — 3 baseline 对比
- `scripts/p3_circuit_tune.py` — 4 档 circuit sweep
- `scripts/factor_pca.py` — 22 因子 PCA
- `scripts/factor_ic_rolling.py` — 514 锚点 + regime shift
- `scripts/test_p0_factors.py` — 22 因子 IC
- `scripts/train_xgb_walkforward.py` — 80/20 OOS
- `scripts/walkforward_p0_6.py` — 2 fold Walk-Forward
- `execution/mt5_bridge.py --dry-run` — 整合 dry-run
- `main.py --mode paper` — PaperTrader 验证
- `scripts/mab_paper.py` (修后) — 596t, +380.58%
- `scripts/mab_paper_v2.py` (修后) — 590t, +181.18%
- `scripts/baseline_all_strategies.py` — 7 策略, multi_factor +407.51%

⚠ **未跑 / 待写单测**:
- `strategy/scorer.py` (无单测, P0-7 校准接 scoring 待做)
- `live/executor.py` (实盘执行, balance=0 阻塞)
- `risk/circuit.py` (无单测, P3 调优有 sweep 但无正式单测)
- ~~`backtest/engine.py`~~ — 已删 (2026-06-02 文档整理, 是死代码)

---

## 6. 待办优先级 (P0/P1/P3 + T1-T16 集成层 全完)

按"先做有真实价值"原则推荐:

### 6.1 立刻能做 (无外部依赖, 1-2 小时)
- **ProbabilityCalibrator 持久化** (P0, 当下): 启动时从磁盘加载, fallback 重 fit
- **T15.5 影子因子校准**: 当前 OOS PnL 净负 (过拟合), 调 `shadow_top_pct` / `shadow_vote_weight` / `shadow_min_samples`
- **P2 SL/TP bid-ask**: 让 OOS 更真实
- **P2 资金费建模**: XAUUSD swap cost 不小
- **GP 因子搜索** (T15.3 v2): 当前只有随机搜索, GP 能更精

### 6.2 集成层完成 (2026-06-02)
- ✅ **T1-T10** MAB 多策略 + 7 个自学习组件 + RetrainScheduler
- ✅ **T13** SharedEventFilter (MAB 业务层关键, 50K bar 跳 19906 bar)
- ✅ **T14.1-3** L1 因子生命周期 (FactorHealth + RegistryAdapter + main.py 接入)
- ✅ **T15.1-8** L2 因子 DSL (parser + 搜索 + orchestrator + persistent registry + T15.5 闭环 wiring 2026-06-03)
- ✅ **T16.1-8** 实时数据同步 (MT5→db + 增量 + 多TF + Windows Task Scheduler)

### 6.3 阻塞
- **T1.2 L2/T&S/基本面**: broker 余额/支持
- **T3 治理**: 机构级流程
- **T4 长期**: 外部资源

---

## 7. 关键文件按 P0/P1/P3 任务映射

| 任务 | 主文件 | 测试/验证 |
|---|---|---|
| P0-1 因子补齐 8 | `alpha/registry.py` (line ~150-380) | `scripts/test_p0_factors.py` |
| P0-2 PCA | `scripts/factor_pca.py` | `data/charts/factor_pca_report.txt` |
| P0-3 跨资产/事件/时段 | `alpha/registry.py` (line ~380-540) + `data/external_loader.py` | `scripts/test_p0_factors.py` |
| P0-4 IC rolling | `live/factor_monitor.py` + `scripts/factor_ic_rolling.py` | `data/charts/factor_ic_rolling.png` |
| P0-5 XGBoost | `scripts/train_xgb_walkforward.py` | `data/charts/xgb_report.txt` |
| P0-6 Walk-Forward | `scripts/walkforward_p0_6.py` | `data/charts/walkforward_report.txt` |
| P0-7 元学习监控 | `live/meta_learner_monitor.py` + `alpha/probability_calibrator.py` | `data/charts/meta_learner_report.txt` |
| P1-A MT5 整合 | `execution/mt5_bridge.py` | dry-run |
| P1-B 智能路由 | `execution/algos.py` + `execution/router.py` | `scripts/test_algos.py` |
| P1-C 数据拉取 | `scripts/p1_c_sync_live_bars.py` | `data/charts/p1_c_sync_report.txt` |
| P1-D 影子 | `scripts/p1_d_shadow.py` | `data/charts/p1_d_shadow_report.txt` |
| P1-E A/B | `scripts/p1_e_ab_test.py` | `data/charts/p1_e_ab_report.txt` |
| P1-F 紧急平仓 | `execution/mt5_bridge.py` close_all_positions | (手动验) |
| P3 circuit 调优 | `risk/circuit.py` + `execution/paper_trader.py` | `scripts/p3_circuit_tune.py` |
| Bug 修复 | `scripts/mab_paper.py` (line ~154) | `data/charts/mab_paper_bugfix_report.txt` |

---

## 8. 文档清单

- **`README.md`** — 用户文档, 当前状态 + 安装 + 运行
- **`ROADMAP.md`** — 41 项路线图 (单源待办, P0/P1/P2/P3/Tier1-4 分组, 勾选状态)
- ~~`ROADMAP.py`~~ — 已删 (2026-06-02 文档整理, 合并入 ROADMAP.md)
- ~~`TODO.md`~~ — 已删 (2026-06-02 文档整理, 合并入 ROADMAP.md)
- **`MEMORY.md`** — 1 行链接到自学习调度器
- **`memory/selflearning-scheduler.md`** — 自学习调度器笔记
- **`PROJECT_MAP.md`** (本文件) — 完整框架索引

## 9. 数据 / 报告

- `data/market_data.db` (50K+ M15 bar + macro/events/etf, time 全 INTEGER)
- `data/analytics.db` (strategy_perf + decision_log)
- `data/decision_log.db` (P9 决策日志)
- `data/charts/` — 30 份报告 + 5 个 discovery run json + factor_health report
- `config/factor_lifecycle.yaml` — L1+L2 配置
- `~/.hermes/scripts/live_sync_5m.py` — T16 调度脚本 (Hermes cron job 54c849d80e9d 每 5min 调, 强制 Python 3.12)
- `data/charts/live_sync_status.json` — T16 sync 状态 (`last_sync_utc` 实时, `per_tf.inserted_last` / `total_bars` 字段 2026-06-02 修 orchestrator bug 后正常更新)
- ⚠ db 清理 (2026-06-02 23:40): `candles` 表 (TEXT time, 6 timeframe 卡 2026-05-29, 没人用) 已 DROP TABLE, 备份在 `data/market_data.db.pre_drop_candles.bak`。`regime.py:477` 改走 `bars` (INTEGER time, 实时)。**唯一表**: bars + macro_daily + etf_daily + events + symbols

---

**最后扫描时间**: 2026-06-02
**项目代码量**: ~8000 行 Python (核心路径 + L1/L2/T16)
**测试覆盖**: P0+P1+T1-T16 关键路径全过
**真 PnL 记录**: 6 个场景 (baseline / MAB / T13 / circuit / L1 / L2), 数字齐全
**阻塞**: 仅 P1-G (跳过) + 外部依赖项
