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
> - ✅ 模型就绪样本导出已落地：`LearningFeatureProvider` + `/api/learning/dataset` + `/api/learning/decision-dataset`
> - ✅ 离线训练数据集快照已落地：`LearningDatasetBuilder` + `/api/learning/dataset/export`
> - ✅ 样本已包含因子贡献对账：`factor_outcomes` + `attribution_alignment` 可解释入场贡献、真实净贡献、helpful/harmful 角色与质量门
> - ✅ 训练前数据体检已落地：`LearningDatasetReadiness` + `/api/learning/dataset/readiness`
> - ✅ 离线快照 manifest 已内嵌 readiness：导出物自带训练门槛、schema issue、ready/warming_up 判断
> - ✅ 离线快照独立校验已落地：`LearningDatasetValidator` + `/api/learning/dataset/validate` 可复验 hash、行数、schema、manifest
> - ✅ 模型适配器安全基线已落地：`DatasetSummaryAdapter` + `/api/learning/dataset/model-card` 可生成并注册离线 model_card，但明确禁止接入实盘执行
> - ✅ 离线统计训练基线已落地：`LearningStatisticalTrainer` + `/api/learning/dataset/train` 可从已校验 snapshot 训练可解释权重 artifact，并可注册为离线模型版本
> - ✅ 离线模型准入门已落地：`ModelPromotionGate` + `/api/learning/model/promotion-gate` 可评估 registered artifact 是否进入 shadow validation，且不会绕过 live/canary
> - ✅ 模型影子验证候选队列已落地：`ModelShadowQueue` + `/api/learning/model/shadow-queue` 可幂等登记、查询和推进 trained model 的 shadow validation 状态
> - ✅ 模型影子验证 runner 已落地：`ModelShadowRunner` + `/api/learning/model/shadow-run` 可消费 queued candidate、生成可解释 shadow report 并回写 passed/failed
> - ✅ 模型金丝雀预审已落地：`ModelCanaryReviewer` + `/api/learning/model/canary-review` 可将 shadow-passed 模型推进到 canary_ready / canary_rejected，仍不接实盘执行
> - ✅ 模型推理合同已落地：`ModelInferenceContract` + `/api/learning/model/inference` 只接受 canary_ready 模型，输出 advisory-only 可解释评分并落审计日志，不下单、不改权重
> - ✅ 受控模型金丝雀试运行已落地：`ModelCanaryExecutor` + `/api/learning/model/canary-trial` 可批量消费 advisory inference、记录 canary_passed / canary_failed，仍不下单、不改权重
> - ✅ 端到端模型学习工作流已落地：`LearningModelPipeline` + `/api/learning/model/pipeline/run` 可串联 train -> gate -> queue -> shadow -> canary review -> controlled trial
> - ✅ 实时因子链路已补齐执行失败账本：`signal/open/close/skip/order_failed/amend_failed` 均可进入模型样本
> - ✅ 模型样本已接入订单/仓位生命周期：`execution_trace` 汇总 order / position 事件、失败订单和 broker lifecycle
> - ✅ 大模型上下文卡片已落地：`llm_context` 提供 prompt_card、evidence_bullets、label_summary
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
| L5 | 服务器开发同步规范固化 | 完成 | 已固化到 `docs/development-workflow.md`：本地为主开发端，GitHub main 为最终合并源，服务器为后端运行/验证端，热修需短事务回推并保持三端一致 |

## 🟡 延期项

| # | 项 | 原因 | 触发条件 |
|---|----|------|---------|
| D1 | Autoencoder 非线性特征压缩 | 过拟合风险高，20K bar 极易学噪声 | bar > 100K |
| D2 | 数据版本控制系统 | DuckDB snapshot 已够用，无需额外系统 | 需要时重构 |

## ⚠️ P1（审计 v13 确认）

| # | 文件 | 问题 | 说明 |
|---|------|------|------|
| TD1 | `execution/ctrader_bridge.py` | spot price 除数 `10**5` 硬编码 XAUUSD | 换品种会价格错误 |
| TD3 | `execution/market_impact.py:117` | 成本计算默认金价 3000.0 硬编码 | 非 XAUUSD 品种会成本偏差 |
| P1-3 | `config/__init__.py` | MT5 相关常量和 config 残留 | MT5 已彻底移除 |

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

## 📋 已知技术债务

| # | 问题 | 说明 |
|---|------|------|
| 1 | `regime_classifier.py` 因子脱节 | 使用 `[aroon, cci, mfi, williams_r]` 不在 39 因子池 |
| 2 | `scripts/train_xgb_walkforward.py` 仅 4 因子 | 与当前模型数据管道脱节 |
| 4 | `scripts/factor_pca.py` docstring 过时 | 声称 15 因子，实际 39 |
| 5 | `data/store.py` -> `duckdb_store.py` 双层包装 | 过度封装，所有引用通过旧 `store.py` 再委派到 `duckdb_store.py` |

> 以上不阻塞运行。当需要扩展 ML、多品种或模型接入时，再按优先级继续处理。
