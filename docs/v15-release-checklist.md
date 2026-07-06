# V15 Release Checklist

> Status: active
> Last verified: 2026-07-06
> Scope: V15 release, validation, freeze, rollback, and documentation checklist for backend/runtime/autonomy changes.

本文是 V15 的 release discipline v1。它是当前发布检查清单、release run ledger、release approval trail 和 incident-control 初版入口说明。任何会影响 live runtime、风控、自治治理、runtime overlay/snapshot、replay 或 API contract 的变更，都先按这里检查。

## 1. Release Classes

| Class | Scope | Required checks |
|---|---|---|
| daily autonomous mutation | 因子权重、模板、轻量参数、治理建议应用 | `RiskPolicyService` verdict、`DecisionPolicy`/overlay 写入口、snapshot、rollback JSON、后验观察 |
| operator override | 人工 API patch、手工 replay、紧急恢复 | 明确操作者、reason、mutation audit、readiness 验证 |
| major release | V15/V16 运行内核、API contract、数据 schema、risk/action matrix | 本清单全量、相关测试、文档事实源更新、回滚计划 |

## 2. Pre-Freeze

发布前先确认：

- `/api/health` 可用。
- `/api/ops/backend-readiness` 返回 `v15.schema_version=v15_readiness_contract.v1`。
- readiness 中 `runtime_config_overlay` 不可疑，`runtime_config_snapshot.ok=true`。
- 最新 `factor_governance_runtime` 不是 failed。
- shadow/advisory 模型仍然是 advisory/shadow-only，没有 live trading capability。
- 如涉及高影响自治动作，必须有 replay evidence，或明确降级为 shadow/observe。

## 3. Incident Control Rule

当前 v1 提供 `runtime_incident_mode` 机器可读入口：

```text
normal | shadow_only | no_new_risk | only_close | frozen
```

入口：

```text
GET  /api/ops/incident-control
POST /api/ops/incident-control
```

设置模式必须先通过 `RiskPolicyService.evaluate("set_incident_control")`，通过后由 `RuntimeConfigMutationService` 写入 `runtime_config_overlay` 和 `runtime_config_snapshot`。从更严格模式 thaw 到更宽松模式必须传 `confirm_thaw=true`。

发布窗口中如果需要冻结自治写入，优先使用 incident-control 入口，同时遵守：

- 不直接改 `settings.yaml`。
- 不手写 `runtime_config_overlay`。
- 不绕过 `RuntimeConfigMutationService`、`DecisionPolicy` 或 `RiskPolicyService`。
- 高影响动作只允许在 `RiskPolicyService.evaluate(...)` 支持并通过后执行。
- 如 readiness/autonomy health 进入 `frozen` 或 `shadow_only`，新增自治动作只能进入 shadow/observe，不能扩大风险。

后续自动化目标：

- freeze/thaw approval event 与 incident playbook event trail 的更深自动关联。
- incident playbook 与真实告警/事件源的自动触发绑定。

## 4. Release Run Ledger

当前 v1 提供 release run 审计账本：

```text
GET  /api/ops/release/latest
POST /api/ops/release/start
POST /api/ops/release/{run_id}/finish
GET  /api/ops/release/{run_id}/approvals
POST /api/ops/release/{run_id}/approvals
```

`ReleaseControlService` 写 `release_run` 和 `release_approval_event`，记录：

- release class、status、change summary。
- 当前 `runtime_config_snapshot.config_hash`。
- 最新 `replay_report.replay_run_id` 与 artifact hash。
- 当前 incident mode。
- readiness/autonomy posture。
- tests run。
- rollback ref。
- approval actor、decision、reason、evidence refs。

release run ledger 和 approval trail 只做审计和 checklist 汇总，不直接修改 release status、runtime config、因子权重、仓位或 broker 状态。涉及风险或配置的真实动作仍必须走 `RiskPolicyService`、`DecisionPolicy`、runtime overlay/snapshot 写入口。

## 4.1 Incident Playbook Plan And Event Trail

当前 P1 提供 incident playbook plan automation v1 和 event binding v1：

```text
GET  /api/ops/incident-playbook/latest
POST /api/ops/incident-playbook/run
GET  /api/ops/incident-playbook/{playbook_id}/events
POST /api/ops/incident-playbook/{playbook_id}/events
```

`RuntimeIncidentControlService` 写 `incident_playbook_run` 和 `incident_playbook_event`，记录 scenario、severity、current/target incident mode、playbook steps、`RiskPolicyService.evaluate("set_incident_control")` 预检、release ref、event evidence refs、operator notes 和控制边界。

incident playbook 只生成和持久化应急计划/证据事件，不直接应用 incident mode，不写 runtime overlay/snapshot，不改订单或仓位。真正切换 incident mode 必须由操作者调用 `/api/ops/incident-control`，并继续通过 `RiskPolicyService`、`RuntimeConfigMutationService`、runtime overlay/snapshot。

## 5. Phase 0 Completion Gate

当前 Phase 0 已提供机器可读完成门：

```text
GET /api/ops/v15/phase0
```

该入口输出：

- `implementation_complete`: Phase 0 能力是否已经落地。
- `operationally_ready`: 当前现场证据是否也齐全新鲜。
- `gates`: readiness、control plane、snapshot、replay、autonomy health、incident control、release ledger。
- `evidence_gaps`: replay/report/release 等运行证据缺口。

Phase 0 完成不等于 Phase 1 完成。bar-by-bar replay、health 趋势/自动收紧和 Web cockpit 页面仍按 Phase 1 处理；release approval trail v1、incident playbook plan automation v1 和 incident playbook event binding v1 已在 Phase 1 落地为审计/计划/证据事件流。

## 6. Validation

最小验证：

```text
pytest tests/test_v15_runtime_platform_phase0.py tests/test_backend_readiness_contract.py
pytest tests/risk/test_policy_service.py tests/test_factor_autonomy_hardening.py
```

如涉及 live execution、position supervisor 或 replay harness，需要追加相关模块测试。

接口验证：

```text
GET  /api/ops/backend-readiness
GET  /api/ops/autonomy-health/scope-approvals/latest
POST /api/ops/autonomy-health/scope-approvals
GET  /api/ops/autonomy-health/scope-enforcements/latest
POST /api/ops/autonomy-health/scope-enforcements
GET  /api/ops/replay/latest
POST /api/ops/replay/run
POST /api/ops/replay/bar-run
GET  /api/ops/incident-control
GET  /api/ops/incident-playbook/latest
POST /api/ops/incident-playbook/run
GET  /api/ops/v15/phase0
GET  /api/ops/release/latest
POST /api/ops/release/start
GET  /api/ops/release/{run_id}/approvals
POST /api/ops/release/{run_id}/approvals
GET  /api/v4/catalog
```

## 7. Release

发布时必须记录：

- change summary。
- impacted facts: runtime、overlay、snapshot、replay、readiness、risk、frontend contract。
- tests run。
- replay report id and artifact hash if applicable。
- rollback point: `runtime_config_snapshot.config_hash` or explicit rollback JSON。
- release run id: `release_run.run_id`。
- approval event ids: `release_approval_event.event_id` if operator approval was recorded。

## 8. Rollback

回滚原则：

- 配置回滚只使用当时 decision 的 `rollback_json` 或明确的 `runtime_config_snapshot`。
- 因子权重回滚仍走 `DecisionPolicy`/runtime config mutation path。
- 交易风险相关动作仍走 `RiskPolicyService`。
- 不临场猜测 overlay 内容，不手写生产 state 表。

## 9. Documentation Marking

完成发布后按影响面更新：

- `TODO.md`: 当前主线、活跃 gap、最新验证。
- `docs/system-source-of-truth.md`: 新事实源或权力边界。
- `docs/system-operation-map.md`: 运行链路、API、状态表。
- `docs/rule-driven-intelligence-inventory.md`: 新智能单元、审计字段、精度语义。
- 对应 planning 文档只能标注已落地事实，不能把 future work 写成当前能力。
