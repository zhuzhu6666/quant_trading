# Change Impact Checklist

> Status: active
> Last verified: 2026-07-06
> Scope: pre-change and post-change checklist for backend, factor, governance, risk, data, and frontend contract work.

本文是每次动代码前后的检查清单。目标是先扩大影响面，再把改动收口，避免只修眼前一个点却破坏其他链路。

## 1. 改动前先判断类型

先把任务归类：

| 类型 | 例子 | 风险 |
|---|---|---|
| 因子语义 | role、归一化、组合、权重 | 可能影响方向评分、AWE、readiness、前端 |
| 自治治理 | 晋升、降权、禁用、回滚、模板切换 | 可能影响配置持久化和实盘安全 |
| 风控执行 | gate、sizing、position supervisor、event sizing | 可能影响下单、改仓、平仓 |
| 数据链路 | bars/ticks/L2/external/events/state | 可能影响 PIT、freshness、回测/live 一致性 |
| 学习链路 | 样本、evidence、模型、policy suggestion | 可能影响训练准入和治理证据 |
| API/前端契约 | 新字段、旧字段兼容、展示含义 | 可能造成小程序/Web 误读 |
| 运维启动 | systemd、startup、overlay restore、readiness | 可能造成重启后状态漂移 |

## 2. 必扫影响面

每次改动至少扫这些问题：

1. 这个改动改变了哪个事实源？
2. 是否有旧路径仍会写入同一状态？
3. 是否改变了 live、backtest、shadow、learning 任一链路的行为？
4. 是否改变了前端/API 字段含义？
5. 是否改变了 readiness 或健康检查口径？
6. 是否需要配置持久化、快照或回滚点？
7. 是否有历史文档或测试仍在表达旧理解？
8. 是否需要把旧债登记为 fixed/migrating/deprecated？

## 3. 因子改动检查

改因子系统时必须检查：

| 检查项 | 目标 |
|---|---|
| `StreamingFactorEngine` | 因子是否被计算，生命周期是否正确 |
| `SignalNormalizer` | 高频/低频采样是否合理 |
| `PortfolioCompositor` | 是否只有 alpha 进入方向评分 |
| `DecisionPolicy` | 权重写入是否唯一、是否过滤 lifecycle/role |
| `AdaptiveWeightEngine` | 是否只调整 alpha |
| `Factor Catalog` | role、enabled、used_in_score、lifecycle 是否一致 |
| `readiness` | 是否误报 context/gate/sizing 缺权重 |
| `risk/concentration` | 是否只统计 alpha 共识 |
| 前端展示 | context 不显示为多空投票 |
| 测试 | live tick、alpha、AWE、readiness、frontend contract |

## 4. 自治治理改动检查

改 Orchestrator、overlay、policy suggestion、模板或回滚时必须检查：

| 检查项 | 目标 |
|---|---|
| `RiskPolicyService` | 动作是否有明确风控入口 |
| `RuntimeConfigMutationService` | 配置写入是否持久化 overlay 和 snapshot |
| `evolution_decision` | 是否记录判断和 rollback_json |
| `learning_application_log` | 是否记录应用状态 |
| `learning_application_effect` | 是否能支撑后验回滚 |
| `runtime_config_overlay` | 重启是否可恢复 |
| `factor_catalog_snapshot` | 每轮治理是否留痕 |
| 冷却/限频 | 单周期动作数量是否受限 |
| 测试污染 | pytest/test overlay 是否被生产拒绝 |

## 5. 风控执行改动检查

改 gate、sizing、仓位监督、事件窗口时必须检查：

| 检查项 | 目标 |
|---|---|
| `ExecutionGate` | 信号阈值、冷却、NFP/GVZ 等硬门槛是否保留 |
| `RiskPolicyService` | 新动作是否统一裁决 |
| `ContextPolicyService` | context 只影响阈值/仓位，不改方向 |
| `live_tick_pipeline` | gate 前后的顺序是否正确 |
| `sizing trace` | 最终仓位变化是否可解释 |
| ledger | skip/open/close/amend 是否可追溯 |
| tests | live tick、risk、position lifecycle |

## 6. 数据改动检查

改数据源、PIT、外部因子或库路径时必须检查：

| 检查项 | 目标 |
|---|---|
| `FactorFrameBuilder` | live/evolution/health 是否共用入口 |
| `release_at` | 外部数据是否 point-in-time |
| monthly DuckDB | 当前月链接是否正确 |
| PostgreSQL state | 是否避免新增 SQLite state 写入 |
| freshness | readiness 是否暴露数据时效 |
| fallback | 外部 enrichment 失败是否可降级 |

## 7. API/前端改动检查

改接口字段或展示语义时必须检查：

| 检查项 | 目标 |
|---|---|
| 旧字段兼容 | 小程序和旧调用不崩 |
| 新字段含义 | 前端不需要自行推断 |
| auth | 未授权返回是否符合预期 |
| readiness | 运维页能看到阻断/观察/审计状态 |
| Factor Cards | 与 Catalog 语义一致 |
| 文档 | schema/contract 是否更新 |

## 8. 改动后验证顺序

默认顺序：

```text
最小单测
  -> 相关模块测试
  -> 关键 contract/readiness 测试
  -> 必要时全量测试
  -> 服务日志
  -> 接口健康
```

服务器运行问题优先：

```text
先看日志
  -> 再看接口
  -> 再改代码
  -> 最后重启验证
```

## 9. 文档收口

改完后检查：

1. `docs/README.md` 是否需要新增入口。
2. `docs/system-source-of-truth.md` 是否改变事实源。
3. `docs/legacy-debt-register.md` 是否有旧债状态变化。
4. 对应 contract/schema/SOP 是否需要更新。
5. planning 文档是否仍只是历史计划，不能变成隐性事实源。
