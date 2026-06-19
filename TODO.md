# TODO — 剩余工作（Phase 0-7 已全部完成）

Factor Takeover v4 全部 Phase (0~7) 已完成交付，详见 `docs/UPGRADE_BLUEPRINT.md`。
以下为剩余边缘项和技术债务。

> **2026-06-19 审计 v13 更新**: 
> - ✅ C2 (factor_health independence → 已用真实 np.corrcoef) 
> - ✅ C1 (AWE CausalCheck → 数据流已接入 `_scheduled_awe_adapt`)
> - ✅ TD2 (on_fill 反向成交 → 已实现减仓/平仓/翻仓)

---

## 🟡 延期项（蓝图明确标记 [~]）

| # | 项 | 原因 | 触发条件 |
|---|----|------|---------|
| D1 | Autoencoder 非线性特征压缩 | §12 过拟合风险，20K bar 极易学噪声 | bar > 100K |
| D2 | 数据版本控制系统 | DuckDB snapshot 已够用，无需额外系统 | 需要时重构 |

## 🔴 P0（审计 v13 确认）

| # | 文件 | 问题 | 说明 |
|---|------|------|------|
| P0-9 | `MainDashboard.tsx:344` | drawdown_pct 未 ×100 → 显示 "0%" 应为 "5%" | `Math.round(dd)` → `Math.round(dd*100)` |

## ⚠️ P1（审计 v13 确认）

| # | 文件 | 问题 | 说明 |
|---|------|------|------|
| TD1 | `execution/ctrader_bridge.py` | spot price 除数 `10**5` 硬编码 XAUUSD | 换品种会价格错误 |
| TD3 | `execution/market_impact.py:117` | 成本计算默认金价 3000.0 硬编码 | 非 XAUUSD 品种会成本偏差 |
| P1-3 | `config/__init__.py` | MT5 相关常量和 config 残留 | MT5 已彻底移除 |
| TD5 | 前端 8+ 处 | polling 缺少 AbortController | 组件卸载后 setState 泄漏 |
| TD6 | 前端 | 死导入清理 (50+ 处) | 代码整洁 |

## 🔵 P2（审计 v13 确认）

| # | 文件 | 问题 | 说明 |
|---|------|------|------|
| 1 | `monitor/evolution_story.py` vs `evolution_story/` | 文件与目录冲突 → Python import 未定义 | 清掉一边 |
| 2 | `./nul` | Windows 设备名意外创建的文件 | 直接删除 |
| 3 | `alpha/factor_attribution.py` | 旧版归因，已被 `attribution_engine.py` 取代 | 无人 import |
| 4 | `execution/paper_bridge.py` | 旧版模拟盘桥接 | 无人 import |
| 5 | `live/` 目录 | factor_monitor, meta_learner_monitor 仅被旧 main.py 引用 | 迁移或删除 |
| 6 | 根目录 `_` 前缀调试脚本 | 6个一次性脚本散落根目录 | 移入 scripts/debug/ |
| 7 | `scripts/ctrader_live_runner.py` | docstring 仍引用 MT5 | 更新或删除 |

## 📋 已知技术债务（蓝图 Appendix C.1）

| # | 问题 | 说明 |
|---|------|------|
| 1 | `regime_classifier.py` 因子脱节 | 使用 `[aroon, cci, mfi, williams_r]` 不在 39 因子池 |
| 2 | `scripts/train_xgb_walkforward.py` 仅 4 因子 | 非蓝图 68 特征设计 |
| 4 | `scripts/factor_pca.py` docstring 过时 | 声称 15 因子，实际 39 |
| 5 | `data/store.py` → `duckdb_store.py` 双层包装 | 过度封装，所有引用通过旧 store.py 再委派到 duckdb_store.py |

> 以上不阻塞运行。当需要扩展 ML 或多品种时按优先级处理。
