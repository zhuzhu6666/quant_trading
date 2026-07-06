# Rule-Driven Intelligence Inventory

> Status: active
> Last verified: 2026-07-06
> Scope: current inventory of rule-driven intelligence, shadow/advisory model intelligence, their runtime chain, required audit data, and precision semantics.

本文是系统“智能单元总账”。它回答三个问题：

- 当前到底有多少会做判断、拦截、调权、建议或治理的单元。
- 它们在 live、learning、shadow model、factor governance 之间怎么串起来。
- 每一步必须记录什么数据，以及这些数据的精度/证据等级怎么理解。

长期架构看 `architecture.md`，运行链路看 `system-operation-map.md`，学习样本证据细则看 `learning-evidence-contract.md`。本文只做当前代码事实盘点。

## Counting Rule

本总账按“能产生判断结果”的单元计数，不把普通 CRUD、API 路由、数据读取器、日志 helper 单独计入。

当前代码扫描口径：

| 类别 | 数量 | 含义 |
|---|---:|---|
| 规则/策略执行单元 | 30 | 会拦截交易、改阈值/仓位、调权、切模板、禁用/退役/回滚，或生成/执行受控治理动作；V16 P3 low-impact executor 只允许 read-only replay job 和显式风险收紧，V16 P4 只生成 medium-impact governance suggestions |
| 影子/建议模型与模型护栏单元 | 9 | LightGBM、shadow/canary、model permissions、LLM/meta advisory，只能输出审计、建议或 shadow 分数 |
| 诊断汇总单元 | 12 | readiness、replay harness、autonomy health、autonomy scope approval/enforcement、release run ledger/approval trail、incident playbook plan/event trail、V15 Phase 0 gate、V16 read-only brain state/memory、V16 shadow action planner/evaluator、V16 live-ready guardrails、V16 Web Brain page，把事实源、回放误差、自治状态、发布证据、事故计划/证据事件、完成状态、记忆检索、只读认知状态、影子计划、后验比较和实盘前护栏汇总给前端和运维 |
| 合计纳入总账 | 51 | 不是全部都会下游执行；执行权限由 `RiskPolicyService`、`DecisionPolicy`、model permissions 限制 |

如果按“会直接改变订单/仓位/配置”的严格口径，当前是 19 个左右；如果把每个 `RiskPolicyService.evaluate(action)` 子动作拆开，数量会超过 40。日常治理建议使用上表 51 个总账口径。

## Runtime Chain

当前智能链路分四层：

```text
live trading
  -> factor scoring / context / gate / sizing / risk / execution / supervisor
  -> decision_ledger + trace + lifecycle + trade review

learning evidence
  -> autonomous_learning_sample + evidence_contract
  -> policy_suggestion + experience stats + application effect

factor/config governance
  -> catalog + AWE + DecisionPolicy + redundancy + orchestrator
  -> runtime_config_overlay + runtime_config_snapshot + rollback

shadow/advisory models
  -> permission gate + shadow audit + canary/advisory
  -> catalog/readiness/meta governance

runtime replay / autonomy health
  -> replay_report + backend-readiness v15 contract
  -> autonomy_health_snapshot + autonomy_scope_approval_event
  -> read-only posture, no direct execution authority

incident controls
  -> runtime_incident_mode overlay/snapshot
  -> RiskPolicyService incident gate

release controls
  -> release_run
  -> release_approval_event
  -> incident_playbook_run
  -> incident_playbook_event
  -> release checklist + replay/snapshot/incident/readiness evidence

phase0 completion gate
  -> v15_phase0 completion
  -> implementation_complete + operationally_ready

v16 read-only brain state
  -> brain_state_snapshot + brain_memory
  -> world model + memory retrieval + observe-only hypotheses + critic scope limit

v16 shadow action planner
  -> brain_action_plan
  -> factor weight / parameter template / context policy / supervisor template shadow plans

v16 shadow action evaluator
  -> brain_action_plan_eval
  -> replay / trade outcome / learning effect / supervisor trace posterior comparison

v16 low-impact executor
  -> brain_low_impact_execution
  -> RiskPolicy-gated read-only replay job + optional incident-control tighten

v16 medium-impact governance
  -> brain_medium_impact_governance + policy_suggestion
  -> RiskPolicy verdict + DecisionPolicy preview + rollback/release requirements

v16 live-ready guardrails
  -> brain_live_ready_guardrail
  -> capability lock + broker/local divergence + incident memory + release rollback + tightening-only incident-control
```

硬边界：

- 交易动作必须经过 `ExecutionGate` 和 `RiskPolicyService`。
- 权重写入必须经过 `DecisionPolicy`/自治配置写入口。
- context 只能改阈值和仓位，不改多空方向。
- shadow/advisory 模型不能下单、平仓、改硬风控或绕过配置治理。
- `policy_suggestion` 是自治建议/执行审计，不再是必须人工审批队列。
- replay harness v1 只能校验已有 ledger/risk verdict 锚点，不替代 `RiskPolicyService` 做 live 裁决。
- autonomy health v1 只读展示；health 下降和 scope approval 只能作为后续收紧自治范围的依据，不能放大风险。
- incident controls 必须先过 `RiskPolicyService`，再经 runtime overlay/snapshot 持久化；不能直接手写 state 表。
- release run ledger / approval trail / incident playbook plan/event trail 只做审计汇总、应急计划或证据绑定，不直接执行风险、权重、配置或 broker 动作。
- V15 Phase 0 gate 只读汇总完成状态，不授予任何执行权限。
- V16 read-only brain state/memory 只把 V15 事实源翻译成 world model、memory retrieval、observe-only hypotheses 和 Critic 限制，不执行 action plan，不写 overlay/snapshot，不改权重、仓位、订单或学习样本；negative memory 只能收紧 scope，counter-evidence 只能展示反证。

## Inventory

### Live Execution And Risk

| # | 单元 | 代码锚点 | 输出/动作 | 必须记录的数据 | 精度语义 |
|---:|---|---|---|---|---|
| 1 | 因子组合评分 | `alpha/portfolio_compositor.py` | `CompositeSignal`、方向、`alpha_score`、`context_state` | `factor_signals`、`factor_values`、`factor_roles`、`active_weights`、`context_signals`、`redundancy_groups` | score 为 `-1..1` 连续值；context 不进方向分；`used_in_score` 必须可还原 |
| 2 | context policy | `backend/services/context_policy.py` | `signal_threshold_delta`、`position_multiplier` | `context_state`、reason、applied、最终 multiplier | threshold delta clamp 到约 `-0.05..0.15`；仓位乘数默认 clamp `0.5..1.25` |
| 3 | 执行信号门 | `alpha/execution_gate.py` | pass/block、reason、cooldown | threshold、score、direction、cooldown、NFP/GVZ reason | 只处理执行门禁；不负责 broker 风险 |
| 4 | 事件仓位缩放 | `execution/event_sizing.py` | event multiplier、event context | `event_sizing.short_window.v2`、event type、importance、hours/minutes until、window bucket、tier multiplier | multiplier `0..1`；事件后窗口只保留短 post-event 容错 |
| 5 | Kelly 动态仓位 | `backend/services/live_risk_sizing.py` | base API volume、sizing trace | `position_sizing_trace.v1`、equity、Kelly fraction、SL distance、risk budget、broker min/step/max | volume 按 broker API step 取整；缺 equity/kelly 时退回最小量 |
| 6 | effective event/context sizing | `backend/services/live_service.py` | final requested API volume | base volume、event adjusted volume、context adjusted volume、blocked reason | 低于最小量时不静默抬回，由后续 risk policy 决定 |
| 7 | 动作级风险策略 | `risk/policy_service.py` | `RiskVerdict` for open/close/reduce/template/factor/model actions | action、context、allowed、reason、severity、audit payload | 唯一动作裁决入口；新增自治动作必须在这里注册 |
| 8 | 账户/系统硬风控 | `risk/governor.py` | allow/block | drawdown、daily loss、trade limit、data lag、disk、L2、bridge、circuit breaker | fail closed；`force_dry_run`、断连、严重数据延迟优先阻断 |
| 9 | session/order block | `backend/services/live_tick_pipeline.py`、`backend/services/live_service.py` | market order block、skip stage | market session、risk verdict、event sizing below min、order block reason | 最终发单前把交易时段和 risk verdict 合并成 skip/open |
| 10 | 持仓监督 | `backend/services/position_supervisor.py` | hold/tighten/reduce/close 建议 | confidence、severity、evidence、template、trigger tags、recommended controls | hold 低置信可观察；close/reduce 需要更高 confidence 并再次过 `RiskPolicyService` |
| 11 | 保护执行/超时执行 | `backend/services/live_service.py` | amend SL/TP、reduce、close、repair | protection candidate、supersede reason、execution result、close reason | 只执行 supervisor/risk 允许后的控制动作；执行失败必须写 lifecycle |

### Learning And Rule Evolution

| # | 单元 | 代码锚点 | 输出/动作 | 必须记录的数据 | 精度语义 |
|---:|---|---|---|---|---|
| 12 | evidence contract | `research/features/evidence_contract.py` | `train_weight`、allowed uses、model_ready | integrity、causal_level、label_status、quality_score、hashes、blockers | `train_weight = quality * integrity_weight * causal_weight * label_weight`，最终 `0..1` |
| 13 | 学习样本 materializer | `backend/services/autonomous_learning.py` | `autonomous_learning_sample` | source id、features、label、trace、evidence_contract、config hash | pending/partial/missing 不得伪装强监督 |
| 14 | 经验策略建议 | `research/learning/policy_suggester.py` | factor downweight/boost/watch suggestion | sample_count、avg_reward、bad_loss_count、confidence | 最小样本通常 3/4 起步；低样本只能 watch |
| 15 | 规则演化 governor | `research/learning/governor.py` | apply/reject/rollback policy suggestion | policy suggestion、experience stats、application log/effect | 应用后必须可观察后验；无证据默认拒绝或保守观察 |
| 16 | entry cluster 治理 | `backend/services/autonomous_learning.py` | 同向簇冷却/阈值建议 | same-direction bucket、sample_count、bad_rate、avg_reward | 默认 min samples 3、bad rate 0.5 左右才建议 |
| 17 | event window 治理 | `backend/services/autonomous_learning.py` | event sizing/post-window 建议 | event type、window bucket、sample_count、bad_rate、reward | 只建议收紧/复盘，不直接放大事件窗口仓位 |
| 18 | entry quality 治理 | `backend/services/autonomous_learning.py` | 弱信号阈值/因子一致性建议 | weak_signal、factor_conflict、worst_factor、sample_count、bad_rate | 默认 min samples 5、bad rate 0.6 左右才建议 |

### Factor, Weight, Template, And Config Governance

| # | 单元 | 代码锚点 | 输出/动作 | 必须记录的数据 | 精度语义 |
|---:|---|---|---|---|---|
| 19 | AWE 自适应权重 | `alpha/adaptive_weight_engine.py` | factor weight patch | IC、health、DSR/causal gate、trade count、reason | 只处理 `role=alpha`；单次变化受 max delta 限制 |
| 20 | DecisionPolicy | `alpha/decision_policy.py` | 最终权重决策 | old/new weight、confidence、source_scores、reason | 权重写入口；执行 role/lifecycle/redundancy cap |
| 21 | redundancy detector | `backend/services/factor_redundancy.py` | redundancy group/leader report | normalized factor history、corr、sample_count、leader score | 只看 live enabled alpha；context/gate/sizing 不入组 |
| 22 | FactorGovernanceOrchestrator | `backend/runtime/factor_governance_orchestrator.py` | promote/downweight/disable/retire/rollback/template | catalog、risk verdict、snapshot、run id、decision id | 每轮限速；动作必须过 `RiskPolicyService` 并有 rollback snapshot |
| 23 | Factor Catalog | `backend/services/factor_catalog.py` | 实时事实视图和 snapshot | registry、runtime config、weights、health、shadow perf、rollback state | 不是权重写入口；是治理/readiness/frontend 的统一读模型 |
| 24 | runtime overlay/snapshot | `backend/services/runtime_config_overlay.py` | DB overlay、startup restore、rollback point | patch、source、hash、updated_at、snapshot JSON | overlay 是自治事实源，不写回 `settings.yaml` |
| 25 | 参数模板服务 | `backend/services/parameter_templates.py` | active template、switch suggestion | factor card evidence、recommended scope、confidence、target template | `online_light` 可治理应用；`offline_deep` 先验证 |
| 26 | 参数模板验证 | `backend/services/parameter_template_validation.py` | validation job/result | replay/counterfactual evidence、metrics、candidate template | 深调不能直接 live；必须先出验证证据 |
| 27 | 持仓监督模板治理 | `backend/services/supervisor_learning_scheduler.py`、`risk/policy_service.py` | supervisor template switch | approved suggestion、replay evidence、counterfactual evidence | demo autonomous 才能自动切换；必须保留 active template snapshot |
| 28 | backend readiness | `backend/services/backend_readiness.py` | blockers、observations、freshness、V15 contract | DB table freshness、model permissions、catalog snapshot、dataset health、replay、autonomy health | 诊断汇总，不直接改交易；前端应优先读取 |
| 29 | replay harness | `backend/services/replay_harness.py` | `ReplayReport`、factor/gate/risk 对齐误差、P1 bar/factor-frame/recompute/lifecycle/deep evidence | `replay_report`、`data/replay_reports/*.json`、decision ids、input_dataset_hash、runtime_config_hash、metric_summary、bar_replay_metrics、factor_frame_replay_metrics、execution_gate_recompute_metrics、risk_policy_recompute_metrics、order_lifecycle_replay_metrics、position_lifecycle_replay_metrics、supervisor_action_replay_metrics、order_outcome_causality_metrics、broker_fill_slippage_metrics、supervisor_counterfactual_replay_metrics、risk_policy_subaction_replay_metrics、evidence_grade、artifact_hash | 只读审计；校验 ledger 中已有 `RiskPolicyService` verdict；P1 bar-run 对齐历史 bar window、复建 FactorFrame，通过 `ExecutionGate.filter()` / `RiskPolicyService.evaluate(...)` 做 offline recompute，并验证 order/position/supervisor lifecycle、broker deal/slippage 和 counterfactual evidence，不重放 broker 执行，不喂 circuit breaker |
| 30 | autonomy health v1 | `backend/services/autonomy_health.py` | score、posture、blockers、trend、scope recommendation、tightening enforcement | `autonomy_health_snapshot`、`autonomy_scope_enforcement_event`、action_success_rate、rollback_rate、blocked_by_risk_rate、post_action_reward_delta、config/replay/catalog/shadow/evidence/live scores | 评分/趋势只读；scope approval 不执行；enforcement 只能通过 incident-control 收紧 `runtime_incident_mode`，不能放宽权限 |
| 31 | runtime incident controls | `backend/services/incident_controls.py`、`risk/policy_service.py` | normal/shadow_only/no_new_risk/only_close/frozen gate | `runtime_incident_mode`、risk verdict、runtime overlay、snapshot、mutation audit | 阻断新风险或治理动作；close/rollback 等恢复动作仍需经过 `RiskPolicyService` |
| 32 | release run ledger v1 | `backend/services/release_control.py` | release checklist、start/finish ledger、approval trail | `release_run`、`release_approval_event`、runtime_config_hash、replay_run_id、artifact_hash、incident mode、readiness posture、tests、rollback_ref、approval actor/decision/reason/evidence_refs | 只读证据汇总和发布审批审计；approval event 不改变 release status；不直接改 runtime、权重、仓位或 broker 状态 |
| 33 | autonomy scope approval v1 | `backend/services/autonomy_health.py` | health scope recommendation approval audit | `autonomy_scope_approval_event`、snapshot_id、posture、recommendation、actor、decision、reason、boundary | 只读审批审计；`applied=false`；不直接改 runtime 权限、权重、订单或仓位 |
| 34 | V15 Phase 0 completion gate | `backend/services/v15_phase0.py` | implementation_complete、operationally_ready、gates、evidence_gaps | readiness `v15`、replay status、autonomy health、incident control、release latest、snapshot status | 只读完成状态；区分代码能力和现场证据，不替代任何执行入口 |
| 35 | incident playbook plan/event binding v1 | `backend/services/incident_controls.py` | incident scenario -> target mode plan、RiskPolicy precheck、steps、evidence event trail | `incident_playbook_run`、`incident_playbook_event`、scenario、severity、current_mode、target_mode、risk_precheck、steps、release_ref、event_type、evidence_refs、boundary | 只读计划、事件绑定和审计；不直接应用 incident mode，不写 overlay/snapshot；真正切换仍走 `RiskPolicyService` + runtime overlay/snapshot |
| 36 | V15 Web cockpit | `web_frontend/src/pages/V15CockpitPage.tsx` | Runtime、Factors、Governance、Replay、Risk、Learning、Incidents、Release 展示和受控操作按钮 | readiness、catalog、replay_report、autonomy health、incident control、release ledger、risk summary、learning/governance summaries | 前端只汇总和触发后端 API；不重新实现风控/权重/overlay 判断 |
| 45 | V16 read-only brain state/memory | `backend/services/brain_state.py`、`backend/services/brain_memory.py` | `world_model`、memory retrieval、observe-only `hypotheses`、`critic`、`evidence_refs` | `brain_state_snapshot`、`brain_memory`、readiness、replay status、autonomy health、incident control、governance freshness、experience/trade review/policy suggestion/model permission/shadow audit refs、只读 boundary | Phase 1 只读认知层；`affects_trading=false`，不执行 action plan，不改 runtime overlay、权重、订单、仓位或学习样本 |
| 46 | V16 Web Brain page | `web_frontend/src/pages/V16BrainPage.tsx` | World Model、Memory、Hypotheses、Critic、Evidence 展示和只读刷新 | `/api/ops/brain/state`、`/api/ops/brain/memory`、`/api/ops/backend-readiness` | 前端只展示后端事实和触发只读刷新；不重算策略/风控，不执行 action plan |
| 47 | V16 shadow action planner | `backend/services/brain_action_planner.py` | factor weight、parameter template、context policy、supervisor template 的 shadow action plans、Critic verdict、required services、validation refs、shadow eval contract | `brain_action_plan`、`brain_state_snapshot`、memory refs、readiness `v16.action_plans`、`/api/ops/brain/action-plans`、只读 boundary | Phase 2 影子计划账本；record-only，不执行、不写 runtime overlay/snapshot、不改权重/模板/订单/仓位或学习样本；future execution 必须重新走 `RiskPolicyService`、`DecisionPolicy` 和 rollback evidence |
| 48 | V16 shadow action evaluator | `backend/services/brain_action_evaluator.py` | shadow action plans 的 posterior coverage、comparison verdict、evidence refs | `brain_action_plan_eval`、`brain_action_plan`、`replay_report`、`trade_outcome_review`、`learning_application_effect`、`position_supervisor_trace`、readiness `v16.action_plan_evals`、`/api/ops/brain/action-plan-evals` | Phase 2 后验可比性审计；只读/record-only，不执行计划，不改变 plan 状态，不写学习标签，不触发治理或 live mutation |
| 49 | V16 low-impact executor | `backend/services/brain_low_impact_executor.py`、`risk/policy_service.py` | `run_replay_job` 低影响执行、RiskPolicy verdict、rollback/downgrade plan、posterior monitor、可选 `shadow_only` 收紧 | `brain_low_impact_execution`、`brain_action_plan_eval`、`replay_report`、`runtime_incident_mode`、`runtime_config_overlay/snapshot`（仅显式允许坏化收紧时）、`/api/ops/brain/low-impact-executions/run` | Phase 3 低影响执行；当前只允许 read-only replay job，不能改权重/模板/订单/学习样本；坏化收紧必须显式允许并走 incident-control + `RiskPolicyService` |
| 50 | V16 medium-impact governance | `backend/services/brain_medium_impact_governance.py` | medium-impact `policy_suggestion` candidates、RiskPolicy verdict、DecisionPolicy preview、rollback/release requirement | `brain_medium_impact_governance`、`policy_suggestion`、`brain_action_plan_eval`、`RiskPolicyService` verdict、`DecisionPolicy` preview、readiness `v16.medium_impact_governance`、`/api/ops/brain/medium-impact-governance/materialize` | Phase 4 中等影响治理候选；只写建议和审计，不应用权重/模板/模型 promotion，不写 runtime overlay/snapshot、订单或学习样本；future apply 仍走受控治理写入口 |
| 51 | V16 live-ready guardrails | `backend/services/brain_live_ready_guardrail.py` | live capability lock、broker/local divergence、incident memory、release rollback、P3/P4 evidence、tightening-only incident-control request | `brain_live_ready_guardrail`、readiness `v16.live_ready_guardrails`、`/api/ops/brain/live-ready-guardrails/evaluate`、`/api/ops/brain/live-ready-guardrails/tighten`、`RiskPolicyService` verdict、runtime incident overlay/snapshot（仅收紧时） | Phase 5 实盘前护栏；评估不授权下单或应用治理建议；tighten 只能通过 `RuntimeIncidentControlService` 进入更严格 incident mode，不能放宽权限、写学习样本或提交订单 |

### Shadow, Advisory, And Model Guardrails

| # | 单元 | 代码锚点 | 输出/动作 | 必须记录的数据 | 精度语义 |
|---:|---|---|---|---|---|
| 36 | model permissions | `backend/services/model_permissions.py` | allowed/block audit | model type、artifact contract、capabilities、status、reason | artifact 必须 `advisory_only/shadow_only`，禁止 live trading capability |
| 37 | model promotion gate | `research/model_promotion.py` | shadow/canary/live readiness verdict | model metrics、dataset contract、guardrails、required next stage | live capability blocked；canary 前必须 shadow |
| 38 | shadow/canary/inference contract | `research/model_shadow_queue.py`、`research/model_shadow_runner.py`、`research/model_canary.py`、`research/model_canary_executor.py`、`research/model_inference_contract.py` | shadow report、canary review/trial、advisory inference | candidate id、artifact hash、validation metrics、trial result | advisory-only；canary 也不能直接控制 live orders |
| 39 | open quality LightGBM | `research/open_quality_lightgbm.py` | open quality shadow audit | sample id、quality/risk score、prediction label、feature importance | 使用 matured open outcome；阈值当前围绕 0.5 分类 |
| 40 | position quality LightGBM | `research/position_quality_lightgbm.py` | position quality shadow audit | position id、quality score、prediction label、features | 只评价持仓质量，不直接退出 |
| 41 | factor governance LightGBM | `research/factor_governance_lightgbm.py` | factor weakness shadow audit / advisory | factor、weakness_score、bucket、features、audit row | weakness 高才作为 Orchestrator 证据之一，不单独写权重 |
| 42 | meta model LightGBM | `research/meta_model_lightgbm.py` | global posture shadow audit/report | posture score、contract/observe/recover score、weak rates | 只给全局状态，不直接改 risk/gate |
| 43 | meta governance/sidecar | `backend/services/meta_governance.py`、`research/meta_model_sidecar.py` | meta governance suggestion/advisory ledger | meta shadow report、forbidden actions、permission audit、context snapshot | 可建议 observe/block/review，不可执行交易动作 |
| 44 | LLM advisory | `research/llm_advisory.py` | advisory audit | task type、target、structured context、result、permission status | 只做解释/建议；结果必须落审计，不进入执行层 |

## Required Audit Data By Step

| 链路步骤 | 主事实表/对象 | 必须字段 | 精度/质量要求 |
|---|---|---|---|
| 因子计算与组合 | `CompositeSignal`、`decision_factor_snapshot` | factor id、raw/normalized value、role、weight、contribution、used_in_score | alpha/context 必须分离；normalized value 可复算；context contribution 为 0 |
| 信号门禁 | `decision_ledger`、`GateResult` | score、direction、threshold、reason、cooldown、bar ts | score 为连续值；threshold 要记录 context 调整后的有效值 |
| 动态仓位 | `sizing_trace` | base API volume、risk budget、SL distance、event multiplier、context multiplier、final volume | volume 必须按 broker min/step/max 记录取整前后 |
| 风控裁决 | `RiskVerdict`、`decision_ledger.risk_state` | action、allowed、reason、severity、evidence、controls | fail closed；所有自治动作也必须有 risk verdict |
| 订单/成交 | `order_lifecycle_event`、`position_lifecycle_event`、`ctrader_deals` | broker id、requested/actual volume、fill price、SL/TP、status、error | broker 回执是执行事实；本地推断只能作辅助 |
| 持仓监督 | `position_supervisor_trace` | action、confidence、severity、template、evidence、controls、trace_integrity | confidence `0..1`；trace_integrity 决定能否进训练 |
| 交易复盘 | `trade_outcome_review`、`factor_contribution_review` | realized pnl、duration、MFE/MAE、factor attribution、review status | 成熟后才能给 open outcome / contribution label |
| 学习样本 | `autonomous_learning_sample` | sample_type、features、label、trace、evidence_contract | 强训练必须 `model_ready=true` 且 allowed uses 包含 `supervised_training` |
| 经验统计/建议 | `experience_pattern_stats`、`policy_suggestion` | scope、action、sample_count、bad_rate、avg_reward、confidence | 低样本只能观察；建议不等于已执行 |
| 治理执行 | `evolution_run`、`evolution_decision`、`learning_application_log` | run id、action、risk verdict、patch、rollback_json、status | 动作前必须写 snapshot；后验坏化可 rollback |
| 后验回滚 | `learning_application_effect` | observed_trade_count、delta_avg_reward、status | 当前因子治理回滚默认要求 trades >= 3 且 delta <= -0.15 |
| 配置事实 | `runtime_config_overlay`、`runtime_config_snapshot` | overlay hash、source、updated_at、runtime config JSON | startup 必须 restore overlay 并写 startup snapshot |
| Catalog 审计 | `factor_catalog_snapshot` | full catalog JSON、hash、run_id、created_at | 实时 Catalog 是服务视图；snapshot 用于审计/回放 |
| replay evidence | `replay_report` + artifact JSON | replay_run_id、scope、input_dataset_hash、runtime_config_hash、decision/mismatch counts、evidence_grade、artifact_path、artifact_hash、bar_replay_metrics、factor_frame_replay_metrics、order_outcome_causality_metrics、broker_fill_slippage_metrics、supervisor_counterfactual_replay_metrics、risk_policy_subaction_replay_metrics | replay 不能只给收益；必须给 live ledger 对齐误差；P1 bar-run 必须给 bar_window_hash、factor_frame_hash、causality/slippage/counterfactual/subaction hash 和 coverage；artifact 必须可由 metadata 找回并校验 |
| autonomy health | `/api/ops/backend-readiness.autonomy_health` + `autonomy_health_snapshot` + `autonomy_scope_approval_event` + `autonomy_scope_enforcement_event` | score、posture、blockers、各维度分数、trend、scope recommendation、scope approval/enforcement、updated_at | health 下降不能自动放大交易风险；approval event 不是 runtime 权限写入口；enforcement 只能显式收紧 incident mode，必须走 `RiskPolicyService` + overlay/snapshot |
| incident controls | `runtime_incident_mode`、`RiskVerdict`、`runtime_config_overlay/snapshot` | mode、current/target、confirm_thaw、risk verdict、mutation result | thaw/放松必须显式确认；执行动作仍由 `RiskPolicyService` 按 mode 裁决 |
| incident playbook plan/event trail | `incident_playbook_run` / `incident_playbook_event` | scenario、severity、current/target mode、risk_precheck、steps、release_ref、event_type、evidence_refs、boundary | 只写计划/事件账本；`set_incident_control` step 必须声明需要 `RiskPolicyService`，playbook/event 不直接执行 |
| release run ledger | `release_run`、`release_approval_event` | run_id、release_class、status、checklist、runtime_config_hash、replay_run_id、incident_mode、tests、rollback_ref、approval actor、decision、reason、evidence_refs | 只记录发布证据和审批事件；涉及风险或配置的动作必须回到对应权威入口 |
| V15 Phase 0 gate | `/api/ops/v15/phase0` / readiness `v15_phase0` | implementation_complete、operationally_ready、gates、blockers、evidence_gaps | 完成状态是诊断事实，不是执行授权 |
| V15 Web cockpit | `/v15` | Runtime、Factors、Governance、Replay、Risk、Learning、Incidents、Release 汇总状态和受控操作结果 | 只展示事实源和触发后端受控 API；不能在前端推断或绕过策略/风控 |
| V16 read-only brain state/memory | `brain_state_snapshot`、`brain_memory` / `/api/ops/brain/state`、`/api/ops/brain/memory` / readiness `v16.brain_state` | snapshot_id、world_model、perceptions、memory items、negative_matches、counter_evidence、hypotheses、critic、evidence_refs、boundary、created_at | Phase 1 只读；只能输出 observe-only 认知事实、记忆检索和 Critic 限制，不能执行 action plan 或改变任何 live/governance/learning 状态 |
| V16 shadow action planner | `brain_action_plan` / `/api/ops/brain/action-plans` / readiness `v16.action_plans` | plan_id、scope、Critic verdict、required services、validation refs、shadow eval contract、future rollback requirement、boundary | Phase 2 shadow-only；只记录计划，不执行、不改 overlay/snapshot/权重/模板/订单/学习样本 |
| V16 shadow action evaluator | `brain_action_plan_eval` / `/api/ops/brain/action-plan-evals` / readiness `v16.action_plan_evals` | eval_id、plan_id、coverage_score、comparison verdict、comparison summary、evidence refs、boundary | Phase 2 posterior comparison；只读/record-only，不执行计划，不写学习标签，不改变 live/governance/learning 状态 |
| V16 low-impact executor | `brain_low_impact_execution` / `/api/ops/brain/low-impact-executions/run` / readiness `v16.low_impact_executions` | execution_id、plan/eval refs、evidence score、Critic verdict、RiskPolicy verdict、rollback/downgrade plan、replay result、posterior monitor | Phase 3 low-impact；只执行白名单 read-only replay job；可选坏化收紧走 incident-control，不改变权重/模板/订单/学习样本 |
| V16 medium-impact governance | `brain_medium_impact_governance` / `policy_suggestion` / `/api/ops/brain/medium-impact-governance/materialize` / readiness `v16.medium_impact_governance` | governance_id、policy_suggestion、RiskPolicy verdict、DecisionPolicy preview、rollback/release requirements、boundary | Phase 4 medium-impact；只生成治理候选，不应用 runtime mutation；future apply must use governed backend paths |
| V16 live-ready guardrails | `brain_live_ready_guardrail` / `/api/ops/brain/live-ready-guardrails/evaluate` / `/api/ops/brain/live-ready-guardrails/tighten` / readiness `v16.live_ready_guardrails` | guardrail_id、capability lock、divergence status、incident memory、release rollback、recommendation、RiskPolicy precheck、boundary | Phase 5 live-ready；评估只写审计，tighten only uses incident-control/RiskPolicy and never relaxes incident mode |
| V16 Web Brain page | `/v16` | World Model、Memory、Hypotheses、Critic、Evidence、Shadow Action Plans、Posterior Evaluations、P3 Executions、P4 Governance、P5 Guardrails、Readiness contract | 只展示后端事实；运行按钮只调用后端白名单/建议生成/护栏评估/收紧 API，不能在前端推断或执行动作 |
| 模型权限 | `model_permission_audit` | artifact hash、capabilities、permission status、reason | live_trading capability 必须 blocked |
| 影子模型 | `*_shadow_audit`、`model_canary_*` | sample/candidate id、score、prediction、metrics、artifact path/hash | shadow/canary/advisory 不能直接执行交易 |
| readiness | `/api/ops/backend-readiness` response | blockers、known observations、freshness、last good | 运维展示入口，不替代事实表 |

## Data Precision Summary

核心精度字段分五类：

| 类别 | 字段 | 范围/含义 | 不能做什么 |
|---|---|---|---|
| 信号强度 | `score`、`alpha_score`、factor normalized value | 通常 `-1..1` 或归一化连续值 | context/gate/sizing 不能伪装方向票 |
| 置信度 | `confidence`、model probability、posture/weakness score | `0..1`，越高越可信或越强烈 | 置信高不等于有执行权限 |
| 证据等级 | `integrity`、`causal_level`、`label_status`、`train_weight` | 决定训练/治理可用性 | pending/missing 不能进强监督 |
| 仓位精度 | API volume、display units、min/step/max、multiplier | 必须同时记录原始量和取整后量 | 不能只记录最终 volume，否则无法复盘缩放来源 |
| 治理效果 | `sample_count`、`bad_rate`、`avg_reward`、`delta_avg_reward` | 决定建议、应用、回滚 | 小样本不能强治理；后验变差必须可回滚 |

当前最重要的质量门槛：

- dataset readiness: 默认 ready 需要足够 matured trades/decisions，且 schema issues 为 0。
- strong supervised training: 必须 matured、full/recovered、replay/intervention 或合格 counterfactual。
- factor governance rollback: 默认 `observed_trade_count >= 3` 且 `delta_avg_reward <= -0.15`。
- redundancy: alpha live enabled，样本数默认不少于 200，相关性阈值默认约 0.85。
- context policy: 只允许改变 threshold/sizing，不允许改变 direction。
- model permissions: 所有模型 artifact 必须保持 shadow/advisory-only。
- replay harness: 输出 `replay_report`，至少包含 factor/gate/risk verdict 覆盖率和 mismatch_count；P1 bar-run 还必须包含 `bar_replay_metrics.v1`、`factor_frame_replay_metrics.v1`、`execution_gate_recompute_metrics.v1`、`risk_policy_recompute_metrics.v1`、`order_lifecycle_replay_metrics.v1`、`position_lifecycle_replay_metrics.v1`、`supervisor_action_replay_metrics.v1`、`order_outcome_causality_metrics.v1`、`broker_fill_slippage_metrics.v1`、`supervisor_counterfactual_replay_metrics.v1` 和 `risk_policy_subaction_replay_metrics.v1`。
- autonomy health v1: `posture` 必须机器可读，取值 `full/constrained/shadow_only/frozen`；trend 必须输出 `autonomy_health_trend.v1`；scope recommendation 必须 `can_tighten_only=true` 且 `applied=false`。
- autonomy scope approval v1: `autonomy_scope_approval_event` 必须记录 snapshot/recommendation/actor/decision/reason，边界必须 `applied=false`、`can_tighten_only=true`。
- autonomy scope enforcement v1: `autonomy_scope_enforcement_event` 必须记录 current/target incident mode、risk verdict、mutation result 和 boundary；只能应用更严格 mode，真实执行必须经 `RuntimeIncidentControlService.set_mode()`、`RiskPolicyService` 和 runtime overlay/snapshot。
- incident controls v1: `runtime_incident_mode` 必须机器可读，取值 `normal/shadow_only/no_new_risk/only_close/frozen`。
- incident playbook plan/event binding v1: `incident_playbook_run` 必须包含 scenario、severity、target_mode、`RiskPolicyService.evaluate("set_incident_control")` 预检和 `does_not_apply_incident_mode=true` 边界；`incident_playbook_event` 只能绑定 evidence refs 和 notes，必须声明 audit-only。
- release run ledger v1: `release_run` 必须能追溯 snapshot hash、replay artifact hash、incident mode、tests 和 rollback ref；`release_approval_event` 必须能追溯 actor、decision、reason 和 evidence refs，且不能改变 release status。
- V15 Phase 0 gate: 必须区分 `implementation_complete` 和 `operationally_ready`。
- V16 read-only brain state/memory: 必须声明 `read_only=true`、`affects_trading=false`，hypothesis 第一阶段只能 `observe_only`；`brain_memory` 不替代来源事实表，不生成训练标签。

## Current Gaps To Watch

这些不是立即阻断 demo 盘运行的问题，但属于后续大版本治理必须持续盯住的点：

- 部分规则单元的审计粒度仍不一致，例如 live helper 里有些 skip/block reason 还依赖 ledger payload 合并。
- `RiskPolicyService` 子动作很多，后续应在文档和 API 里导出 action matrix，避免新增动作绕过审计。
- shadow/advisory 模型数量已经较多，前端需要按“权限边界”展示，而不是按“模型名字”展示。
- readiness 当前是诊断汇总，不是不可变审计表；关键状态仍应回到事实表或 snapshot。
- replay harness P1 已有 decision/bar-window/factor-frame evidence、`ExecutionGate` / `RiskPolicyService` offline recompute v1、order/position/supervisor lifecycle coverage v1，以及 broker outcome causality、fill slippage、supervisor counterfactual 和 supervisor risk subaction replay v1；后续可扩展更多 `RiskPolicyService` action matrix。
- autonomy health 已有 persistence/trend、scope approval trail 和 tightening-only enforcement binding；后续需要观察真实 health 降级时的告警/审批联动质量。
- release run ledger v1 目前已有发布证据账本、approval trail v1、incident playbook plan v1 和 incident playbook event binding v1；后续需要和真实告警/事件源、freeze/thaw approval event 自动触发更深绑定。
- 历史样本的 degraded/recovered 语义要继续保守，不能为了训练数量补造无法还原的实时上下文。

## Update Rule

新增或修改以下内容时，必须同步更新本文：

- 新增 `RiskPolicyService.evaluate()` action。
- 新增会写 `policy_suggestion`、`evolution_decision`、`learning_application_log/effect` 的治理单元。
- 新增 shadow/advisory 模型、canary runner 或模型权限能力。
- 新增能改变 threshold、position size、factor weight、runtime overlay、position controls 的规则。
- 修改 evidence contract、train gate、readiness、catalog snapshot、replay report、autonomy health、incident controls、release run ledger、V15 Phase 0 gate、V16 brain state 或 runtime overlay 语义。
