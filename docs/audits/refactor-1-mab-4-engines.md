# refactor-1: MABPaperRunner 4 引擎架构拆解方案

> **来源**: 原 `TODO.md` 第 316-385 行 (2026-06-06 审计时由 Claude 起草)
> **状态**: 🛡️ **架构护栏已加** (本次完成, 0 风险);真拆解**待执行**(等 verify-2 PnL baseline)
> **关联代码**: `execution/mab_paper_runner.py:71-105, 113-138, 184-195, 336-358, 386-393`
> **关联任务**: ROADMAP.md §"Phase 2 审计 refactor" refactor-1 (MABRunner 重构, 1 天)

---

## 当前状态 (2026-06-06 护栏已加)

- `mab_paper_runner.py:113-138` 启动时只 1 个 PaperTrader + 1 个 PaperEngine
- `mab_paper_runner.py:336-358` 主循环每 bar 临时切换 `self.paper.strategy` reference
- `mab_paper_runner.py:182-195` 启动时一次性 ARCH-1 warning, 提醒 caller
- 4 策略共享 position / SL/TP / last_indicators

## 拆解后架构目标

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

## 拆解步骤 (1 天工作量)

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

## 风险 + 回滚

- **风险**: MAB router 学到的是旧行为 (选最优开仓), 拆解后 router 选择模式不变但仓位 ×4, 风险 +4×, max_dd 可能爆
- **回滚**: git revert + 关闭 ARCH-1 warning 即可 (warning 用 `self._arch1_quiet` 控制)
- **建议执行窗口**: 等 verify-2 跑完, 拿到 fix-4 后的新 PnL baseline 之后再拆

## 护栏已加 (本次完成, 0 风险)

- ✅ `mab_paper_runner.py:71-105` class docstring 加 ARCH-1 KNOWN ISSUE 段
- ✅ `mab_paper_runner.py:184-195` 启动时一次性 warning
- ✅ `mab_paper_runner.py:386-393` 主循环 ARCH-1 注释
- ✅ `docs/audits/refactor-1-mab-4-engines.md` 写完整拆解方案 (本文件)
- ✅ caller 可设 `runner._arch1_quiet = True` 关 warning (后续接测试)

---

**维护说明**:
- 本文件于 2026-06-09 从原 `TODO.md` 第 316-385 行迁出(根目录归类整理时)
- ROADMAP.md 已声明 TODO.md 是"单源待办的过时副本",本文件作为 ARCH-1 详细设计文档保留
- 真拆解执行时,在此文件 `Status` 行打勾,ROADMAP §"Phase 2 审计 refactor" refactor-1 同步
