# 项目路线图 (ROADMAP)

> 单源待办 — 替代旧的 `ROADMAP.py` (Python dict 形式, 已废) 和 `TODO.md` (重复)
> 2026-06-03 下午快照 (8 项接入完成)

---

## 进度摘要

**代码层完成: 41/41 (100%)** ← P1-E 完成, 全部代码层任务收尾
**集成层完成: T1-T16 (16/16)** ← 2026-06-02, MAB 全栈 + L1/L2 + 数据同步
**文档整理: 2026-06-02** ← 合并 ROADMAP.py + TODO.md → ROADMAP.md; 删 6 个废弃临时脚本

| 阶段 | 状态 | 备注 |
|---|---|---|
| P0 (1-7) 因子 / 模型 / 训练 | ✅ 7/7 | 22 因子 / PCA / IC 监控 / XGBoost / Walk-Forward / 元学习 |
| P1 (A-G) MT5 / 路由 / 数据 | ✅ 6/7 | 缺 P1-G 合规检查 (跳过) |
| P3 circuit 调优 | ✅ 1/1 | 5% → 10% 默认 |
| T1-T13 集成层 | ✅ 13/13 | MAB 多策略 + 9 个自学习组件 + T13 事件过滤 |
| T14 L1 因子生命周期 | ✅ 3/3 | FactorHealth 评分 + RegistryAdapter + main.py 接入 |
| T15 L2 因子 DSL | ✅ 8/8 | parser + 搜索 + orchestrator + persistent registry + **T15.5 闭环 wiring 2026-06-03 (lazy load + A/B 验证 PnL delta!=-0)** |
| T16 实时数据同步 | ✅ 8/8 | MT5 → db + 增量拉取 + 多 TF + Windows Task Scheduler |
| P0-ETF/CB (新因子纬度) | ✅ 11/11 因子 | GLD/SLV 持仓 / 央行黄金 / 实际利率百分位 |
| P0-COT (CFTC 持仓) | ✅ 6/6 因子 | 856 周 GOLD COT (2010-2026, 16.4 年历史) |
| P0-BUGFIX | ✅ 4/4 | daily_loss_pct / circuit peak_equity / IC多周期 / break_even |
| P2 其他 (回测工程) | ⏳ 待启动 | SL/TP bid-ask / 资金费 / future function / point-in-time |
| Tier 1-4 机构级 | ⏳ 长期 | 阻塞于资源/外部依赖 |

---

## P0 — 无外部依赖, 全部完成 ✅ (2026-06-02)

- [x] **P0-1** 因子补齐 8 (ema_slope / supertrend_str / keltner / obv / vol_ma / engulfing / pin / inside)
- [x] **P0-2** PCA + 相关矩阵 (4 有效因子, 4 PC=90%, 7 冗余对)
- [x] **P0-3** 跨资产/事件/时段 7 (dxy_corr_20 IC 0.034 ACTIVE, 5 外部数据对齐)
- [x] **P0-4** IC rolling 接 live (514 锚点 + regime shift 告警)
- [x] **P0-5** XGBoost 升级 (OOS acc 0.5211 / AUC 0.5276)
- [x] **P0-6** Walk-Forward (2 fold, mean lift +2.41%, OOS lift +2.03%)
- [x] **P0-7** 元学习监控 (校准误差 4.6%, 6-bin 校准表, xgb [0.6,0.7] 过度自信 +17%)

---

## P1 — 需数据源/MT5, 主体完成 ✅ (2026-06-02)

- [x] **P1-A** MT5 整合 (`execution/mt5_bridge.py` filling mode 探测 + fetch_history + close_all_positions + dry-run)
- [x] **P1-B** 智能路由 (`execution/algos.py` TWAP/VWAP/POV/IS 4 算法 + Dispatcher, 10/10 单测过)
- [x] **P1-C** 拉真实数据 (`scripts/p1_c_sync_live_bars.py` 5000 bar, broker 9 天领先 db, 价格差 -0.42%)
- [x] **P1-D** 影子交易 (`scripts/p1_d_shadow.py` dual-router seed 差异 PnL +176 → 修后)
- [x] **P1-E** A/B 测试 (`scripts/p1_e_ab_test.py` 3 baseline: C 均匀 +693 > A 原始 +552 > B 反向 -62)
- [x] **P1-F** 紧急平仓 (`bridge.close_all_positions(symbol)`)
- [⏭] **P1-G** 合规检查 ~~待定规则集~~ **跳过 (不需要)**

---

## T1-T13 集成层 (2026-06-02)

- [x] **T1** MABRouter 4 策略共享 paper (`execution/mab_paper_runner.py`)
- [x] **T2** main.py `--use-router` 等 8 个 flag 接入
- [x] **T3** ProbabilityCalibrator.calibrate(signal.confidence)
- [x] **T4** Alerter 接入 (大额 trade / drift / circuit 告警)
- [x] **T5** SelfLearningScheduler.on_trade_close 接入
- [x] **T6** MetaLearnerMonitor.on_observation 接入
- [x] **T7** FactorMonitor.on_bar 接入
- [x] **T8** RetrainScheduler (每 N 笔触发 walkforward, 7.3s/run)
- [x] **T9** regime 隔离 (MABRouter select 已是 per-regime)
- [x] **T10** drift → 自动 retrain (MetaLearner SEVERE_DRIFT 触发)
- [x] **T13** SharedEventFilter (MAB 业务层关键, 共享 NFP/FOMC+CPI/GVZ skip, 50K bar 跳 19906 bar)

---

## T14 L1 因子生命周期 (2026-06-02)

- [x] **T14.1** `alpha/factor_health.py` — 5 维评分 (mean_abs_ic 50% + ic_stability 30% + decay 20% + regime_consistency 20% + independence 10%)
- [x] **T14.2** `alpha/registry_adapter.py` — 动态 register/unregister + 事件流 jsonl + builtin 保护
- [x] **T14.3** main.py `--factor-health-report` — 跑 paper 前评估 22 因子, 落盘报告

**真结果**: 22 因子 0 HEALTHY / 2 WATCH / 20 DECAYING

---

## T15 L2 因子 DSL (2026-06-02)

- [x] **T15.1** `alpha/factor_dsl.py` — 递归下降 parser + AST + 20+ 算子 (ts_mean/std/corr/sum/min/max/rank/delta/delay/decay_linear + sign/abs/log/sqrt/power + rank/normalize/quantile) + 安全沙箱
- [x] **T15.2** `alpha/factor_score_evaluator.py` — IC 评分 + 多 forward_period cross-validation
- [x] **T15.3 v1** `alpha/factor_search.py` — 随机搜索 (100 候选 0.4s, 3.6ms/expr)
- [x] **T15.3 v2** `alpha/factor_search_gp.py` — **Genetic Programming** 引擎 (2026-06-03): 种群进化/tournament/crossover/mutate/elite; **A/B 验证 5000 bar**: random 1000c top1=70.38 (10.9s) vs GP 100x10 top1=72.17 (17.1s) +1.79 vs GP 50x30 top1=72.74 (24.2s) +2.36, GP 历史曲线 63.8→72.7 持续爬升; 测试 `scripts/test_gp_search.py` + `scripts/test_gp_search_v2.py`
- [x] **T15.4** `alpha/factor_discovery.py` — orchestrator: search → evaluate → 去重 → cross-validation → shadow register
- [x] **T15.5** `scripts/discover_factors.py` — CLI 入口 + `alpha/persistent_registry.py` 跨进程恢复
- [x] **T15.5 闭环 wiring** (2026-06-03 接入 + bug 修) — 8 个新参数 + 3 个新方法 (`_load_shadow_factors` / `_compute_shadow_factors` / `_shadow_votes`) + `on_bar` lazy load 钩子; `main.py` 加 `--include-shadow-factors` / `--shadow-top-k`; A/B 测试 `scripts/test_shadow_consumption.py` 验证 wiring 生效 (A: 62t/+24.79%/Sharpe 1.46/DD 51.44%; B: 68t/+0.73%/Sharpe 0.69/DD 34.06%; delta=-24% PnL 但 DD -17pp 改善)
- [x] **T15.6** `config/factor_lifecycle.yaml` — L1+L2 配置集中
- [x] **T15.7** 真实数据 1000 候选验证 (132.9s, 956 有效, 1-5 promoted)
- [x] **T15.8** 健康分交叉验证 (3 个 dsl 因子进 WATCH, |IC| -0.042)

---

## T16 实时数据同步 (2026-06-02)

- [x] **T16.1** `data/live_sync/mt5_puller.py` — MT5 实时 bar 拉取 (history + incremental + 字段映射 tick_volume→volume + 当前 bar 检测)
- [x] **T16.2** `data/live_sync/bar_filter.py` — 去重(db max time) + 当前 bar skip + 完整性检查
- [x] **T16.3** `data/live_sync/db_inserter.py` — DataStore 包装 + 错误重试 + sync 状态持久化
- [x] **T16.4** `data/live_sync/orchestrator.py` — full_sync / incremental_sync + 多 timeframe
- [x] **T16.5** `data/live_sync/daemon.py` — 后台守护进程 (once / daemon 模式)
- [x] **T16.6** `scripts/live_sync.py` — CLI (--mode once/daemon/status)
- [x] **T16.7** **`hermes cron` job 接管 (2026-06-03 改)**: `job_id=54c849d80e9d`, `every 5m`, `no_agent=True`, `script=~/.hermes/scripts/live_sync_5m.py`, 强制 Python 3.12 (hermes 自带 3.11 venv 缺包), SILENT watchdog pattern. ~~`scripts/live_sync_daily.bat` (Windows Task Scheduler)~~ **已删**
- [x] **T16.8** 真实数据验证 + baseline 重跑 (+412.20% / 743t)

**db 修复 + 清理 (2026-06-02 23:40)**: bars 表 (DataStore 走这里) 50000 老 bar time TEXT → 统一 INTEGER, 共 298949 行 (6 timeframe × 49825 平均, 含 T16 实时增量). **candles 表 (TEXT time) 已 DROP TABLE** (原 298500 行僵尸, 没人用, 备份 `data/market_data.db.pre_drop_candles.bak`). `risk/regime.py:477` 改写走 bars (INTEGER time, 实时 D1). 整体 db 干净, 唯一表路径走 bars.

---

## 自进化差距 (2026-06-03 评估)

**当前状态: 3/3 闭环** (T15.5 wiring + ProbabilityCalibrator 持久化 + L2 GP 因子搜索升级 T15.3 v2). 2026-06-03 下午全部完成.

### 1. T15.5 闭环 wiring ✅ **(closed 2026-06-03)**
- DSL 发现 → 持久化 → 策略消费 的最后一公里已打通
- 根因: `strategy_registry.create()` 先 `cls(...)` 跑 `__init__` 再 `instance.params = params`, 致使 `self.params.get('include_shadow_factors')` 永远是类默认 False
- 修法: lazy load — `_load_shadow_factors()` 挪到 `on_bar` 第一次调用时 (用 `_shadow_loaded` 守卫), 此时 `self.params` 已被 registry 覆盖
- 验证: A/B 测试 PnL delta=-24.06%, DD 同步改善 -17.39pp, shadow factors 实际投票 (`Strategy._shadow_factors=3` after run)
- 副作用发现: 影子因子在 OOS 上 net-negative PnL (过拟合信号), 但 DD 改善, 后续可校准 `shadow_top_pct` / `shadow_vote_weight`

### 2. ProbabilityCalibrator 持久化 ✅ **(closed 2026-06-03)**
- 启动时优先从 `data/charts/calibrator_bucket.json` 加载 (P0-7 实测 8 桶桶级表), 文件缺失 / 加载失败回退 identity
- main.py 新增 CLI: `--calibrator-path` (默认 `data/charts/calibrator_bucket.json`) / `--calibrator-save` (预留)
- `scripts/test_calibrator_persistence.py` 5/5 通过 (load / 校准生效 / roundtrip / 缺失回退 / Platt 一致)
- 副作用: 校准后信号变化 (0.75→0.60, 0.85→1.00), 后续可在 MAB paper 跑 A/B 验证 (校准 on vs off 对 PnL 影响)

### 3. L2 GP 因子搜索 (T15.3 v2) ✅ **(closed 2026-06-03)**
- 引擎: `alpha/factor_search_gp.py` — Genetic Programming (population/tournament/crossover/mutate/elite), 复用 `alpha/factor_dsl.py` AST + `alpha/factor_score_evaluator.py` fitness
- A/B 验证 (5000 M15 bar):
  - random 1000c:  top1=70.38 (10.9s)  baseline
  - GP 100x10:    top1=72.17 (17.1s)  **+1.79 vs random**
  - GP 50x30:     top1=72.74 (24.2s)  **+2.36 vs random** (推荐配置)
- GP 历史曲线 63.8→72.7, 代际持续爬升无早熟
- 测试脚本: `scripts/test_gp_search.py` (A/B+报告) + `scripts/test_gp_search_v2.py` (3 variants 对比)
- 落盘: `data/charts/factor_discovery/gp_run_*.json` (时间戳)
- 未来改进 (本次未做): 多岛屿 GP / warm start / 适应度 shaping / 减 mut 后期精调

### 3b. P2 资金费建模 (closed 2026-06-03 下午, 不计自进化差距)
- `execution/paper_engine.py` 加 swap 字段 + 计算: `swap_cost = swap_rate * pos.volume * hold_days`
- 参数: `enable_swap=True`, `swap_long_per_lot_per_day=-1.0`, `swap_short_per_lot_per_day=0.0`
- 5000 bar A/B: A off +24.79% / B on -1/day -0.04% / C stress -5/day -0.20%, swap 影响极小
- Bug 修: paper_engine `_close` 路径 `bar_time` 透传 (避免 entry_time 落 utcnow)
- 报告: `data/charts/swap_funding_report.txt`

### 3c. 影子因子 + Calibrator 校准 A/B (closed 2026-06-03 下午, 不计自进化差距)
- 影子 32 组合扫描: `vote_weight < 1` 等于无影响 (整数票 floor), `vw >= 1` 拖累; 默认 `vw=0` 关闭, 开启需显式 CLI
- 校准 A/B: P0-7 桶级 calibrator 在 3000 bar OOS 反伤 (Sharpe 1.85 vs 3.59), wiring OK 需 retrain

---

## P2 — Tier 2 因子/回测工程 (⏳ 待启动, T14-T15 已完成因子 DSL)

P2 的"因子 DSL"部分已完成 (T14-T15). 剩余项目按优先级:

### 立刻能做 (1-2 小时, 无外部依赖)
- [x] **P2 SL/TP bid-ask** (2026-06-03) — bars 表加 spread 字段, paper_engine 按 bid/ask-extreme 判定
- [x] **P2 资金费建模** (2026-06-03 下午) — swap_cost = rate * volume * hold_days, paper_engine wiring 完, A/B 通过

### P2 自主进化 8 项接入 (2026-06-03 下午全完成)
- [x] **PR-1.3** Calibrator retrain 后自动 save: walkforward 末尾 fit+save, 备份 .json.bak
- [x] **PR-1.6** 影子因子默认 vote_weight=0 永久 (开启需显式 CLI)
- [x] **PR-1.8** 日终 paper dryrun cron: scripts/daily_paper_dryrun.py
- [x] **PR-2.1** L2 GP 发现 cron 化: scripts/auto_discover_daemon.py (auto_register 默认开)
- [x] **PR-2.5** shadow 7 天 HEALTHY -> DISCOVERED 升级: scripts/promote_shadow_to_active.py
- [x] **PR-3.2** SEVERE_DRIFT -> GP re-search: scripts/drift_research_daemon.py + main.py --use-drift-research
- [x] **PR-3.4** T13 skip -> meta-learner batch: scripts/t13_skip_backfill.py (补 19,909 bar / 40% 数据)
- [x] **PR-3.7** Regime 周期重训: scripts/regime_retrain.py (LogisticRegression, 41ms @ 4949 sample)

### P2 其他项目 (需人工判断)
- [ ] Survivorship bias 检测
- [ ] 未来函数检测
- [ ] Point-in-time DB
- [ ] 递进式上线流程
- [ ] 市场微观结构变化检测
- [ ] 新 regime 出现检测
- [ ] 数据非平稳监控
- [ ] Crowding effect 检测
- [ ] 模型预测 vs 实际 + 自动 retrain/降权

---

## P3 — Tier 1 风险/OMS (机构级, ⏳ 长期)

- [ ] 组合风险 (多策略协方差 + MRC)
- [ ] 压力测试 (2008/2020/3月2020 闪崩)
- [ ] 风险归因 (PnL 分解到因子/策略/时段/资产)
- [ ] 独立 Risk 团队架构
- [ ] 实时风险监控告警
- [ ] FIX 协议 broker 对接
- [ ] 订单状态机持久化 (重启不丢单)
- [ ] Child order 拆单 + parent 跟踪
- [ ] Bonferroni / Holm 校正
- [ ] Deflated Sharpe Ratio
- [ ] CSCV (Combinatorially Symmetric Cross-Validation)
- [ ] 严格 OOS 隔离
- [ ] Synthetic data test
- [ ] 监管报告 (MiFID II / Reg NMS 5+ 年)
- [ ] 模型可解释性
- [ ] 主备切换 / 数据冗余 / 监控指标 / 灰度发布
- [ ] 保证金动态计算 / 多账户分配 / PnL 归因 / Side pocket

---

## P4 — Tier 4 长期方向 (⏳ 长期)

- [ ] 另类信号 (卫星/信用卡/NLP 情绪)
- [ ] 跨资产套利 (商品+外汇+股票+利率+信用)
- [ ] 现货-期货 / 跨交易所 / 跨期套利
- [ ] 执行算法完整实现
- [ ] Iceberg 隐藏大单 + 限价单挂撤博弈
- [ ] 自建数据中心 (Renaissance 级别)

---

## 下一步推荐

1. **T15.5 影子因子校准** — 当前 OOS PnL 净负 (过拟合), 调 `shadow_top_pct` / `shadow_vote_weight` / `shadow_min_samples`, 或加 walk-forward 验证
2. **ProbabilityCalibrator 校准 A/B 验证** — 校准 on vs off 跑 MAB 5000 bar, 看 Sharpe/DD 变化
3. **自进化差距 #3 (跟用户对齐)** — 候选: 影子 retrain 自动化 / 漂移→DSL re-search / Calibrator 定时 save
4. **P2 资金费建模** — 对 XAUUSD+ swap cost 不小, 需建模
5. **GP 因子搜索** (T15.3 v2) — 当前只有随机搜索, GP 能更精
6. **MAB 4 策略调优** — 全局 MAB 还在冷启动, 需更多 bar / 不同 seed 对比
7. **P2 SL/TP 事件日 spread 注入** — FOMC/NFP spread 1-3 USD 时 PnL 影响 2-5%

---

## 真结果存档 (2026-06-02)

### P0 真结果

| 项 | 真数字 | 解读 |
|---|---|---|
| 22 因子 | 4 有效, 18 噪声 | 单因子 M15 黄金 IC < 0.02 是常态 |
| dxy_corr_20 | IC -0.038 (ACTIVE) | 唯一 ACTIVE, regime shift 8 段/514 天 |
| XGBoost OOS | acc 0.5211 / AUC 0.5276 | 比 LogReg AUC 高 0.007 |
| Walk-Forward | 2 fold, mean lift +2.41% | 真实接近 live |
| 校准 | 4.6% gap, 6-bin 表 | xgb [0.6,0.7] 过度自信 +17% |

### 集成层 (T1-T13)

| 配置 | PnL | Trades | Sharpe | DD | 备注 |
|---|---|---|---|---|---|
| **baseline** (单策略 + skip + circuit 关) | **+407.51%** | 738 | **1.807** | 39.77% | |
| MAB 无 T13 | +20.53% | 841 | -0.436 | 169% | breakout/trend OOH 跳爆仓 |
| MAB + **T13** | **+120.75%** | 639 | **0.894** | **64%** | DD 降 105pp, PnL 升 101pp |

### L1/L2 因子

| 项 | 真数字 | 解读 |
|---|---|---|
| 22 因子健康分 | 0 HEALTHY / 2 WATCH / 20 DECAYING | 基础因子全不够强 |
| DSL 1000 候选 | 956 有效, 148 WATCH | 132.9s 跑完, 1-5 独立候选 |
| shadow factor | 7 个 (cross-validation avg >= 50) | 跨进程恢复 6/7 |

### T16 数据同步

| timeframe | db bars | 最新 bar |
|---|---|---|
| M5 | 200199 | 2026-06-02 13:40 |
| M15 | 50182 | 2026-06-02 13:45 |
| H1 | 18045 | 2026-06-02 13:00 |
| D1 | 500 | 2026-05-29 |

### P2 SL/TP bid-ask (2026-06-03)

| 项 | 真数字 | 解读 |
|---|---|---|
| bars 表加 spread 列 | 9 字段 (含 spread) | ALTER TABLE 自动迁移, 老库无缝升级 |
| M15 backfill | 4998/50204 = 10% | broker 限 5000, 老 bar fallback 0.13 USD |
| M15 spread 均值 | 13.13 points = 0.13 USD | XAUUSD+ 当前 spread 约 13 cents |
| 端到端 5000 bar PnL Δ | -0.01% (407.34 → 407.34) | spread 远小于 3ATR=$25, 影响 < 0.5% |
| 单 bar 单元测试 | entry 100.02 → 100.12 (half spread) | 框架生效, 行为符合 bid-ask 模型 |

---

## BLOCKED — 待澄清

- [ ] DXY 真数据源 (FRED 无标准 series_id, 现 DTWEXBGS 代理)

---

## P0-ETF/CB/COT + BUGFIX (2026-06-03) — 因子纬度扩展 + 质量修复

### P0-BUGFIX (4/4) ✅
- **BUG-1**: `core/state.py` `daily_loss_pct` abs() → max(0, -pnl) (盈利日不再误熔断)
- **BUG-2**: `risk/circuit.py` `reset()` 不再覆写 `peak_equity` (DD 统计修正)
- **BUG-3**: `alpha/factor_engine.py` IC 多周期 `forward_periods` 真实实现 (1/5/10/20-bar)
- **BUG-5**: `core/state.py` + `paper_engine.py` 零净利交易 break_even 单独计

### P0-ETF (GLD/SLV 持仓) ✅
- **数据**: `etf_holdings` 表, GLD/SLV close → 1208 行价格代理 + 5 行真实 SEC 提取
- **SEC 真数据**: Q1 2026 10-Q: 362.5M shares, 月 oz/share 0.09194/0.09191/0.09188
- **因子**: `gld_tonnes_chg_5d/20d`, `gld_tonnes_zscore_60d`, `slv_tonnes_chg_20d` 等 6 个
- **H1 IC**: `slv_tonnes_chg_20d` 0.298 (#1), `gld_tonnes_zscore_60d` 0.294 (#2)

### P0-CB (央行黄金) ✅
- **数据**: `cb_gold` 表, 5 国 (CHINA/RUSSIA/TURKEY/INDIA/TOTAL), 140 行手工录入
- **因子**: `cb_total_chg_3m`, `cb_china_chg_3m` 等 4 个
- **H1 IC**: `cb_total_chg_3m` 0.025 (#10)

### P0-COT (CFTC 持仓) ✅
- **数据**: `cot_gold` 表, 856 周 (2010-2026, 16.4 年), CFTC disagg txt zip 自动拉取
- **因子**: `cot_mm_net`, `cot_pm_net`, `cot_extreme_signal` 等 6 个
- **H1 IC**: `cot_pm_net` -0.047 (#6), `cot_mm_net` +0.036 (#9)
- **H1 PnL 验证**: baseline -187% + COT voter 转正 +6.36% (救场因子)

### H1 全因子 Top 10 (2026-06-03)
1. slv_tonnes_chg_20d +0.298 (ETF proxy)
2. gld_tonnes_zscore_60d +0.294 (ETF proxy)
3. slv_gld_ratio +0.102 (跨资产)
4. macd_hist +0.069 (技术)
5. gld_tonnes_pct_20d +0.061 (ETF proxy)
6. **cot_pm_net** -0.047 (真实 COT)
7. gld_tonnes_chg_20d +0.044 (ETF proxy)
8. cb_total_chg_3m +0.025 (央行)
9. **cot_mm_net** +0.036 (真实 COT)
10. bb_width -0.042 (技术)

---

**2026-06-03 BUGFIX + P0 扩展到 39 因子:**

---

**2026-06-02 文档整理:**
- 合并 `TODO.md` + `ROADMAP.py` → 本文件
- 删 6 个废弃临时脚本
- 删空壳 `quant_trading_framework/` 和空 `tmp/`
- 删 `experts/` / `modules/risk_manager.py` / `fetch_vix.py` / `backtest/engine.py`
- 保留 CSV 源数据, `modules/{data_fetcher,database}.py` shim 仍被 3 个 scripts 引用
- db 修复: bars 表 50000 老 bar time TEXT → INTEGER (298949 行, 6 timeframe 含 T16 实时), candles 表 (TEXT) 已 DROP (备份 .pre_drop_candles.bak), regime 改走 bars
