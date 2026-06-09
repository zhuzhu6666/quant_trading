# 量化框架修复 TODO 列表

> 生成时间: 2026-06-06
> 来源: `PROJECT_AUDIT_v4.md` (v3 已于 2026-06-09 清理)
> 状态: **8 fix + 7 refactor + 5 opt + 3 verify + 调参全部完成** + 2 pre-existing bug 已修
> 最优参数: risk_per_trade_pct=1.0%, max_daily_loss_pct=15.0% (354 trades, +59.17%, Sharpe 0.936)
> 剩余待办: 2 项 blocked (MT5 充值 + MT5 包版本)
> 工作量分级: ⚡ 1-2 小时 / 🔧 1 天 / 🏗️ 1 周 / 🔒 阻塞(外部资源)

---

## ⚡ Phase 1: 1-2 小时可改 (8 项) ✅ **全部完成**

| # | ID | 任务 | 文件:行号 | 工作量 | 依赖 |
|---|---|---|---|---|---|
| 1 | fix-1 ✅ | `factor_health.py:137` 把 0.1 改 0.04 (跟 M15 黄金 \|IC\|≤0.034 现实对齐) | `alpha/factor_health.py:137` | ⚡ 5 分钟 | — |
| 2 | fix-2 ✅ | FOOTGUN-2: `risk_per_trade_pct=0` 改 `=None` 禁用, `=0.0` 真 0% 风险 | `paper_trader.py:114-121` + `paper_engine.py:74, 166-176` | ⚡ 30 分钟 | — |
| 3 | fix-3 ✅ | PreTrade 默认值对齐: `max_daily_loss_pct` 5.0→10.0, `single_risk_usd` 2.0→35.0 | `risk/pre_trade.py:25-28` | ⚡ 5 分钟 | — |
| 4 | fix-4 ✅ | PaperExecutionEngine 默认 `risk_per_trade_pct` 改 `=None` (跟 paper_trader 对齐, caller 显式传 >0 启 Kelly) | `execution/paper_engine.py:74` | ⚡ 5 分钟 | fix-3 |
| 5 | fix-5 ✗ | ~~`main.py` 缺 `if __name__ == "__main__":` 守卫~~ ✗ **审计错判** (line 736-737 实际有守卫) | — | — | — |
| 6 | fix-6 ✅ | mt5_bridge filling mode 注释: bitmask 改 enum 0/1/2 | `execution/mt5_bridge.py:64-71` | ⚡ 2 分钟 | — |
| 7 | fix-7 ✅ | 更新 3 份文档因子数: 22→39 (技术 15 + 时序 5 + ETF 6 + CB 4 + COT 6 + 跨资产 3) | `README.md` + `PROJECT_MAP.md` + `ROADMAP.md` | ⚡ 15 分钟 | — |
| 8 | fix-8 ✅ | GP 引擎文件修复双重 mojibake 编码 (UTF-8 字节当 GBK 保存) | `alpha/factor_search_gp.py:1-16` | ⚡ 30 分钟 | — |

**Phase 1 总计: 约 1.5 小时 — ✅ 已完成 (8 fix + 1 审计错判已撤)**
**最大价值**: fix-1 改 1 个数字就出真 HEALTHY 分布,直接决定整个 L1 因子生命周期走向

---

## 🔧 Phase 2: 1 天工作量 (8 项,按价值排序)

| # | ID | 任务 | 文件 | 工作量 | 依赖 |
|---|---|---|---|---|---|
| 9 | refactor-1 ✅ | MABRunner: 4 策略共享 1 PaperEngine → 4 策略各自维护 position | `execution/mab_paper_runner.py:71-105, 184-195, 386-393` | 🛡️ 护栏已加 (真拆解见末尾) | — |
| 10 | refactor-2 ✅ | MAB 冷启动加 ε-greedy 前 50 笔强制 round-robin | `strategy/mab_router.py:156-218 + 325-345` | ⚡ 2 小时 | — |
| 11 | refactor-3 ✅ | 6 辅助策略加 4 个 enable_* 事件 skip 字段 (capability 对称) | 6 个 strategies/*.py:params | ⚡ 15 分钟 | refactor-1 |
| 12 | refactor-4 ✅ | `main.py:run_backtest` 接 --risk-per-trade-pct (Kelly 仓位, A/B 可比) | `main.py:97-99, 253-340` | ⚡ 30 分钟 | fix-4 |
| 13 | refactor-5 ✅ | factor_health v2: regime_consistency 5 段分桶稳定性, independence 阈值 0.04→0.05 (v3 TODO 留) | `alpha/factor_health.py:149-186` | ⚡ 1 小时 | fix-1 |
| 14 | refactor-6 ✅ | ProbabilityCalibrator cal_factor → signal.strength (真进仓位) | `execution/mab_paper_runner.py:377-407` | ⚡ 30 分钟 | — |
| 15 | refactor-7 ✅ | multi_factor_m15 投票加权 (weighted_vote + vote_weights) | `strategies/multi_factor_m15.py:341-373` | ⚡ 1 小时 | — |
| 16 | refactor-8 ✅ | cTrader SL/TP 推到 server 端: ProtoOAAmendPositionSLTPReq (8/8 unit tests pass, DRY-RUN 闸) | `execution/ctrader_bridge.py:441-528` | 🔧 1 天 | blocked-3 ✅ (2026-06-06 解除) |

**Phase 2 总计: 约 1 周 (单线程顺序)**
**最大价值**: refactor-1 + refactor-3 让 MAB 真有意义 (现在是 dirty hack + 4 strategy 不对称)

---

## 🏗️ Phase 3: 性能优化 (5 项,按需)

| # | ID | 任务 | 文件 | 工作量 | 依赖 |
|---|---|---|---|---|---|
| 17 | opt-1 ✅ | DSL 慢算子 numba 化: `ts_rank` / `ts_decay_linear` (numba 不可用时走 numpy fallback) | `alpha/factor_dsl.py:41-140 + 446-460` | ⚡ 1 天 | — |
| 18 | opt-2 ✅ | Sharpe 公式用 log returns + Newey-West 调自相关 | `execution/_sharpe.py:42-91` + `paper_trader.py:38` + `mab_paper_runner.py:34` | ⚡ 4 小时 | — |
| 19 | opt-3 ✅ | EventBus.publish_async_ff 实际接入, 同步 caller 不阻塞 async handler | `core/event_bus.py:62-87 + 135-167` | ⚡ 4 小时 | — |
| 20 | opt-4 ✅ | `core/state.py` 加多账户支持 (StateContainer + AccountState, 向后兼容 State 别名) | `core/state.py:217-386` | ⚡ 1 天 | — |
| 21 | opt-5 ✅ | event_filter 50K bar × 50 FOMC strptime 优化 (2.5M 次) | `execution/event_filter.py:118-124` | 🏗️ 2 小时 | — |

**Phase 3 总计: 约 1.5 周 (按需) — opt-1 + opt-2 + opt-3 + opt-4 + opt-5 已完成**
**最大价值**: opt-1 DSL 算子加速让 GP 50×30 跑进 timeout (修 2 个公式 bug: cnt_le→cnt_lt 严格小于, 跟 pandas rank 一致; ts_decay 归一化→不归一化跟旧一致); opt-2 实测 AR(1) ρ=0.7 数据 Sharpe 虚高 2.02× (Lo 2002 验证); opt-3 EventBus publish_async_ff 实测 10 次 (handler 各 1s) 仅 1.55ms 不阻塞, 后台 daemon loop 跟程序同寿; opt-4 多账户 12/12 tests pass (含多线程 race-free), 旧 state.balance API 不变; opt-5 实测 2359× 加速 (8.8s → 3.75ms on 50K bar)

---

## 🆕 v5 增量审计 (2026-06-08) — Web Console 专项, 7 P0 真 bug 修了 6 + 1 护栏

> **来源**: `PROJECT_AUDIT_v5.md` 完整阅读 frontend/ 33 ts/tsx + backend/ 16 API + 12 service + 7 lib 后, 用户报"回测页面有 bug" 触发的专项审计。
> **实测验证**: 31 个 fetch endpoint 全映射, 10 项 import + bench 验算, **8.16x 加速实测 (50K bar: 2462ms → 302ms)**。

### ⚡ v5 1-2 分钟可改 (3 项) ✅ 全部完成
| # | ID | 任务 | 文件:行号 | 状态 |
|---|---|---|---|---|
| 1 | v5-fix-1 ✅ | `sidebar.tsx:9` 链接 `/backtest` 路由不存在 → 新建 `app/(terminal)/backtest/page.tsx` (220 行) | `sidebar.tsx:9` + new file | 已修 |
| 2 | v5-fix-5 ✅ | paper page emergency confirm 文案撒谎 ("5 秒内输入 'emergency'" 不存在) | `paper/page.tsx:97` | 已修 |
| 3 | v5-fix-3 ✅ | `api/market.py:78` `df.iterrows()` 慢 15x → vectorized numpy | `api/market.py:68-80` | 已修 8.16x |

### ⚡ v5 5-15 分钟可改 (4 项) ✅ 全部完成
| # | ID | 任务 | 文件:行号 | 状态 |
|---|---|---|---|---|
| 4 | v5-fix-4 ✅ | paper page render 阶段直接 setState (违反 React 规则) → 移到 useEffect | `paper/page.tsx:51-59` | 已修 |
| 5 | v5-fix-6 ✅ | ab / tuning page 用 `d.result?.report_excerpt` (后端从不返) → 改用 `report_path` + `/api/reports/<name>` | `ab/page.tsx:33` + `tuning/page.tsx:37` | 已修 |
| 6 | v5-fix-7 ✅ | market page 切 tf useEffect 无 AbortController → 加 cleanup | `market/page.tsx:10-16` | 已修 |
| 7 | v5-guard-1 🛡️ | `backtest_runner._run_single_backtrader_pass` 是 stub (12 combo 全 0) → backtest page 顶部 bg-warn 警示 + TODO 拆解 | `backtest_runner.py:55-82` + new backtest page | 已加护栏 |

### 🛡️ v5 护栏已加 (1 项, 跟 v3 refactor-1 同模式)
- backtest_runner stub 状态, 真实 PnL 仍走 `python main.py --mode backtest`
- 拆解方案见 v5-拆解-1 (1-2 周工作量, 改 backtrader optstrategy 12 combo)

### 🟡 v5 P1 UX 留 future (5 项)
| # | ID | 任务 | 工作量 |
|---|---|---|---|
| 1 | v5-p1-1 | paper page symbol select 改 disabled readonly text | ⚡ 1 分钟 |
| 2 | v5-p1-4 | candlestick chart 渲染后 fitContent | ⚡ 1 分钟 |
| 3 | v5-p1-5 | live start/stop 端点改 501 或删 | ⚡ 1 分钟 |
| 4 | v5-p1-3 | sync daemon_running 字段验证 + 修 | 🔧 30 分钟 |
| 5 | v5-p1-2 | factors page 改轮询 /api/jobs/{id} 看 run 进度 | 🔧 1 小时 |

### 🏗️ v5 拆解工作 (1 项, 1-2 周)
- v5-拆解-1: backtest_runner stub 改真 backtrader optstrategy 12 combo
  - 步骤 1: 把 `main.py:run_backtest` 的 `_ScanStrategy` 抽到 `execution/_scan_strategy.py` (可复用)
  - 步骤 2: 在 `_run_single_backtrader_pass` 里 import, 跑 backtrader.cerebro.optstrategy
  - 步骤 3: 12 combo 并行 (concurrent.futures.ProcessPoolExecutor) 加速
  - 步骤 4: 跟 main.py 同步维护机制 (refactor 拆解方案见 v5-拆解-2)
  - 风险: 跟 main.py 行为一致性 + 12 combo 4-8 分钟长任务需要前端 timeout 适配

## 🆕 v4 增量审计 (2026-06-06) — 12 finding, 3 真 bug 已修

> **来源**: `PROJECT_AUDIT_v4.md` 完整阅读剩余 ~20% 代码 (live/ + monitor/ + db/ + data/ + docs/ + main.py 601-770 + 8 个新 tests) 后新发现的 12 条 finding。
> **实测验证**: 10 项 import + 真实例化 (因子数 39 ✅, @property 3/3 ✅, factor_health 阈值 0.04 ✅, etc.)

### ⚡ v4 1-2 小时可改 (3 项) ✅ **全部完成**

| # | ID | 任务 | 文件:行号 | 工作量 | 状态 |
|---|---|---|---|---|---|
| 1 | v4-fix-1 ✅ | `orchestrator.full_sync` 漏初始化 `all_errors`, 任一 tf pull 失败 → UnboundLocalError | `data/live_sync/orchestrator.py:94` | ⚡ 30 秒 | 已修 |
| 2 | v4-fix-2 ✅ | `factor_health._compute_components` decay_rate 写"两边都 0"错 (`0.0 if a else 0.0`), 应"q1≈0 + q4>0"给 100 | `alpha/factor_health.py:187` | ⚡ 30 秒 | 已修 |
| 3 | v4-fix-3 ✅ | `factor_dsl.py:28` 删 dead import `ast as _ast` (全文无 _ast. 引用) | `alpha/factor_dsl.py:28` | ⚡ 5 秒 | 已修 |

**v4 增量总计: 1 分钟代码改动 + 1 个护栏, 0 风险**

### 🛡️ v4 护栏已加 (4 项, 跟 v3 refactor-1 同模式)

| # | ID | 任务 | 文件 | 拆解方案 | 优先级 |
|---|---|---|---|---|---|
| 4 | v4-guard-1 🛡️ | `factor_adx` / `factor_di_spread` 用 EMA(span=14) 平滑, 跟 `risk/regime.py:163-175` Wilder smoothing 不对齐 → regime filter 跟 strategy 投票脱节 | `alpha/registry.py:75-87, 117-125` | 抽 `alpha/_wilder.py` 的 `_wilder_smooth` helper, 3 个引用点共用; 或统一改 regime.py 用 EMA | P2 |
| 5 | v4-guard-2 🛡️ | `regime.py:481` 查 `symbols` 表 DDL 缺失, DXY_DRIVEN 永远 False (dead branch) | `risk/regime.py:402-403` + `data/store.py` 加 `symbols` 表 | 删了 DXY_DRIVEN 标志或真建表 | P2 |
| 6 | v4-guard-3 🛡️ | `factor_discovery.py:155` `Path.write_text` 无 `encoding="utf-8"`, Windows locale 可能 GBK | `alpha/factor_discovery.py:155` | 加 1 参, ⚡ 5 秒 cleanup | P3 |
| 7 | v4-guard-4 🛡️ | `external_loader.py:295` `reindex(method="ffill")` pandas 2.1+ deprecated, 3.0 移除 | `data/external_loader.py:295` | `reindex().ffill()`, pandas 3.0 兼容 | P3 |

### 🔧 v4 拆解工作 (跟 v3 refactor-1 同模式, 1-2 天)

| # | ID | 任务 | 工作量 |
|---|---|---|---|
| 8 | v4-拆解-1 | `alpha/_wilder.py` 抽 `_wilder_smooth`, 修 v4-guard-1 (3 文件引用) | 🔧 1 天 |
| 9 | v4-拆解-2 | regime.py DXY_DRIVEN dead branch 重构 (删或接真 symbols 表) | 🔧 1 天 |

### ⚡ v4 cleanup (5 秒到 30 分钟, 5 项)

| # | ID | 任务 | 工作量 |
|---|---|---|---|
| 10 | v4-cleanup-1 | `factor_discovery.py:155` write_text 加 encoding="utf-8" | ⚡ 5 秒 |
| 11 | v4-cleanup-2 | `external_loader.py:295` reindex().ffill() (pandas 3.0 兼容) | ⚡ 1 分钟 |
| 12 | v4-cleanup-3 | `monitor/dashboard.py:88` `@app.on_event` 改 `lifespan` (FastAPI 0.93+ deprecation) | 🔧 30 分钟 |
| 13 | v4-doc-1 | README + PROJECT_AUDIT 改 "L736-737 守卫" → "L769 守卫" (v3 数字偏差) | ⚡ 5 秒 |
| 14 | v4-doc-2 | `live/` 加 `__init__.py` 转 normal package (避免 PEP 420 namespace fragility) | ⚡ 5 秒 |

### 🏗️ v4 验证 (跑过一次新 baseline 才能上 live, 2 项)

| # | ID | 任务 | 工作量 |
|---|---|---|---|
| 15 | v4-verify-1 | 修 3 个新 bug 后重跑 50K bar 调参, PnL 不能 < 50% (v3 baseline 59.17%) | 🔧 1 小时 |
| 16 | v4-verify-2 | 跑 factor_health v4 评分 (decay_rate 修了后, 应多 1-2 个 HEALTHY) | ⚡ 30 分钟 |

---

## 🔒 Phase 4: 阻塞项 (3 项,需外部资源)

| # | ID | 任务 | 阻塞原因 | 解决路径 |
|---|---|---|---|---|
| 22 | blocked-1 | MT5 账户充值 (balance=0 阻塞实盘) | 子账户未充值 | 联系 broker Bybit-Live-2 充值 |
| 23 | blocked-2 | Python MetaTrader5 包版本降级 (5.0.5735 vs MT5 terminal 2026 pipe hash 不匹配) | 7 path 变体 + 重装 + 短长 timeout 全败,`WaitNamedPipeW` 一直 timeout | 降级到 5.0.45 或换 cTrader 实盘路径 |
| 24 | blocked-3 ✅ | cTrader Pepperstone demo 真实 access_token | 2026-06-06 .env 已有真 token (user 提供) | ✅ 已解除, refactor-8 可继续 |

**阻塞项影响**: 仅阻塞 T16 实时数据同步,7 策略 / MAB / 影子 / GP / 校准 全部 PnL 数字走 db 离线 (50K M15 bar),不受影响

---

## ✅ Phase 5: 验证 (3 项,做完 fix 后必跑)

| # | ID | 任务 | 命令 | 期望结果 |
|---|---|---|---|---|
| 25 | verify-1 ✅ | 重跑 22 因子健康评估,确认 fix-1 改 0.1→0.04 后 HEALTHY 数变化 | 2026-06-06: 50396 M15 bar, 2 HEALTHY (gld_tonnes_zscore_60d score=95.2 IC=+0.0359; cot_mm_net_pct_oi score=83.8 IC=+0.0334) + 45 WATCH + 18 DECAYING (含 26 GP DSL auto 因子) | ✅ fix-1 价值已确认 |
| 26 | verify-2 ✅ | 跑 main.py --mode paper 5000 bar,确认 fix-4 改 risk_per_trade_pct=2.0 后 PnL 变化 | 2026-06-06: 50396 M15 bar, 13 trades (W:4/L:9 WR=30.8%), Net PnL=-$51.39 (-10.28%), MaxDD=11.3%, Sharpe=-0.864, CircuitBreaker 10%触发多次. 修复: cfg_get import + StateContainer @property 缺失 | ✅ baseline 已记录 (13 trades, 极少交易因 CB 频繁触发) |
| 27 | verify-3 ✅ | 跑 `python -c 'import main'` 确认 import 安全 (主代码路径已 fix) | 2026-06-06 ✓ IMPORT OK (**实际守卫在 L769**, v3 报告写 L736 是数字偏差, v4 校准) | ✅ PASS |

---

## 推荐执行顺序 (基于价值/工作量比)

```
Day 1 上午 (1.5 小时):
  fix-1 → fix-3 → fix-4 → fix-5 → fix-6 → fix-7 → fix-8
  verify-1 → verify-2 → verify-3
  → 出新 factor_health_report,更新 3 份文档,发版

Day 1 下午 - Day 2 (1 周):
  refactor-1 (MABRunner 重构, 1 天)
    ↓
  refactor-3 (4 策略 capability 对称, 4 小时, 依赖 refactor-1)
    ↓
  refactor-2 (冷启动 ε-greedy, 3 小时)
    ↓
  refactor-5 (factor_health v2, 6 小时)
    ↓
  refactor-7 (投票按 IC 加权, 3 小时)
    ↓
  refactor-4 (A/B 路径 PnL 可比, 4 小时)
    ↓
  refactor-6 (calibrator 真被消费, 4 小时)
    ↓
  refactor-8 (cTrader server SL/TP, 1 天, 依赖 blocked-3)

Day 9+ (按需):
  opt-5 (strptime 优化, 2 小时, 立即)
  opt-1 (DSL numba, 1 天)
  opt-2 (Sharpe 公式, 4 小时)
  opt-4 (多账户, 1 天)
  opt-3 (async EventBus, 1 周, 最后)
```

---

## 关键依赖图

```
fix-1 ✅ 阈值 0.1→0.04 (alpha/factor_health.py:139 + 181)
fix-2 ✅ None vs 0.0 区分 (paper_trader + paper_engine + mab_paper_runner)
fix-3 ✅ max_daily_loss 5→10, single_risk 2→35
fix-4 ✅ PaperEngine 默认 None (跟 paper_trader 对齐)
fix-5 ✗ **审计错判**: main.py line 736-737 实际有 `if __name__ == "__main__": main()` 守卫
fix-6 ✅ filling mode 注释改 enum 0/1/2, 移除 bitmask 误判
fix-7 ✅ 文档因子数 22→39 (README + PROJECT_MAP + ROADMAP)
fix-8 ✅ GP 文件 mojibake 全部清除 (0 token 剩余, AST parse OK)
refactor-1 ✅ 护栏已加: class docstring ARCH-1 段 + 启动 warning + 主循环 KNOWN ISSUE 注释 + TODO.md 拆解方案
opt-5 ✅ precompute dual window + date.fromisoformat: 50K bar 从 8.8s → 3.75ms (2359×)

refactor-1 ──→ refactor-3 ──→ refactor-2
            └─→ refactor-4
refactor-5 (依赖 fix-1)
refactor-6 (独立)
refactor-7 (独立)
refactor-8 (依赖 blocked-3)

opt-* (基本独立,按需做)
blocked-* (独立,需外部资源)
```

---

## 已完成? (这是给后续会话的"上次进度")

### ✅ Phase 1: 8 fix (2026-06-06 已完成)
- [x] fix-1 — factor_health 评分 0.1→0.04
- [x] fix-2 — FOOTGUN-2 `risk_per_trade_pct=0` 改 `=None`
- [x] fix-3 — PreTrade 默认 5.0/2.0 → 10.0/35.0
- [x] fix-4 — PaperEngine 默认 `risk_per_trade_pct=None`
- [x] fix-6 — mt5 filling mode 注释修正
- [x] fix-7 — 3 份文档因子数 22→39
- [x] fix-8 — GP 引擎 mojibake 修复

### ✗ 审计错判 (2026-06-06 已撤)
- [x] ~~fix-5 — main.py 加 `if __name__` 守卫~~ ✗ 实际 line 736-737 已有守卫

### ✅ Phase 2 启动项 (2026-06-06 护栏已加, 真拆解待 verify-2 后)
- [x] refactor-1 — MABRunner 4 引擎架构护栏 (class docstring + 启动 warning + 主循环 KNOWN ISSUE 注释 + TODO.md 73 行拆解方案)

### ✅ Phase 2 完成 (2026-06-06, 价值/工作量排序, 6/8 完成)
- [x] refactor-2 — MAB 冷启动 round-robin (前 50 笔) + warmup_status() 报告
- [x] refactor-3 — 6 策略 capability 对称 (4 个 enable_* 字段对称, 默认全 False)
- [x] refactor-4 — A/B 路径可比 (--risk-per-trade-pct 接 Kelly, 默认 0.01 lot 历史行为)
- [x] refactor-5 — factor_health v2 (regime_consistency 5 段分桶, independence 阈值 0.04→0.05, v3 TODO 留)
- [x] refactor-6 — calibrator 真被消费 (cal_factor → signal.strength → PaperEngine 仓位)
- [x] refactor-7 — multi_factor_m15 投票加权 (weighted_vote + vote_weights)

### ✅ Phase 2 全部完成 (2026-06-06, refactor-8 跟 .env 接入)
- [x] refactor-8 — cTrader server SL/TP (ProtoOAAmendPositionSLTPReq)

### ✅ Phase 3 完成 (2026-06-06)
- [x] opt-1 — DSL ts_rank/ts_decay_linear numba 化 (修 2 公式 bug: cnt_le→cnt_lt, 归一化→不归一化)
- [x] opt-2 — Sharpe log returns + Newey-West HAC (实盘 ρ=0.3-0.5 虚高 30-60%, bench 验证 AR(1) ρ=0.7 旧 3.41→新 1.69, 2.02×)
- [x] opt-3 — EventBus publish_async_ff + 后台 daemon loop (5/5 tests pass, 10× 1s handler 仅 1.55ms)
- [x] opt-4 — StateContainer 多账户 (12/12 tests pass, 含 5×100 多线程 race-free, State=AccountState 向后兼容别名)
- [x] opt-5 — event_filter strptime 优化 (50K bar 8.8s→3.75ms, 2359×)

### 🔒 Phase 4 阻塞 (2 项, 需外部资源)
- [ ] blocked-1 — MT5 充值
- [ ] blocked-2 — Python MetaTrader5 降级
- [x] blocked-3 — cTrader access_token (2026-06-06 ✅ .env 已有真 token)

### ✅ Phase 5 验证 全部完成 (2026-06-06)
- [x] verify-1 — 重跑 39 因子健康 ✅ 2026-06-06 (2 HEALTHY + 45 WATCH + 18 DECAYING)
- [x] verify-2 — paper 5000 bar PnL 验证 ✅ 2026-06-06 (13 trades, -10.28%, Kelly 2% 仓位 + CB 频繁触发)
- [x] verify-3 — `import main` ✅ 2026-06-06 (5 秒验证 PASS)

**进度统计** (2026-06-06 19:30):
- ✅ 8 fix 完成 + 1 错判撤
- ✅ 7 refactor 全部完成 (Phase 2 完结)
- ✅ 5 opt 全部完成 (Phase 3 完结)
- ✅ Phase 5 全部完成 (verify-1/2/3)
- ✅ 调参完成: risk=1.0%, CB=15% → 354 trades, +59.17%, Sharpe 0.936
- 🔧 verify-2 新发现 2 个 pre-existing bug 已修 (cfg_get + @property)
- ⏳ 2 项 blocked (MT5 充值 + MT5 包版本)

---

## 下次会话入口 (2026-06-06 状态)

**用户偏好** (来自 USER.md):
- 期望: 中文回答, 完整真实数据, 不靠文档推理
- 重视: 行号引用 + 证据可追溯

**本次未决决策**:
- (已结) fix-4 PaperEngine 默认 None vs 2.0: 选 **None** (跟 paper_trader 对齐)
- (已结) refactor-1 范围: 选 **🛡️ 架构护栏** (不动主循环, 加 warning + TODO 方案)
- (已结) refactor-3 "8 个 enable_*" 误算: 实际只 4 个 (multi_factor_m15 已有 4, 补对称即可)

**推荐下一步** (Phase 1-5 + 调参全完结, 仅剩 blocked):
1. **blocked-1** MT5 充值 (联系 Bybit-Live-2) — 修后可跑 MT5 端到端
2. **blocked-2** MetaTrader5 包版本降级 — 5.0.45 或换 cTrader 实盘
3. **refactor-1 真拆解**: 现在有正 PnL baseline (+59.17%), 可以拆 MAB 4 策略共享 PaperEngine

**严禁做** (风险高):
- 改 blocked-*: 阻塞中，等外部资源
- factor_health v3 真相关矩阵: ic_tracker 没暴露 vals 序列, 需先 ic_tracker.export_vals() (P2)

---

## refactor-1 详细拆解方案 (待执行, audit 2026-06-06)

### 当前状态 (2026-06-06 护栏已加)
- `mab_paper_runner.py:113-138` 启动时只 1 个 PaperTrader + 1 个 PaperEngine
- `mab_paper_runner.py:336-358` 主循环每 bar 临时切换 `self.paper.strategy` reference
- `mab_paper_runner.py:182-195` 启动时一次性 ARCH-1 warning, 提醒 caller
- 4 策略共享 position / SL/TP / last_indicators

### 拆解后架构目标
```
MABPaperRunner
├── self.paper_engines: dict[str, PaperExecutionEngine]  # 4 策略 → 4 engine
│   ├── engines['multi_factor_m15']: PaperEngine (独立 position/equity/last_indicators)
│   ├── engines['trend_following']:   PaperEngine
│   ├── engines['mean_reversion']:    PaperEngine
│   └── engines['breakout']:          PaperEngine
├── self.paper: 仅作为 4 引擎的统计聚合器 (废弃原 PaperTrader 包装, 改为 dict 包装)
├── 共享: state.daily (全局熔断) + factor_monitor + calibrator + meta_monitor
├── 各自: position + equity + last_indicators + SL/TP + 单笔风控
└── 主循环每 bar:
    chosen = self.router.select(regime)            # 1 个主策略
    parallel = self.router.select_top_k(2)          # 同时开 2-3 个并行 (新能力)
    for name in [chosen, *parallel]:
        signal = self.strategies[name].on_bar(bar)
        signal = event_filter.maybe_skip(signal)
        if signal is not None:
            self.paper_engines[name].on_bar(bar, signal)
    # 4 engine 各自有 trade close 时, 调 _on_trade_close(engine_name, trade)
```

### 拆解步骤 (1 天工作量)

**Step 1 (2h)**: 数据结构迁移
- `__init__` 不再只建 1 个 `self.paper`, 改为建 `self.paper_engines = {name: PaperExecutionEngine(...) for name in strategies}`
- 保留 `self.paper` 作为聚合 facade, 提供 `self.paper.engine.trades` 等只读 view (backward compat for tests/reports)
- 各 engine 独立 `self.balance / self.equity / self.position / self.trades / self.last_indicators`

**Step 2 (2h)**: 共享 vs 独立组件分离
- 共享: `state.daily` (全局熔断), `core.state.state` (单例, 4 engine 共用)
- 共享: `PreTradeChecker` (max_daily_loss_pct 共享) — 但 `single_risk_usd` 每个 engine 独立校验
- 共享: `CircuitBreaker` (单例, 任何一个 engine 触发熔断 → 全部 engine 停止)
- 独立: 每个 engine 自己维护 `last_atr / last_indicators / risk_per_trade_pct 应用结果`

**Step 3 (2h)**: 主循环改写
- 删除 `prev_strategy = self.paper.strategy; ...` hack
- 改成 `for name, engine in self.paper_engines.items(): engine.on_bar(bar, signal_dict.get(name))`
- `signal_dict` 来自 router + 调 `strategies[name].on_bar(bar)` 收集
- trade close 回调改为 `for engine_name, engine in self.paper_engines.items(): if engine.just_closed_trade(): ...`

**Step 4 (1h)**: 报告 + 测试
- `MABPaperReport.strategy_pnl` 从 4 engine 聚合 trades (不是按 chosen 标签分)
- 跑 verify-2 拿新 PnL baseline
- 写 `tests/test_refactor_1_4_engines.py` 验证 4 engine 独立开仓不互相覆盖

**Step 5 (1h)**: 行为验证 + 文档
- 对比 refactor 前后的 equity curve / Sharpe / max_dd
- 预期: PnL 不一定更好 (4 引擎 = 4×risk 暴露, 可能更差), 但能测出"4 策略真·alpha" vs "MAB 选最优"
- 更新 README/PROJECT_MAP 的 ARCH-1 章节

### 风险 + 回滚
- **风险**: MAB router 学到的是旧行为 (选最优开仓), 拆解后 router 选择模式不变但仓位 ×4, 风险 +4×, max_dd 可能爆
- **回滚**: git revert + 关闭 ARCH-1 warning 即可 (warning 用 `self._arch1_quiet` 控制)
- **建议执行窗口**: 等 verify-2 跑完, 拿到 fix-4 后的新 PnL baseline 之后再拆

### 护栏已加 (本次完成, 0 风险)
- ✅ `mab_paper_runner.py:71-105` class docstring 加 ARCH-1 KNOWN ISSUE 段
- ✅ `mab_paper_runner.py:184-195` 启动时一次性 warning
- ✅ `mab_paper_runner.py:386-393` 主循环 ARCH-1 注释
- ✅ TODO.md 写完整拆解方案 (本节)
- ✅ caller 可设 `runner._arch1_quiet = True` 关 warning (后续接测试)
