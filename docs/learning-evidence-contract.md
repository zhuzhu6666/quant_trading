# Learning Evidence Contract

状态：第一版已落地，2026-06-29；训练准入语义已收紧，2026-06-30；开仓质量、反事实训练契约和数据健康检查已补齐，2026-07-02

目标：让规则系统产生的数据、训练样本、模型产出都具备同一种可解释、可追溯、可被机器识别的证据语义。

## Contract

每条可进入学习链路的样本必须带 `evidence_contract`：

- `schema_version`: 当前为 `learning_evidence_contract.v1`
- `source`: 来源表、source id、decision/review/trade/position 锚点
- `integrity`: `full / recovered / partial / missing`
- `causal_level`: `observational / counterfactual / replay_validated / intervention_observed`
- `label_status`: `pending / matured / invalid`
- `train_weight`: `0.0 .. 1.0`
- `allowed_uses`: `audit / explainability / weak_supervision / counterfactual_training / supervised_training / strong_governance`
- `blockers`: 阻断强训练或强治理的原因
- `hashes`: features、label、trace、explanation 的稳定 hash

`autonomous_learning_sample` 还必须保留运行回放锚点：

- `config_version`
- `config_hash`
- `evolution_run_id`

## Training Gate

强监督训练只允许使用：

- `quality.model_ready=true`
- `evidence_contract.allowed_uses` 包含 `supervised_training`
- `label_status=matured`
- `integrity` 不能是 `missing`
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
- `event_context`: 事件窗口、event multiplier
- `data_quality_context`: quote/bar/context freshness
- `market_session`: 市场会话状态

历史样本不得伪造不可恢复的实时上下文。旧 open decision 可以回填同向簇和组合暴露，但 `bar_context / execution_context / market_micro_context` 等只能从真实新单开始自然积累。

同向簇治理由 `materialize_entry_cluster_governance_suggestions` 消费 matured open outcome 样本：

- 统计 `same_direction_ge_1 / same_direction_ge_2 / same_direction_ge_3`
- 写入 `experience_pattern_stats(scope_type='entry_cluster')`
- 当坏结果比例或平均 reward 达到阈值时，生成 `policy_suggestion(scope_type='entry_cluster')`
- 当前建议仍为 governance/advisory，不能直接越过风控下单

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

- `open_quality_lightgbm`: 开仓时机质量影子评分，来源为 matured open outcome samples
- `position_quality_lightgbm`: 持仓质量影子评分
- `factor_governance_lightgbm`: 因子弱化、因子治理建议
- `meta_model_lightgbm`: 全局姿态/元模型影子报告
- LLM advisory: 结构化复盘、治理说明、人审辅助；不进入执行层

所有 LightGBM 训练必须记录 `split=time_ordered`、holdout 指标、规则基线和 majority baseline 对照。模型未通过基线比较时，只能继续 shadow/advisory。

## Data Quality Health

学习数据健康由 `/api/learning/dataset/quality-health` 暴露：

- `evidence_contract`: 检查 `allowed_uses/model_ready/label_status/integrity` 是否自洽
- `entry_context`: 检查最近开仓决策是否带齐 open outcome 所需上下文

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
