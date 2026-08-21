# Change Impact Checklist

> Status: active
> Last verified: 2026-08-10
> Scope: mandatory admission, impact, deletion, verification, and documentation closure for every production change.

本文是后续修复的唯一执行清单。目标不是让每批增加更多保护层，而是确认事实、
替换旧路径、删除冗余，并以最小生产代码完成验证。

## 1. 每批固定流程

```text
读取事实源和旧债
  -> 用代码图谱确认真实调用链
  -> 读取日志/API/PostgreSQL/runtime_kv/运行服务
  -> 声明 canonical authority 和被替代路径
  -> 确认安全、readiness、risk sizing 权力层
  -> 做最小实现
  -> 跑针对性测试/迁移/OpenAPI/构建
  -> 删除旧实现、旧字段回退和实现耦合测试
  -> 重启并验证运行事实
  -> 更新事实源、旧债、状态和验收矩阵
```

任一步发现事实未知，都保持 `unknown/warming_up`，不得用默认零、兼容值或猜测值继续。

## 2. 修改前必须写清的六件事

每批在动代码前确认：

1. **问题事实**：真实错误由哪个日志、API、数据库行、运行快照或测试证明。
2. **canonical authority**：修复后由哪个现有模块负责计算或写入。
3. **调用方**：live、replay、RiskPolicy、readiness、API、frontend 中哪些只读消费。
4. **替代对象**：哪些旧计算、写入口、fallback、wrapper、字段和测试会被删除。
5. **权力层**：该逻辑属于 Safety、Readiness 或 Risk sizing，不能跨层复制。
6. **不新增项**：本批明确不增加哪些 service、表、线程、调度器、阈值和兼容层。

缺少 canonical authority 或删除清单时，不得开始新增抽象。

## 3. 复杂度准入

新增生产抽象必须至少满足一项：

- 立即删除两个或更多真实重复实现；
- 隔离真实变化源，例如 broker adapter；
- 已有多个生产调用方共享同一行为；
- 形成当前不存在且可独立验证的必要生产合同。

以下理由不构成准入：

- 以后可能扩展；
- 文件太长所以再包一层；
- 为了命名更整齐；
- 只有一个实现却提前设计插件/接口；
- 仅原样转发参数或重新命名字段；
- 保留旧路径“以后再删”但没有退出条件。

使用 deletion test 判断：删除模块后，复杂度如果只是消失，它是冗余；复杂度如果会
重新散落到多个真实调用方，它才有保留价值。

## 4. 单一 authority 检查

| 检查 | 必须满足 |
|---|---|
| 事实计算 | 同一事实只有一个生产计算器 |
| 状态写入 | 同一状态只有一个 writer |
| 风控裁决 | `RiskPolicyService` 是动作裁决入口，调用方不复制阈值 |
| 配置变更 | typed plan / Coordinator / RuntimeConfig mutation 复用现有提交链 |
| replay | 复用 live 的冻结输入和纯计算，不建立历史专用算法 |
| readiness | 只读 canonical snapshot/reason，不读取原始输入重算 |
| API | 序列化 canonical contract，不生成另一套业务结果 |
| frontend | 显示事实和触发既有 API，不推断风险、授权或 readiness |

允许多个投影，不允许多个裁决。投影必须携带来源、观测时间、freshness 和稳定
reason code，不能因为重新序列化而成为新事实源。

## 5. 三层安全权力检查

### Safety

只处理必须立即禁止新增风险的硬事实：

- broker/account/positions/spot 不可确认；
- unknown execution；
- SL/TP 或保护状态不可确认；
- emergency、本地 latch、硬损失限制；
- close/reduce/tighten 的独立可用性。

Safety 不计算 alpha、VaR/CVaR 或最终 candidate volume。

### Readiness

只回答当前 canonical 事实是否存在、新鲜和足够：

- 不重新计算风险；
- 不修改 RuntimeConfig；
- 不清理 latch；
- 不切换静态发布开关；
- 不把多个同义 blocker 重新包装成新的最终权力。

### Risk sizing

只处理 exposure、VaR/CVaR、Kelly、stress、concentration 和最终 candidate volume：

- 使用冻结、可追溯输入；
- live/replay 共享纯计算；
- 缺输入保持 unknown/warming_up；
- 不拥有进程健康、发布能力或前端状态。

同一条件只能归属一个 owner。跨层需要该条件时，消费 owner 发布的 reason/fact。

## 6. 领域影响面

只检查本批实际涉及的领域，但必须覆盖所有真实调用方。

| 领域 | 必查 |
|---|---|
| broker/execution | raw broker contract、intent、fresh reconcile、禁止猜测/重发、close/reduce/tighten 独立性 |
| risk | frozen input、current/final candidate notional、单位、95%/99%角色、unknown 语义 |
| factor | role、PIT/closed bar、normalizer cadence、compositor、唯一权重写入口、lifecycle |
| learning | lineage、污染资格、application/effect、同 scope 并发、terminal prior |
| governance | typed plan、V16 single-use、Coordinator 原子性、risk direction、rollback |
| data/schema | PostgreSQL `runtime` + `canonical_v2`、forward-only migration、幂等、无 SQLite state 写入 |
| API/readiness | canonical contract、component freshness、只读投影、OpenAPI |
| frontend | endpoint-level decoder、unknown/stale/error、known zero、无旧字段回退 |
| operations | systemd owner、loaded flags、日志、runtime_kv、重启恢复、回滚 |

需要更细的当前合同，读取 `system-source-of-truth.md` 和对应 `*-contract.md`，不在本清单
重复维护实现细节。

## 7. 兼容与删除合同

兼容代码只有同时具备以下信息才能保留：

- 当前真实调用方；
- canonical replacement；
- 退出条件；
- 最晚删除阶段；
- 证明旧调用方迁移完成的测试或运行证据。

新路径验收通过后，同批优先删除：

- 旧计算和旧 writer；
- API/live/replay/readiness 平行重算；
- 前端旧字段 fallback；
- pass-through wrapper 和单实现 adapter；
- 只保护旧实现结构的测试；
- 失去生产 reader 的配置、依赖、导出和文档。

必须永久保留的安全降级路径不属于普通兼容层，但仍必须只有一个 owner，且不能产生
与 canonical path 不同的“成功”语义。

## 8. 验证顺序

默认按风险最小、反馈最快的顺序：

```text
最小红绿回归
  -> 相关模块/contract/parity 测试
  -> migration check 或 isolated PostgreSQL 测试（涉及 schema 时）
  -> OpenAPI / frontend typecheck-build（涉及契约时）
  -> import/CLI/systemd/dynamic entry 扫描（涉及删除时）
  -> git diff --check / compile
  -> 受控重启
  -> 日志、API、PostgreSQL、runtime_kv、broker 只读验证
```

全量测试只在以下情况运行：

- 阶段收口或发布门；
- 修改跨越多个无法隔离的生产 authority；
- 删除公共模块且影响面无法由图谱和针对性测试完整证明；
- operator 明确要求。

单测不能替代真实 broker deal、完整持仓生命周期、shadow continuity 或进程加载事实。

## 9. 批次完成条件

以下全部满足才可标记完成：

- canonical authority 已部署并由真实调用方使用；
- 同一事实不存在第二个生产计算者或 writer；
- 删除清单已执行，剩余兼容均有明确退出合同；
- 针对性测试和必要 contract/migration/OpenAPI/build 通过；
- 服务重启后的日志、API、PostgreSQL/runtime_kv 与文档一致；
- `unknown/warming_up/stale/error` 未被默认值掩盖；
- Safety、Readiness、Risk sizing 没有新增重复门控；
- 生产代码没有无理由净增；若净增，新增部分对应真实必要合同；
- 事实源、旧债、当前状态和验收矩阵已同步。

canonical 路径完成但旧路径仍在，批次状态只能是 `migrating`，不能是 `complete`。

## 10. 文档收口

按固定职责更新：

1. `system-source-of-truth.md`：只更新当前 authority 和合同。
2. `legacy-debt-register.md`：只记录仍未删除的兼容/冗余；完成后从登记册删除，追溯通过 Git、测试和审计事实保留。
3. `production-autonomy-repair-optimization-plan.md`：只更新阶段和剩余目标。
4. `phased-repair-rollout-status.md`：只更新当前状态、下一批和运行证据。
5. `phased-repair-acceptance-matrix.md`：只更新可重复测试/命令和未满足证据。
6. 对应 schema/contract/SOP：仅在合同真实变化时更新。

禁止把同一实现说明复制到多份文档；其他文档引用权威入口即可。
