# System Source Of Truth

> Status: active
> Last verified: 2026-07-07
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
| 自治配置覆盖 | PostgreSQL `runtime_config_overlay` + `config.runtime_config.refresh_from_overlay()` | 自治层事实源；写入必须走受控 mutation，启动恢复，长期运行进程通过 `runtime_config.shared()` 低频吸收最新 overlay，避免跨进程配置漂移 |
| 配置快照 | PostgreSQL `runtime_config_snapshot` | 审计和回滚用，不临场推断 |
| 配置写入口 | `RuntimeConfigMutationService` / `DecisionPolicy` | 自治配置变更必须走统一写入口 |
| 启动恢复 | `RuntimeConfigStartupService` | base YAML + DB overlay 后替换内存配置 |
| incident control | `runtime_incident_mode` + `RuntimeIncidentControlService` + `RiskPolicyService` | V15 freeze/shadow_only/no_new_risk/only_close/frozen 控制入口 |
| live autonomy unlock | PostgreSQL `live_autonomy_unlock_event` + `LiveAutonomyService` + runtime overlay | `live_autonomous` 一次性人工解锁/撤销账本；写入 `autonomy_mode`、`live_autonomy_unlocked`、`live_autonomy_unlock_id` 必须经 `RuntimeConfigMutationService` |

判断原则：

- 生产自治动作不应直接修改 `settings.yaml`。
- 看到内存配置异常时，先查 overlay、snapshot 和进程内 `runtime_config.shared()` 是否已刷新到最新 overlay hash。
- 可疑测试 overlay 不应被生产启动恢复。
- incident control 模式变更必须先过 `RiskPolicyService.evaluate("set_incident_control")`，再由 runtime overlay 持久化。
- position supervisor 模板切换必须先过 `RiskPolicyService.evaluate("switch_position_supervisor_template")`，再由 `RuntimeConfigMutationService` 写入 `runtime_config_overlay` 和 `runtime_config_snapshot`；只写 snapshot 或只改内存不算生效。
- `autonomy_mode=live_autonomous` 不是风控旁路；解锁必须写 `live_autonomy_unlock_event`，并由 runtime overlay/snapshot 恢复。未解锁或预算触顶时，`RiskPolicyService` 阻断 open/update/promote 等新增风险动作，只允许 close/reduce/tighten/rollback 等降风险动作继续走受控入口；live 开仓路径若收到 `live_autonomy_budget_breach`，必须通过 `RuntimeIncidentControlService` 请求收紧到 `no_new_risk`，不能直接改 overlay。

## 3. 因子事实源

| 事项 | 权威来源 | 说明 |
|---|---|---|
| 因子输入帧 | `FactorFrameBuilder` | live、health、evolution 统一 PIT 数据入口 |
| 因子角色 | `factor_signal_config.role` / registry fallback | `alpha/context/gate/sizing` |
| 方向评分 | `PortfolioCompositor` | 只使用 enabled、weight > 0、role=alpha 的因子 |
| live 信号决策编排 | `backend.services.live_decision_pipeline.LiveDecisionFrame` | factor refresh/append、normalizer、compositor、context policy、ExecutionGate 的单 tick 决策输出；不读账户、不触发 RiskPolicy、不下单 |
| 权重写入 | `DecisionPolicy` | 权重治理唯一写入口 |
| 因子生命周期 | `RegistryAdapter` / lifecycle event | lifecycle 权威来源 |
| DSL 因子表达式校验 | `alpha.factor_dsl.parse_dsl` + `backend.runtime.evolution_orchestrator` + `alpha.factor_health` | 进化注册前必须解析校验；历史坏表达式在健康评估中标记 `invalid_dsl`/`DEAD` 并跳过执行，不允许反复进入评估报错 |
| 因子治理视图 | `Factor Catalog` | 聚合 registry、runtime config、weights、health、shadow、AWE、learning |
| 因子组合体检 | `backend.services.factor_blend_health` + `/api/ops/backend-readiness.factor_blend_health` | 只读诊断 active alpha 数量、DSL/PCA 噪声族、低权重尾部、冗余组/标签集中度和弱健康活跃因子；默认以 Factor Catalog `used_in_score=true` 作为当前生产 active 口径，避免 runtime config 冷尾部污染健康状态；不写权重、不禁用因子、不授权交易 |
| 因子裁剪候选 | `backend.services.factor_pruning_candidates` + `decision_factor_snapshot` / `trade_outcome_review` + `/api/ops/backend-readiness.factor_pruning_candidates` | 只读生成 `review_downweight` / `review_disable` 候选；要求近期真实决策参与和非零贡献，优先处理真实亏损贡献压力，也可纳入 runtime config 缺失但 live 快照中高贡献的 discovered DSL/PCA 因子；不写 `policy_suggestion`、不写 `brain_governance_candidate`、不改 runtime 权重 |
| 因子裁剪反证 | `backend.services.factor_counter_evidence` + `shadow_factor_perf` / `factor_contribution_review` / `experience_memory` | 只读计算 `keep_score`、`prune_score` 和 regime exception；作为 pruning 晋级刹车，不直接写权重、不提交提案 |
| 因子裁剪治理候选 | `backend.services.factor_pruning_governance` + PostgreSQL `brain_governance_candidate` / `brain_governance_candidate_review` + `/api/ops/factor/pruning-governance/materialize` / `promote-ready` / `bridge-ready` | 将裁剪候选稳定 upsert 到隔离治理候选池，并可在证据足够时晋级 `governance_ready`；候选 lineage 必须携带 `agent_generation_context`；`demo_nursery` 可限速桥接为 `policy_suggestion`，但自动桥接前必须通过 Candidate Review/scorecard/briefing 反证门，不直接改 runtime 权重，不禁用因子 |
| 因子治理效果回流 | `backend.services.factor_governance_effect_tracker` + `learning_application_log/effect` + `/api/ops/factor/governance-effects` | 汇总 pruning suggestion 的应用与后续效果；只读状态不改权重，reconcile 复用 `RuleEvolutionGovernor.reconcile_application_effects` |
| 因子治理周期 | `FactorGovernanceOrchestrator` | 自治决策中枢 |

判断原则：

- `bb_width/adx/atr_ratio/keltner_width` 是 context，不是方向投票。
- context 可以影响状态、阈值、仓位，但不能直接改变多空方向。
- live 信号决策层只产出 `CompositeSignal`、context policy effect、`GateResult` 和审计 payload；交易授权仍必须进入 `RiskPolicyService`，权重治理仍必须进入 `DecisionPolicy`。
- shadow 因子不直接交易，必须经治理晋升。
- 搜索/进化生成的 DSL 因子必须先通过 `parse_dsl()`；解析失败的表达式不能注册为 shadow 因子，历史残留坏 DSL 只能作为健康/治理噪音进入 `invalid_dsl` 审计，不能继续执行计算。
- disabled/DEAD 因子应被 engine、compositor、AWE、readiness 一致排除。
- 因子组合体检只能产生 `issues` 和 `recommendations`；默认 `build()` / readiness 使用 Factor Catalog `used_in_score=true` 统计当前实际参与评分的 alpha，显式传入 runtime config 的测试/分析可保留配置全量口径；后续 pruning、降权、禁用或晋升仍必须走 `DecisionPolicy`、`RiskPolicyService`、runtime overlay/snapshot 和治理审计。
- 因子裁剪候选只能作为多智能体复盘、反证和治理提案草稿输入；任何实际权重变更仍必须先形成受控提案，再经过 `DecisionPolicy` 和 `RiskPolicyService`。没有近期 `decision_factor_snapshot` 参与记录、或近期平均贡献接近 0 的因子不能占用 demo governance 桥接名额；`dsl_auto` / `pca` 若不在 runtime config 但已在真实决策快照中以非零权重/贡献参与，可作为 snapshot-only discovered 候选进入治理。
- 因子裁剪反证必须在 pruning candidate 晋级前运行；强保留证据或 regime exception 会阻止 `governance_ready`，但不会直接恢复权重或提交反向提案。
- 因子裁剪治理候选可以写入 `brain_governance_candidate`，并可在风险预览、DecisionPolicy 预览、弱健康证据、反证检查和冲突检查通过后晋级 `governance_ready`；`demo_nursery` 下允许通过 `factor_pruning_governance.bridge_ready_candidates` 限速桥接到 `policy_suggestion`，但每个候选必须先由 `BrainGovernanceCandidateReviewService.review_candidate` 生成审查记录且 `bridge_ready=true`，再由 `RuleEvolutionGovernor`、`DecisionPolicy`、`RiskPolicyService`、runtime snapshot 和回滚证据约束实际应用。
- 因子治理效果回流只解释和复核已经应用的治理建议；效果变差时由 `RuleEvolutionGovernor.reconcile_application_effects` 标记回滚/无效，下一次权重同步自然移除学习偏置，不能直接绕过 `DecisionPolicy` 写权重。

## 4. 风控与执行事实源

| 事项 | 权威来源 | 说明 |
|---|---|---|
| 动作裁决 | `RiskPolicyService.evaluate(...)` | 风控统一裁决入口 |
| 风险阈值快照 | `risk.runtime_policy.RiskLimitSnapshot` + `RuntimeConfig` / runtime overlay | 日内亏损、回撤、交易次数、数据延迟、L2、磁盘、VaR/CVaR 等阈值的统一输入口径 |
| 运行健康快照 | `risk.runtime_policy.RuntimeHealthSnapshot` + `monitor.system_health` / live runtime context | loop、bridge、data lag、disk、L2 等运行态统一输入口径 |
| 决策 K 线/信号新鲜度 | `backend.services.live_data_sync_helpers` + `_live_state.decision_bar_freshness` + `_live_state.last_processed_decision_bar_ts` + `RiskPolicyService.evaluate("open_trade")` | live tick 只用最新已闭合 bar；缺应有闭合 bar 时先经主 cTrader bridge 回补月库并重载，修复失败以 `decision_bar_stale` 阻断开仓；同一根已闭合决策 bar 只推进一次 signal/open，重复 tick 只运行持仓观察/保护；即使 bar fresh，信号 age 超过 `max(180s, 1.5 * timeframe)` 时以 `decision_signal_age_stale` 阻断开仓 |
| 下单/改仓/平仓 | cTrader bridge + ledger | broker 执行事实与账本共同追溯；live 开仓先经 `_run_open_trade_pipeline()` 生成 candidate/risk verdict/order block，再触达 broker |
| 信号门槛 | `ExecutionGate` + context policy effect | gate 前应用有效阈值；live 只负责信号/冷却，不作为事件风险最终裁决口 |
| 仓位监督 | `PositionSupervisor` / `position-supervisor-contract.md` | 持仓期间动作建议和 trace |
| 动态仓位 | `backend.services.live_risk_sizing` + live sizing trace | Kelly、event sizing、context policy 统一生成 `position_sizing_trace.v1`；`kelly_risk_per_trade_pct` 是有效 Kelly 风险分数上限，不再与 Kelly 分数相乘；`dynamic_sizing_max_api_volume` 是 demo 硬上限，实际 raw volume 由当前账户 equity、SL distance 和 Kelly 风险预算推导；启用动态 Kelly 时，非正 Kelly、缺 equity 或低于 broker 最小量都产生 0 volume/blocked trace，不再提升为 broker 最小量 |
| 事件缩放 | `execution/event_sizing.py` + `data/events.duckdb` | 事件窗口风控输入 |

判断原则：

- 风控不产生 alpha，但拥有最高执行裁决权。
- 因子治理、模板切换、回滚都不能绕过 RiskPolicyService。
- live 日内熔断可以做执行快停，但阈值必须来自 `RiskLimitSnapshot`，不能在 live loop 内另设事实源。
- `autonomy_mode=demo_autonomous` / `demo_nursery` 下的真实 demo 采样可以使用 `RuntimeConfig.demo_learning_max_daily_trades` 作为有效日交易上限；该口径只通过 `RiskLimitSnapshot.source=runtime_config:demo_learning` 输入 `RiskPolicyService`，不绕过断连、stale market、仓位、volume、日亏损或熔断裁决。
- `autonomy_mode=demo_nursery` 是 demo 学习育苗模式，不是 live 风控旁路；`RiskPolicyService` 只会把 `loss_cooldown_active`、`consecutive_losses`、VaR/CVaR、同向学习冷却、entry quality 和 event-window learning control 记录为 `demo_nursery_observations`，断连、熔断、日亏损、最大回撤、decision stale、仓位/API 上限、重大事件硬窗口和运行健康底线仍硬拦。
- NFP/GVZ/重大事件等事件风险在 live 中只能作为 `RiskPolicyService` 输入；`ExecutionGate` 的事件过滤保留给 backtest/legacy 兼容。
- live 因子决策不得使用当前未闭合 K 线；`spot_quote` 只能修正执行参考价，不能把旧 bar 信号变成新信号。同一根已闭合 bar 不得重复 append 到 `StreamingFactorEngine`，也不得重复生成 open 决策；重复 loop tick 只能执行持仓观察、close/reduce/tighten 和保护修复。若 `decision_freshness.schema_version=decision_bar_freshness.v1` 且 `fresh=false`，开仓必须由 `RiskPolicyService` 返回 `decision_bar_stale`；若 bar fresh 但 `age_seconds` 超过 `max(180s, 1.5 * timeframe_seconds)`，开仓必须返回 `decision_signal_age_stale`。持仓监督的 close/reduce/tighten 仍可继续。
- live open-trade pipeline 只编排候选 sizing、`RiskPolicyService.evaluate("open_trade")`、market-session/order block、broker order 和 post-fill audit；它不拥有独立风控事实源。
- demo nursery 的动态 Kelly 默认风险上限为 6%，单笔 API volume 硬顶为 `dynamic_sizing_max_api_volume=1000`，但实际档位必须由当前 equity、SL distance、Kelly 分数、broker min/step/max 和总仓位上限共同约束；同一历史样本应能自然落到 0/100/200/.../1000 档，而不是全部被抬成 100。context policy 缩仓低于 broker 最小量时仍输出 0；高于最小量时按最近 broker step 映射，避免 300 被粗暴压成 100。
- `position_supervisor:profit_protection.v1` 对年轻仓位的 thesis-broken 判断采用最小证据窗：未满模板 `min_thesis_break_seconds` 时优先输出 tighten/observe 语义的 `thesis_broken_delayed`，不直接 full close；超过证据窗只代表 `thesis_break_ready`，还必须有接近止损、regime confirmed、time decay、连续 thesis broken 确认或信号反转等强证据，才允许 `thesis_broken` full close；close/reduce/tighten 仍必须经过 `RiskPolicyService`。
- 最小交易量仓位的 supervisor reduce 不能因为 reduce volume 不可交易就无条件升级 full close；只有 `thesis_break_confirmed=true`、连续 thesis broken、信号反转、接近原止损，或 MFE 完全回吐且接近原止损等强风险证据，才允许走 reduce-to-close 兜底。
- 同品种存在反向持仓时，开仓必须由 `RiskPolicyService` 以 `opposite_direction_position_open` 硬拦；系统不支持隐式 hedge/flip，未来如果要反手必须走显式“先平后开”动作链。
- context policy 或 Kelly sizing 把建议仓位压到 broker 最小交易量以下时，live 开仓候选必须以 0 volume 进入风控并被 `non_positive_requested_volume` 阻断，不能悄悄恢复为最小仓位。
- Dukascopy `tick_data` 是研究/订单流支路，不是 cTrader live 执行事实源；tick 库 stale/critical 必须作为 observe-only component 暴露给 risk summary/readiness，不能单独把 system health overall 降为 degraded/critical，不能阻断 demo live 交易，也不能单独把交易复盘归因为 `market_data_stale` / `data_quality`。
- `risk/pre_trade.py`、`risk/circuit.py`、`execution/router.py` 属于 paper/backtest/legacy execution router，不是当前 live 主授权口。
- 模型阶段裁决必须复用 `backend.services.model_permissions`，不能维护第二套模型权限事实。
- BB 不进入 ExecutionGate 作为硬过滤器。

## 5. 学习与自治事实源

| 事项 | 权威来源 | 说明 |
|---|---|---|
| 样本证据语义 | `learning-evidence-contract.md` | label、integrity、causal_level、allowed_uses |
| 学习样本统一表 | PostgreSQL `autonomous_learning_sample` | features、label、trace、evidence_contract、config hash |
| 样本来源事实 | `decision_ledger` / `position_supervisor_trace` / `trade_outcome_review` / `supervisor_counterfactual_review` / `factor_contribution_review` | 不能从模型输出反推原始事实 |
| 交易复盘时间与系统污染 | `trade_outcome_review.review_json.entry_timing_context` / `decision_freshness_context` / `system_issue_context` + `order_lifecycle_event` | `entry_ts` 以实际成交时间优先；信号 K 线时间保留为 `signal_bar_ts`。数据时效、信号到成交延迟等系统污染样本只能审计/弱用，不能满权重训练因子或开仓模板 |
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
| V16 read-only brain state | PostgreSQL `brain_state_snapshot` + `backend.services.brain_state` + `/api/ops/brain/state` | V16 Phase 1 只读大脑状态快照，汇总 V15 readiness/replay/incident/autonomy/governance/memory 事实生成 world model、observe-only hypothesis 和 Critic 限制；不执行动作、不写 overlay、不改权重/仓位/学习样本 |
| V16 memory retrieval | PostgreSQL `brain_memory` + `backend.services.brain_memory` + `/api/ops/brain/memory` | V16 Phase 1 只读记忆检索/索引，从 `experience_memory`、`trade_outcome_review`、`policy_suggestion`、model permission audit 和可选 shadow audit 表生成 evidence/similarity/counter-evidence 摘要；原始事实源仍以来源表为准 |
| V16 shadow action plans | PostgreSQL `brain_action_plan` + `backend.services.brain_action_planner` + `/api/ops/brain/action-plans` | V16 Phase 2 影子 ActionPlan 账本，覆盖 factor weight、parameter template、context policy、supervisor template；只记录 `pass/caution/reject`、所需后端服务、validation refs、shadow eval contract 和 future rollback 要求，不执行、不改 overlay/snapshot/权重/模板/订单/学习样本 |
| V16 shadow action evaluations | PostgreSQL `brain_action_plan_eval` + `backend.services.brain_action_evaluator` + `/api/ops/brain/action-plan-evals` | V16 Phase 2 后验可比性审计，把 shadow action plan 与 `replay_report`、`trade_outcome_review`、`learning_application_effect`、`position_supervisor_trace` 比较，输出 coverage、comparison verdict 和 evidence refs；只记录评价，不授权执行 |
| V16 low-impact executions | PostgreSQL `brain_low_impact_execution` + `backend.services.brain_low_impact_executor` + `/api/ops/brain/low-impact-executions` / `/api/ops/brain/low-impact-executions/run` | V16 Phase 3 低影响执行账本和显式执行入口；当前白名单只允许 read-only replay job，执行前必须记录 P2 eval、evidence score、Critic verdict、`RiskPolicyService.evaluate("run_replay_job")`、rollback/downgrade plan，坏化收紧只能显式允许并通过 incident-control/RiskPolicy/overlay |
| V16 medium-impact governance | PostgreSQL `brain_medium_impact_governance` + `brain_governance_candidate` + `backend.services.brain_medium_impact_governance` + `/api/ops/brain/medium-impact-governance` / `/api/ops/brain/medium-impact-governance/materialize` | V16 Phase 4 中等影响治理候选账本；基于 P2/P3 证据、`RiskPolicyService` verdict 和权重动作 `DecisionPolicy` preview 生成隔离 `brain_governance_candidate`，不直接写 `policy_suggestion`、不直接应用权重/模板/订单/学习样本 |
| V16 governance candidate bridge | PostgreSQL `brain_governance_candidate` + `brain_governance_candidate_review` + `policy_suggestion` + `backend.services.brain_governance_candidates` + `/api/ops/brain/governance-candidates` / `/api/ops/brain/governance-candidates/{candidate_id}/submit` + readiness `candidate_generation_context_coverage` | V16 候选到旧治理队列的手动桥接入口；候选 lineage 必须携带 `agent_generation_context`；API 和底层 submit 都要求已有 `bridge_ready=true` 的 Candidate Review，且候选为 `governance_ready/applyable`、RiskPolicy allowed、payload 被旧 `RuleEvolutionGovernor` 理解，才提交 `policy_suggestion(status='proposed')`；提交 evidence 会记录 `candidate_review_required_before_submit=true` 和 review 引用，之后仍由既有 governor/conflict/risk/release/rollback 链路处理；readiness 持续审计 required context 覆盖率 |
| V16 governance candidate review | PostgreSQL `brain_governance_candidate_review` + `brain_governance_candidate` + `backend.services.brain_governance_candidate_review` + `/api/ops/brain/governance-candidate-reviews` / `/api/ops/brain/governance-candidates/review` + readiness `candidate_bridge_review_coverage` | V16 候选审查事实源；输出 evidence gaps、bridge preview、control-surface conflicts、source reliability 和可选 LLM advisory audit；只写审计，不提交 `policy_suggestion`，不执行 runtime mutation；readiness 覆盖率审计会把缺少 `candidate_review_required_before_submit` review 合同的新桥接标为 degraded，历史旧桥接只标 `legacy_unreviewed` |
| V16 live-ready guardrails | PostgreSQL `brain_live_ready_guardrail` + `backend.services.brain_live_ready_guardrail` + `/api/ops/brain/live-ready-guardrails` / `/api/ops/brain/live-ready-guardrails/evaluate` / `/api/ops/brain/live-ready-guardrails/tighten` | V16 Phase 5 实盘前护栏审计账本；评估 live capability lock、broker/local divergence、incident memory、release rollback 和 P3/P4 evidence；显式 tightening 只能通过 `RuntimeIncidentControlService` + `RiskPolicyService` 收紧 incident mode，不能放宽权限、下单、提交或应用治理候选、写学习样本 |
| Agent Authority Registry | `backend.services.agent_authority_registry` + `/api/ops/agent-authority` + readiness `agent_authority` / `v16.agent_authority` | 智能体权责合同事实源；登记 `v16_brain`、`autonomous_learning`、`factor_governance`、`factor_pruning_governance`、`llm_advisory`、`lightgbm_shadow_models` 的 allowed writes、control surfaces、required gates 和 forbidden actions；未知来源只能 review-only，LLM 永远 advisory-only，不授权下单、runtime mutation 或绕过风控 |
| Agent Scorecard / Briefing / Chain Health | `backend.services.agent_scorecard` + `backend.services.agent_briefing` + `/api/ops/agent-scorecard` / `/api/ops/agent-briefing` / `/api/ops/agent-trade-attribution` / `/api/ops/agent-chain-health` + readiness `agent_scorecard` / `agent_briefing` / `agent_chain_health` | 只读聚合智能体行为质量和统一战况简报；从 Proposal Registry、治理候选、`policy_suggestion`、`learning_application_log/effect`、`trade_outcome_review`、`experience_memory`、shadow/LLM audit 统计提案、应用、效果、合同违规和交易反馈，并暴露 candidate context/review coverage 与 proposal generation context coverage；可提高低可靠来源的证据要求和审查严格度，但不改变 agent 权限、不执行动作、不自动批准 |
| Proposal Registry | PostgreSQL `proposal_registry` + `backend.services.proposal_registry` + `/api/ops/autonomy/proposals*` + readiness `proposal_generation_context_coverage` | 统一 proposal 读模型，把 `policy_suggestion`、`brain_governance_candidate`、`brain_action_plan`、`learning_application_log`、`evolution_decision`、`live_autonomy_unlock_event`、shadow/advisory audit 和 LLM advisory audit 归一化；只做展示、来源可靠性评分、证据新鲜度标记、冲突检测、review 记录、生成上下文覆盖审计和路由建议；主汇总字段统计当前可行动提案，`raw_*`/`historical_noise_count` 保留历史噪音背景；不审批、不应用、不改来源状态 |
| Autonomous Blueprint Status | `/api/ops/backend-readiness.autonomous_blueprint` / `v16.autonomous_blueprint` | 最终自治交易大纲的只读对齐状态；汇总 demo nursery scope、agent authority、proposal/candidate context、candidate review、proposal registry、memory/scorecard、执行边界和 live-ready guardrails；只报告 `ok/partial` 和 blockers，不执行、不审批、不改 runtime |
| live autonomy unlock | PostgreSQL `live_autonomy_unlock_event` + `backend.services.live_autonomy` + `/api/ops/autonomy/live-status` / `/api/ops/autonomy/live-unlock*` | `live_autonomous` 一次性人工解锁事实源；评估 readiness/cTrader/live loop/incident/release rollback/replay/broker alignment/proposal conflict/RiskPolicy budget/evidence freshness，成功后经 `RuntimeConfigMutationService` 写 overlay/snapshot，撤销回到 `live_candidate`；预算触顶事件会作为 incident tighten proposal 进入 Proposal Registry，live 开仓被预算门阻断时由 `RuntimeIncidentControlService` 自动请求 `no_new_risk` |
| Meta Governance Web page | `web_frontend/src/pages/V16BrainPage.tsx` + `/v16` | 元治理大脑展示入口；读取 brain state/memory/action-plans/action-plan-evals/low-impact-executions/medium-impact-governance/governance-candidate-reviews/live-ready-guardrails/proposal-registry/live-autonomy/readiness API，展示 world model、memory、hypotheses、Critic、提案总线、实盘自治状态和边界；按钮只触发受控后端 API，不在前端重算策略/风控或执行未授权动作 |
| 后验效果 | `learning_application_effect` | 回滚判断事实源 |
| 应用日志 | `learning_application_log` | 动作应用状态 |
| 建议/审计状态 | `policy_suggestion` + normalized status | `proposed/auto_approved/applied/rolled_back/blocked_by_risk/superseded` |
| 智能单元总账 | `docs/rule-driven-intelligence-inventory.md` | 规则智能、影子模型、审计数据和精度口径 |
| 自治治理架构 | `docs/autonomous-governance-architecture.md` | 多智能体、模型、自治大脑、控制面和权力边界分层；不替代具体事实表 |
| 最终自治交易大纲 | `docs/autonomous-trading-final-blueprint.md` | demo nursery、多智能体权责、统一提案、统一审查、单一路径执行、记忆成长和偏离检查的最终目标；后续治理推进必须能映射到该大纲 |

判断原则：

- 模型输出默认 advisory/shadow，不能直接接管实盘。
- 后续自治治理推进必须先对照 `docs/autonomous-trading-final-blueprint.md` 的 deviation guard；不能说明服务于大纲的改动不应自动推进。
- 智能体链路以 `AgentAuthorityRegistryService` 为权责合同事实源；新增 source agent 或控制面前，必须先声明 allowed writes、required gates、authority state 和 forbidden actions。
- agent 生成治理候选前必须把 `agent_generation_context` 写入 lineage；该上下文包含 authority verdict、scorecard、近期负反馈和 review rules，只用于审查/复盘，不授权执行。
- 新提案若声明 `agent_context_required=true` 或来自需要 review 的 governance bridge，必须在 evidence 或 lineage 中携带 `agent_generation_context`；直接写 `policy_suggestion` 的新路径应复用 `backend.services.policy_suggestion_context.attach_policy_suggestion_agent_context()` 生成同一上下文；readiness `proposal_generation_context_coverage` 只读审计该覆盖率，缺新上下文 degraded，历史旧提案只标 legacy。
- `policy_suggestion` 旧行缺 `source_agent` 时必须通过 `infer_policy_suggestion_source_agent()` 统一推断来源；已知 LightGBM shadow/advisory schema 归入 `lightgbm_shadow_models` 并固定 `advisory_only` gate，不能默认归到 `autonomous_learning`。
- 智能体质量以 `AgentScorecardService` 为只读观测口径；scorecard 可以影响 Proposal Registry 路由、证据要求和 candidate review 严格度，高分只能提高审查优先级，不能直接放大权限、改权重或下单。
- `AgentBriefingContextService` 是多智能体统一战况简报；包含 chain health、proposal flow、scorecard、最近交易反馈和治理覆盖率，只能作为 review/prompt/context 输入，不是执行授权。
- 交易 lesson 写入 `experience_memory` 时可附带 `agent_attribution` / `feedback_agents`，用于把盈亏经验反馈给参与过提案、shadow audit 或 LLM advisory 的 agent；该反馈是记忆和评分证据，不是自动奖惩执行入口。
- `model_ready=true` 还必须配合 `allowed_uses` 包含 `supervised_training`，才可进入强监督训练。
- `train_weight` 由 `quality_score`、`integrity`、`causal_level`、`label_status` 共同决定。
- 历史缺失字段只能标 degraded/partial/missing，不能补造实时上下文。
- 交易复盘必须区分 `signal_bar_ts`、`decision_evaluated_at`、`order_submitted_at`、`fill_ts` 和 `close_ts`；用信号 K 线时间替代实际入场时间会污染持仓学习。
- `system_issue_context.contaminates_learning=true` 的样本必须降为 `integrity=partial` 或更低权重；`factor_contribution_review` 只能保留审计，不可作为高置信因子治理训练样本。
- `supervisor_counterfactual_review` 复用已有 post-close counterfactual 链路，覆盖 `supervisor_close`、`supervisor_reduce` 和保护类平仓；优先用 M1 后续 K 线判断原 SL/TP 谁先触发、是否 `correct_stop` 或 `protection_too_tight`，结果作为 advisory/counterfactual evidence，不直接授权下一笔交易。
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
- V16 Phase 1 brain state 只读：`brain_state_snapshot` 是认知层审计事实，不是执行授权。`BrainStateService` 只能生成 world model、memory retrieval、observe-only hypotheses 和 Critic 限制；任何未来 action plan 必须重新回到 `RiskPolicyService`、`DecisionPolicy`、runtime overlay/snapshot、model permissions 和 replay/release 证据边界。
- V16 memory retrieval 不替代原始表，不生成训练标签，不授权治理动作；negative memory 只能收紧 Critic scope，positive counter-evidence 只能作为反证展示。
- V16 Phase 2 shadow action plan 仍是只读审计事实：`brain_action_plan` 只记录候选动作、Critic verdict、validation refs、所需服务和 future rollback 要求。它不能调用 live mutation、不能写 runtime overlay/snapshot、不能改权重/模板、不能下单、不能写学习样本；任何 future execution 都必须重新经过 `RiskPolicyService`、`DecisionPolicy`（权重相关）、runtime snapshot/rollback 和 replay/release 证据。
- V16 Phase 2 shadow action evaluation 也是只读审计事实：`brain_action_plan_eval` 只比较已存在的 replay/后验/学习效果/supervisor trace 证据，不改变 action plan 状态，不生成学习标签，不触发 governance 或 live mutation。
- V16 Phase 3 low-impact execution 只允许显式白名单动作。当前 `brain_low_impact_execution` 的自动动作是 read-only replay job；它必须经过 `RiskPolicyService.evaluate("run_replay_job")`，记录 rollback/downgrade plan 和后验 monitor。任何 bad-posterior 收紧必须显式开启，并继续走 incident-control、`RiskPolicyService.evaluate("set_incident_control")` 和 runtime overlay/snapshot；P3 仍不能改权重/模板、下单或写学习样本。
- V16 Phase 4 medium-impact governance 只 materialize `brain_governance_candidate` 候选和 `brain_medium_impact_governance` 审计账本。它不能直接调用 `_update_weights()`、不能激活模板、不能推广模型到 live、不能写 runtime overlay/snapshot，也不能直接写 `policy_suggestion`；权重类候选必须带 `DecisionPolicy` preview。候选若要进入旧自治建议队列，必须通过手动 bridge，并再次满足 `RuleEvolutionGovernor` 可理解 evidence、`RiskPolicyService`、`DecisionPolicy`、release evidence 和 rollback JSON。
- V16 governance candidate review 只审查候选，不提交候选。`BrainGovernanceCandidateReviewService` 复用现有 `GovernanceConflictResolver` 的 control surface 口径、复用 candidate bridge preview，并可选调用现有 `LLMAdvisoryService` 写 advisory audit；LLM 不能改变 review status、不能授权 bridge 或执行。Ops submit API 和底层 `BrainGovernanceCandidateService.submit_candidate_to_policy_suggestion()` 都要求已有单候选 review 且 `bridge_ready=true`；提交到 `policy_suggestion` 的 evidence 必须携带 review 引用和 `candidate_review_required_before_submit=true`，否则 readiness 视为新合同缺审查。
- V16 Phase 5 live-ready guardrails 只评估和审计实盘前护栏。`brain_live_ready_guardrail` 的 capability lock/divergence/rollback/incident memory 结论不是下单授权；`tighten` 入口只能把 incident mode 调得更严格，并由 `RuntimeIncidentControlService` 走 `RiskPolicyService.evaluate("set_incident_control")` 与 runtime overlay/snapshot。P5 不能 thaw、不能恢复 normal、不能应用 P4 suggestion、不能写学习样本或提交订单。
- Proposal Registry 是元治理总线读模型，不是新执行器。它可以识别同一 `control_surface + target_scope` 的 active conflict、记录 operator review 和推荐 `observe/request_replay/submit_governance/request_review` route，但不能把 proposal 标记为 approved/applied，不能写来源表状态，不能直接调用 broker、`DecisionPolicy` 或 runtime overlay。
- Proposal Registry 的 `source_reliability` 和 `evidence_freshness` 只是排序/审查辅助信号：低可信和证据过期会让前端/readiness 可见，但不会自己批准、拒绝或应用任何动作。`conflict_count`、`stale_evidence_count`、`low_reliability_count` 代表当前可行动提案；历史 shadow、reviewed、needs_evidence 等非执行队列通过 `raw_*` 和 `historical_noise_count` 复盘，不再污染主告警。
- LLM advisory 只能通过 `llm_advisory_audit` 进入 Proposal Registry，`authority_state=advisory_only`；任何 `approved/applied/auto_approved` review 都必须被拒绝。
- `live_autonomous` 恢复后仍必须持续满足 unlock evidence；`LiveAutonomyService.status()` 会在 replay/release/readiness/unlock event 过期时标记 `operational_posture=degraded` 并建议 `no_new_risk`。解锁不会授予 LLM、shadow model、brain action plan 或前端直接执行权限。

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
- live 决策 K 线以月库中的已闭合 bar 为准；`data_sync` 和 live tick 即时修复都可以通过主 cTrader bridge 回补月库，但不能把未闭合 bar 当成 `complete=true` 的决策输入。
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
| V16 brain state | `/api/ops/brain/state` + readiness `v16.brain_state` | 查看 V16 Phase 1 只读 brain state、world model、memory、hypotheses、Critic 和边界；前端只能展示后端事实，不推断或执行动作 |
| V16 brain memory | `/api/ops/brain/memory` | 查看 V16 Phase 1 只读 memory retrieval/index；用于展示相似历史、失败记忆和反证，不授权动作 |
| V16 brain action plans | `/api/ops/brain/action-plans` + readiness `v16.action_plans` | 查看或刷新 V16 Phase 2 shadow action plan ledger；只记录候选计划和 shadow eval contract，不执行动作或修改 live/governance/learning 状态 |
| V16 brain action evaluations | `/api/ops/brain/action-plan-evals` + readiness `v16.action_plan_evals` | 查看或刷新 V16 Phase 2 shadow action posterior comparison；只记录 coverage/verdict/evidence refs，不执行动作或修改 live/governance/learning 状态 |
| V16 low-impact executions | `/api/ops/brain/low-impact-executions` / `/api/ops/brain/low-impact-executions/run` + readiness `v16.low_impact_executions` | 查看或显式运行 V16 Phase 3 低影响执行；当前仅允许 read-only replay job，所有执行写账本并通过 RiskPolicyService |
| V16 medium-impact governance | `/api/ops/brain/medium-impact-governance` / `/api/ops/brain/medium-impact-governance/materialize` + readiness `v16.medium_impact_governance` | 查看或显式生成 V16 Phase 4 中等影响治理候选；只写 `brain_governance_candidate` 和审计账本，不应用 runtime mutation，不直接写 `policy_suggestion` |
| V16 governance candidates | `/api/ops/brain/governance-candidates` / `/api/ops/brain/governance-candidates/{candidate_id}/submit` + readiness `v16.governance_candidates` | 查看隔离候选池；手动 submit 仅把兼容候选桥接进旧 `policy_suggestion` review，不应用 runtime mutation |
| Candidate generation context coverage | readiness `candidate_generation_context_coverage` / `v16.candidate_generation_context_coverage` | 只读审计治理候选 lineage 是否携带 `agent_generation_context`；缺 required context 的新候选 degraded，历史旧候选标 `legacy_missing_agent_context` |
| V16 governance candidate reviews | `/api/ops/brain/governance-candidate-reviews` / `/api/ops/brain/governance-candidates/review` + readiness `v16.governance_candidate_reviews` | 查看或显式运行候选审查；只生成 bridge preview、证据缺口、冲突面、source reliability 和可选 LLM advisory audit，不提交候选 |
| Candidate bridge review coverage | readiness `candidate_bridge_review_coverage` / `v16.candidate_bridge_review_coverage` | 只读审计已桥接 `policy_suggestion` 是否存在 `bridge_ready=true` 的 candidate review；缺 required review 的新桥接 degraded，历史旧桥接标 `legacy_unreviewed` |
| V16 live-ready guardrails | `/api/ops/brain/live-ready-guardrails` / `/api/ops/brain/live-ready-guardrails/evaluate` / `/api/ops/brain/live-ready-guardrails/tighten` + readiness `v16.live_ready_guardrails` | 查看或显式评估 V16 Phase 5 实盘前护栏；收紧入口只能调用 incident-control 进入更严格模式，不能放宽权限 |
| Proposal Registry | `/api/ops/autonomy/proposals` / `/api/ops/autonomy/proposals/{proposal_id}` / `/api/ops/autonomy/proposals/refresh` / `/api/ops/autonomy/proposals/{proposal_id}/review` + readiness `v16.proposal_registry` | 查看、刷新和记录统一提案审查；包含 source reliability、evidence freshness、conflict 和 route；review 只能写 registry review，不能授权或应用来源 proposal |
| Proposal generation context coverage | readiness `proposal_generation_context_coverage` / `v16.proposal_generation_context_coverage` | 只读审计 `policy_suggestion` evidence/lineage 是否携带 `agent_generation_context`；缺 required context 的新提案 degraded，历史旧提案标 `legacy_missing_agent_context` |
| Autonomous Blueprint Status | readiness `autonomous_blueprint` / `v16.autonomous_blueprint` | 查看最终大纲对齐状态；只汇总现有 readiness 组件和 deviation guard，不新增执行入口 |
| live autonomy unlock | `/api/ops/autonomy/live-status` / `/api/ops/autonomy/live-unlock/evaluate` / `/api/ops/autonomy/live-unlock` / `/api/ops/autonomy/live-unlock/revoke` + readiness `v16.live_autonomy` | 查看、评估、一次性人工解锁或撤销 `live_autonomous`；评估包含 evidence freshness、operational posture 和 budget breach response；成功 mutation 必须走 `RuntimeConfigMutationService` 和 overlay/snapshot，失败只写审计 |
| Meta Governance Web page | `/v16` + `web_frontend/src/pages/V16BrainPage.tsx` | Web 展示元治理大脑、提案总线、实盘自治状态、shadow action plans、posterior evaluations、P3 executions、P4 governance candidate/review 和 P5 guardrails；运行按钮只触发后端白名单/候选生成/候选审查/护栏评估/收紧/提案审查/解锁 API，不在前端推断或执行动作 |
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
