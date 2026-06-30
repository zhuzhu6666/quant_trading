# Learning Evidence Contract

状态：第一版已落地，2026-06-29；训练准入语义已收紧，2026-06-30

目标：让规则系统产生的数据、训练样本、模型产出都具备同一种可解释、可追溯、可被机器识别的证据语义。

## Contract

每条可进入学习链路的样本必须带 `evidence_contract`：

- `schema_version`: 当前为 `learning_evidence_contract.v1`
- `source`: 来源表、source id、decision/review/trade/position 锚点
- `integrity`: `full / recovered / partial / missing`
- `causal_level`: `observational / counterfactual / replay_validated / intervention_observed`
- `label_status`: `pending / matured / invalid`
- `train_weight`: `0.0 .. 1.0`
- `allowed_uses`: `audit / explainability / weak_supervision / supervised_training / strong_governance`
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

`missing / invalid / pending` 样本只能用于审计、解释、弱监督或观察，不进入强监督训练。

特别约束：

- `pending` 样本不得声明 `supervised_training`
- `supervisor_execution_trace` 是 `autonomous_learning_sample.sample_type`，不是独立表；初始默认是 pending，只能作为轨迹证据
- supervisor trace 只有结合 `trade_outcome_review / supervisor_counterfactual_review` 成熟后，才能进入强训练候选

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
