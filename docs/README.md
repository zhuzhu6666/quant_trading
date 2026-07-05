# Documentation Index

> Status: active
> Last verified: 2026-07-06
> Scope: maintained documentation entry points and documentation governance.

当前文档集只保留正在维护的主入口。后续如无特殊需要，不再为同一主题新增平行蓝图文档，避免路线分叉。

## 文档治理入口

- [documentation-governance.md](documentation-governance.md) - 文档状态、权威入口、更新规则和新增文档准入
- [system-source-of-truth.md](system-source-of-truth.md) - 配置、因子、风控、学习、数据、API 的事实源索引
- [legacy-debt-register.md](legacy-debt-register.md) - 历史残留、废弃路径、迁移状态和防复活记录
- [change-impact-checklist.md](change-impact-checklist.md) - 每次改代码前后的影响面检查清单

## 主文档

- [../README.md](../README.md) - 项目总览与快速入口
- [../AGENTS.md](../AGENTS.md) - 当前工作区规则与防误操作提醒
- [system-operation-map.md](system-operation-map.md) - 当前真实运行架构、启动顺序、live tick 链路、worker 自治治理、状态库和 API 入口
- [rule-driven-intelligence-inventory.md](rule-driven-intelligence-inventory.md) - 规则驱动智能、影子模型、链路审计数据和精度语义总账
- [architecture.md](architecture.md) - 当前系统状态、目标完全体、分层定义、完整开发路线
- [factor-card-schema.md](factor-card-schema.md) - 因子解释卡片与治理展示 schema
- [../TODO.md](../TODO.md) - 当前工作面板，只保留当前主线、下一步和活跃缺口
- [development-workflow.md](development-workflow.md) - 本地前端 / 服务器后端的协作规则
- [server-backend-sop.md](server-backend-sop.md) - 服务器后端日常操作 SOP
- [startup.md](startup.md) - 后端、小程序和常用脚本启动方式
- [state-postgres-store.md](state-postgres-store.md) - PostgreSQL `state_v1` 运行态状态库和旧 SQLite 边界
- [web-frontend-upgrade-plan.md](web-frontend-upgrade-plan.md) - 当前 Web 操作台职责、路由和后续扩展边界
- [CTRADER_INTEGRATION.md](CTRADER_INTEGRATION.md) - cTrader 执行通道说明
- [position-supervisor-contract.md](position-supervisor-contract.md) - 持仓监督、trace、反事实成熟化与模板治理 contract
- [learning-evidence-contract.md](learning-evidence-contract.md) - 学习样本证据契约、训练准入与模型追溯语义
- [parameter-template-contract.md](parameter-template-contract.md) - 参数模板治理 contract
- [parameter-tuning-boundary.md](parameter-tuning-boundary.md) - 在线轻调 / 离线深调边界
- [../miniprogram_v2/README.md](../miniprogram_v2/README.md) - 当前微信小程序前端

## 辅助规划文档

- [planning/self-evolving-upgrade-plan.md](planning/self-evolving-upgrade-plan.md) - 历史升级计划索引；主路线现已并入 `architecture.md`
- [planning/multi-symbol-pipeline.md](planning/multi-symbol-pipeline.md) - 多品种扩展专项规划
- [planning/v15-autonomous-runtime-platform.md](planning/v15-autonomous-runtime-platform.md) - V15 自治运行内核大版本设计入口
- [planning/v16-autonomous-intelligence-brain.md](planning/v16-autonomous-intelligence-brain.md) - V16 自治交易大脑、世界模型、记忆、假设、模拟和自我批判设计

## 文档使用规则

- 文档治理规则、状态字段、新增文档准入：写入 `docs/documentation-governance.md`
- 系统事实源、冲突处理优先级：写入 `docs/system-source-of-truth.md`
- 历史残留、废弃路径、旧理解迁移：写入 `docs/legacy-debt-register.md`
- 代码改动影响面、验证顺序、文档收口：写入 `docs/change-impact-checklist.md`
- 长期架构、完全体定义、系统角色边界：写入 `docs/architecture.md`
- 近期开发动作和当前主线：写入 `TODO.md`
- 技术债、旧理解、废弃路径：写入 `docs/legacy-debt-register.md`
- 因子数据事实来源、PIT 外部数据、discovery 默认门禁：优先写入 `docs/system-source-of-truth.md` / `docs/architecture.md`
- 持仓监督、学习证据、参数模板这类稳定接口：写入对应 contract 文档
- 模型清单、训练准入、open outcome 和数据质量健康：优先写入 `learning-evidence-contract.md`
- 规则智能数量、执行链路、每步审计数据和精度口径：写入 `docs/rule-driven-intelligence-inventory.md`
- 涉及三端开发、发布、同步规则：写入 `docs/development-workflow.md`
- 不再把同主题内容拆成多份并行主文档

## 当前推荐阅读顺序

1. [../README.md](../README.md)
2. [system-operation-map.md](system-operation-map.md)
3. [rule-driven-intelligence-inventory.md](rule-driven-intelligence-inventory.md)
4. [documentation-governance.md](documentation-governance.md)
5. [system-source-of-truth.md](system-source-of-truth.md)
6. [architecture.md](architecture.md)
7. [change-impact-checklist.md](change-impact-checklist.md)
8. [legacy-debt-register.md](legacy-debt-register.md)
9. [development-workflow.md](development-workflow.md)
10. [server-backend-sop.md](server-backend-sop.md)
