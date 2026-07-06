# System Source Of Truth

> Status: active
> Last verified: 2026-07-06
> Scope: authoritative sources for runtime state, configuration, governance, data, and frontend contracts.

本文回答一个问题：当文档、注释、接口、数据库和历史理解冲突时，到底以哪里为准。

## 1. 总规则

1. 运行态事实优先于历史注释。
2. 数据库事实优先于临时日志片段。
3. 当前服务入口优先于旧脚本。
4. `RiskPolicyService` 和 `DecisionPolicy` 的权力边界优先于旧自动化路径。
5. 文档只描述事实，不替代运行态审计。

## 2. 配置事实源

| 事项 | 权威来源 | 说明 |
|---|---|---|
| 静态默认配置 | `settings.yaml` / `config/runtime_config.py` | 基础配置，不由自治治理直接回写 |
| 自治配置覆盖 | PostgreSQL `runtime_config_overlay` | 自治层事实源，重启后恢复 |
| 配置快照 | PostgreSQL `runtime_config_snapshot` | 审计和回滚用，不临场推断 |
| 配置写入口 | `RuntimeConfigMutationService` / `DecisionPolicy` | 自治配置变更必须走统一写入口 |
| 启动恢复 | `RuntimeConfigStartupService` | base YAML + DB overlay 后替换内存配置 |
| incident control | `runtime_incident_mode` + `RuntimeIncidentControlService` + `RiskPolicyService` | V15 freeze/shadow_only/no_new_risk/only_close/frozen 控制入口 |

判断原则：

- 生产自治动作不应直接修改 `settings.yaml`。
- 看到内存配置异常时，先查 overlay 和 snapshot。
- 可疑测试 overlay 不应被生产启动恢复。
- incident control 模式变更必须先过 `RiskPolicyService.evaluate("set_incident_control")`，再由 runtime overlay 持久化。

## 3. 因子事实源

| 事项 | 权威来源 | 说明 |
|---|---|---|
| 因子输入帧 | `FactorFrameBuilder` | live、health、evolution 统一 PIT 数据入口 |
| 因子角色 | `factor_signal_config.role` / registry fallback | `alpha/context/gate/sizing` |
| 方向评分 | `PortfolioCompositor` | 只使用 enabled、weight > 0、role=alpha 的因子 |
| 权重写入 | `DecisionPolicy` | 权重治理唯一写入口 |
| 因子生命周期 | `RegistryAdapter` / lifecycle event | lifecycle 权威来源 |
| 因子治理视图 | `Factor Catalog` | 聚合 registry、runtime config、weights、health、shadow、AWE、learning |
| 因子治理周期 | `FactorGovernanceOrchestrator` | 自治决策中枢 |

判断原则：

- `bb_width/adx/atr_ratio/keltner_width` 是 context，不是方向投票。
- context 可以影响状态、阈值、仓位，但不能直接改变多空方向。
- shadow 因子不直接交易，必须经治理晋升。
- disabled/DEAD 因子应被 engine、compositor、AWE、readiness 一致排除。

## 4. 风控与执行事实源

| 事项 | 权威来源 | 说明 |
|---|---|---|
| 动作裁决 | `RiskPolicyService.evaluate(...)` | 风控统一裁决入口 |
| 下单/改仓/平仓 | cTrader bridge + ledger | broker 执行事实与账本共同追溯 |
| 信号门槛 | `ExecutionGate` + context policy effect | gate 前应用有效阈值 |
| 仓位监督 | `PositionSupervisor` / `position-supervisor-contract.md` | 持仓期间动作建议和 trace |
| 事件缩放 | `execution/event_sizing.py` + `data/events.duckdb` | 事件窗口风控输入 |

判断原则：

- 风控不产生 alpha，但拥有最高执行裁决权。
- 因子治理、模板切换、回滚都不能绕过 RiskPolicyService。
- BB 不进入 ExecutionGate 作为硬过滤器。

## 5. 学习与自治事实源

| 事项 | 权威来源 | 说明 |
|---|---|---|
| 样本证据语义 | `learning-evidence-contract.md` | label、integrity、causal_level、allowed_uses |
| 学习样本统一表 | PostgreSQL `autonomous_learning_sample` | features、label、trace、evidence_contract、config hash |
| 样本来源事实 | `decision_ledger` / `position_supervisor_trace` / `trade_outcome_review` / `supervisor_counterfactual_review` / `factor_contribution_review` | 不能从模型输出反推原始事实 |
| 数据集就绪 | `/api/learning/dataset/readiness` | trade/decision schema、required fields、ready 样本数 |
| 数据精度健康 | `/api/learning/dataset/quality-health` | evidence contract 自洽性和 open context 覆盖率 |
| 模型权限 | `model_permission_audit` / `backend.services.model_permissions` | shadow/advisory guardrails |
| shadow 模型审计 | `*_shadow_audit` 表 | open、position、factor、meta 的 shadow inference 事实 |
| 治理动作记录 | `evolution_decision` | 每轮自治判断 |
| 治理运行记录 | `evolution_run` | 为什么运行、运行结果 |
| replay 证据 | PostgreSQL `replay_report` + `backend.services.replay_harness` | V15 replay harness 的 factor/gate/risk ledger 对齐报告；P1 已增加 bar/factor-frame、ExecutionGate/RiskPolicy recompute、order/position/supervisor lifecycle coverage evidence；bar-preview 支持选择历史 `decision_id`，并只读汇总交易盈亏、平仓归因和学习样本状态 |
| replay artifact | `data/replay_reports/*.json` + `replay_report.artifact_hash` | replay 报告文件和校验 hash |
| 自治健康 | `backend.services.autonomy_health` + `/api/ops/backend-readiness` + PostgreSQL `autonomy_health_snapshot` | 只读 score/posture/trend，不参与下单或配置裁决；scope recommendation 只可作为收紧证据 |
| 自治 scope approval | PostgreSQL `autonomy_scope_approval_event` + `backend.services.autonomy_health` | V15 health scope recommendation 的审批审计事件；`applied=false`，不写 runtime 权限 |
| 自治 scope enforcement | PostgreSQL `autonomy_scope_enforcement_event` + `RuntimeIncidentControlService` | V15 health scope recommendation 的显式收紧执行事件；只允许更严格 incident mode，必须走 `RiskPolicyService` + runtime overlay/snapshot |
| release checklist | `docs/v15-release-checklist.md` | V15 发布、验证、回滚和文档标记清单 |
| release run ledger | PostgreSQL `release_run` + `backend.services.release_control` | V15 发布运行审计账本，记录 snapshot、replay、incident、readiness、tests、rollback ref |
| release approval trail | PostgreSQL `release_approval_event` + `backend.services.release_control` | V15 发布审批事件流，记录 actor、decision、reason、evidence refs 和审计边界；不授权执行动作 |
| incident playbook run | PostgreSQL `incident_playbook_run` + `backend.services.incident_controls` | V15 事故 playbook 计划账本，记录 scenario/severity、target mode、步骤、RiskPolicy 预检和边界；不直接切换 incident mode |
| incident playbook event trail | PostgreSQL `incident_playbook_event` + `backend.services.incident_controls` | V15 事故 playbook 事件流，把 readiness、replay、release、operator note 等 evidence refs 绑定到 playbook；只做审计，不执行动作 |
| V15 Phase 0 completion | `backend.services.v15_phase0` + `/api/ops/v15/phase0` | Phase 0 机器可读完成门，区分 implementation complete 与 operational evidence |
| V15 Web cockpit | `web_frontend/src/pages/V15CockpitPage.tsx` + `/v15` | V15 操作台展示入口；读取 readiness/replay/autonomy/incident/release/catalog/risk/learning API，控制动作仍由后端边界执行 |
| 后验效果 | `learning_application_effect` | 回滚判断事实源 |
| 应用日志 | `learning_application_log` | 动作应用状态 |
| 建议/审计状态 | `policy_suggestion` + normalized status | `proposed/auto_approved/applied/rolled_back/blocked_by_risk/superseded` |
| 智能单元总账 | `docs/rule-driven-intelligence-inventory.md` | 规则智能、影子模型、审计数据和精度口径 |

判断原则：

- 模型输出默认 advisory/shadow，不能直接接管实盘。
- `model_ready=true` 还必须配合 `allowed_uses` 包含 `supervised_training`，才可进入强监督训练。
- `train_weight` 由 `quality_score`、`integrity`、`causal_level`、`label_status` 共同决定。
- 历史缺失字段只能标 degraded/partial/missing，不能补造实时上下文。
- 强治理必须有证据等级、样本数量、风控通过和回滚点。
- 回滚只能使用当时 decision 的 rollback JSON，不临场猜测。
- replay v1 校验 ledger 中已有 factor、gate、`RiskPolicyService` verdict 锚点；不能替代 live 风控裁决。
- bar/factor-frame/recompute/lifecycle/deep replay evidence v1 对齐 decision 周围历史 bar window，通过 `FactorFrameBuilder.enrich_bars()` 生成 factor-frame hash/coverage，通过 `ExecutionGate.filter()` 与 `RiskPolicyService.evaluate("open_trade")` 生成 offline recompute coverage/agreement/input-gap 指标，并读取 `order_lifecycle_event`、`position_lifecycle_event`、`position_supervisor_trace`、`ctrader_deals`、`supervisor_counterfactual_review` 验证 broker/supervisor 证据覆盖、order outcome causality、fill slippage、counterfactual 和 supervisor risk subaction replay；bar-preview 可按 `decision_id` 选择历史单，还读取 `trade_outcome_review` 和 `autonomous_learning_sample` 输出 `trade_outcome_learning_preview.v1`，解释这单盈亏和学习状态；这些结果只做审计证据，不授权 live 执行，不喂 circuit breaker，不写学习样本。
- autonomy health v1 只能收紧后续自治范围的解释口径，不能自动放大交易风险；`autonomy_health_snapshot` 是审计趋势事实源，不是 runtime 权限写入口。
- autonomy scope approval v1 只记录 health scope recommendation 的审批审计，`applied=false`；真正执行收紧仍必须通过 `RiskPolicyService`、`DecisionPolicy` 和 runtime overlay/snapshot 对应入口。
- autonomy scope enforcement v1 是显式执行入口，只能把 health recommendation 转成更严格的 `runtime_incident_mode`；执行必须调用 incident-control 服务并经过 `RiskPolicyService.evaluate("set_incident_control")` 与 runtime overlay/snapshot，不能放宽权限，不能改权重、订单或仓位。
- release run ledger v1 只记录发布证据和 checklist，不直接修改 runtime config、权重、仓位或 broker 状态。
- release approval trail v1 只记录审批事件和 evidence refs，不改变 release status；风险/配置/权重动作仍必须回到 `RiskPolicyService`、`DecisionPolicy` 和 runtime overlay/snapshot 写入口。
- incident playbook plan v1 只生成应急步骤和 `RiskPolicyService.evaluate("set_incident_control")` 预检；真正切换 incident mode 仍必须走 `/api/ops/incident-control`，并由 runtime overlay/snapshot 持久化。
- incident playbook event trail v1 只把 evidence refs 绑定到 playbook，不改变 incident mode、release status、runtime overlay/snapshot、权重、仓位或 broker 状态。
- V15 Phase 0 completion gate 只读，不替代 `RiskPolicyService`、`DecisionPolicy` 或 release run ledger。

## 6. 数据事实源

| 数据 | 权威来源 | 说明 |
|---|---|---|
| K 线 | `data/bars_monthly/bars_YYYY_MM.duckdb` | `data/bars.duckdb` 是当前月兼容链接 |
| tick | `data/ticks_monthly/ticks_YYYY_MM.duckdb` | `data/ticks.duckdb` 是当前月兼容链接 |
| L2 | `data/l2_monthly/l2_YYYY_MM.duckdb` | 由 backend 内 cTrader 主连接采集 |
| 外部研究数据 | `data/external_data.duckdb` | COT/ETF/FRED/宏观，必须按 `release_at` 做 PIT |
| 经济事件 | `data/events.duckdb` | 风控事件缩放读取 |
| 运行态状态 | PostgreSQL `state_v1` | 不再使用 `data/state.db` |
| 状态库运维边界 | `docs/state-postgres-store.md` | PostgreSQL state store、迁移留痕和旧 SQLite 禁用边界 |

判断原则：

- live 实时执行状态以 cTrader 为准。
- 外部研究数据不能替代 broker 实时行情和执行状态。
- 不新增生产路径写入 SQLite state。

## 7. API 与前端事实源

| 事项 | 权威来源 | 说明 |
|---|---|---|
| 后端健康 | `/api/health` | 最小服务健康 |
| 运维就绪 | `/api/ops/backend-readiness` | readiness、overlay、governance freshness、V15 replay/autonomy health |
| autonomy scope approval | `/api/ops/autonomy-health/scope-approvals/latest` / `/api/ops/autonomy-health/scope-approvals` | 查看或记录 health scope recommendation 审批审计；不应用 runtime 权限 |
| autonomy scope enforcement | `/api/ops/autonomy-health/scope-enforcements/latest` / `/api/ops/autonomy-health/scope-enforcements` | 查看或显式执行 health scope recommendation 收紧；必须走 incident-control、`RiskPolicyService` 和 overlay/snapshot |
| replay 报告 | `/api/ops/replay/latest` / `/api/ops/replay/run` / `/api/ops/replay/bar-run` / `/api/ops/replay/bar-decisions` / `/api/ops/replay/bar-preview` | 查看或手动触发 V15 factor/gate/risk replay；P1 bar-run 生成 decision/bar window 与 factor-frame evidence；bar-decisions 供 Web 选择历史单；bar-preview 供 Web 快速展示 K线、盈亏归因和学习状态 |
| incident control | `/api/ops/incident-control` | 查看/设置 V15 runtime incident mode；写入前必须通过 `RiskPolicyService` |
| incident playbook | `/api/ops/incident-playbook/latest` / `/api/ops/incident-playbook/run` / `/api/ops/incident-playbook/{playbook_id}/events` | 查看或生成 V15 incident playbook plan，查看或记录 playbook evidence event trail；只写计划/事件账本，不应用 incident mode |
| release run | `/api/ops/release/latest` / `/api/ops/release/start` / `/api/ops/release/{run_id}/finish` | 查看、开始、收尾 V15 release run ledger |
| release approval trail | `/api/ops/release/{run_id}/approvals` | 查看或记录 V15 release approval audit event；只做审批审计，不执行发布或配置动作 |
| V15 Phase 0 completion | `/api/ops/v15/phase0` | 查看 Phase 0 implementation/operational evidence gate |
| V15 Web cockpit | `/v15` + `web_frontend/src/pages/V15CockpitPage.tsx` | 前端汇总 Runtime、Factors、Governance、Replay、Risk、Learning、Incidents、Release；不在前端重算策略/风控 |
| 因子治理展示 | `/api/v4/catalog` | 实时 Catalog；支持 latest snapshot |
| 因子卡片 | `factor-card-schema.md` + Factor Cards API | 前端解释展示 |
| 前端职责 | `development-workflow.md` | 本地 Windows 负责小程序/Web 展示 |

判断原则：

- 前端展示不应重新推断因子角色。
- context 因子不应显示成多空投票。
- 旧字段保留兼容，但新语义以 V2/V3 字段为准。

## 8. 冲突处理顺序

当信息冲突时，按下面顺序判断：

1. 当前运行中的服务和数据库审计事实。
2. 当前代码入口和测试契约。
3. `docs/system-source-of-truth.md`、`docs/architecture.md`、对应 contract。
4. `docs/legacy-debt-register.md` 中的迁移说明。
5. 历史 planning 文档和旧注释。

历史 planning 文档和旧注释只能提供背景，不能单独作为实现依据。
