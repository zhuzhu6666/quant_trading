# Parameter Template Contract

> Status: active
> Last verified: 2026-07-26
> Scope: parameter template schema, runtime application boundary, autonomous governance entry, and online/offline tuning boundary.

本文定义 `parameter_template.v1`。参数模板已经从只读派生对象进入运行时治理链路；当前主路径是自治建议、风控裁决、overlay/snapshot 写入和后验回滚。人工入口只作为覆盖和审计，不是日常必要步骤。

---

## 1. 为什么要单独做 parameter template

Phase D 已经能识别：

- `factor_logic_ok_but_param_suspect`
- `holding_too_long`
- `regime_changed_during_hold`

但如果系统没有稳定的参数模板对象，就只能停留在“怀疑参数有问题”，还无法继续表达：

- 该因子的默认参数版本是什么
- 有没有更保守或更激进的模板
- 哪些 regime 更适合哪套模板
- 切换模板时该走什么自治治理、风控裁决和回滚动作

---

## 2. `parameter_template.v1` 顶层结构

```json
{
  "schema_version": "parameter_template.v1",
  "template_id": "rsi_14:default.v1",
  "factor_id": "rsi_14",
  "factor_family": "momentum_oscillator",
  "template_version": "default.v1",
  "template_role": "default",
  "formula_version": "registry_builtin.v1",
  "base_parameter_version": "default.v1",
  "parameters": {
    "length": 14
  },
  "applicable_regimes": ["range", "mean_reversion"],
  "avoid_regimes": ["strong_trend"],
  "holding_profile_hint": {
    "style": "short_swing",
    "min_bars": 2,
    "max_bars": 12
  },
  "tuning_bias": "neutral",
  "evidence": {
    "derived_from_factor_card": true,
    "last_primary_responsibility": "parameter",
    "recent_responsibility_labels": ["holding_too_long"]
  }
}
```

---

## 3. 必填字段

- `schema_version`
- `template_id`
- `factor_id`
- `factor_family`
- `template_version`
- `template_role`
- `formula_version`
- `base_parameter_version`
- `parameters`
- `applicable_regimes`
- `avoid_regimes`

---

## 4. 首批模板角色

E2 第一版先固定三类模板角色：

- `default`
- `conservative`
- `aggressive`

含义：

- `default`:
  - 当前因子卡片声明的基础参数
- `conservative`:
  - 更偏稳健、过滤更多噪声、通常更适合弱势或不确定环境
- `aggressive`:
  - 更偏敏捷、响应更快、通常更适合趋势或 breakout 环境

这三类已经可以作为运行时参数模板治理对象；是否自动切换取决于 `online_light/offline_deep` 边界、RiskPolicyService verdict、runtime overlay 写入和后验观察。

---

## 5. 与 factor card 的关系

`parameter_template.v1` 默认从 `factor_card.v1` 派生：

- `factor_id`
- `factor_family`
- `formula_version`
- `base_parameter_version`
- `parameters`
- `expected_regimes`
- `weak_regimes`
- `expected_holding_profile`
- `evidence_summary.last_primary_responsibility`
- `evidence_summary.recent_responsibility_labels`

也就是说：

- factor card 负责描述“这个因子是谁”
- parameter template 负责描述“这个因子当前可切换的参数版本”

---

## 6. 风控与治理入口

任何高影响模板切换，都必须进入统一动作：

`RiskPolicyService.evaluate("switch_parameter_template", context)`

当前要求：

- 动作名正式入位
- 能留下统一审计上下文
- `online_light` 可以由自治治理主路径自动应用
- `offline_deep` 只自动提交验证，不能未验证直接上线
- 所有应用必须写入 `runtime_config_overlay` 和 `runtime_config_snapshot`
- 后验变差时必须支持基于当时 rollback JSON 的自动回滚

---

## 7. 后续演进

后续继续补：

1. 更多重点因子的手工模板元数据
2. regime-aware 模板推荐策略
3. 更强的 replay / walk-forward 验证
4. 模板切换后的后验效果解释和回滚报告

## 8. Online / Offline 变更边界

`online_light` 可以进入现有自治治理链，但不能绕过 `policy_suggestion`、typed governance mutation、`RiskPolicyService`、committed overlay/snapshot 和后验回滚。只允许：

- formula 与 factor family 不变；
- 已有 default/conservative/aggressive 模板之间的有限数值变更；
- 当前已接入 runtime parameter override 的 factor；
- 参数相对变化不超过合同既有限制。

以下一律属于 `offline_deep`，不能直接进入 live：

- 公式、特征族、窗口结构或信号方向变化；
- 新因子或新数据源；
- 超出轻量范围的参数搜索；
- 缺少 PIT、parity、成本和稳定性证据的候选。

offline 结果只能先成为 diagnostic/shadow evidence；是否进入治理仍由现有 research evidence、factor lifecycle 和 RiskPolicy 合同决定，调用方不能用自报字段提升权限。
