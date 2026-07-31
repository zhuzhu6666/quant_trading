# Learning Evidence Contract

> Status: active
> Last verified: 2026-07-31
> Scope: evidence semantics for learning samples, model training, governance, and autonomous replay/audit.

状态：第一版已落地，2026-06-29；训练准入语义已收紧，2026-06-30；开仓质量、反事实训练契约、数据健康检查、动态仓位 trace 与事件窗口治理已补齐，2026-07-02；自治治理 V3 继续沿用本文作为证据等级 contract；2026-07-06 补齐学习系统、影子模型和数据精度关系。

目标：让规则系统产生的数据、训练样本、模型产出都具备同一种可解释、可追溯、可被机器识别的证据语义。

## Contract

每条可进入学习链路的样本必须带 `evidence_contract`：

- `schema_version`: 当前为 `learning_evidence_contract.v1`
- `source`: 来源表、source id、decision/review/trade/position 锚点
- `integrity`: `full / recovered / partial / missing`
- `causal_level`: `observational / counterfactual / replay_validated / intervention_observed`
- `label_status`: `pending / matured / invalid`
- `train_weight`: `0.0 .. 1.0`
- `allowed_uses`: `audit / explainability / weak_supervision / counterfactual_training / supervised_training / strong_governance / executable_governance`
- `blockers`: 阻断强训练或强治理的原因
- `hashes`: features、label、trace、explanation 的稳定 hash

`autonomous_learning_sample` 还必须保留运行回放锚点：

- `config_version`
- `config_hash`
- `evolution_run_id`
- `system_contaminated`
- `governance_eligible`
- `governance_effective_weight`
- `governance_eligibility_version`
- `governance_eligibility_fingerprint`
- `governance_ineligible_reason`

本批（2026-07-31）进一步收敛证据合同的唯一计算路径：样本物化与
`repair_evidence_contracts()` 共用同一 canonical normalization/evaluator。repair 只能复用样本
实际保存的 `quality.executable_governance_allowed`，不能从 `sample_type` 推断可执行治理；污染、缺
lineage、未验证 recovered、pending 或未成熟样本继续 fail-closed，且同一批数据第二次 repair 应为零修复。
`allowed_uses`、`model_ready`、`governance_eligible` 与资格列必须由同一份 v1 contract 保持一致，不新增
schema、列或旁路 writer。

部署后验收（2026-07-31）：`autonomous_learning_sample` 全量 `18521` 行的 contract/资格列无漂移，
污染样本无 `model_ready` 或 `executable_governance_allowed` 放行；最终 repair 为 `42` 行，重复
repair 为 `0`，`evidence_contract_health.bad_total=0`。这些是既有 repair ledger 与只读 PostgreSQL
检查结果，不改变 `learning_evidence_contract.v1` 或 `governance_eligibility.v1`。

## Learning Data Flow

当前学习链路按下面顺序运行：

```text
live decision / supervisor trace / close review
  -> autonomous_learning_sample
  -> evidence_contract
  -> dataset readiness / quality-health
  -> dataset snapshot 或专项 LightGBM loader
  -> shadow/advisory model
  -> shadow audit / advisory ledger
  -> policy_suggestion / Factor Catalog / backend-readiness
  -> governed application / rollback observation
```

主要事实表：

| 表 | 角色 |
|---|---|
| `decision_ledger` | open/skip/supervisor 等实时决策事实 |
| `position_supervisor_trace` | 持仓监督动作轨迹 |
| `trade_outcome_review` | 成熟交易结果和复盘事实 |
| `supervisor_counterfactual_review` | 退出反事实成熟化事实 |
| `factor_contribution_review` | 单因子贡献复盘 |
| `autonomous_learning_sample` | 学习样本统一表，带 features/label/trace/evidence contract |
| `policy_suggestion` | 自治建议和执行审计入口 |
| `learning_application_log` | 治理动作应用记录 |
| `learning_application_effect` | 后验效果和回滚事实来源 |

`autonomous_learning_sample.sample_type` 当前包括：

- `shadow_open_decision`
- `risk_rejection`
- `entry_supervisor_feedback`
- `supervisor_trajectory`
- `supervisor_execution_trace`
- `trade_review_outcome`
- `post_close_counterfactual`

## Data Precision And Weight

学习数据精度不是一刀切，而是按证据等级计算：

```text
train_weight = quality_score * integrity_weight * causal_weight * label_weight
```

当前权重来自 `research/features/evidence_contract.py`：

| 字段 | 值 | 权重/含义 |
|---|---|---:|
| `integrity` | `full` | 1.00 |
| `integrity` | `recovered` | 0.75 |
| `integrity` | `partial` | 0.45 |
| `integrity` | `missing` | 0.00 |
| `causal_level` | `intervention_observed` | 1.00 |
| `causal_level` | `replay_validated` | 0.85 |
| `causal_level` | `counterfactual` | 0.70 |
| `causal_level` | `observational` | 0.55 |
| `label_status` | `matured` | 1.00 |
| `label_status` | `pending` | 0.55 |
| `label_status` | 其他/invalid | 0.00 |

`quality_score` 必须在 `0.0..1.0` 内。最终 `train_weight` 也被截断到 `0.0..1.0`。

判断原则：

- `full + intervention_observed + matured` 才是最强证据。
- `recovered` 可以训练，但权重低于 full。
- `partial` 只能 weak supervision，不能进强监督训练。
- `missing` 权重为 0，只能用于审计或解释。
- `observational` 即使 matured，也不能直接当强因果样本。
- 高置信 `counterfactual` 可以训练，但必须满足 counterfactual training 条件。

`train_weight` 与 `governance_effective_weight` 是两个独立权力面。前者只控制
模型训练采样；后者只控制可执行治理聚合，统一由
`backend.services.governance_eligibility.evaluate_governance_eligibility()`
计算：

- full、matured、非污染、model-ready、显式允许
  `executable_governance` 且 lineage 唯一完整：权重 1；
- 只有显式 `verified_recovered=true` 的 recovered 样本可进入治理，权重上限
  0.5；
- partial、missing、未验证 recovered、污染样本、缺 lineage、缺 version 或缺
  fingerprint：治理权重 0；
- fingerprint 绑定样本来源、证据 hashes、资格门禁结果与有效权重，历史空值
  必须先经 repair/backfill 重验，不能按兼容默认放行。

## Training Gate

强监督训练只允许使用：

- `quality.model_ready=true`
- `evidence_contract.allowed_uses` 包含 `supervised_training`
- `label_status=matured`
- `integrity in {full, recovered}`
- `trace / features / label` 非空

2026-06-30 起，`supervised_training` 只有在以下条件同时满足时才允许出现：

- `label_status=matured`
- `integrity in {full, recovered}`
- `causal_level in {replay_validated, intervention_observed}`
- 没有 `missing_trace / missing_features / missing_label` blocker

2026-07-02 起，高置信 post-close counterfactual 可以作为低权重训练样本进入模型训练契约：

- `causal_level=counterfactual`
- `label_status=matured`
- `integrity in {full, recovered}`
- `quality_score >= 0.7`
- `allowed_uses` 必须同时包含 `counterfactual_training` 和 `supervised_training`

低置信、pending、invalid 或缺少 trace/features/label 的反事实样本仍只能用于审计和解释，不能进入强训练。

`missing / invalid / pending` 样本只能用于审计、解释、弱监督或观察，不进入强监督训练。

dataset readiness 当前默认门槛：

- `min_ready_trades=50`
- `min_ready_decisions=200`
- `max_schema_issues=0`

readiness 状态语义：

- `ready`: 达到 ready 样本数且 schema/evidence contract 无阻断。
- `warming_up`: 已有可用样本，但数量或覆盖仍不足。
- `not_ready`: 没有可用样本或存在 schema/evidence contract 阻断。

`/api/learning/dataset/quality-health` 会同时检查：

- evidence contract 自洽性
- open outcome 所需上下文覆盖率
- `model_ready` 是否错误地绕过 `supervised_training`
- pending/invalid/missing 是否误入强训练

特别约束：

- `pending` 样本不得声明 `supervised_training`
- `supervisor_execution_trace` 是 `autonomous_learning_sample.sample_type`，不是独立表；初始默认是 pending，只能作为轨迹证据
- supervisor trace 只有结合 `trade_outcome_review / supervisor_counterfactual_review` 成熟后，才能进入强训练候选

## Open Outcome Samples

开仓质量学习使用 `autonomous_learning_sample.sample_type=shadow_open_decision`，只有开仓样本与 `trade_outcome_review` 对齐并成熟后，才能转换为 `label.label=open_outcome`。

每条 open outcome 训练样本至少应保留：

- `entry_cluster`: 同向仓位数量、金字塔深度、同向 API volume、近 5/15/30 分钟同向开仓
- `portfolio_exposure`: 开仓前后组合暴露
- `market_micro_context`: bid/ask/spread、quote freshness、滑点
- `bar_context`: 入场 K 线实体、区间、收盘位置
- `execution_context`: requested/executed volume、entry/sl/tp、保护距离
- `decision_quality_context`: action score、因子冲突、正负贡献、活跃/弃权因子数量
- `event_context`: 事件窗口、event multiplier、causal event、event type、importance、距离事件的分钟/小时数、pre/post 状态、window bucket、tier multiplier
- `sizing_trace`: dynamic sizing 的 base/final API volume、event-adjusted volume、broker step/min/max、block reason
- `data_quality_context`: quote/bar/context freshness
- `decision_freshness_context`: 决策 K 线新鲜度、缺失闭合 bar、data lag 和 sync health
- `entry_timing_context`: `signal_bar_ts`、`decision_evaluated_at`、`order_submitted_at`、`fill_ts`、`close_ts` 和各阶段 delay
- `system_issue_context`: 数据时效、决策陈旧、信号到成交延迟等是否污染学习样本
- `market_session`: 市场会话状态

复盘摘要的附加口径：`factor_attribution` 必须标注 `causal_level=observational`，`largest_contribution_factor` 只表示最大观测贡献，不表示因果责任；`summary_consistency` 用于记录 sizing trace、执行量、成交量和事件上下文之间的可比性。发现 mismatch 或 different scopes 时只能增加 evidence gap/审计提示，不能直接生成因子惩罚或策略结论。因子治理建议只消费已有责任域、最差因子、冲突、贡献复核和 counter-evidence；责任域为 `exit`、`holding`、`data_quality` 或 `parameter` 时不得生成因子惩罚。

历史样本不得伪造不可恢复的实时上下文。旧 open decision 可以回填同向簇和组合暴露，但 `bar_context / execution_context / market_micro_context / event_context / sizing_trace` 等只能从真实新单开始自然积累；缺少事件距离或窗口桶的旧样本不得事后猜测补造。

如果 `system_issue_context.contaminates_learning=true`：

- `trade_review_outcome` 和成熟 `shadow_open_decision` 必须降为 `integrity=partial` 或更低、`train_weight<=0.25`。
- `entry_supervisor_feedback` 不得生成 `downweight_entry_factor`，只能生成数据链路/系统质量复核建议。
- `factor_contribution_review` 行只能作为 audit/explainability，不能作为高置信因子治理训练样本。

同向簇治理由 `materialize_entry_cluster_governance_suggestions` 消费 matured open outcome 样本：

- 只消费当前 `governance_eligibility.v1` 且 weight > 0 的样本；
- 统计 `same_direction_ge_1 / same_direction_ge_2 / same_direction_ge_3`
- 写入 `experience_pattern_stats(scope_type='entry_cluster')` 的 raw count、effective
  sample count、weighted win/bad-loss/reward 和聚合 fingerprint；
- 当坏结果比例或平均 reward 达到阈值时，生成 `policy_suggestion(scope_type='entry_cluster')`
- 当前建议仍为 governance/advisory，不能直接越过风控下单

事件窗口治理由 `materialize_event_window_governance_suggestions` 消费 matured open outcome 样本：

- 只消费当前 eligibility version、非空 fingerprint 且 weight > 0 的样本；
- 按 `event_type:window_bucket` 归因，例如 `NFP:pre_0_4h`、`FOMC:post_0_15m`
- 写入 `experience_pattern_stats(scope_type='event_window')`
- 当事件窗口内坏结果比例过高时，生成 `policy_suggestion(scope_type='event_window', action='tighten_event_window_sizing')`
- 当事件后窗口平均 reward 显著偏负时，生成 `policy_suggestion(scope_type='event_window', action='extend_event_post_window_review')`
- 当前建议仍为 governance/advisory，不能直接改写事件分层、放大仓位或绕过 `RiskPolicyService`

`RuleEvolutionGovernor` 对样本聚合建议只读取 effective sample count 和 weighted
win/bad-loss/reward。`policy_suggestion` 与 `experience_pattern_stats` 的 eligibility
version/fingerprint 必须一致；缺失或不一致直接 rejected，样本仍保留在
observation/research 链路，但不能触发 mutation。

## Model Output

模型输出必须保留：

- 输入样本 id
- 输入 evidence contract
- features hash
- artifact path / artifact hash
- top terms / feature importance
- advisory/shadow guardrails
- audit id 或可追溯记录

模型输出默认仍为 `shadow_only / advisory_only`，不能绕过 `RiskPolicyService`。

当前数学模型清单：

- `open_quality_lightgbm`: 开仓时机质量影子评分，来源为 matured `shadow_open_decision` + `open_outcome`，必须通过 evidence contract supervised-training gate
- `position_quality_lightgbm`: 持仓质量影子评分，来源为 `trade_outcome_review`，输出 hold/exit risk shadow audit
- `factor_governance_lightgbm`: 因子弱化、因子治理建议，来源为 `factor_contribution_review` + `trade_outcome_review`
- `meta_model_lightgbm`: 全局姿态/元模型影子报告，来源为 rolling `trade_outcome_review`、position/factor shadow weak rates 和治理状态
- `ModelShadowQueue` 通用 shadow 候选: 来源为 dataset snapshot artifact，只能通过 promotion gate 进入 shadow validation
- LLM advisory: 结构化复盘、治理说明、人工覆盖审计辅助；不进入执行层

所有 LightGBM 训练必须记录 `split=time_ordered`、holdout 指标、规则基线和 majority baseline 对照。模型未通过基线比较时，只能继续 shadow/advisory。

学习 worker 仍只有既有的 `offmarket_position_quality_lightgbm` 任务负责这组模型的重任务调度：`full` profile 在训练后为 position、open、factor、meta 四类模型各写一次影子评分；市场开盘或本轮不满足训练条件时，任务不训练，只用现有 artifact 以小批量、按 artifact 与来源样本去重的方式刷新 shadow audit。该刷新不进入 promotion、不物化治理建议，也不改变交易权限。

Shadow candidate 的 DSL 仍只由既有 `parse_dsl()` 校验；malformed expression 在进入 Registry/
lifecycle 前跳过并保留 `shadow_register_invalid_dsl_skipped` 审计事件。没有真实 shadow performance 的候选
保持当前 stage，不得用合成分数推进 promotion 或 active；该约束不新增冷启动阈值，也不关闭 valid shadow
注册能力。

## Shadow Model Boundaries

影子模型的共同边界：

- 必须声明 `live_trading=false`
- 必须声明 `advisory_only=true`
- 必须声明 `shadow_only=true`
- 不得声明 `can_place_orders=true`
- 不得声明 `can_close_positions=true`
- 不得声明 `can_change_risk_limits=true`
- 不得声明 `can_change_factor_weights=true`
- 不得绕过 `RiskPolicyService`
- 每次 inference 必须写入 shadow audit 或 advisory ledger

模型权限由 `backend/services/model_permissions.py` 审计，并写入 `model_permission_audit`。

专项 shadow audit 表：

| 模型 | Audit 表 | 输出语义 |
|---|---|---|
| `open_quality_lightgbm` | `open_quality_shadow_audit` | `quality_score/risk_score/prediction_label` |
| `position_quality_lightgbm` | `position_quality_shadow_audit` | `hold_score/exit_risk_score/risk_bucket` |
| `factor_governance_lightgbm` | `factor_governance_shadow_audit` | `positive_score/weakness_score/weakness_bucket` |
| `meta_model_lightgbm` | `meta_model_shadow_audit` | `posture/posture_score/risk_budget_advice/trade_frequency_advice` |
| 通用 inference | `model_inference_audit` | canary-ready advisory score |

`factor_governance_lightgbm` 可以把高 weakness score 转成 `policy_suggestion`，但该建议仍是 advisory/governance 输入。`demo_nursery` 下，`FactorGovernanceLightGBMService.materialize_demo_governance_advisories` 仅把当前仍启用且可进入 live 的活跃 alpha 因子、至少两条弱样本且平均 weakness 不低于 0.85 的强证据规范化为白名单 `downweight`；这不是模型直接写权重。若因子在桥接后被 quarantine/disabled，建议自动 supersede，避免把历史建议误报为采用。真正降权、禁用、退役或回滚必须由 `RuleEvolutionGovernor`、`FactorGovernanceOrchestrator`、`DecisionPolicy`、`RiskPolicyService`、`FactorWeightChangeService` 和 runtime overlay/snapshot 链路执行，并进入 `learning_application_log/effect` 后验观察。

`demo_nursery` 的 effect reconcile 对超过 24 小时仍没有可比较 baseline 的旧 observing 窗口标记为 `inconclusive`，不把它当作成功或失败经验；该终态只释放同 scope 的实验准入，后续建议仍须重新经过 Governor、DecisionPolicy、RiskPolicy 和效果观察。

`meta_model_lightgbm` 可以 materialize advisory ledger，但不能直接改变风险预算、交易频率、因子权重或 hard risk limits。

通用模型 promotion gate 只输出：

- `decision=shadow_candidate`
- `action=queue_shadow_validation`
- `capabilities.live_trading=false`
- `shadow_validation_required=true`
- `canary_required_before_live=true`

它不授予实盘执行权限。

## Data Quality Health

学习数据健康由 `/api/learning/dataset/quality-health` 暴露：

- `evidence_contract`: 检查 `allowed_uses/model_ready/label_status/integrity` 是否自洽
- 同时检查污染样本是否仍声明 `supervised_training/strong_governance/executable_governance`，以及
  `governance_eligible` 与 contract 资格是否一致
- `entry_context`: 检查最近开仓决策是否带齐 open outcome 所需上下文

学习数据就绪由 `/api/learning/dataset/readiness` 暴露：

- trade samples 使用 `learning_trade_sample.v*` schema
- decision samples 使用 `learning_decision_sample.v*` schema
- required fields 必须齐全
- `quality.model_ready` 的样本必须有非空核心字段
- schema issues 默认不能大于 0

健康检查发现历史覆盖不足时，优先区分：

- 旧数据确实缺字段：保留 degraded，不补造
- 新单链路缺字段：修 live ledger 写入
- evidence contract 异常：运行 `repair_evidence_contracts`

## Causal Boundary

系统不声称金融样本具备绝对因果真相。`causal_level` 是证据等级：

- `observational`: 只说明观察相关性
- `counterfactual`: 有反事实窗口，但不等于真实干预
- `replay_validated`: 可通过 ledger / lifecycle replay 验证路径
- `intervention_observed`: 系统真实执行或治理干预后的观察结果

治理和训练必须按证据等级使用样本，不能把相关性样本当成强因果标签。

## Evolution Traceability

2026-06-30 起，学习样本必须能追溯到统一进化账本：

- `evolution_run`: 本轮为什么运行，例如样本物化、trace 回填、trace 成熟化、demo 自动治理
- `evolution_decision`: 本轮做了哪些关键判断，例如审批、应用、回滚、成熟标签
- `runtime_config_snapshot`: 本轮使用的 RuntimeConfig 版本与 hash

模型训练、shadow、inference audit 应优先保留这些锚点，保证模型产出也能解释“用了哪批样本、这些样本来自哪个配置版本、证据等级是什么”。

## API Map

学习和影子模型的当前入口：

| API | 用途 |
|---|---|
| `GET /api/learning/dataset/readiness` | 数据集训练就绪度 |
| `GET /api/learning/dataset/quality-health` | evidence contract 和 open context 质量 |
| `POST /api/learning/dataset/export` | 导出 dataset snapshot |
| `POST /api/learning/dataset/validate` | 验证 dataset snapshot |
| `POST /api/learning/model/promotion-gate` | artifact 进入 shadow candidate 前的门禁 |
| `POST /api/learning/model/shadow-queue` | 排入 shadow validation 队列 |
| `POST /api/learning/model/shadow-run` | 通用 shadow validation |
| `POST /api/learning/model/inference` | canary-ready advisory inference |
| `GET /api/learning/model/permissions/audits` | 模型权限审计 |
| `POST /api/learning/model/open-quality-lightgbm/train` | 训练开仓质量影子模型 |
| `POST /api/learning/model/open-quality-lightgbm/shadow-run` | 开仓质量 shadow 打分 |
| `GET /api/learning/model/open-quality-lightgbm/audits` | 开仓质量 shadow 审计 |
| `POST /api/learning/model/position-quality-lightgbm/train` | 训练持仓质量影子模型 |
| `POST /api/learning/model/position-quality-lightgbm/shadow-run` | 持仓质量 shadow 打分 |
| `GET /api/learning/model/position-quality-lightgbm/audits` | 持仓质量 shadow 审计 |
| `POST /api/learning/model/factor-governance-lightgbm/train` | 训练因子治理影子模型 |
| `POST /api/learning/model/factor-governance-lightgbm/shadow-run` | 因子治理 shadow 打分 |
| `GET /api/learning/model/factor-governance-lightgbm/audits` | 因子治理 shadow 审计 |
| `GET /api/learning/model/factor-governance-lightgbm/advisories` | 因子治理模型建议 |
| `POST /api/learning/model/meta-lightgbm/train` | 训练元模型 |
| `POST /api/learning/model/meta-lightgbm/shadow-run` | 元模型 shadow 打分 |
| `GET /api/learning/model/meta-lightgbm/shadow-report` | 元模型 shadow 报告 |
| `POST /api/learning/model/llm/advisory-run` | LLM advisory |
| `GET /api/learning/model/llm/audits` | LLM advisory 审计 |
