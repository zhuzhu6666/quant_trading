# 框架审计报告 (2026-06-03)

> 审计范围: 全部核心模块 (strategy/execution/alpha/data/core/risk)
> 审计方法: 逐文件源码审读 + 交叉引用验证

---

## 🔴 BUG (影响 PnL 正确性)

### BUG-1: `daily_loss_pct` 盈利日误判为亏损
- **文件**: `core/state.py:82-85`
- **现状**: `abs(self.daily.net_pnl)` 使盈利日也返回正数 → `pre_trade.py:47` 的 `daily_loss_pct >= max_daily_loss_pct` 在大盈利日触发熔断，阻止后续交易
- **修法**: `max(0, -self.daily.net_pnl) / self.balance * 100`
- **影响**: 日内大涨6%后所有新单被拒

### BUG-2: `circuit_breaker.reset()` 抹掉 `peak_equity`
- **文件**: `risk/circuit.py:122-126` + `execution/paper_trader.py:234-238` + `execution/mab_paper_runner.py:290-293`
- **现状**: `_reset_daily_stats` 先保存 peak → 创建新 DailyStats(date, peak_equity=peak) → 调 circuit.reset() → 内部调 state.reset_daily() → `DailyStats()` 默认 peak_equity=0
- **修法**: circuit.reset() 不调 state.reset_daily()，仅清 is_circuit_breaker/circuit_reason/_atr_history；或 paper_trader reset 后重新赋值 peak
- **影响**: DD 统计完全错误，从 0 重新计算

### BUG-3: IC 分析 `forward_periods` 参数无效
- **文件**: `alpha/factor_engine.py:100`
- **现状**: 函数签名接受 forward_periods 但实现硬编码 1-bar return，多周期 IC 从未计算
- **修法**: 实现多周期 fwd_ret = close[i+fp] / close[i] - 1
- **影响**: 所有 IC 报告只反映 1-bar 预测力

### BUG-4: SL/TP slippage 方向可能倒转
- **文件**: `execution/paper_engine.py:465-471`
- **现状**: SL hit 时 `_close(sl, reason="sl")` → `_apply_slippage(sl, close_dir=-pos.direction)` → long 止损时 close_dir=-1 → slippage 是 `price - slip` → 实际成交价优于 SL 价
- **修法**: 止损成交应对交易者不利，close_dir 应取 `pos.direction`（对 long 止损滑向更差方向）
- **影响**: 止损亏损被系统性低估

### BUG-5: 零净利交易计为亏损
- **文件**: `execution/paper_engine.py:289-294`
- **现状**: `net_pnl == 0` 走 else 分支 → `losing_trades += 1`
- **修法**: `elif net_pnl > 0` / `elif net_pnl < 0` / `else` (break-even 单独计)
- **影响**: WR 微偏低

---

## 🟡 架构问题

### ARCH-1: 全局 state 单例 + 无锁突变
- **文件**: `core/state.py:136` + paper_engine/circuit/pre_trade 直接 mutate
- **现状**: `state = State()` 全局单例，`paper_engine.py:286-296` 绕过 `state._lock` 直接写 `state.daily.*`
- **影响**: 多线程（live + sync daemon）必出 data race
- **修法**: paper_engine 通过 state 方法修改，或引入 context 对象

### ARCH-2: config/settings.yaml 从未被加载
- **文件**: `config/settings.yaml` + `main.py`
- **现状**: YAML 定义 max_daily_loss_pct=5.0 / single_trade_risk_usd=2.0，但 main.py 不读它；PaperTrader 硬编码 10.0 / 35.0
- **影响**: 改配置文件不改代码 → 不生效
- **修法**: main.py 加载 YAML 并注入

### ARCH-3: MAB 策略热切换 mid-bar
- **文件**: `execution/mab_paper_runner.py:333-338`
- **现状**: 每根 bar 临时 `paper.strategy = strategies[chosen]`，IC tracker / factor buffer 跟踪被选策略
- **影响**: warmup 只预热主策略，被选策略 indicator state 延迟；IC 统计不准

### ARCH-4: classify_regime 每根 bar 重算
- **文件**: `execution/mab_paper_runner.py:274-276`
- **现状**: 50K bar × O(200) = 10M 运算
- **修法**: 预计算 rolling EMA/ATR → O(n)
- **影响**: 50K bar ~5s → 优化后 <0.5s

### ARCH-5: 双重记账
- **文件**: paper_engine.py + state.py
- **现状**: PaperExecutionEngine 维护 self.balance/equity/position，镜像到 state；state.daily.winning_trades 在 paper_engine:286 和 state.record_trade:119 两处更新
- **影响**: 如果两处都调用 → 统计翻倍

---

## 🟢 优化建议

### OPT-1: SQLite WAL + busy_timeout
- **文件**: `data/store.py:33-40`
- **现状**: 默认 journal mode，live_sync + paper 并发写 SQLITE_BUSY
- **修法**: `PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;`

### OPT-2: `_trades` 列表无上限
- **文件**: paper_engine.py:110, mab_paper_runner.py:141
- **现状**: live 模式跑数月 → 内存泄漏
- **修法**: 定期 flush 到 analytics.db

### OPT-3: EventBus async publish 从未使用
- **文件**: `core/event_bus.py`
- **现状**: 只有 publish_sync 被调用

### OPT-4: 死代码
- `strategy/portfolio.py` PortfolioManager 未被引用
- `BaseStrategy.on_tick` 从未调用
- `State.active_orders` 从未写入
- `live/executor.py` 已被 mt5_bridge 替代

### OPT-5: event_filter.py 字符串日期重复解析
- **文件**: `execution/event_filter.py:118-126`
- **现状**: 50K bar × 50 FOMC dates = 2.5M 次 strptime
- **修法**: 预转 date objects + set 查找

---

## 修复优先级

| 优先 | 编号 | 问题 | 工作量 |
|---|---|---|---|
| P0 | BUG-2 | circuit reset 覆写 peak_equity | 2 行 |
| P0 | BUG-1 | daily_loss_pct abs() | 1 行 |
| P1 | BUG-4 | SL slippage 方向 | 3 行 |
| P1 | BUG-3 | forward_periods 无效 | 15 行 |
| P2 | ARCH-2 | config 不加载 | 20 行 |
| P2 | ARCH-1 | state 无锁突变 | 50 行 |
| P2 | ARCH-4 | regime 重算 | 30 行 |