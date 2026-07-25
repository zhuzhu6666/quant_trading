# 全项目分期修复发布状态

> Status: active current-state index
> Snapshot: 2026-07-26
> Scope: current phase, last verified evidence, next batch, and unresolved runtime acceptance
> Source of truth: 运行状态必须在每次实施前重新读取服务、PostgreSQL、`runtime_kv`、日志和 broker

本文不保存逐时操作流水，也不重复架构合同。历史细节通过 Git 历史和 repair ledger
追溯。当前权力边界见 `docs/system-source-of-truth.md`，执行流程见
`docs/change-impact-checklist.md`。

## 1. 当前阶段

| 阶段 | 状态 | 剩余工作 |
|---|---|---|
| P0 保护现场 | complete | 无 |
| P1 broker 成交事实 | runtime acceptance | 新 broker deal、restart replay、完整持仓生命周期 |
| P2 风险指标平面 | complete | 继续观察，不新增平行风险路径 |
| P3 证据/记忆/effect | not started | 先扫描真实 writer、identity、消费者和删除对象 |
| P4 V16 因果调度 | not started | 等 P3 canonical evidence |
| P5 架构收敛 | continuous | 每个 P3/P4 小批同步删除，不再单独堆大重构 |
| P6 Demo 观察/毕业 | blocked | 等前置正确性和真实样本 |

当前安全约束：

- `no_new_risk` 保持；
- Safety 保持 `shadow`；
- Generation、Execution Outcome、PG Job Queue 保持 disabled；
- Governance 保持 `dual_record`；
- 不自动清锁、不自动切 flag、不进入 `live_autonomous`。

上述值必须在下一批开始前从 process-loaded flags 和运行事实重新验证，不能只相信本文。

## 2. 已完成事实

### P0

- incident：`AUTONOMY-REPAIR-20260724-01`。
- 数据备份、污染 cohort、repair ledger 和修复不变量已建立。
- close/reduce/tighten/rollback 与只读观察保持可用。

### P1 代码和历史修复

- 1,150/1,150 broker deals 已按原始价格合同更正。
- 金额字段与价格字段解析已拆分。
- 10,800 条直接关联学习样本已隔离。
- 12 条无法权威恢复的 close quote 保留审计原值并 quarantine。
- 48 条双重污染 counterfactual 终态失效，13 条干净记录重建。
- 唯一污染扩张 mutation 已原子回滚。
- Execution Outcome price-integrity 已进入故障矩阵。

P1 尚未取得 post-repair 新成交和完整生命周期，因此仍是 runtime acceptance，不得因
单测或历史数据修复标记 complete。

### P2 canonical risk

唯一生产链：

```text
closed-bar forward_var_input.v1
  + fresh account/positions
  + current/final candidate signed notional
  -> backend.risk canonical calculators
  -> risk_metrics_snapshot.v2
  -> RiskPolicy / readiness / API / Web read-only consumers
```

已完成：

- 删除重复 root risk 模块、live 内联统计和 API 平行重算。
- live/replay 共用 frozen input、projection 和 lifecycle payload builder。
- 95% 使用既有硬闸；99% 只 shadow，没有新增阈值。
- readiness 只读 `runtime_kv[risk_metrics_snapshot.v2]`。
- Web 四个页面统一使用 `decodeCanonicalRiskSnapshot()`。
- 旧 VaR/stress/concentration 字段和别名 fallback 已删除。
- known 空仓零敞口可见；unknown/warming_up/error 不补零。
- schema v12 已应用，OpenAPI 未发生非预期变化。

最后一轮 P2 证据：

- D16/risk/policy/live/parity/readiness/replay/API 针对性批次：`275 passed`；
- 补充模块批次：`236 passed`、`163 passed`；
- 前端 `npm test`、`typecheck`、`build`：通过；
- 上一轮全量基线：`2452 passed, 9 skipped`；
- 本批按 operator 要求未重新运行全量。

## 3. 当前唯一下一批

P3 第一批只做 writer/identity 收敛，不先建新平台。

必须先输出：

1. review、counterfactual、memory、sample、application/effect 的全部生产 writer。
2. 每个 writer 的调用方、authority、identity 和持久化目标。
3. 重复、冲突、污染或无法归因的运行证据。
4. 选择复用的 canonical writer。
5. 本批立即删除和后续退出的旧路径。
6. 不新增 service、表、scheduler、阈值和兼容字段的证明；如确实无法复用，再单独说明。

未完成这六项前，不修改 P3 schema 或创建新 `ExperienceMemoryService/Writer`。

## 4. 仍需真实运行证明

以下不能由测试替代：

- post-repair 新 broker deal 的价格/金额合同；
- restart 后 deal replay；
- `open -> protection -> close -> deal sync -> review -> sample` 完整生命周期；
- Safety shadow 连续 24 小时空仓或一个完整 broker position lifecycle；
- 当前源码绑定的 fault matrix；
- 每次发布阶段的 process-loaded flags 和 release preflight。

真实证据未满足时：

- P1 保持 runtime acceptance；
- Safety 不从 shadow 切 enforce；
- 后续静态开关不推进；
- `no_new_risk` 不释放。

## 5. 每批状态更新格式

以后本文件只追加或替换以下当前信息，不保留逐时流水：

```text
Batch:
Canonical authority:
Deleted paths:
Targeted verification:
Migration/OpenAPI/build:
Runtime verification:
Remaining compatibility:
Unresolved live evidence:
Next batch:
```

阶段完成后删除已失效的中间描述，只保留最终结论和指向验收矩阵/repair ledger 的引用。
