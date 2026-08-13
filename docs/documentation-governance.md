# Documentation Governance

> Status: active
> Last verified: 2026-08-13
> Scope: 文档职责、更新与删除规则。

## 1. 原则

1. `docs/README.md` 是唯一入口和当前状态摘要。
2. `system-source-of-truth.md` 只定义长期事实源、权力边界和稳定运行合同。
3. `legacy-debt-register.md` 只保留 active、migrating、quarantined、regressed 项；完成项由 Git 历史追溯。
4. `planning/production-autonomy-repair-optimization-plan.md` 是唯一全局生产工程计划；有独立生命周期、owner、读者和退出条件的领域计划可以作为
   scoped companion 存在，但必须明确不替代全局计划，不复制系统事实。
5. rollout status 记录实际进度，acceptance matrix 记录通过门槛；二者不重复架构说明。
6. 领域合同只保存该领域无法由事实源概括的 schema、状态机或消费规则。
7. 命名为 V15/V16、final blueprint、upgrade plan 的历史版本文档不再作为活动事实源。

领域计划必须在 docs/README.md 有入口，在计划正文写明 owner、scope、替代/删除
清单、完成条件和回滚方式；完成或取消后应从活动入口移除，剩余事实转入对应
合同、状态或旧债登记。

## 2. 更新责任

| 变化 | 必须更新 |
|---|---|
| 当前阶段、运行姿态、测试基线、下一步 | `docs/README.md` |
| authority、数据源、writer、runtime/API 合同 | `system-source-of-truth.md` |
| 新兼容、重复路径、迁移或回归 | `legacy-debt-register.md` |
| 实施范围或先后顺序 | 当前 production plan |
| 小批实际完成或阻塞 | rollout status |
| 验收证据或发布门 | acceptance matrix |
| schema/状态机细节 | 对应领域合同 |
| 启动、日志、迁移、重启命令 | server backend SOP |

同一事实只在一处展开；其他文档只链接，不复制段落。

## 3. 新增与删除

新增文档前必须证明：

- 现有文档无法承载；
- 内容有独立生命周期；
- 有明确 owner、读者和删除条件。

否则合并进现有文档。临时调研、完成流水、一次性 checklist 不进入长期文档；需要留痕使用 Git、测试、migration ledger 或运行审计。

以下情况直接删除而不另建归档目录：

- 被当前事实源完整替代；
- 只描述已完成版本或旧阶段；
- 与另一文档大段重复；
- 引用已退役架构；
- 内容可从 Git 历史恢复。

## 4. 新对话读取规则

用户说“读一遍文档”“确认当前状态”时：

1. 先读 `AGENTS.md` 和 `docs/README.md`；
2. 再读四份系统级必读文档；
3. 只按当前任务进入领域合同；
4. 用代码、服务、PostgreSQL、runtime snapshot、日志和测试刷新易变事实。

不得把“读全部 Markdown”理解为逐份读取历史材料；文档体系已经通过入口和按需路由表达完整上下文。
