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
| 规则/策略执行单元 | 27 | 会拦截交易、改阈值/仓位、调权、切模板、禁用/退役/回滚，或生成可执行治理动作 |
| 影子/建议模型与模型护栏单元 | 9 | LightGBM、shadow/canary、model permissions、LLM/meta advisory，只能输出审计、建议或 shadow 分数 |
| 诊断汇总单元 | 1 | readiness，把事实源和阻断项汇总给前端和运维 |
| 合计纳入总账 | 37 | 不是全部都会下游执行；执行权限由 `RiskPolicyService`、`DecisionPolicy`、model permissions 限制 |

如果按“会直接改变订单/仓位/配置”的严格口径，当前是 18 个左右；如果把每个 `RiskPolicyService.evaluate(action)` 子动作拆开，数量会超过 40。日常治理建议使用上表 37 个总账口径。

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
```

硬边界：

- 交易动作必须经过 `ExecutionGate` 和 `RiskPolicyService`。
- 权重写入必须经过 `DecisionPolicy`/自治配置写入口。
- context 只能改阈值和仓位，不改多空方向。
- shadow/advisory 模型不能下单、平仓、改硬风控或绕过配置治理。
- `policy_suggestion` 是自治建议/执行审计，不再是必须人工审批队列。

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
| 28 | backend readiness | `backend/services/backend_readiness.py` | blockers、observations、freshness | DB table freshness、model permissions、catalog snapshot、dataset health | 诊断汇总，不直接改交易；前端应优先读取 |

### Shadow, Advisory, And Model Guardrails

| # | 单元 | 代码锚点 | 输出/动作 | 必须记录的数据 | 精度语义 |
|---:|---|---|---|---|---|
| 29 | model permissions | `backend/services/model_permissions.py` | allowed/block audit | model type、artifact contract、capabilities、status、reason | artifact 必须 `advisory_only/shadow_only`，禁止 live trading capability |
| 30 | model promotion gate | `research/model_promotion.py` | shadow/canary/live readiness verdict | model metrics、dataset contract、guardrails、required next stage | live capability blocked；canary 前必须 shadow |
| 31 | shadow/canary/inference contract | `research/model_shadow_queue.py`、`research/model_shadow_runner.py`、`research/model_canary.py`、`research/model_canary_executor.py`、`research/model_inference_contract.py` | shadow report、canary review/trial、advisory inference | candidate id、artifact hash、validation metrics、trial result | advisory-only；canary 也不能直接控制 live orders |
| 32 | open quality LightGBM | `research/open_quality_lightgbm.py` | open quality shadow audit | sample id、quality/risk score、prediction label、feature importance | 使用 matured open outcome；阈值当前围绕 0.5 分类 |
| 33 | position quality LightGBM | `research/position_quality_lightgbm.py` | position quality shadow audit | position id、quality score、prediction label、features | 只评价持仓质量，不直接退出 |
| 34 | factor governance LightGBM | `research/factor_governance_lightgbm.py` | factor weakness shadow audit / advisory | factor、weakness_score、bucket、features、audit row | weakness 高才作为 Orchestrator 证据之一，不单独写权重 |
| 35 | meta model LightGBM | `research/meta_model_lightgbm.py` | global posture shadow audit/report | posture score、contract/observe/recover score、weak rates | 只给全局状态，不直接改 risk/gate |
| 36 | meta governance/sidecar | `backend/services/meta_governance.py`、`research/meta_model_sidecar.py` | meta governance suggestion/advisory ledger | meta shadow report、forbidden actions、permission audit、context snapshot | 可建议 observe/block/review，不可执行交易动作 |
| 37 | LLM advisory | `research/llm_advisory.py` | advisory audit | task type、target、structured context、result、permission status | 只做解释/建议；结果必须落审计，不进入执行层 |

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

## Current Gaps To Watch

这些不是立即阻断 demo 盘运行的问题，但属于后续大版本治理必须持续盯住的点：

- 部分规则单元的审计粒度仍不一致，例如 live helper 里有些 skip/block reason 还依赖 ledger payload 合并。
- `RiskPolicyService` 子动作很多，后续应在文档和 API 里导出 action matrix，避免新增动作绕过审计。
- shadow/advisory 模型数量已经较多，前端需要按“权限边界”展示，而不是按“模型名字”展示。
- readiness 当前是诊断汇总，不是不可变审计表；关键状态仍应回到事实表或 snapshot。
- 历史样本的 degraded/recovered 语义要继续保守，不能为了训练数量补造无法还原的实时上下文。

## Update Rule

新增或修改以下内容时，必须同步更新本文：

- 新增 `RiskPolicyService.evaluate()` action。
- 新增会写 `policy_suggestion`、`evolution_decision`、`learning_application_log/effect` 的治理单元。
- 新增 shadow/advisory 模型、canary runner 或模型权限能力。
- 新增能改变 threshold、position size、factor weight、runtime overlay、position controls 的规则。
- 修改 evidence contract、train gate、readiness、catalog snapshot 或 runtime overlay 语义。
