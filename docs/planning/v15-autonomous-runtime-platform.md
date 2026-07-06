# V15 Autonomous Runtime Platform

> Status: active
> Last verified: 2026-07-06
> Scope: next major-version direction after Factor Takeover v4, Phase H, and factor governance V3.

V15 的方向不是继续堆因子，而是把系统升级成可长期自治运行的平台内核。核心目标是：系统自己运行、自己审计、自己回放、自己回滚；人工只看状态、审计、紧急覆盖和大版本发布。

## 1. Why V15

从 v14 到当前状态，系统已经补齐：

- 实时因子链路
- 持仓监督
- 归因复盘
- 学习证据契约
- 因子角色 V2
- 因子治理 V3
- RuntimeConfig overlay
- Catalog snapshot
- 后验 rollback
- Web 操作台方向

下一步瓶颈不再是“有没有治理动作”，而是：

- 自动动作能否长期证明有效
- 配置和模型状态能否重启后完全恢复
- 每次变更能否 replay 验证
- 事故能否快速定位和回滚
- 多品种扩展前是否有足够强的运行内核

## 2. Core Theme

```text
V15 = Autonomous Runtime Platform

live runtime
  + config control plane
  + digital twin / replay
  + autonomy health
  + release / rollback discipline
  + ops cockpit
```

## 3. V15 Pillars

### 3.1 Runtime Kernel Split

目标：

- 把 live loop、broker IO、factor pipeline、risk verdict、ledger 写入和 scheduler job 的边界进一步固化。
- `live_service.py` 保留 facade 和最终执行编排，重逻辑继续迁到注入式服务。

完成标准：

- 交易主链可以被 replay harness 调用。
- 关键动作输入/输出可序列化。
- 任何 broker 执行动作都有清晰风险 verdict 和 ledger anchor。

### 3.2 Config Control Plane

目标：

- `settings.yaml` 只做 base config。
- DB overlay 是自治配置事实源。
- 每次变更都有 patch、snapshot、decision、rollback JSON。

完成标准：

- 重启后 RuntimeConfig 与治理前一致。
- 可按 run_id 回看配置变更。
- 可对自治动作做单点 rollback。

### 3.3 Digital Twin / Replay

目标：

- 对 live decision、skip、open、close、supervisor、factor governance action 做离线 replay。
- 自动动作上线前尽量先过 replay，不只依赖 live 后验。

第一版范围：

- replay 最近 N 天的 factor pipeline + gate + risk verdict。
- replay supervisor trace 和参数模板切换。
- replay factor governance 权重变更对历史 decision 的影响。

完成标准：

- replay 输出和 live ledger 可以对齐。
- rollback 决策能引用 replay evidence。
- V15 之后高影响自治动作默认要求 replay evidence。

### 3.4 Autonomy Health Score

目标：

- 给自治系统本身打分，而不是只给因子或模型打分。

候选维度：

- action_success_rate
- rollback_rate
- blocked_by_risk_rate
- post_action_reward_delta
- config_restore_success
- catalog_freshness
- shadow_freshness
- evidence_integrity
- live_loop_stability

完成标准：

- `/api/ops/backend-readiness` 暴露 autonomy health。
- Web 操作台能看到最近自治动作质量。
- health 下降时可以自动收紧自治动作上限。

### 3.5 Release And Rollback Discipline

目标：

- 区分 daily autonomous mutation、operator override、major release。
- 任何大版本动作都能冻结、验证、发布、回滚。

完成标准：

- 有 release checklist。
- 有 config freeze / thaw。
- 有 production cleanup script 的标准入口。
- 有事故 playbook。

### 3.6 Ops Cockpit

目标：

- Web 成为完整操作台。
- 小程序只保留轻量状态面。

必备页面：

- Runtime
- Factors
- Governance
- Replay
- Risk
- Learning
- Incidents
- Release

## 4. Non-Goals

V15 不做：

- 直接让模型接管实盘下单。
- 关闭硬风控。
- 大规模多品种扩展。
- 重写交易系统。
- 把所有逻辑转 Rust。

多品种应在 V15 runtime/replay/control plane 稳定后再推进。

## 5. Initial Milestones

1. V15 readiness contract：定义 runtime、overlay、catalog、worker、replay、autonomy health 的就绪字段。`Phase 0 done`: `/api/ops/backend-readiness` 暴露 `v15`、`replay`、`autonomy_health`、`incident_control`、`release`、`phase0`。
2. Replay harness v1：先覆盖 factor pipeline、gate、risk verdict。`Phase 0 done`: `backend.services.replay_harness` 写 `replay_report`，校验 ledger 中已有 factor/gate/`RiskPolicyService` verdict 锚点。
3. Autonomy health v1：只读评分，不参与裁决。`Phase 0 done`: `backend.services.autonomy_health` 输出 `score`、`posture`、`blockers` 和候选维度。
4. Release checklist v1：冻结、验证、发布、回滚。`Phase 0 done`: `docs/v15-release-checklist.md` 已定义 v1 检查清单；`runtime_incident_mode` 和 `/api/ops/incident-control` 已提供 freeze/shadow_only/no_new_risk/only_close/frozen 初版入口；`release_run` 和 `/api/ops/release/*` 已提供 release run ledger v1。
5. Phase 0 completion gate：`Phase 0 done`: `/api/ops/v15/phase0` 和 readiness `v15.phase0` 输出 `implementation_complete`、`operationally_ready`、`gates`、`evidence_gaps`。
6. Web cockpit v1：展示 runtime、catalog、overlay、governance run、rollback 和 replay。`Phase 1 done`: Web 前端 `/v15` cockpit 已承接 runtime、factor catalog/snapshot、governance、replay、risk、learning、incident control 和 release 入口。

Phase 0 当前状态：`complete`。

Phase 1 当前状态：`complete`。服务器侧 replay/health/release/incident control plane 已按 P1 收口；Web cockpit 页面已在 `web_frontend/src/pages/V15CockpitPage.tsx` 和 `/v15` 路由落地。

- `P1 replay evidence done`: `ReplayHarnessService.run_bar_replay_evidence()` 和 `POST /api/ops/replay/bar-run` 已生成 decision/bar-window/factor-frame evidence，输出 `bar_replay_metrics.v1`、`factor_frame_replay_metrics.v1`、`bar_window_hash` 和 `factor_frame_hash`。
- `P1 replay operator explainability done`: `ReplayHarnessService.run_bar_window_preview()` 和 `POST /api/ops/replay/bar-preview` 支持默认最近真实交易或按 `decision_id` 精确选择历史决策；`GET /api/ops/replay/bar-decisions` 提供最近历史候选及盈亏/学习摘要。除 K 线窗口外同步输出 `trade_outcome_learning_preview.v1`，把 `trade_outcome_review`、`position_lifecycle_event` 和 `autonomous_learning_sample` 串成“这单盈利/亏损、平仓原因、是否已有成熟学习样本”的只读证据；这些入口不写 replay_report、不改学习样本、不绕过 `RiskPolicyService` 或 runtime overlay/snapshot。
- `P1 replay recompute done`: bar-run 已在上述 evidence 之上接入只读 `ExecutionGate.filter()` 和 `RiskPolicyService.evaluate("open_trade")` offline recompute v1，输出 `execution_gate_recompute_metrics.v1`、`risk_policy_recompute_metrics.v1`、coverage、agreement/disagreement 和 input gap 样例；该结果只做审计证据，不授权 live execution。
- `P1 lifecycle evidence done`: bar-run 已接入 `order_lifecycle_event`、`position_lifecycle_event` 和 `position_supervisor_trace` coverage/alignment evidence v1，输出 `order_lifecycle_replay_metrics.v1`、`position_lifecycle_replay_metrics.v1`、`supervisor_action_replay_metrics.v1`，只验证 ledger/artifact 证据完整性，不重放 broker。
- `P1 deep replay evidence done`: bar-run 已继续输出 `order_outcome_causality_metrics.v1`、`broker_fill_slippage_metrics.v1`、`supervisor_counterfactual_replay_metrics.v1` 和 `risk_policy_subaction_replay_metrics.v1`，覆盖 order->fill->position opened 因果链、broker fill/reference slippage、`supervisor_counterfactual_review` 对齐，以及 supervisor close/reduce/tighten 子动作的只读 `RiskPolicyService` recompute；这些指标只写 replay artifact/report，不喂 circuit breaker，不执行 broker，不授权 supervisor template 切换。
- `P1 autonomy health persistence done`: `AutonomyHealthService` 已写 `autonomy_health_snapshot` 审计快照，并在 readiness `autonomy_health.trend` 暴露 `autonomy_health_trend.v1`；scope recommendation 只做收紧建议，`applied=false`，不自动改 runtime 权限。
- `P1 autonomy scope approval done`: `AutonomyHealthService.record_scope_approval()` 已写 `autonomy_scope_approval_event`，并通过 `GET /api/ops/autonomy-health/scope-approvals/latest`、`POST /api/ops/autonomy-health/scope-approvals` 记录 health scope recommendation 的审批审计；approval event 仍 `applied=false`，不写 runtime overlay/snapshot，不改权重、订单或仓位。
- `P1 autonomy health enforcement binding done`: `AutonomyHealthService.enforce_scope_recommendation()` 已写 `autonomy_scope_enforcement_event`，并通过 `GET/POST /api/ops/autonomy-health/scope-enforcements` 把 health scope recommendation 显式绑定到 `runtime_incident_mode` 收紧动作；执行只允许 `normal -> no_new_risk/shadow_only/frozen` 等更严格方向，必须调用 `RuntimeIncidentControlService.set_mode()`，从而继续经过 `RiskPolicyService.evaluate("set_incident_control")` 和 runtime overlay/snapshot，不能放宽权限、不能改权重、订单或仓位。
- `P1 release approval trail done`: `ReleaseControlService` 已写 `release_approval_event` 审批事件流，并通过 `GET/POST /api/ops/release/{run_id}/approvals` 暴露 actor、decision、reason 和 evidence refs；事件只做审计，不改变 release status、runtime overlay/snapshot、权重、仓位或 broker 状态。
- `P1 incident playbook automation done`: `RuntimeIncidentControlService.build_playbook()` 已写 `incident_playbook_run`，并通过 `GET /api/ops/incident-playbook/latest`、`POST /api/ops/incident-playbook/run` 生成 scenario/severity 到 target incident mode 的自动化计划、步骤和 `RiskPolicyService.evaluate("set_incident_control")` 预检；playbook 本身不应用 incident mode，不写 runtime overlay/snapshot。
- `P1 incident playbook event binding done`: `RuntimeIncidentControlService.record_playbook_event()` 已写 `incident_playbook_event` 事件流，并通过 `GET/POST /api/ops/incident-playbook/{playbook_id}/events` 把 readiness、replay、release、operator note 等 evidence refs 绑定到 playbook；事件只做审计，不应用 incident mode，不改变 release status、runtime overlay/snapshot、权重、仓位或 broker 状态。
- `P1 Web cockpit done`: `/v15` cockpit 已读取 `/api/ops/backend-readiness`、`/api/ops/v15/phase0`、`/api/ops/replay/latest`、`/api/ops/replay/bar-decisions`、`/api/ops/replay/bar-preview`、`/api/ops/incident-control`、`/api/ops/incident-playbook/latest`、`/api/ops/autonomy-health/scope-enforcements/latest`、`/api/ops/release/latest`、`/api/v4/catalog`、risk 和 learning/governance API；Replay 页面可选择历史单并展示 K 线窗口、实际盈亏、平仓归因和学习样本状态；控制动作保留 Web 端二次确认，并继续调用后端 `RiskPolicyService`、`DecisionPolicy` 和 runtime overlay/snapshot 边界。
- P1 剩余项：无阻断项。后续可继续扩展更多 `RiskPolicyService` action matrix、真实告警自动绑定和更深 release 流程自动化，但不阻断 V15 Phase 1 closure。

## 6. V16 Foundation Contract

V15 完成后，V16 可以依赖的不是“某些功能已经存在”，而是一组稳定契约。只要这些契约没稳定，V16 大脑就会把运行层缺口误判成市场判断或模型判断。

### 6.1 Stable Runtime Facts

V16 可以依赖 V15 提供：

- 每个 live decision、skip、open、close、reduce、amend、supervisor action 都有稳定 id、timestamp、symbol、config hash 和 trace anchor。
- `decision_ledger`、`position_supervisor_trace`、`trade_outcome_review`、`factor_contribution_review`、`learning_application_log/effect` 能串成一条可回放事实链。
- broker 回执、local lifecycle、risk verdict 三者不一致时，必须显式暴露 divergence，不允许静默合并。
- context policy、event sizing、Kelly sizing、risk block 都必须写入可复算 payload。

### 6.2 Replay Evidence Contract

V15 replay 输出必须成为 V16 假设和行动计划的证据输入。

最小 replay artifact：

```text
ReplayReport {
  replay_run_id
  scope
  input_dataset_hash
  runtime_config_hash
  code_version
  decision_count
  matched_live_count
  mismatch_count
  metric_summary
  replay_error
  evidence_grade
  artifact_path
  artifact_hash
}
```

要求：

- replay 不能只输出收益曲线；必须输出和 live ledger 的对齐误差。
- 高影响自治动作至少引用一个 replay run 或明确说明为什么只能 shadow/observe。
- replay artifact 必须可由 PostgreSQL metadata 找回，并能校验 hash。

### 6.3 Autonomy Health Contract

V16 的 `autonomy_posture` 依赖 V15 health，而不是自己重新猜系统是否健康。

最小字段：

```text
AutonomyHealth {
  score
  posture: full | constrained | shadow_only | frozen
  blockers
  action_success_rate
  rollback_rate
  blocked_by_risk_rate
  post_action_reward_delta
  config_restore_success
  catalog_freshness
  replay_freshness
  shadow_freshness
  evidence_integrity
  live_loop_stability
  updated_at
}
```

要求：

- health 下降只能收紧自治范围，不能自动放大交易风险。
- `frozen` 和 `shadow_only` 必须是机器可读状态，不只是前端文字。
- readiness 可以展示 health，但 health 本身应有可审计来源。

### 6.4 Control Plane Contract

V16 产生的任何行动计划，最终只能通过 V15 control plane 落地。

稳定边界：

- `settings.yaml` 仍是 base config，不由自治动作直接回写。
- `runtime_config_overlay` 是自治配置事实源。
- `runtime_config_snapshot` 是回滚事实源。
- `RuntimeConfigMutationService` / `DecisionPolicy` 是配置和权重写入口。
- `RiskPolicyService` 是交易、治理、模板、回滚动作的裁决入口。

V16 只能提交计划和证据，不能绕过这些入口直接改运行态。

### 6.5 Learning And Shadow Contract

V15 必须继续保证：

- 学习样本带 `learning_evidence_contract.v1`。
- degraded/recovered/partial/missing 语义不被训练或治理伪装成强证据。
- shadow/advisory 模型必须经过 `model_permissions`，不能声明 live trading capability。
- shadow 输出必须写 audit，不能只存在日志里。
- model/canary/advisory 的事实能被 V16 memory 检索，但不能直接成为行动权限。

### 6.6 Web And Operator Contract

V15 Web cockpit 至少要让人看懂：

- 当前 overlay 是否恢复成功。
- 最近自治动作改了什么，证据是什么，回滚点在哪里。
- replay 最近一次覆盖了哪些链路，误差是多少。
- autonomy health 为什么是 full/constrained/shadow_only/frozen。
- shadow/advisory 模型当前只是在建议，还是已经进入某个受控治理流程。

小程序不承载这些深度治理视图。

## 7. Pre-V16 Start Gate

开始 V16 前，V15 不要求完美，但必须过下面的启动门槛：

| Gate | 必须达到的状态 | 不达标后果 |
|---|---|---|
| Runtime trace | live decision 到 review/learning 能串链 | V16 perception 会缺事实 |
| Overlay restore | backend 和 worker 重启后恢复同一 runtime config | V16 action plan 无法可靠回滚 |
| Replay harness | factor/gate/risk 至少可离线 replay 并输出误差 | V16 hypothesis 缺 simulation evidence |
| Autonomy health | 有机器可读 posture 和 blockers | V16 world model 无法判断自治状态 |
| Risk/action matrix | 高影响动作都注册到 `RiskPolicyService` | V16 planner 可能绕过风控边界 |
| Web audit | governance/replay/overlay/health 可在 Web 查到 | 人工无法审计大脑行为 |
| Incident controls | freeze、shadow_only、no_new_risk、only_close 有明确入口 | V16 出错时无法快速降级 |

这张表是 V16 Phase 0 的实际 checklist。未通过的项先补 V15，不应在 V16 Brain 里临时绕开。

## 8. Success Criteria

V15 完成时，系统应该能回答：

- 当前运行的配置从哪里来？
- 最近一次自治动作改了什么？
- 这个动作有什么证据？
- 它有没有变差？
- 如果变差，回滚到了哪里？
- 如果重启，能不能恢复同样状态？
- 如果要扩多品种，哪些风险会被放大？
- 如果 V16 大脑提交行动计划，运行层会通过哪个入口裁决、落地和回滚？
