# Documentation Governance

> Status: active
> Last verified: 2026-07-06
> Scope: how project documentation is organized, updated, and used before code changes.

本文定义文档治理规则。目标不是把文档写多，而是让每次升级、修复和排障都能先找到权威口径，避免旧理解反复被带回系统。

## 1. 核心原则

1. 文档必须服务运行系统，不维护脱离代码的平行蓝图。
2. 同一个主题只能有一个权威入口；其他文档只能引用它，不能重新定义它。
3. 任何涉及交易、风控、学习、因子治理、配置写入的改动，都必须先查对应权威文档和旧债登记。
4. 文档必须标记状态，避免草案、历史计划和当前事实混在一起。
5. 过时内容优先改写或移动到旧债登记，不继续保留模糊说法。

## 2. 文档状态

每份长期文档顶部应该包含：

```text
Status: active | draft | historical | deprecated
Last verified: YYYY-MM-DD
Scope: 这份文档覆盖什么，不覆盖什么
Source of truth: 可选；当本文不是权威来源时必须填写
```

状态含义：

| 状态 | 含义 |
|---|---|
| `active` | 当前系统按此执行 |
| `draft` | 正在设计，不能当作现网事实 |
| `historical` | 历史方案或升级记录，只用于追溯 |
| `deprecated` | 明确废弃，不能作为实现依据 |

## 3. 文档分层

| 层级 | 用途 | 当前入口 |
|---|---|---|
| 总入口 | 读者从哪里开始 | `docs/README.md` |
| 架构事实 | 系统是什么、边界是什么 | `docs/architecture.md` |
| 运转地图 | 一眼看懂链路 | `docs/system-operation-map.md` |
| 自治治理架构 | 多智能体、模型、大脑、控制面和权力边界 | `docs/autonomous-governance-architecture.md` |
| 智能总账 | 规则智能、影子模型、审计数据和精度口径 | `docs/rule-driven-intelligence-inventory.md` |
| 事实源索引 | 每类状态以哪里为准 | `docs/system-source-of-truth.md` |
| 变更检查 | 改代码前后扫影响面 | `docs/change-impact-checklist.md` |
| 旧债登记 | 历史残留、废弃路径、迁移状态 | `docs/legacy-debt-register.md` |
| 操作 SOP | 服务器和排障动作 | `docs/server-backend-sop.md` |
| 协作规则 | 本地/服务器职责边界 | `docs/development-workflow.md` |
| 稳定契约 | 学习、模板、Factor Card 等接口语义 | 对应 `*-contract.md` / `*-schema.md` |
| 规划 | 尚未成为当前事实的大方向 | `docs/planning/` |

## 4. 改动前阅读规则

不同改动必须先读不同文档：

| 改动类型 | 必读文档 |
|---|---|
| 因子、权重、组合、AWE | `architecture.md`, `system-source-of-truth.md`, `legacy-debt-register.md`, `change-impact-checklist.md` |
| 自治治理、学习、回滚 | `rule-driven-intelligence-inventory.md`, `learning-evidence-contract.md`, `system-source-of-truth.md`, `change-impact-checklist.md` |
| 多智能体/模型/自治大脑治理边界 | `autonomous-governance-architecture.md`, `system-operation-map.md`, `rule-driven-intelligence-inventory.md`, `system-source-of-truth.md` |
| RuntimeConfig、overlay、snapshot | `system-source-of-truth.md`, `server-backend-sop.md`, `change-impact-checklist.md` |
| 风控、执行、仓位监督 | `rule-driven-intelligence-inventory.md`, `position-supervisor-contract.md`, `system-operation-map.md`, `change-impact-checklist.md` |
| 前端展示契约 | `factor-card-schema.md`, `system-source-of-truth.md`, `development-workflow.md` |
| 服务器运行问题 | `server-backend-sop.md`, `startup.md`, `system-source-of-truth.md` |

## 5. 新文档准入

新增文档前先判断：

1. 是否已有权威文档可以扩展。
2. 是否只是某次任务的临时计划，若是应放入 `docs/planning/`。
3. 是否会和现有文档重复定义系统行为。
4. 是否需要加入 `docs/README.md`。
5. 是否需要在 `legacy-debt-register.md` 记录取代了哪个旧口径。

## 6. 代码改动后的文档收口

完成代码改动后必须检查：

1. 是否改变系统事实源。
2. 是否改变运行链路。
3. 是否改变自治治理动作。
4. 是否改变前端/API contract。
5. 是否让某条旧债完成、延期或新增。
6. 是否需要更新验证命令或 SOP。

如果答案有任意一个是“是”，必须同步更新对应文档。

## 7. 禁止事项

- 禁止让历史计划继续伪装成当前事实。
- 禁止在多个文档里重复定义同一条权力边界。
- 禁止把测试临时配置写成生产规则。
- 禁止只在对话里确认关键架构结论，不落文档。
- 禁止文档只写“应该”，却不写当前实际入口、状态和验证方式。
