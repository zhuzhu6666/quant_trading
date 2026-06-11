# 项目路线图 (ROADMAP)

> 单源待办 — 替代旧的 ROADMAP.py (已删) 和 TODO.md (已合并)
> 2026-06-11: 自进化 + cTrader 全流 + 评估框架 + 部署层完成

---

## 进度摘要

| 阶段 | 状态 | 备注 |
|---|---|---|
| P0 (1-7) 因子/模型/训练 | ✅ 7/7 | 39 因子 / PCA / IC 监控 / XGBoost / Walk-Forward / 元学习 |
| P1 (A-G) MT5/路由/数据 | ✅ 6/7 | 缺 P1-G 合规检查 (跳过) |
| P3 circuit 调优 | ✅ 1/1 | 15% CB (2026-06-06 调参) |
| T1-T13 集成层 | ✅ 13/13 | MAB 多策略 + 9 自学习组件 + T13 事件过滤 |
| T14 L1 因子生命周期 | ✅ 3/3 | FactorHealth + RegistryAdapter + main.py 接入 |
| T15 L2 因子 DSL | ✅ 8/8 | parser + 搜索 + orchestrator + persistent registry + 闭环 |
| T16 实时数据同步 | ⏸ **暂停** | Python MT5 包 vs terminal 2026 IPC pipe 不匹配 |
| Phase 1-5 审计 | ✅ 完结 | 8 fix + 7 refactor + 5 opt + 3 verify + 调参 |
| **自进化全闭环 (P5)** | ✅ **2026-06-11** | Orchestrator + Scheduler + Canary + WeightPolicy + Retire |
| **cTrader 执行** | ✅ **2026-06-11** | 开→SLTP→平 全流通过, 5 bug 修复 |
| **外部数据自动刷新** | ✅ **2026-06-12** | `scripts/refresh_external_data.py` + start-all.py 整合 |
| **因子评估框架** | ✅ **2026-06-11** | PurgedWalkForward + BootstrapCI + CausalCheck + Attribution |
| **部署层** | ✅ **2026-06-11** | canary.py + weight_policy.py + risk_rebalancer.py |
| **Web Console** | ✅ 2026-06-07 | Vite + React 19 重构, 43 REST + 1 WS |

---

## 已完成里程碑

### P0 — 因子/模型/训练 (全部完成)

- [x] **P0-1** 因子补齐 8 (ema_slope / supertrend_str / keltner / obv / vol_ma / engulfing / pin / inside)
- [x] **P0-2** PCA + 相关矩阵 (4 有效因子, 4 PC=90%, 7 冗余对)
- [x] **P0-3** 跨资产/事件/时段 7 (dxy_corr_20 IC 0.034)
- [x] **P0-4** IC rolling 接 live (514 锚点 + regime shift 告警)
- [x] **P0-5** XGBoost 升级 (OOS acc 0.5211 / AUC 0.5276)
- [x] **P0-6** Walk-Forward (2 fold, mean lift +2.41%)
- [x] **P0-7** 元学习监控 (校准误差 4.6%)

### P1 — MT5/路由/执行 (6/7)

- [x] **P1-A** MT5 整合 (mt5_bridge: filling mode + fetch + 紧急平仓)
- [x] **P1-B** 智能路由 (TWAP/VWAP/POV/IS)
- [x] **P1-C** 拉真实数据 (5000 bar 对比)
- [x] **P1-D** 影子交易 (dual-router)
- [x] **P1-E** A/B 测试 (3 baseline)
- [x] **P1-F** 紧急平仓 (close_all_positions)
- [⏭] **P1-G** 合规检查 — 跳过

### T1-T13 — MAB 集成层

- [x] **T1** MABRouter 4 策略共享 paper
- [x] **T2-T10** 7 个自学习组件 + 校准 + 告警 + 调度 + retrain
- [x] **T13** SharedEventFilter (MAB 业务层关键修复)

### T14-T15 — L1/L2 因子系统

- [x] **T14.1-3** 因子健康 + 注册适配器 + main.py 接入
- [x] **T15.1-8** DSL parser + GP 引擎 + 搜索 + 闭环 wiring

### 自进化全闭环 (P5, 2026-06-11)

- [x] **EvolutionOrchestrator** — GP→OOS→Canary→WeightPolicy→Retire 编排
- [x] **InProcessScheduler** — 5-job cron (evolution_hourly, canary_fast, retire_hourly, sync_health, data_pull)
- [x] **CanaryDeploy** — 金丝雀部署 (1 pos/2h → promote/rollback)
- [x] **WeightPolicy** — 因子权重策略 (IC/Sharpe/衰减)
- [x] **RiskRebalancer** — 账户风险重平衡
- [x] **EvolutionStory** — 事件日志 + 可视化报告

### cTrader 执行 (2026-06-11)

- [x] **PoC** — TCP + App/Account auth + Symbol resolve
- [x] **全流** — market_buy → amend_position_sltp → close_position
- [x] **5 bug 修复** — symbolId嵌套/price缩放/volume必填/ExecutionEvent解析/OrderError处理
- [x] **回归测试** — `scripts/test_ctrader_full_flow.py`

### 因子评估框架 (2026-06-11)

- [x] **PurgedWalkForward** — 清洗前向验证
- [x] **BootstrapCI** — 自助法置信区间
- [x] **CausalCheck** — 因果检验
- [x] **Attribution** — 收益归因

### Web Console (2026-06-07)

- [x] Vite + React 19 + Tailwind 3 重构
- [x] 43 REST 端点 + 1 WebSocket
- [x] JWT 认证 + 5 个下滑面板

---

## 待启动

### 高优先级
- [ ] **cTrader 影子 A/B** — 同信号 MT5 vs cTrader 双执行对比
- [ ] **T16 MT5 数据同步修复** — 降级 MetaTrader5 包或换 cTrader 行情
- [ ] **50K bar K线端点优化** — 当前 3.1s, 目标 <500ms

### 中优先级
- [ ] **JWT_SECRET env 化** — 当前硬编码在 `backend/core/auth.py`
- [ ] **Playwright E2E 实跑** — 测试代码就位, 需 playwright chromium
- [ ] **组合风险** — 多策略协方差 + MRC
- [ ] **CSCV / Deflated Sharpe Ratio** — 多重假设检验

### 长期 (外部依赖)
- [ ] MT5 充值 (blocked-1)
- [ ] 另类信号 (卫星/情绪)
- [ ] 跨资产套利

---

## 真结果存档

### PnL (multi_factor_m15, M15, 50K bar)

| 配置 | PnL | Trades | Sharpe | DD |
|---|---|---|---|---|
| 无风控 baseline | +407.51% | 738 | 1.807 | 39.77% |
| MAB T1-T13 | +120.75% | 639 | 0.894 | 64% |
| **调参最优 (risk=1% CB=15%)** | **+59.17%** | 354 | **0.936** | **15.9%** |

### 因子健康 (2026-06-06)

2 HEALTHY / 45 WATCH / 18 DECAYING (65 因子)
- HEALTHY: gld_tonnes_zscore_60d (95.2), cot_mm_net_pct_oi (83.8)

### cTrader 全流 (2026-06-11)

connect 6.1s → market_buy ✅ → amend sl/tp ✅ → close ✅
- account: 47276606 @ Pepperstone demo, 1000 JPY
- 5 bug 修复 (见 `docs/CTRADER_INTEGRATION.md`)

---

**最优先**: 继续跑稳 cTrader 执行通道, 建回归测试护城河
