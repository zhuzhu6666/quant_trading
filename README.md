# Python 量化交易框架

XAUUSD+ 黄金 M15 趋势/回归/因子合成, 7 层架构, 本地 paper + backtest baseline 已实盘验证 (read-only 模式)。

**最后更新: 2026-06-02**

---

## 当前状态 (2026-06-02 验证)

### PnL 数字 (50K bar, multi_factor_m15 M15)

| 路径 | PnL | Trades | Sharpe | 备注 |
|---|---|---|---|---|
| `main.py --mode paper` | **+407.51%** | 738 | 1.807 | enable_circuit=False baseline |
| `mab_paper` 修后 | +380.58% | 596 | 1.452 | MAB router + 事件 skip 真的生效 |
| `mab_paper_v2` 修后 | +181.18% | 590 | 1.105 | v2 行为差异 (trend 选 76 次 vs v1 17 次) |
| paper w/ circuit 10% | -9.54% | 123 | -0.105 | P3 调优后默认值 |
| paper w/ circuit 5% (原) | -33.61% | 62 | -0.872 | 频繁触发 (13+ 次) |

### 代码层进度: 41/41 (100%)

- ✅ P0 (1-7): 因子库 22 个 / PCA / IC 监控 / XGBoost / Walk-Forward / 元学习
- ✅ P1 (A/B/C/D/E/F): MT5 整合 / 智能路由 / 数据拉取 / 影子 / A/B / 紧急平仓
- ✅ P3 circuit 调优: 5% → 10%

### 关键发现

- 4 策略 + 22 因子 + 8 标签 regime + circuit/pre_trade/position 风控实装
- 22 因子 4 有效 (dxy_corr_20 IC 0.034 ACTIVE 金矿, macd_hist 0.022, bb_width/ema_slope fading)
- XGBoost OOS: acc 0.5211 / AUC 0.5276 (lift ~2%, 单模型边缘, 矫正留给 P9)
- Walk-Forward 2 fold: mean lift +2.41% / OOS lift +2.03%
- 校准误差 4.6%, xgb [0.6,0.7] 过度自信 +17%
- MAB router RANGING regime 下 100% 选 multi_factor, 探索不足

> **注: mab_paper +407% 是 bug 下数字 (事件 skip 永远不触发), 修后真实 +380.58%**
> **注: MT5 账户 9823690 balance=0, 不能 live trade, 全 read+paper 模式**
> **注: 当前真实金价 4512 USD/oz (2026-06-02), 旧 memory 2000-3000 已过时**

---

## 目录结构

```
quant_trading/
├── main.py                  # 入口 (backtest/paper/live/dashboard)
├── README.md                # 本文件 (用户文档)
├── ROADMAP.md               # 单源待办 (P0/P1/P2/P3/Tier1-4 任务清单)
├── PROJECT_MAP.md           # 框架索引 + 真状态数字 + 文件路径速查
├── MEMORY.md                # 笔记链接
├── requirements.txt         # Python 3.12 依赖
│
├── config/                  # settings.yaml + instruments.yaml + __init__.py shim
├── core/                    # event_bus / clock / state
├── data/                    # bar_builder / feed / store / tick / external_loader
│   ├── market_data.db       # 100MB M15 bars 50K + macro/events/etf
│   └── analytics.db         # 52K strategy_perf + decision_log
├── db/                      # schema / store
├── factors/                 # aroon / cci / mfi / williams_r (老接口 4 因子)
├── strategies/              # 7 策略: multi_factor_m15 ★ / ma_cross_h4 / macd_bb /
│                            # gold_momentum / trend / mean_rev / breakout
├── strategy/                # base / signal_bus / registry / portfolio /
│                            # mab_router ★ / scheduler / scorer
├── alpha/                   # 22 因子 registry / factor_engine / ic_tracker /
│                            # regime_classifier / probability_calibrator
├── execution/               # oms / router / paper_trader ★ / mt5_bridge ★ /
│                            # algos (TWAP/VWAP/POV/IS) / slippage / impact /
│                            # match_replay / latency / order_retry
├── risk/                    # circuit (P3 调优 10%) / pre_trade / position / regime
├── monitor/                 # dashboard / alerter / alerts
├── live/                    # factor_monitor (P0-4) / meta_learner_monitor (P0-7)
├── modules/                 # 老兼容 shim (1 文件, 包 data.store.DataStore)
├── scripts/                 # 30+ 测试 + 工具脚本 (P0/P1/P3 + 单测)
├── memory/                  # selflearning-scheduler 笔记
├── data/charts/             # 24 报告 + 3 图 + 4 npy + 2 json
└── logs/                    # 运行日志
```

---

## 安装

```bash
# Python 3.12
C:\Users\zhu\AppData\Local\Programs\Python\Python312\python.exe -m pip install -r requirements.txt
```

---

## 运行

```bash
# 回测 (默认 M15, 扫描 SL/TP/CD 12 组合)
python main.py --mode backtest --timeframe M15

# 模拟盘 (复现实盘链路) — 单一策略 baseline, 显式无事件过滤, +407.51% / 738t / Sharpe 1.807
python main.py --mode paper --timeframe M15

# 模拟盘 — MAB 多策略 (T1-T13 全栈, 2026-06-02 集成)
python main.py --mode paper --timeframe M15 \
  --use-router --use-scheduler --use-calibrator \
  --use-meta-monitor --use-factor-monitor --use-alerter \
  --use-retrain --retrain-every-n 300 --use-event-filter

# 因子健康评估 (T14.1, 22 builtin + 已 register dsl 因子)
python main.py --mode paper --timeframe M15 --factor-health-report

# L2 因子发现 (T15.5, DSL 搜索 + 自动 register)
python scripts/discover_factors.py --n-candidates 1000 --top-k 50 \
  --forward-periods 1,5,20 --auto-register

# 实时数据同步 (T16, MT5 → db 正增长)
python scripts/live_sync.py --mode once --type incremental --timeframes M15,H1,D1
python scripts/live_sync.py --mode status  # 查看当前 db bar 数

# 实盘 (stub, 需配 MT5 — 当前 balance=0 阻塞)
python main.py --mode live

# 监控面板
python main.py --mode dashboard --port 8050

# MAB paper (脚本路径, 不走 main.py)
python scripts/mab_paper.py
python scripts/mab_paper_v2.py
python scripts/baseline_all_strategies.py
```

---

## 文档导航

| 想了解 | 看 |
|---|---|
| 任务清单 / 优先级 / 待办 | `ROADMAP.md` |
| 框架索引 / 文件路径 / 真 PnL 数字 | `PROJECT_MAP.md` |
| 自学习调度器细节 | `memory/selflearning-scheduler.md` |
| 历史完整路线图 (含旧版规划) | git log `ROADMAP.py` 删除前 |

---

## 核心原则

1. **先回测, 后实盘** — 至少 2K 根 bar, 样本外衰减<150% 才算过验证
2. **风控第一** — 单笔风险 4-6% 账户, $500+0.01 lot (1 oz XAUUSD, contract_size=100) + 3 ATR SL ≈ $21 = 4.2% 账户, 跟 P0 原则一致
3. **参数不贪** — 12 组合全过, 过拟合=未来函数=假
4. **数据质量** — MT5 真实 tick, 不用 Yahoo Finance
5. **本地代理** — claude CLI 走 `ANTHROPIC_BASE_URL=http://127.0.0.1:15721` → deepseek-v4-flash
