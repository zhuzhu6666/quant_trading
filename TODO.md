# TODO — 当前收尾与待办

Factor Takeover v4 主体框架已经落地，规则驱动学习闭环已进入联调与验证阶段。
当前重点不再是“有没有框架”，而是“闭环是否稳定、自愈是否可靠、前后端展示是否一致”。

> **2026-06-25 状态更新**
> - ✅ 决策账本、平仓复盘、经验沉淀、规则建议、治理审批已打通
> - ✅ 学习应用日志 `learning_application_log` 已落库
> - ✅ 应用效果追踪 `learning_application_effect` 已落库
> - ✅ 治理接口支持“治理后自动同步权重”
> - ✅ 学习应用记录已支持幂等复用，重复活跃记录会标记为 `superseded`
> - ✅ 后端启动后可自动恢复 loop，并延迟执行 learning backfill 修复重启断点
> - ✅ 小程序 V2 已替换旧版，当前为唯一维护前端
> - 🟡 当前仍处于 demo 实盘联调期，需要继续验证“应用后效果追踪 -> 自动回滚/增强”在真实交易流中的稳定性

---

## 🟢 当前最高优先级

| # | 项 | 状态 | 说明 |
|---|----|------|------|
| L1 | 学习应用效果追踪实盘验证 | 进行中 | 验证 `observing -> effective / ineffective / reinforced` 是否随真实平仓正确推进 |
| L2 | 历史重复应用记录清理脚本 | 待做 | 旧逻辑遗留了少量重复 `application`，现在已不会继续增长，但建议补一次正式清理 |
| L3 | 小程序学习页状态文案对齐 | 待做 | 将 `observing / effective / ineffective / superseded / reinforced` 做更明确展示 |
| L4 | 重启恢复场景回归测试 | 待做 | 覆盖“开仓后重启 / 重启期间平仓 / loop 未立即恢复”三类场景 |
| L5 | 服务器开发同步规范固化 | 待做 | 后端以服务器为准、本地前端为准、最终合并 GitHub，补成团队约定文档 |

## 🟡 延期项（蓝图明确标记 [~]）

| # | 项 | 原因 | 触发条件 |
|---|----|------|---------|
| D1 | Autoencoder 非线性特征压缩 | §12 过拟合风险，20K bar 极易学噪声 | bar > 100K |
| D2 | 数据版本控制系统 | DuckDB snapshot 已够用，无需额外系统 | 需要时重构 |

## 🔴 P0（审计 v13 确认）

| # | 文件 | 问题 | 说明 |
|---|------|------|------|
| P0-9 | `MainDashboard.tsx:344` | drawdown_pct 未 ×100 -> 显示 "0%" 应为 "5%" | `Math.round(dd)` -> `Math.round(dd*100)` |

## ⚠️ P1（审计 v13 确认）

| # | 文件 | 问题 | 说明 |
|---|------|------|------|
| TD1 | `execution/ctrader_bridge.py` | spot price 除数 `10**5` 硬编码 XAUUSD | 换品种会价格错误 |
| TD3 | `execution/market_impact.py:117` | 成本计算默认金价 3000.0 硬编码 | 非 XAUUSD 品种会成本偏差 |
| P1-3 | `config/__init__.py` | MT5 相关常量和 config 残留 | MT5 已彻底移除 |
| TD5 | 前端 8+ 处 | polling 缺少 AbortController | 组件卸载后 setState 泄漏 |
| TD6 | 前端 | 死导入清理 (50+ 处) | 代码整洁 |

## 🟠 学习闭环专项技术债

| # | 文件 / 模块 | 问题 | 说明 |
|---|------|------|------|
| LE1 | `research/learning/governor.py` | 历史重复应用记录仍可能残留 | 新逻辑已避免继续膨胀，但旧数据建议补清理脚本 |
| LE2 | `backend/api/learning.py` | 治理接口依赖 `_update_weights()` 内部函数 | 现阶段可用，后续建议抽成正式 service 接口 |
| LE3 | `research/learning/policy_suggester.py` | 当前仍是保守规则阈值 | 后续真实样本足够后，应迁移到可配置阈值或模型 adapter |
| LE4 | `backend/services/learning_backfill.py` | 断点修复与实时闭环仍需压测 | 需要用真实重启/补单场景确认无重复复盘 |
| LE5 | `miniprogram_v2/pages/learning/*` | 学习生命周期状态与后端枚举已增多 | 前端展示层还可再做一次精简和映射统一 |

## 🔵 P2（审计 v13 确认）

| # | 文件 | 问题 | 说明 |
|---|------|------|------|
| 1 | `monitor/evolution_story.py` vs `evolution_story/` | 文件与目录冲突 -> Python import 未定义 | 清掉一边 |
| 2 | `./nul` | Windows 设备名意外创建的文件 | 直接删除 |
| 3 | `alpha/factor_attribution.py` | 旧版归因，已被 `attribution_engine.py` 取代 | 无人 import |
| 4 | `execution/paper_bridge.py` | 旧版模拟盘桥接 | 无人 import |
| 5 | `live/` 目录 | factor_monitor, meta_learner_monitor 仅被旧 main.py 引用 | 迁移或删除 |
| 6 | 根目录 `_` 前缀调试脚本 | 6 个一次性脚本散落根目录 | 移入 `scripts/debug/` |
| 7 | `scripts/ctrader_live_runner.py` | docstring 仍引用 MT5 | 更新或删除 |

## 📋 已知技术债务（蓝图 Appendix C.1）

| # | 问题 | 说明 |
|---|------|------|
| 1 | `regime_classifier.py` 因子脱节 | 使用 `[aroon, cci, mfi, williams_r]` 不在 39 因子池 |
| 2 | `scripts/train_xgb_walkforward.py` 仅 4 因子 | 非蓝图 68 特征设计 |
| 4 | `scripts/factor_pca.py` docstring 过时 | 声称 15 因子，实际 39 |
| 5 | `data/store.py` -> `duckdb_store.py` 双层包装 | 过度封装，所有引用通过旧 `store.py` 再委派到 `duckdb_store.py` |

> 以上不阻塞运行。当需要扩展 ML、多品种或模型接入时，再按优先级继续处理。
