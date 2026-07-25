# 生产自治修复与架构收敛总方案

> Status: implementation active — P0 complete, P1 runtime acceptance active, P2 complete, P3 not started
> Last verified: 2026-07-26
> Scope: production correctness repair, authority convergence, legacy deletion, runtime acceptance, and autonomy graduation
> Source of truth: 本文只定义阶段、流程和退出条件；当前生产事实以 `docs/system-source-of-truth.md`、代码、PostgreSQL 和运行服务为准

## 1. 当前结论

当前系统不需要重写，也不允许继续通过新增 service、门控、表和兼容层解决局部问题。
后续修复采用唯一原则：

> 一个事实一个计算者、一个状态一个写入者；建立 canonical 路径后，同阶段删除旧路径。

当前阶段状态：

| 阶段 | 状态 | 当前结论 |
|---|---|---|
| P0 保护现场 | complete | incident、备份、污染 cohort 和 `no_new_risk` 姿态已建立 |
| P1 broker 成交事实 | runtime acceptance | 代码和历史修复完成，等待 post-repair 新 broker deal 与完整持仓生命周期 |
| P2 风险指标平面 | complete | live/replay/Policy/readiness/API/Web 已收敛到 canonical snapshot |
| P3 学习证据与记忆 | not started | 不得在未确认调用链、writer 和删除对象前增加新存储或 service |
| P4 V16 因果调度 | not started | 只修真实闭环缺口，不增加新的调度平面 |
| P5 持续架构收敛 | active discipline | 不再作为最后才做的“大重构”；每个 P3/P4 批次同时执行删除 |
| P6 Demo 观察与毕业 | blocked | 等待前置阶段和真实运行证据 |

持续约束：

- 保持 `no_new_risk`，不得擅自清锁。
- 不切换 Safety、Generation、Execution Outcome、Governance、PG Job Queue 静态发布开关。
- close/reduce/tighten/rollback 和只读观察继续。
- 未经 operator 明确授权，不进入 `live_autonomous`。

## 2. 文档和事实优先级

发生冲突时按以下顺序：

1. 当前运行服务、broker 对账、PostgreSQL `state_v1`、`runtime_kv` 和日志。
2. 当前代码调用链和测试合同。
3. `docs/system-source-of-truth.md` 和稳定 `*-contract.md`。
4. `docs/legacy-debt-register.md`。
5. 本计划、历史状态和旧注释。

本计划不得复制运行快照；当前 PID、样本数、blocker 和测试结果只写入
`docs/phased-repair-rollout-status.md`。

## 3. 后续唯一修复流程

每个小批次严格执行：

```text
事实确认
  -> authority/调用链确认
  -> replacement + deletion contract
  -> 最小实现
  -> 针对性验证
  -> 删除旧路径
  -> 受控重启和运行事实验证
  -> 文档收口
```

### 3.1 事实确认

依次读取：

1. `AGENTS.md`
2. `docs/system-source-of-truth.md`
3. `docs/legacy-debt-register.md`
4. `docs/change-impact-checklist.md`
5. 本计划
6. `docs/phased-repair-rollout-status.md`
7. `docs/phased-repair-acceptance-matrix.md`

随后检查：

- 当前 `git diff`；
- 运行服务和加载代码；
- PostgreSQL `state_v1`；
- `runtime_kv`；
- 日志；
- 最近针对性和阶段测试。

文档与运行事实冲突时，以运行事实和代码为准并立即修正文档。

### 3.2 Authority 和调用链确认

代码发现优先使用：

```text
search_graph -> trace_path -> get_code_snippet
```

图谱不足时再使用 `rg`、直接源码、动态 import、CLI/systemd/cron 和配置扫描。

修改前必须写清：

- canonical 计算者；
- canonical writer；
- live/replay/readiness/API/frontend 调用方；
- Safety、Readiness、Risk sizing 中的所属层；
- 被替代代码、字段、配置、测试和文档；
- 本批明确不新增的 service、表、线程、调度器、阈值和兼容层。

### 3.3 Replacement + deletion contract

每批必须形成以下最小合同：

| 项 | 内容 |
|---|---|
| Problem fact | 可重复日志、API、DB、运行快照或测试 |
| Canonical authority | 修复后唯一计算者/writer |
| Consumers | 只读复用该事实的真实调用方 |
| Delete now | 本批验证后立即删除的旧路径 |
| Temporary compatibility | 当前真实调用方、退出条件、最晚删除阶段 |
| Unknown semantics | 缺失/过期/错误时的真实状态 |
| Rollback | 如何恢复上一已验证代码/配置/数据投影 |

没有 `Delete now` 且不能证明新合同不可由现有 authority 承担时，不得新增生产抽象。

### 3.4 最小实现

- 优先修改 canonical 纯函数或现有 owner。
- API、readiness、replay 和 frontend 只做适配/投影。
- 不建立第二个 scheduler、缓存、状态机或风险计算器。
- 不为单调用方创建 pass-through service。
- 不以新表解决可由现有 ledger/`runtime_kv` 表达的问题。
- 不新增阈值掩盖结构或数据错误。
- Demo 不叠加与现有硬安全重复的保守门控。

### 3.5 针对性验证

默认验证顺序：

```text
最小回归
  -> 相关 contract/parity 测试
  -> 必要 migration/OpenAPI/frontend build
  -> 删除入口扫描
  -> compile/diff check
  -> 受控重启
  -> 日志/API/PG/runtime_kv/broker 只读验证
```

全量测试只在阶段收口、发布门、公共模块大范围删除或影响面无法隔离时运行。
测试不能替代真实 broker deal、完整持仓 lifecycle、shadow continuity 或 process-loaded
flags。

### 3.6 删除与完成

canonical 路径通过后必须同批删除：

- 平行计算和 writer；
- readiness/API/live/replay 重算；
- 前端旧字段 fallback；
- pass-through wrapper；
- 无生产 reader 的配置和导出；
- 只保护旧结构的测试；
- 重复文档说明。

旧路径未删除时，批次只能标记 `migrating`。只有 authority 单一、运行验证完成且文档
同步后才能标记 `complete`。

## 4. 开仓风险判定三层权力

### 4.1 Safety

负责 broker/account/positions/spot freshness、unknown execution、本地 latch、
emergency、SL/TP 和硬损失限制。Safety 必须保证 close/reduce/tighten 在 PG、学习或
审计故障时仍可执行。

### 4.2 Readiness

只读投影 canonical facts 和 blocker。不得重新计算风险、修改控制状态、清理 latch
或切换发布开关。

### 4.3 Risk sizing

负责 exposure、VaR/CVaR、Kelly、stress、concentration 和最终 candidate volume。
使用冻结输入并由 live/replay 共用。缺失保持 `unknown/warming_up`。

同一开仓风险 blocker 只能由其 owner 产生一次稳定 reason code。不得新增第四套“最终就绪”
或“统一裁决”对象；优先收敛现有 `RiskPolicyService`、readiness snapshot 和 release
preflight 的消费关系。

治理和发布仍保持 Safety / Release / Autonomy 三层分权：静态发布能力由 operator 和
release preflight 管理，自治扩张由 V16/专员/Coordinator 管理，均不得借 readiness
或 Risk sizing 获得新的写权限。需要统一展示时复用现有 readiness/preflight 投影，
不新建 `ExpansionDecision` authority。

## 5. 已完成阶段

### 5.1 P0

- incident：`AUTONOMY-REPAIR-20260724-01`。
- 保持 `no_new_risk` 和治理扩张暂停。
- 建立备份、污染 cohort、repair ledger 和修复不变量。

### 5.2 P1 代码与数据

- 修复 cTrader deal `executionPrice` 被错误按 `moneyDigits` 缩放的问题。
- broker 40 日只读重拉覆盖并更正 1,150/1,150 deal。
- 隔离 10,800 条直接关联学习样本。
- 12 条无法权威恢复的 close quote 保留审计原值并 quarantine。
- 61 条旧 counterfactual 已归档；48 条污染记录终态失效；13 条干净记录重建。
- 唯一污染扩张 mutation 已通过 risk-tightening mutation 原子回滚。
- 新成交价格 unknown 时仍允许金额/session 和风险缩减恢复，但不得进入价格复盘、
  experience 或治理。

P1 尚缺：

- post-repair 新 broker deal；
- `open -> protection -> close -> deal sync -> review -> sample` 完整生命周期；
- restart replay 的真实成交合同观察。

### 5.3 P2

canonical authority：

- `backend.risk.metrics_snapshot`
- `backend.risk.var`
- `runtime_kv[risk_metrics_snapshot.v2]`
- `RiskPolicyService`

完成项：

- 删除重复 root risk 模块、live 内联统计和 API 平行重算。
- 使用冻结 `forward_var_input.v1` 的 closed-bar returns。
- current 和最终 candidate signed notional 在同一输入上投影前瞻 VaR/CVaR。
- 95% 进入既有硬闸，99% 只 shadow，不新增阈值。
- live/replay 共享冻结输入、candidate projection 和 lifecycle payload builder。
- readiness/API/Web 只读 canonical snapshot。
- 前端删除旧风险字段和别名 fallback。
- `unknown/warming_up/error` 不补零；known 空仓零敞口保持真实零。
- schema v12 已应用；`risk_daily_equity` 仅保留审计，不参与开仓 VaR。

P2 完成不授权清除 `no_new_risk`、推进 P1 runtime acceptance 或切换静态 flag。

## 6. P3：证据、记忆和效果归因

目标不是先创建 `ExperienceMemoryWriter`、新表或新索引，而是先确认现有真实 writer、
重复 identity 和消费链。

固定顺序：

1. 图谱列出 review、counterfactual、memory、sample、application/effect 的全部 writer。
2. 证明重复、冲突或污染如何进入可执行治理。
3. 选择现有最接近 canonical 的 writer/identity。
4. 先迁移调用方并删除平行 writer。
5. 只有现有 schema 无法表达 stable lineage/revision 时，才允许 additive migration。
6. raw evidence 保持 append-only；canonical projection 可重建。

必须保持：

- broker account、position/deal、review、causal scope、contract version 和 source hash
  的稳定 lineage；
- contaminated/partial/missing evidence 治理权重为零；
- entry、supervisor、execution、data-quality scope 不互相覆盖；
- Experience Prior 只来自 terminal、bounded、可比 effect；
- 同 scope 同时只有一个 active effect；
- raw memory 不直接授权权重或 mutation。

P3 每个批次都必须净删除 writer、重复 projection 或兼容读取；不允许先构建新 memory
platform 再等待以后迁移。

## 7. P4：V16 因果调度与专员闭环

只修以下真实缺口：

- 同一 broker account/position/terminal close 内的 causal grouping；
- 唯一 actionable predicate；
- available/claimed/finalized/cancelled/failed 生命周期一致；
- 一条扩张命令最多绑定一个 committed mutation；
- command、specialist、policy、Coordinator、application/effect 可追溯；
- scope→agent→required gates 只由现有 Agent Authority 合同拥有。

禁止：

- 为 readiness、stepper、planner、Gate 各写一套 actionable 判断；
- 新建第二套 command queue；
- observe 生成可领取命令；
- claim/release/recovery 续期 `authority_issued_at`；
- LLM research specialist 写 RuntimeConfig、权重、模板、学习标签或 broker。

三条 deterministic lane 必须分别完成 success/noop/reject/retry/rollback/effect 证据：

- autonomous learning；
- factor governance；
- position supervisor governance。

## 8. 持续架构收敛

架构收敛不再推迟到独立 P5 大批次。P3/P4 每批同时执行：

- authority 数量检查；
- blocker owner 去重；
- RuntimeConfig field reader/writer 检查；
- snapshot/review/eval 幂等；
- 零生产引用删除；
- `live_service` 只保留 composition/wiring；
- learning 不反向导入 broker/live façade；
- current projection 与 history ledger 分离。

不再采用固定 LOC、字段数或模块数作为硬目标。完成标准是：

- 同一事实只有一个生产计算者/writer；
- 同一动作只有一个最终 authority；
- 旧路径和兼容层有真实调用方或已删除；
- 生产代码净复杂度下降；
- 测试验证公开 contract，而不是内部文件结构。

## 9. P6：Demo 恢复和自治毕业

静态发布顺序保持：

```text
safety_enforce
  -> generation_enable
  -> execution_outcome_enable
  -> governance_enforce
  -> pg_job_queue_enable
  -> pg_job_queue_verify
```

每次只允许 operator 推进一个开关，并在重启和运行事实恢复后评估下一项。

Demo 恢复仍采用已确认 profile：

- 单笔风险预算 0.50%；
- 单日损失 2.0%；
- 最大回撤 8%；
- 每日开仓上限 10；
- broker 最小量超过风险预算时拒绝，不向上取整。

自治毕业必须同时满足：

- 至少 100 笔修复后、独立、干净 closed trades；
- 至少 30 个日历日真实 Demo；
- 至少 2 个有效 regimes；
- 成本后 Profit Factor ≥ 1.10；
- 最大回撤 ≤ 8%；
- 最近连续 14 天无 P0 事件；
- 零 unresolved execution、cross-trade posterior 和重复 committed mutation；
- executable sample lineage 覆盖率 100%；
- 三条专员 lane 均有完整闭环和回滚证据。

满足条件只允许进入 operator 评估，不自动切 flag 或进入 `live_autonomous`。

## 10. 验收与回滚

### 10.1 批次验收

每批使用 `docs/phased-repair-acceptance-matrix.md` 记录：

- 针对性命令和结果；
- migration/OpenAPI/build 状态；
- 删除扫描；
- 服务重启与运行事实；
- 尚不能由测试替代的证据。

### 10.2 一票否决

出现任一情况立即停止推进并保持 `no_new_risk`：

- broker 事实单位或 identity 不可确认；
- 污染样本重新获得治理资格；
- unknown/stale/warming_up 被编码为零或 safe；
- 同一事实出现第二个生产计算者/writer；
- readiness/API/frontend 重新独立裁决；
- 跨交易 posterior；
- 过期命令可 claim；
- command apply count 与 committed mutation 不一致；
- PG/审计故障阻断 close/reduce/tighten；
- duplicate broker mutation、双 generation 或 unknown execution；
- effect 未成熟但同 scope 再扩张。

### 10.3 回滚

| 类型 | 固定做法 |
|---|---|
| 代码 | revert commit 或上一已验证 SHA；有仓时不做无人值守 git 回滚 |
| RuntimeConfig | 使用当时 snapshot/rollback JSON 和 hash，不临场猜值 |
| Schema | forward-only additive repair，不依赖 destructive down migration |
| 数据 | correction manifest、revision、quarantine/supersede，保留原始证据 |

自动响应只负责 latch `no_new_risk`、停止阶段推进和保持风险缩减能力。

## 11. Operator 决策摘要

以下决策继续有效：

| ID | 已选约束 |
|---|---|
| D01-A | 修复和运行验收期间保持 `no_new_risk` |
| D02-A | correction manifest；可恢复者重建，无法恢复者 quarantine |
| D03-B | Demo profile 0.50% / 2% / 8% / 10 |
| D04-B | LLM 仅 research/shadow proposal/read-only replay |
| D05-A | canonical memory 和检索基准完成前不引入 pgvector |
| D06-A | 治理与发布保持 Safety / Release / Autonomy 三层；统一展示只读且不形成新 authority |
| D07-A | canonical ledger 热存、snapshot 分层保留、冷档带 SHA manifest |
| D08-B | 图谱和入口扫描通过后直接删除零生产引用模块 |
| D09-B | 100 trades / 30 days / 2 regimes / PF 1.10 / DD 8% / 14 days no P0 |
| D10-A | exact context、严格 effect 成熟、prior 有界 |
| D11-A | V16 命令 30 分钟，基础设施错误最多 3 次且不续期 |
| D12-A | 仅成熟负效果允许自动 risk-reducing rollback |
| D13-A | 渐进收敛 façade；不整体重写 |
| D14-A | 本机压缩冷档、每日离机备份、SHA manifest |
| D15-A | 五项静态发布开关永久 operator-only |
| D16-A | closed-bar distribution + current/final candidate notional |
| D17-A | 污染扩张 mutation 已逐项回滚 |
| D18-A | 完整 Demo lifecycle 后才评估 Execution Outcome v2 |
| D19-A | 非 deal-exact quote 保留审计原值并 quarantine |
| D20-A | 双重污染 counterfactual 终态失效，只重建干净记录 |

## 12. 完成定义

本方案完成需要：

- P1 真实 broker lifecycle 验收完成；
- P3/P4 正确性和闭环修复完成；
- 每阶段 canonical authority 单一，旧路径已删除；
- 数据库重复写和大快照膨胀得到实际控制；
- readiness、RiskPolicy、release preflight 和前端事实一致；
- P6 真实 Demo 观察满足毕业门；
- operator 单独授权下一运行阶段。

在此之前系统统一表述为：

```text
受治理的 Demo 半自治交易平台；
具备安全监督、审计、记忆和有限治理能力；
生产级无人自治资格尚未获得。
```
