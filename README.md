# Python 量化交易框架

XAUUSD+ 黄金 M15 趋势/回归/因子合成, 7 层架构, 本地 paper + backtest baseline 已实盘验证 (read-only 模式)。
**Web 总控台全完结(2026-06-07):39 API 端点 + 16 页面 + JWT auth + PWA scaffold — 见 `README_WEB.md`**

**最后更新: 2026-06-07 (Web Console Phase 1-5 全完结)**

---

## 当前状态 (2026-06-06 审计)

### 代码层进度: Phase 1-5 全完结 + 调参

- ✅ **Phase 1 (8 fix)**: 因子健康阈值 0.04 / risk_per_trade_pct=None 区分 / PreTrade 默认值 / filling mode 注释 / 22→39 因子文档 / mojibake 清除 / cfg_get import / StateContainer @property
- ✅ **Phase 2 (7 refactor)**: MAB 架构护栏 / 冷启动 round-robin / 投票 IC 加权 / capability 对称 4 enable_* / backtest Kelly 仓位 / calibrator 真消费 / regime_consistency 5 段分桶 / cTrader SL/TP server 端
- ✅ **Phase 3 (5 opt)**: DSL numba 化 (ts_rank/ts_decay_linear) / Sharpe log+NW HAC / EventBus publish_async_ff / 多账户 StateContainer / strptime 2359× 加速
- ✅ **Phase 5 (3 verify)**: 39 因子健康 (2 HEALTHY) / paper 5000 bar PnL / import main 安全
- ✅ **调参**: risk=1.0% + CB=15% → 354 trades, +59.17%, Sharpe 0.936
- ⏸ **T16 实时数据同步**: Python MetaTrader5 包版本 vs terminal 2026 IPC pipe 不匹配, 改按需手动

### PnL 数字

| 路径 | PnL | Trades | Sharpe | DD | 备注 |
|---|---|---|---|---|---|
| **main.py baseline (无风控)** | **+407.51%** | 738 | 1.807 | 39.77% | 单策略 + 事件 skip + circuit 关闭 |
| **MAB T1-T13 全栈** | **+120.75%** | 639 | **0.894** | 64% | SharedEventFilter 是业务层关键修复 |
| **verify-2 调参后 (risk=1% + CB=15%)** | **+59.17%** | 354 | **0.936** | 15.9% | Kelly 1% + 15% CB, 最优参数已固化 |
| verify-2 调参前 (risk=2% + CB=10%) | -10.28% | 13 | -0.864 | 11.3% | Kelly 2% + 10% CB, CB 频繁触发 |

### MT5 真值 (2026-06-02 验证)

- 账户: 9823690 / Bybit-Live-2 / **leverage=500x**
- XAUUSD+: **contract_size=100 oz/lot**, volume_min=0.01, step=0.01
- 0.01 lot = **1 oz**, 3 ATR SL ≈ **$25 = 5% 账户** (跟 P0 原则一致)
- 当前金价: **4529 USD/oz**, ATR14 mean ≈ **$8.42** (M15)
- MT5 账户 balance=0, **不能 live trade**, 全 read+paper 模式

### 因子健康 (verify-1, 2026-06-06)

39 builtin + 26 GP DSL auto 因子在 50K M15 bar 上的健康分 (fix-1 阈值 0.1→0.04, refactor-5 v2 regime_consistency 5 段分桶):

| 状态 | 数量 | 代表 |
|---|---|---|
| **HEALTHY** | 2 | gld_tonnes_zscore_60d (95.2, IC=+0.0359), cot_mm_net_pct_oi (83.8, IC=+0.0334) |
| WATCH | 45 | dsl_auto_* (69.x), cot_mm_net_zscore_52w (68.5), atr_ratio (67.8) |
| DECAYING | 18 | rsi_14 (23.5), adx (24.0), stoch_k (25.8), cb_china_* (NaN) |

报告: `data/charts/factor_health_report.{txt,json}`

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

### 自主进化 8 项接入 (2026-06-03)

完整 L1-L5 自循环已闭环, 8 项 cron 化接入 (详见 ROADMAP.md §P2 自主进化)。

### 影子因子 (2026-06-03)

默认 `shadow_vote_weight=0` (关闭), 开启需 `--include-shadow-factors --shadow-vote-weight 1.0`。

### ProbabilityCalibrator (2026-06-03)

P0-7 桶级 calibrator 持久化: 启动时 load `data/charts/calibrator_bucket.json` (8 桶), 缺失回退 identity。
refactor-6 (2026-06-06): calibrator 校准因子真进入 signal.strength → PaperEngine 仓位决策。

### 自进化状态 (2026-06-06)

L1-L5 自循环 3/3 闭环 (T15.5 wiring + Calibrator 持久化 + GP T15.3 v2) + Phase 1-5 审计 + 调参全完结。

**调参脚本**: `scripts/tune_risk_params.py` — 3 轮梯度测试, 保留供后续调参。

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
├── alpha/                   # 39 因子 registry / factor_engine / ic_tracker /
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

## Web 端到端测试 (e2e)

2026-06-08 v7 audit 装的 Playwright e2e 跑 18 路由 + 4 功能验证,1.4 min 全过。
GitHub Actions 每次 push/PR 自动跑(.github/workflows/e2e.yml)。

### 本地跑

```bash
# 前置: backend :8000 + frontend :3000 在跑 (start.bat / start.sh)
cd frontend

# 一次性安装 (用户本地)
npm ci                                                    # 装 @playwright/test
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npx playwright test    # 用系统 Chrome

# 完整装 chromium (需网络可达 playwright.azureedge.net)
npx playwright install --with-deps chromium
npx playwright test --project=chromium                    # 用 bundled chromium
```

22 个 spec 包含:
- `critical_paths.spec.ts` — 18 路由 mount + pageerror/console error 检测
- `functional_checks.spec.ts` — 4 路由真 DOM 数据验证 (factors 有数字 / calibrator
  bucket 行数 / market K线出现 / backtest 按钮可见)

### CI

`.github/workflows/e2e.yml` 在 `ubuntu-latest` 跑:

1. Python 3.11 + Node 20
2. `pip install -r requirements.txt` (MetaTrader5 等不可装,continue-on-error)
3. `npm ci` frontend deps
4. 启 backend + frontend dev server (各起一个,后台,waitUntil health)
5. `npx playwright install --with-deps chromium`
6. `npx playwright test --project=chromium`

失败时自动上传 `playwright-report/` + `test-results/` 截图,7 天保留。

### 已知 v7 修过但可能回归的 4 类 bug (e2e 守门)

- Schema 错配 (前端读 `f.b` 后端返 `f.a.b`) — factors/calibrator
- 加速 dtype 错 (`// 1e9` 假设错精度) — market
- Endpoint 路径 trailing slash (FastAPI `@router.get("/")` vs no slash) — backtest
- NaN 渲染未守卫 (字段可能 undefined) — factors/radar

详见 `PROJECT_AUDIT_v7.md` 每类 bug 的根因 + 修法。

---

## 文档导航

| 想了解 | 看 |
|---|---|
| **Web 总控台用户文档** | `README_WEB.md` (页面速查 + 启动方式 + 已知限制) |
| **Web UI 设计 spec** | `docs/superpowers/specs/2026-06-07-quant-web-console-design.md` |
| **Web UI 实施 plan** | `docs/superpowers/plans/2026-06-07-quant-web-console.md` |
| **Web UI nginx 配置模板** | `docs/nginx.example.conf` |
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

## Audit 验证状态 (2026-06-06 Phase 1-5 审计)

| 项 | 状态 | 验证 |
|---|---|---|
| fix-1 因子健康阈值 0.1→0.04 | ✅ 已修 | verify-1: 2 HEALTHY (gld_tonnes_zscore_60d 95.2, cot_mm_net_pct_oi 83.8) |
| fix-2 risk_per_trade_pct=None 区分 | ✅ 已修 | paper_trader + paper_engine + mab_paper_runner 三分支 |
| fix-3 PreTrade 默认值 | ✅ 已修 | max_daily_loss_pct 10.0, single_risk_usd 35.0 |
| fix-4 PaperEngine 默认 None | ✅ 已修 | 跟 paper_trader 对齐, caller 显式传 |
| fix-5 审计错判 (main.py 守卫) | ✗ 撤回 | L769 有 `if __name__` 守卫 (v3 报告写 L736-737 是数字偏差, v4 实测校准), verify-3 import OK |
| fix-6 filling mode 注释 | ✅ 已修 | bitmask→enum 0/1/2 |
| fix-7 文档因子数 22→39 | ✅ 已修 | README + PROJECT_MAP + ROADMAP |
| fix-8 mojibake 清除 | ✅ 已修 | factor_search_gp.py 26 行乱码→英文 |
| refactor-1 MAB 架构护栏 | ✅ 护栏 | class docstring + startup warning + KNOWN ISSUE 注释 |
| refactor-2 MAB 冷启动 | ✅ 已修 | round-robin 前 50 笔 + warmup_status() |
| refactor-3 capability 对称 | ✅ 已修 | 6 策略 4 enable_* 字段 |
| refactor-4 backtest Kelly | ✅ 已修 | --risk-per-trade-pct CLI |
| refactor-5 factor_health v2 | ✅ 已修 | 5 段分桶稳定性 + independence 阈值 0.05 |
| refactor-6 calibrator 真消费 | ✅ 已修 | cal_factor → signal.strength |
| refactor-7 投票 IC 加权 | ✅ 已修 | weighted_vote + vote_weights |
| refactor-8 cTrader SL/TP | ✅ 已修 | amend_position_sltp + ProtoOAAmendPositionSLTPReq |
| opt-1 DSL numba 化 | ✅ 已修 | ts_rank/ts_decay_linear, 修 2 公式 bug |
| opt-2 Sharpe NW HAC | ✅ 已修 | log returns + Newey-West, bench 2.02× |
| opt-3 EventBus async | ✅ 已修 | publish_async_ff + daemon loop |
| opt-4 多账户 | ✅ 已修 | StateContainer + AccountState |
| opt-5 strptime 优化 | ✅ 已修 | precompute dual window, 2359× 加速 |
| 调参 | ✅ 完成 | risk=1.0% + CB=15% → +59.17%, Sharpe 0.936 |
| cfg_get NameError | ✅ 已修 | main.py run_paper 内补 import |
| StateContainer @property | ✅ 已修 | has_position/win_rate/daily_loss_pct 加 @property |

---

## 环境

- Python 3.11 (hermes sandbox), numpy-2.4.6 + pandas-3.0.3 + backtrader-1.9.78 + loguru-0.7.3
- MT5 账户 balance=0 (blocked-1), MetaTrader5 包版本不匹配 (blocked-2)
- cTrader .env 已有真 token (blocked-3 ✅ 解除)

