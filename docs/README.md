# Documentation Index

当前文档集只保留正在维护的主入口。后续如无特殊需要，不再为同一主题新增平行蓝图文档，避免路线分叉。

## 主文档

- [../README.md](../README.md) - 项目总览与快速入口
- [../AGENTS.md](../AGENTS.md) - 当前工作区规则与防误操作提醒
- [architecture.md](architecture.md) - 当前系统状态、目标完全体、分层定义、完整开发路线
- [factor-card-schema.md](factor-card-schema.md) - 因子解释卡片与治理展示 schema
- [../TODO.md](../TODO.md) - 近期执行项、结构缺口、验证项、技术债
- [development-workflow.md](development-workflow.md) - 本地前端 / 服务器后端的协作规则
- [server-backend-sop.md](server-backend-sop.md) - 服务器后端日常操作 SOP
- [startup.md](startup.md) - 后端、小程序和常用脚本启动方式
- [web-frontend-upgrade-plan.md](web-frontend-upgrade-plan.md) - Web 操作台接替小程序完整展示能力的升级计划
- [CTRADER_INTEGRATION.md](CTRADER_INTEGRATION.md) - cTrader 执行通道说明
- [position-supervisor-contract.md](position-supervisor-contract.md) - 持仓监督、trace、反事实成熟化与模板治理 contract
- [learning-evidence-contract.md](learning-evidence-contract.md) - 学习样本证据契约、训练准入与模型追溯语义
- [parameter-template-contract.md](parameter-template-contract.md) - 参数模板治理 contract
- [parameter-tuning-boundary.md](parameter-tuning-boundary.md) - 在线轻调 / 离线深调边界
- [../miniprogram_v2/README.md](../miniprogram_v2/README.md) - 当前微信小程序前端

## 辅助规划文档

- [planning/self-evolving-upgrade-plan.md](planning/self-evolving-upgrade-plan.md) - 历史升级计划索引；主路线现已并入 `architecture.md`
- [planning/multi-symbol-pipeline.md](planning/multi-symbol-pipeline.md) - 多品种扩展专项规划
- [../CLAUDE.md](../CLAUDE.md) - 协作上下文与工程约定

## 文档使用规则

- 长期架构、完全体定义、系统角色边界：写入 `docs/architecture.md`
- 近期开发动作、验证项、技术债：写入 `TODO.md`
- 因子数据事实来源、PIT 外部数据、discovery 默认门禁：优先写入 `docs/architecture.md` / `CLAUDE.md`
- 持仓监督、学习证据、参数模板这类稳定接口：写入对应 contract 文档
- 涉及三端开发、发布、同步规则：写入 `docs/development-workflow.md`
- 不再把同主题内容拆成多份并行主文档

## 当前推荐阅读顺序

1. [../README.md](../README.md)
2. [architecture.md](architecture.md)
3. [../TODO.md](../TODO.md)
4. [development-workflow.md](development-workflow.md)
5. [server-backend-sop.md](server-backend-sop.md)
