# Parameter Tuning Boundary

> Last updated: 2026-06-25
> Scope: Phase E / E3 在线轻调与离线深调边界。

本文把 parameter template 相关变更拆成两类：

- `online_light`
- `offline_deep`

目标不是让系统立刻自动切所有模板，而是先把“什么允许在线切、什么必须先离线验证”明确写死。

---

## 1. `online_light`

允许在线进入治理链并由风控统一审计，但仍然不绕过：

- `policy_suggestion`
- `Governor`
- `RiskPolicyService.evaluate("switch_parameter_template", ...)`

首批要求：

1. 因子属于当前已接入运行时参数覆盖链的名单
2. `formula_version` 不变
3. `factor_family` 不变
4. 模板角色属于：
   - `default`
   - `conservative`
   - `aggressive`
5. 数值参数相对变动不超过 35%

当前运行时已接线的 factor：

- `rsi_14`
- `stoch_k`
- `macd_hist`
- `adx`
- `ema_slope`
- `bb_width`
- `obv_slope`
- `vol_ma_ratio`
- `supertrend_str`
- `keltner_width`

---

## 2. `offline_deep`

以下情况默认归为 `offline_deep`：

- 因子尚未接入运行时参数覆盖链
- `formula_version` 变化
- `factor_family` 变化
- 参数跳变过大
- 模板角色超出当前受控集合

当前测试/治理基准里，`offline_deep` 已不再依赖“某个具体因子一定不可调”，也会用
`unsupported_template_role` 这类护栏原因来覆盖审批与灰度链路。

`offline_deep` 的含义是：

- 可以生成建议
- 可以入审批链
- 但不应直接当作“在线轻调”执行
- 应先补回测 / walk-forward / 灰度验证证据

当前实现里，`offline_deep` 已不再只是约定：

- `create_switch_suggestion(...)` 会把边界判定写进 suggestion evidence
- `activate_template(...)` / `POST /api/learning/parameter-templates/apply-switch` 会正式阻断未经离线验证的 `offline_deep` 直接上线
- 只有 gray-release `release / rollback` 这种带离线证据的路径，才允许带着 `allow_offline_deep` 进入正式切换

---

## 3. 当前系统入口

当前已提供边界判定入口：

- `POST /api/learning/parameter-templates/suggest-switch`
- `POST /api/learning/parameter-templates/apply-switch`
- `POST /api/learning/parameter-templates/boundary-check`
- `POST /api/learning/parameter-templates/offline-validate`
- `GET /api/learning/parameter-templates/offline-candidates`
- `POST /api/learning/parameter-templates/offline-candidates/review`
- `POST /api/learning/parameter-templates/offline-candidates/release`
- `POST /api/learning/parameter-templates/offline-candidates/rollback`

返回核心字段：

- `recommended_scope`
- `reasons`
- `current_template`
- `target_template`

其中 `suggest-switch` 现在也会把以下字段写进 `policy_suggestion.evidence`：

- `boundary`
- `approval_path`

其中 `offline-validate` 会：

- 先复用边界判定
- 仅在 `recommended_scope=offline_deep` 时创建 `parameter_template_validation` job
- 先接入现有 backtest sweep 入口
- 生成 `purged walk-forward` 报告
- 把验证结果登记成 `pending_review` 的 gray-release candidate

`offline-candidates` 会列出这些待审 gray-release 候选，供后续审批链和前端继续接入。

学习页当前已开始展示：

- 参数模板建议的边界结论（在线轻调 / 离线深调）
- 边界原因的人话解释
- 建议应走的审批路径
- 从 factor card 参数可疑证据长出的模板推荐项
- 部分离线候选已可回看 recommendation 来源 trace
- lifecycle 时间线里的参数模板候选事件也已可回看 recommendation trace

当前 gray-release candidate 已支持三段式动作：

- `review`：把候选标记为 `approved / rejected`
- `release`：仅对 `approved` 候选执行正式模板切换，底层仍复用 `switch_parameter_template` 风控裁决
- `rollback`：对已发布候选回切到发布前模板

后续 E4 可以直接复用这个结果作为治理审批前置证据和离线验证入口。
