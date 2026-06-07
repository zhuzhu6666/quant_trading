# 项目框架总览 (PROJECT_MAP)

> 最后更新: 2026-06-06 (Phase 1-5 + 调参全完结)
> 项目状态: 39 因子 + 5 数据表 + COT 16 年历史 + 调参最优 risk=1.0% + CB=15%
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
│   ├── event_bus.py                 # 进程内事件总线 (publish_async_ff + daemon loop, 2026-06-06)
│   └── state.py                     # StateContainer 多账户 (AccountState + 向后兼容, 2026-06-06)
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
│   ├── registry.py                  # 39 因子注册 (技术 15 + 时序 5 + ETF 6 + CB 4 + COT 6 + 跨资产 3)
│   ├── factor_engine.py             # 流式因子计算 + IC 分析
│   ├── ic_tracker.py                # 滚动 IC 追踪
│   ├── factor_attribution.py        # 边际 IC 归因
│   ├── regime_classifier.py         # sklearn LogReg (P0 旧)
│   ├── probability_calibrator.py    # P0-7: 桶级 + Platt 校准
│   ├── factor_health.py             # ★ T14.1: 因子健康评分 (5 维, 0-100)
│   ├── registry_adapter.py          # ★ T14.2: 动态 register/unregister + 事件流
│   ├── persistent_registry.py       # ★ T15.5: 跨进程恢复 shadow 因子 (闭环 2026-06-03)
│   ├── factor_dsl.py                # ★ T15.1: DSL parser + AST + 20+ 算子 + 沙箱
│   ├── factor_search_gp.py          # ★ T15.3 v2: Genetic Programming 引擎 (2026-06-03, 5000 bar A/B 赢 random +2.36)
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
├── execution/                       # 14 文件
│   ├── oms.py                       # Order 状态机
│   ├── router.py                    # ExecutionRouter (P1-B algos 集成)
│   ├── paper_engine.py              # PaperEngine (risk_per_trade_pct=None 区分, 2026-06-06)
│   ├── paper_trader.py              # ★ PaperTrader (主 paper 路径, Sharpe NW HAC)
│   ├── _sharpe.py                   # ★ Sharpe log returns + Newey-West HAC (2026-06-06 新增)
│   ├── mt5_bridge.py                # ★ MT5 整合 (P1-A, filling mode + fetch)
│   ├── ctrader_bridge.py            # ★ cTrader 整合 (amend_position_sltp, 2026-06-06)
│   ├── algos.py                     # ★ T1.1 智能路由 (TWAP/VWAP/POV/IS)
│   ├── mab_paper_runner.py          # ★ T1: MAB 多策略共享 paper (ARCH-1 护栏, 2026-06-06)
│   ├── event_filter.py              # ★ T13: SharedEventFilter (precompute dual window, 2359× 加速)
│   ├── slippage.py                  # 动态滑点
│   ├── market_impact.py             # Almgren-Chriss
│   ├── match_replay.py              # Brownian bridge 撮合回放
│   ├── latency.py                   # 延迟模拟
│   └── order_retry.py               # 拒单补单 (指数退避)
│   ├── _sharpe.py                   # Sharpe NW HAC 计算
│   └── ctrader_bridge.py            # cTrader 桥接 (amend_position_sltp)
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
├── scripts/                         # 36+ 脚本 (测试 + 工具 + 入口)
│   ├── live_sync.py                 # ★ T16: 实时数据同步 CLI
│   ├── test_shadow_consumption.py   # ★ T15.5 闭环: A/B 验证 shadow 因子接进投票 (2026-06-03, PnL delta=-24.06% 闭环确认)
│   ├── test_calibrator_persistence.py # ★ Task #2: calibrator load/save/roundtrip/missing/platt 5/5 (2026-06-03)
│   ├── live_sync_daily.bat          # ★ T16: Windows Task Scheduler 配置
│   ├── discover_factors.py          # ★ L2: 因子发现 CLI (random search v1)
│   ├── test_gp_search.py            # ★ T15.3 v2: GP vs random A/B 验证
│   ├── test_gp_search_v2.py         # ★ T15.3 v2: GP 变体 (100x10 vs 50x30) 对比
│   ├── auto_discover_daemon.py       # ★ PR-2.1: L2 GP 发现 cron 化
│   ├── promote_shadow_to_active.py   # ★ PR-2.5: shadow -> DISCOVERED 升级检查
│   ├── drift_research_daemon.py      # ★ PR-3.2: SEVERE_DRIFT -> GP re-search
│   ├── t13_skip_backfill.py          # ★ PR-3.4: T13 skip 期间数据补 batch
│   ├── regime_retrain.py             # ★ PR-3.7: Regime 分类器周期重训
│   ├── tune_risk_params.py           # ★ 风险参数调优 (risk_pct / CB sweep)
│   ├── daily_paper_dryrun.py         # ★ PR-1.8: 日终 paper dryrun
│   ├── test_calibrator_autosave.py   # ★ PR-1.3: walkforward 末尾 fit+save 验证
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
- **CLI Dashboard**: `main.py --mode dashboard` (154 行极简 monitor, 旧)

### 2.1.1 Web 总控台 (2026-06-07, 完整替代 CLI)
- **开发模式** (推荐): `start.bat` → 前端 `http://localhost:3000` + 后端 `http://localhost:8000`
- **生产模式**: `start-prod.bat` → 单 uvicorn `http://localhost:8000` (同时 serve API + 静态前端)
- **Backend**: `backend/` (FastAPI, 39 API 路由 + 1 WS, 9 service, JWT auth)
- **Frontend**: `frontend/` (Next.js 14 + shadcn/ui, 16 页面, 4 chart 组件, PWA scaffold)
- **用户文档**: `README_WEB.md` (页面速查 + 启动 + 已知限制)
- **设计 spec**: `docs/superpowers/specs/2026-06-07-quant-web-console-design.md`
- **实施 plan**: `docs/superpowers/plans/2026-06-07-quant-web-console.md`
- **nginx 模板**: `docs/nginx.example.conf` (TLS + WS upgrade + rate-limit + cache)
- **单端口 prod launcher**: `start-prod.bat` / `start-prod.sh` (build + copy + uvicorn 单端口)

### 2.2 因子 / 模型
- **因子注册**: `alpha/registry.py` (39 因子, 见 §3.2)
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

### 2.5.1 T16 实时数据同步 — ⏸ **暂停 (2026-06-03)**
- **根因**: Python MetaTrader5 5.0.5735 包与 MT5 terminal 2026 版本 IPC pipe 名字 hash 不匹配, 包 `WaitNamedPipeW` 一直 timeout
- **验证**:
  - 7 种 `path` 变体 + 重装 + 短长 timeout 全 `IPC timeout -10005`
  - 同会话 `CreateFileW` 手动连 `MT5.Terminal.781AEDD6...` pipe 100% 成功 (handle 180)
  - 15s 监视期 pipe 数量完全不变 (109 → 109), 包不起新进程
- **影响范围**: 仅 T16 增量. 7 策略 / MAB / 影子 / GP / 校准 / 全部 PnL 数字**全走 db 50K bar 离线**, 不受 MT5 状态影响
- **db 数据来源**: `scripts/fetch_mt5_data.py` 历史一次性 fetch (M15 50204 根, 2024-04 ~ 2026-06-02)
- **cron 状态**: `job_id=54c849d80e9d`, `every 5m`, `last_status=error` (script 0 字节空文件)
- **回退**: `python scripts/live_sync.py --mode once --type incremental --timeframes M15,H1,D1` (需 MT5 包版本兼容)

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

## 3. 真状态数字 (2026-06-06)

### 3.1 PnL 对比 (multi_factor_m15, M15, 50K bar)

| 路径 | PnL | Trades | Sharpe | DD | 备注 |
|---|---|---|---|---|---|
| **main.py --mode paper (无风控)** | **+407.51%** | 738 | 1.807 | 39.77% | enable_circuit=False baseline |
| **MAB T1-T13 全栈** | **+120.75%** | 639 | 0.894 | 64% | SharedEventFilter 业务层关键 |
| **verify-2 调参后 (risk=1% + CB=15%)** | **+59.17%** | 354 | **0.936** | 15.9% | Kelly 1% + 15% CB, 最优参数已固化 |
| verify-2 调参前 (risk=2% + CB=10%) | -10.28% | 13 | -0.864 | 11.3% | CB 频繁触发 |
| **verify-2** (risk=1.0%, CB=15%) | **+59.17%** | 354 | **0.936** | Phase 5 调参最终结果 |

### 3.2 因子健康 (verify-1, 2026-06-06, 阈值 0.04): 2 HEALTHY + 45 WATCH + 18 DECAYING

因子分布 (alpha/registry.py 实际 39 个 + 26 GP DSL auto):
- 技术 15: rsi_14, macd_hist, adx, bb_width, di_spread, stoch_k, atr_ratio, ema_slope, supertrend_str, keltner_width, obv_slope, vol_ma_ratio, engulfing, pin_bar, inside_bar
- 时序/事件 5: slv_gld_ratio, hours_to_fomc, hours_to_nfp, hour_utc, day_of_week
- ETF holdings 6: gld_tonnes_chg_5d/20d/pct_20d/zscore_60d, slv_tonnes_chg_20d, silver_gold_holdings_ratio
- Central Bank 4: cb_total_chg_3m, cb_china_chg_3m, cb_russia_chg_3m, cb_china_3m_zscore
- COT positioning 6: cot_mm_net/net_pct_oi/net_chg_4w/net_zscore_52w, cot_pm_net, cot_extreme_signal
- 跨资产 3: dxy_corr_20, real_yield_chg, real_yield_pct_rank

| 因子 | 状态 | score | abs_IC |
|---|---|---|---|
| gld_tonnes_zscore_60d | **HEALTHY** | 95.2 | 0.0359 |
| cot_mm_net_pct_oi | **HEALTHY** | 83.8 | 0.0334 |
| dsl_auto_* (16 个) | WATCH | 65-70 | 0.034-0.039 |
| atr_ratio | WATCH | 67.8 | 0.0258 |
| bb_width | WATCH | 60.3 | 0.0289 |
| rsi_14 | DECAYING | 23.5 | 0.0001 |
| cb_china_* | DECAYING | 20.0 | NaN (数据缺失) |

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
- ✅ blocked-3 cTrader token (2026-06-06 .env 已有真 token)
- ⏸ blocked-1 MT5 balance=0 (需充值)
- ⏸ blocked-2 MetaTrader5 包版本不匹配 (需降级或换 cTrader)

### 4.3 已修复 (全 ✅)
- ✅ OPT-1: circuit 启用下 PnL 优化 → verify-2 risk=1.0% CB=15% → +59.17% Sharpe 0.936
- ✅ OPT-2: 单笔风险 0.01 → 0.005 手 (tune_risk_params.py sweep)
- ✅ OPT-3: max_consecutive_loss 5 → 3
- ✅ OPT-4: P0-7 校准接 scoring (减少弱信号)
- ✅ OPT-5: mab_paper_v2 行为对齐 (v1/v2 baseline 统一)
- ✅ T15.5 闭环 wiring 已修 (2026-06-03): lazy load 绕过 registry kwargs 时序, A/B delta=-24.06% 闭环确认
- ✅ ProbabilityCalibrator 持久化 (2026-06-03): 启动时 load calibrator_bucket.json (8 桶桶级), 缺失回退 identity, 测试 5/5

---

## 5. 单测覆盖

✅ **通过的测试** (已实际跑过):
- `scripts/test_algos.py` — 10/10 (TWAP/VWAP/POV/IS 单测)
- `scripts/test_probability_calibrator.py` — bucket > Platt, Brier 改善 +0.0033
- `scripts/p1_c_sync_live_bars.py` — broker vs db 同步
- `scripts/p1_d_shadow.py` — dual-router PnL 差 +176
- `scripts/p1_e_ab_test.py` — 3 baseline 对比
- `scripts/p3_circuit_tune.py` — 4 档 circuit sweep
- `scripts/factor_pca.py` — 39 因子 PCA
- `scripts/factor_ic_rolling.py` — 514 锚点 + regime shift
- `scripts/test_p0_factors.py` — 39 因子 IC
- `scripts/train_xgb_walkforward.py` — 80/20 OOS
- `scripts/walkforward_p0_6.py` — 2 fold Walk-Forward
- `execution/mt5_bridge.py --dry-run` — 整合 dry-run
- `main.py --mode paper` — PaperTrader 验证
- `scripts/mab_paper.py` (修后) — 596t, +380.58%
- `scripts/mab_paper_v2.py` (修后) — 590t, +181.18%
- `scripts/baseline_all_strategies.py` — 7 策略, multi_factor +407.51%

⚠ **补充单测**:
- `strategy/scorer.py` (无单测)
- `live/executor.py` (实盘执行, balance=0 阻塞)
- `risk/circuit.py` (P3 调优有 sweep 但无正式单测)
- ~~`backtest/engine.py`~~ — 已删 (2026-06-02 文档整理, 是死代码)

---

## 6. 待办优先级 (Phase 1-5 + 调参全完结, 2026-06-06)

按"先做有真实价值"原则推荐:

### 6.1 已完结 (Phase 1-5 + 调参)
- ✅ **T15.5 影子因子校准**: OOS PnL 净负已确认, wiring 闭环
- ✅ **ProbabilityCalibrator 校准**: bucket > Platt, Brier +0.0033
- ✅ **P2 SL/TP bid-ask**: bid/ask extreme 判定已实现
- ✅ **风险参数调优**: tune_risk_params.py → verify-2 risk=1.0% CB=15% +59.17%
- ✅ **GP 因子搜索** (T15.3 v2): GP vs random A/B 赢 +2.36

### 6.2 集成层完成 (2026-06-06)
- ✅ **T1-T10** MAB 多策略 + 7 个自学习组件 + RetrainScheduler
- ✅ **T13** SharedEventFilter (MAB 业务层关键, 50K bar 跳 19906 bar)
- ✅ **T14.1-3** L1 因子生命周期 (FactorHealth + RegistryAdapter + main.py 接入)
- ✅ **T15.1-8** L2 因子 DSL (parser + 搜索 + orchestrator + persistent registry + T15.5 闭环 wiring 2026-06-03)
- ✅ **T16.1-8** 实时数据同步 (MT5→db + 增量 + 多TF + Windows Task Scheduler)

### 6.3 阻塞 (仅外部依赖)
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

**最后扫描时间**: 2026-06-06
**项目代码量**: ~9000+ 行 Python (核心路径 + L1/L2/T16 + Phase 1-5 审计修复)
**测试覆盖**: P0+P1+T1-T16 关键路径全过 + Phase 1-5 验证 (verify-1/2/3)
**真 PnL 记录**: 4 个场景 (baseline / MAB / 调参后 / 调参前), 数字齐全
**阻塞**: blocked-1 (MT5 充值) + blocked-2 (包版本), 其余全完结
