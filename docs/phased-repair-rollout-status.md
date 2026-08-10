# 全项目分期修复发布状态

> Status: active current-state index
> Snapshot: 2026-08-10
> Scope: current phase, last verified evidence, next batch, and unresolved runtime acceptance
> Source of truth: 运行状态必须在每次实施前重新读取服务、PostgreSQL、`runtime_kv`、日志和 broker

本文只保留当前状态和可复核的未完成证据，不保存逐时操作流水，也不重复架构合同。历史批次通过 Git 历史追溯；权力边界见 `docs/system-source-of-truth.md`，执行流程见 `docs/change-impact-checklist.md`。

## 1. 当前阶段

| 阶段 | 状态 | 剩余工作 |
|---|---|---|
| P0 保护现场 | complete | 无 |
| P1 broker 成交事实 | runtime acceptance | post-repair 新成交、重启 replay 和完整持仓生命周期仍需按验收矩阵持续证明 |
| P2 风险指标平面 | complete | 只做观察，不新增平行风险路径 |
| P3 证据/记忆/effect | complete | 继续按现有 active effect 与污染隔离合同观察 |
| P4 V16 因果调度 | complete | causal grouping、单一 actionable/authority、单次 mutation 与三条 lane 已收口 |
| P5 架构收敛 | continuous | 每个小批同步删除旧路径，不单独堆大重构 |
| P6 Demo 观察/毕业 | blocked | 等前置正确性和真实运行证据 |

## 2. 最近一次运行核对

2026-08-10 只读核对结果：

- `quant-backend.service`、`quant-learning-worker.service` active，均无本次启动后的 restart；`/api/health` 为 `db=connected`、`ctrader=connected`。
- `backend_readiness_snapshot.v1` 当前为 known/ready，live execution、live alpha、autonomous mutation 和 release 均为 `true`，对应 blocker 列表为空。
- live loop 为 running/ready/accepting new risk；account 与 positions reconcile fresh，当前空仓；`risk_metrics_snapshot.v2` 为 known，使用 500 个闭合 M5 样本。
- learning worker capability 为 `boot_status=ready`、`recovery_status=complete`，observation/research/mutation capability available，mutation circuit closed。
- Safety v2 仍为 `shadow/observing`，尚未满足既有 24 小时连续空仓或完整 broker lifecycle 条件；这不改变当前 safety shadow 的发布门。
- 静态 flags 保持 `live_safety_plane_v2_mode=shadow`、`live_generation_controller_v2_enabled=false`、`ctrader_execution_outcome_v2_enabled=false`、`governance_mutation_coordinator_v2_mode=dual_record`、`pg_job_queue_v2_enabled=false`。

readiness 的 ready 只表示当前事实和能力可用，不是开关提交权，也不替代 V16、Candidate Review、RiskPolicy、Coordinator 或真实 broker 证据。

## 3. 已完成并仍有效的事实

### P0 保护现场

- incident、污染 cohort、repair ledger 和修复不变量已建立。
- close/reduce/tighten/rollback 与只读观察保持可用。

### P1 代码与历史修复

- broker deal 的价格字段与金额字段解析已按各自合同拆分，历史错误价格已更正。
- 直接关联的污染学习/反事实记录已隔离；无法权威恢复的 close quote 保留审计原值并 quarantine。
- unknown execution outcome 不进入价格归因、review、experience、counterfactual 或治理。
- 代码修复完成不等于 P1 runtime acceptance 完成；运行验收仍以真实 post-repair lifecycle 为准。

### P2 canonical risk

```text
closed-bar forward_var_input.v1
  + fresh account/positions
  + current/final candidate signed notional
  -> backend.risk canonical calculators
  -> risk_metrics_snapshot.v2
  -> RiskPolicy / readiness / API / Web read-only consumers
```

P2 已删除重复 root risk、live 内联统计、API 平行重算和前端旧字段 fallback；known 空仓零敞口保持真实零，unknown/warming_up/error 不补零。

### P3 证据、记忆与 effect

- review 的 canonical identity 仍是 `trade_outcome_review.review_id`。
- rich lesson 统一由现有 `upsert_trade_lesson_memory()` 写入 `trade_lesson_memory.v1`；旧 `live_review`、`learning_backfill.v1` 重复 projection/writer 已删除。
- application/effect 继续按 scope 和 committed mutation 归因；污染、partial、missing evidence 不得形成可执行治理事实。

### P4 V16 因果调度

- `V16CommandGate.is_actionable()` 是 readiness、stepper、authorize 和 claim 共用的唯一 actionable predicate。
- Agent Authority 提供唯一 execution owner/required gate；同一命令最多一个 committed mutation。
- autonomous learning、factor governance、position supervisor governance 三条 lane 继续复用现有 RiskPolicy、V16、Candidate Review 和 Coordinator；不新增第二套 command queue 或 mutation writer。

### 2026-08-10 智能自主进化代码批次

- Canonical authority：风险只经 `RiskLimitSnapshot/risk_kelly_sizing`；Readiness 只读已有投影；learning worker 单写 evolution watermark；Factor Cards/lifecycle/effect 负责因子准入；broker intent 复用既有状态机。
- Deleted paths：无条件最小量、readiness 重型 V16/因子重算、Web readiness 详情 fallback、`:58` evolution 档、Backend evolution 注册/启动 catch-up 和同 hash snapshot 放大语义已删除。
- Targeted verification：风险/Readiness/执行组合 170 passed；生命周期/Candidate Card/evolution/config/lineage 组合 125 passed；Web 4 组合同测试与 production build 通过。
- Migration：`decision_factor_snapshot_lineage` migration 13 已由正式迁移器应用并复核 6 个 lineage 列；历史默认 `lineage_missing`。
- Runtime posture：在线 backend/learning worker 未因迁移重启；本批代码尚未部署到在线进程，五项静态 flags 未推进。
- Unresolved live evidence：30 次 readiness p95/无重叠、有效 RuntimeConfig `1%/4%/16%/20`、watermark/backpressure 实际排空、legacy ACTIVE 退回、execution intent 100% 和完整 broker lifecycle。

## 4. 仍需真实运行证明

以下证据不能由单测、历史快照或 readiness 替代：

- post-repair 新 broker deal 的价格/金额合同；
- restart 后 deal replay 与 position identity 恢复；
- `open -> protection -> close -> deal sync -> review -> sample` 完整生命周期；
- Safety shadow 连续 24 小时空仓或一个完整 broker position lifecycle；
- 当前源码绑定的 execution/safety fault matrix；
- 每次发布阶段的 process-loaded flags、PID、fingerprint 和 release preflight。

在证据未满足前：

- P1 保持 `runtime acceptance`；
- Safety 不从 `shadow` 切 `enforce`；
- 后续静态开关不推进；
- 不把 readiness ready、单次 bridge、单次 effect 或测试通过解释为自治毕业。

## 5. 下一批处理顺序

1. 只读复核新 broker lifecycle、deal replay、review/sample lineage 和 Safety shadow continuity。
2. 对 `legacy-debt-register.md` 中仍在迁移的路径逐项收集退出证据，并在 canonical 验证后同批删除旧路径。
3. 仅在真实证据满足后运行对应 release gate；不通过 SQL 改写历史 review、command、sample 或 maturity。
4. 保持 Demo adaptive supervisor 为 `observation_only`，不扩大模型、因子或治理生产权限。

## 6. 每批状态更新格式

以后本文件只保留或替换以下当前信息：

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
