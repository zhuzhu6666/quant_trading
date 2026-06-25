# Factor Card Schema

> Last updated: 2026-06-25
> Scope: Phase E / E1 因子解释卡片标准化 contract。

本文定义“因子解释卡片”的统一 schema。目标不是立刻把所有因子都补全，而是先固定后续治理、归因、前端展示和审批链共用的字段边界。

---

## 1. 为什么要有 factor card

当前系统已经能：

- 注册因子
- 记录因子 description
- 跟踪健康分、贡献分、生命周期事件
- 在复盘侧沉淀“参数可疑 / regime 不匹配 / 退出问题”

但系统还缺一个稳定对象，把这些证据汇总成“这个因子到底是谁、适合什么场景、最近出了什么问题”。

factor card 就是这个统一对象。

---

## 2. `factor_card.v1` 顶层结构

```json
{
  "schema_version": "factor_card.v1",
  "factor_id": "rsi_14",
  "display_name": "RSI(14)",
  "factor_family": "momentum_oscillator",
  "source": "builtin",
  "lifecycle_status": "ACTIVE",
  "formula_version": "registry_builtin.v1",
  "parameter_version": "default.v1",
  "parameters": {
    "length": 14
  },
  "expected_regimes": ["range", "mean_reversion"],
  "weak_regimes": ["strong_trend"],
  "expected_holding_profile": {
    "style": "short_swing",
    "min_bars": 2,
    "max_bars": 12
  },
  "failure_modes": [
    "factor_logic_ok_but_param_suspect",
    "regime_changed_during_hold"
  ],
  "governance_state": {
    "weight_state": "active",
    "template_state": "default_only",
    "review_status": "none"
  },
  "evidence_summary": {
    "description": "RSI(14)",
    "health_score": 0.71,
    "shadow_score": 0.0,
    "last_primary_responsibility": "parameter",
    "recent_responsibility_labels": ["holding_too_long"]
  },
  "updated_at": "2026-06-25T21:40:00Z"
}
```

---

## 3. 必填字段

以下字段作为 `factor_card.v1` 必填项：

- `schema_version`
- `factor_id`
- `display_name`
- `factor_family`
- `source`
- `lifecycle_status`
- `formula_version`
- `parameter_version`
- `parameters`
- `expected_regimes`
- `weak_regimes`
- `expected_holding_profile`
- `failure_modes`

其中：

- `factor_id` 必须与 `factor_registry` / `registry_adapter` 中的唯一标识一致
- `display_name` 允许先复用现有 `description`
- `factor_family` 先允许人工枚举，不要求自动推断
- `formula_version` 与 `parameter_version` 在 Phase E / E2 之前允许使用占位版本

---

## 4. 字段解释

### `factor_id`

唯一因子标识。必须稳定，不随展示文案变化。

### `factor_family`

用于把因子归到更高一层的治理分组，建议首批枚举：

- `momentum`
- `momentum_oscillator`
- `trend`
- `volatility`
- `volume`
- `pattern`
- `macro`
- `calendar`
- `cross_asset`
- `ml_signal`
- `composite`

### `source`

来源先与 `alpha/registry_adapter.py` 对齐：

- `builtin`
- `discovered`
- `shadow`
- `removed`

### `lifecycle_status`

生命周期状态先复用现有 registry adapter 语义：

- `ACTIVE`
- `DEAD`
- `UNKNOWN`

### `formula_version`

表达“因子逻辑版本”，用于区分公式结构变化。

建议：

- 内置旧因子先统一记为 `registry_builtin.v1`
- 由 DSL / 发现流程产生的因子，可用 `dsl.<family>.vN`
- 由模型注册的信号因子，可用 `ml.<model_type>.vN`

### `parameter_version`

表达“参数模板版本”，用于区分相同公式下的参数切换。

在 E2 之前，默认允许：

- `default.v1`
- `manual.v1`
- `shadow.v1`

### `parameters`

结构化参数对象。要求：

- 可 JSON 序列化
- 字段名稳定
- 不混用展示文案和数值含义

例如：

```json
{
  "length": 14,
  "upper_band": 70,
  "lower_band": 30
}
```

### `expected_regimes` / `weak_regimes`

用于表达因子理论适配区间，而不是近期绩效。

首批 regime 标签建议先与现有风控/复盘口径保持保守映射，例如：

- `trend`
- `range`
- `breakout`
- `high_vol`
- `low_vol`
- `event_risk`
- `macro_drift`

### `expected_holding_profile`

表达该因子的预期持仓形态，最少包括：

- `style`
- `min_bars`
- `max_bars`

可选扩展：

- `time_in_profit_bias`
- `giveback_tolerance`
- `preferred_exit_modes`

### `failure_modes`

这里填“常见失败模式”，不是单笔 review 的实时结论。

首批应允许复用 Phase D 已有责任标签，例如：

- `entry_good_exit_bad`
- `alpha_correct_but_capture_failed`
- `holding_too_long`
- `regime_changed_during_hold`
- `factor_logic_ok_but_param_suspect`
- `thesis_broken`
- `holding_inefficient`

---

## 5. 现有系统字段如何映射

当前已经存在的字段，可先映射到 factor card：

- `factor_registry`:
  - `factor_id`
  - `display_name`（先复用 `_factor_desc`）
- `registry_adapter._meta`:
  - `source`
  - `description`
- `registry_adapter._lifecycle_statuses`:
  - `lifecycle_status`
- `decision_factor_snapshot`:
  - `health_score`
  - `shadow_score`
  - `contribution_score`
- `factor_contribution_review`:
  - `recent_responsibility_labels`
  - `last_primary_responsibility`
- `trade_outcome_review.failure_taxonomy`:
  - `failure_modes` 的候选来源

这意味着 E1 先不用重构交易主链，只需要把“已有分散证据 -> 统一 schema”固定下来。

---

## 6. Phase E 后续依赖

`factor_card.v1` 会直接服务后续三类工作：

1. E2 参数模板系统
2. E4 因子治理审批工作流
3. 前端 / 运维的人话解释卡片

后续如果 schema 变更，应通过 `schema_version` 升级，而不是静默改字段含义。
