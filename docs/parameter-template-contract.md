# Parameter Template Contract

> Last updated: 2026-06-25
> Scope: Phase E / E2 参数模板系统的第一版 contract。

本文定义 `parameter_template.v1`。目标是先把“参数”从零散字段变成可版本化、可枚举、可审计的模板对象，再逐步接入审批与实际切换。

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
- 切换模板时该走什么审批和风控动作

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

这三类先作为只读模板输出，不要求本轮立即接入 live 自动切换。

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

后续任何高影响模板切换，都应进入统一动作：

`RiskPolicyService.evaluate("switch_parameter_template", context)`

E2 第一版只要求：

- 动作名正式入位
- 能留下统一审计上下文
- 先不要求自动落地到 live 因子执行链

---

## 7. 后续演进

E2 之后继续补：

1. 模板持久化与审批记录
2. regime-aware 模板推荐策略
3. 模板切换回测 / 灰度 / 回滚链路
4. live 因子执行层的真实模板加载
