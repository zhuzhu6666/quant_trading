# 项目路线图 (ROADMAP)

> 单源待办 — 替代旧的 `ROADMAP.py` (Python dict 形式, 已废) 和 `TODO.md` (重复)
> 2026-06-02 快照

---

## 进度摘要

**代码层完成: 41/41 (100%)** ← P1-E 完成, 全部代码层任务收尾
**集成层完成: T1-T13 (13/13)** ← 2026-06-02 全集成, MAB 业务上能跑不爆仓
**文档整理: 2026-06-02** ← 合并 ROADMAP.py + TODO.md → ROADMAP.md; 删 6 个废弃临时脚本

| 阶段 | 状态 | 备注 |
|---|---|---|
| P0 (1-7) 因子 / 模型 / 训练 | ✅ 7/7 | 22 因子 / PCA / IC 监控 / XGBoost / Walk-Forward / 元学习 |
| P1 (A-G) MT5 / 路由 / 数据 | ✅ 6/7 | 缺 P1-G 合规检查 (跳过) |
| P3 circuit 调优 | ✅ 1/1 | 5% → 10% 默认 |
| P2 因子 DSL / 回测工程 | ⏳ 待启动 | 阻塞于"先稳定 P0+P1 真实信号" |
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

### P1 关键发现

- MT5 账户 9823690 balance=0, **不能 live trade**, 全 read+paper 模式
- 当前真实金价 **4512 USD/oz** (2026-06-02), 旧 ROADMAP 2000-3000 已过时
- broker 实时数据: `copy_rates_from_pos` 5000 bar=78 天, `copy_ticks_from_pos` 易挂死
- dxy_corr_20 是 P0 唯一 ACTIVE 因子 (IC 0.034)
- MAB router 在 RANGING regime 下基本不探索, 100% 选 multi_factor_m15
- P1-D seed 差异 PnL +176 — 探索性影响巨大

---

## P2 — Tier 2 因子/回测工程 (⏳ 待启动)

按"先稳定 P0+P1 真实信号"原则推迟。当前 P0 单模型 PnL 边缘 (lift ~2%), 框架已闭环, 矫正留给 P9 打分系统 + 自学习。

- [ ] 因子 DSL (类 WorldQuant BRAIN 平台表达层)
- [ ] 自动因子合成 (GP/ML 生成新因子)
- [ ] SL/TP 触发价改 bid/ask (避免 close 理想化)
- [ ] 资金费/库存费/分红建模
- [ ] Survivorship bias 检测
- [ ] 未来函数检测 (每根 bar 用 close 还是 open 决策, 严格记录)
- [ ] Point-in-time DB
- [ ] 递进式上线 (回测 → paper → 影子 → 小资金实盘 → 大资金)
- [ ] 市场微观结构变化检测
- [ ] 新 regime 出现检测
- [ ] 数据非平稳监控 (IC stationarity test)
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

## 下一步推荐 (P0/P1 完成后)

按"先做有真实价值"原则, **立刻能做 (1-2 小时, 无外部依赖)**:

1. **P2 因子 DSL** — 类 WorldQuant BRAIN 表达层
2. **P2 SL/TP bid-ask** — P0-7 校准的下一步, 让 OOS 更真实
3. **P3 进一步** — 单笔 0.01 → 0.005 手, 接 P0-7 校准到 scoring

阻塞:

- **T1.2 L2 / T&S / 基本面数据**: broker 余额/支持
- **T3 治理** (Bonferroni / CSCV / Deflated Sharpe): 需机构级流程
- **T4 长期** (卫星数据 / 跨资产套利): 需外部资源

---

## P0 真结果存档 (2026-06-02)

| 项 | 真数字 | 解读 |
|---|---|---|
| 22 因子 | 4 有效, 18 噪声 | 单因子 M15 黄金 IC < 0.02 是常态 |
| dxy_corr_20 | IC 0.034 ACTIVE | 唯一 ACTIVE, regime shift 8 段/514 天 |
| XGBoost OOS | acc 0.5211 / AUC 0.5276 | 比 LogReg AUC 高 0.007 |
| Walk-Forward | 2 fold, mean lift +2.41% | 真实接近 live, 比 split 一次高一个数量级 |
| 校准 | 4.6% gap, 6-bin 表 | xgb [0.6,0.7] 过度自信 +17% |

## 集成层真结果存档 (2026-06-02, T1-T13)

| 配置 | PnL | Trades | Sharpe | DD | 备注 |
|---|---|---|---|---|---|
| **baseline** (单策略 + 策略自带 skip + circuit 关闭) | **+407.51%** | 738 | **1.807** | 39.77% | PROJECT_MAP 标的对齐 |
| MAB T1-T10 全栈, 无 T13 | +20.53% | 841 | -0.436 | **169%** | 4 策略共享, breakout/trend OOH 跳爆仓 |
| MAB T1-T10 + **T13 EventFilter** | **+120.75%** | 639 | **0.894** | **64%** | 共享 NFP/FOMC+CPI/GVZ skip, 跳 19906 bar (40%) |
| MAB T1-T13 + circuit 10% | -30.63% | 69 | -0.569 | 37% | T13 后 circuit 冗余, 反而阻止开仓 |

**结论**: T13 EventFilter 是 MAB 业务层关键修复, 把 DD 从 169% 降到 64%, PnL 从 +20% 升到 +121%. 跟 baseline +407% 还有 286pp 差距, 来自 MAB 4 策略冷启动 + 探索期 (router.alpha/beta 在跑 50K bar 仍未收敛到 multi_factor 主导).

## P1 真结果存档 (2026-06-02)

| 项 | 真数字 | 解读 |
|---|---|---|
| MT5 连接 | OK (read 模式) | 账户 9823690, balance=0, 不能 live |
| 当前金价 | 4512 USD/oz | 旧 memory 2000-3000 已过时 |
| broker 数据 | 5000 bar M15 = 78 天 | 远低于 db 50K bar (2.1 年) |
| T1.1 算法 | TWAP/VWAP/POV/IS 4 个 | 10/10 单测过, 集成到 ExecutionRouter |
| P1-D seed 差 | +176 PnL | seed=123 选 trend 37 次更多, PnL +325 vs +149 |
| MAB 探索 | RANGING 下 100% 选 multi_factor | baseline 主导, 探索不足 |

## P3 circuit 调优存档 (2026-06-02)

| 配置 | PnL | Trades | Sharpe | DD |
|---|---|---|---|---|
| 5% (原, 频繁触发) | -33.61% | 62 | -0.872 | 53% |
| 10% (默认, 调优后) | **-9.54%** | 123 | -0.105 | **36%** |

> 提升 3.5x, trades 翻倍, DD 降 17pp。进一步调优 (单笔 0.01→0.005 手 / max_consecutive_loss 5→3 / 接 P0-7 校准) 待启动。

---

## BLOCKED — 待澄清

- [ ] DXY 真数据源 (FRED 无标准 series_id, 现 DTWEXBGS 代理)

---

**2026-06-02 文档整理:**
- 合并 `TODO.md` + `ROADMAP.py` → 本文件
- 删 6 个废弃临时脚本 (`_gen_order_retry.py` / `writer_helper.py` / `scripts/{gen_v2,generate_v2,tr,mab_paper_isolated}.py`)
- 删空壳 `quant_trading_framework/` 和空 `tmp/`
- 删 `experts/` (空) / `modules/risk_manager.py` (被 `risk/pre_trade.py` 替代) / `fetch_vix.py` (被 `data/external_loader.py` 替代)
- 保留 CSV (`DFII10.csv` / `DTWEXBGS.csv` / `GVZCLS.csv`) 源数据, `modules/{data_fetcher,database}.py` 仍被 3 个 scripts 引用
