# Position Supervisor Contract

> Last updated: 2026-08-28
> Phase: C-H
> Status: governed supervisor execution active; per-position binding and selection projection implemented; evidence-qualified auto-selection wired (current projection remains `off` for insufficient evidence); historical/learning observation-only; model influence shadow/disabled

本文定义并固化 `position_supervisor` 的运行 contract。当前 Demo 与未来实盘共用
`governed_execute` 监督执行边界；`observation_only` 只适用于历史审计和 learning-shadow。
后续修改应保持本文的权力边界、证据结构和治理入口不变。当前尚无已应用的 supervisor
template mutation/effect，不能把已有监督记忆误认为模板已经自动改进。

---

## 1. 角色定位

`position_supervisor` 不是开仓信号生成器，也不是最终风控裁决层。

它的职责是：

- 持续观察一笔已经打开的仓位
- 判断当前 thesis 是否仍成立
- 判断继续持有是否还值得占用风险预算
- 输出结构化持仓动作建议
- 把建议及证据送交 `RiskPolicyService`

它不能直接：

- 开新仓
- 绕过风控直接平仓
- 绕过 `RiskPolicyService`
- 直接提高任何硬风控上限

一句话定义：

**`position_supervisor` 负责理解“这笔仓位现在还值不值得继续活着”，`RiskPolicyService` 负责拍板。**

---

## 2. 触发时机

### 2.1 必跑场景

- live loop 每次拿到最新持仓快照后，对每个活跃仓位运行一次
- 持仓出现显著状态变化时立即重跑：
  - 浮盈创新高后明显回吐
  - 浮亏扩大
  - 持仓时长跨过 watch / expired 阈值
  - thesis / regime 判断变化
- 手动或恢复链路准备平仓前，允许补跑一次，作为 close 审计证据

### 2.2 当前建议频率

- 默认按 live loop tick 运行
- 同一仓位允许做轻量去抖，避免每个 tick 都重复写 ledger
- 只有当 supervisor 结论或证据等级发生变化时，才要求写入新的 decision event

---

## 3. 输入 Contract

建议统一接口：

```python
PositionSupervisor.evaluate(position_context: dict[str, Any]) -> PositionSupervisorVerdict
```

### 3.1 顶层输入结构

```python
{
  "position": {...},
  "market": {...},
  "risk": {...},
  "temporal_context": {...},
  "market_space_context": {...},
  "entry_context": {...},
  "runtime": {...},
}
```

### 3.2 `position`

至少包含：

- `position_id`
- `trade_id`
- `symbol`
- `direction`
- `entry_price`
- `current_price`
- `volume`
- `opened_at`
- `unrealized_pnl`
- `realized_pnl`
- `stop_loss`
- `take_profit`

### 3.3 `market`

至少包含：

- `bid`
- `ask`
- `mid`
- `spread`
- `timeframe`
- `timeframe_seconds`
- `regime_state`
- `volatility_state`

### 3.4 `risk`

至少包含：

- `risk_snapshot`
- `policy_state`
- `max_holding_bars`
- `max_holding_seconds`
- `open_position_count`
- `total_api_volume`

### 3.5 `temporal_context`

沿用并扩展现有 Phase B 口径，至少包含：

- `decision_ts`
- `session_label`
- `hour_utc`
- `weekday_utc`
- `holding_seconds`
- `holding_minutes`
- `holding_bars`
- `seconds_since_last_trade`
- `bars_since_last_trade`

### 3.6 `market_space_context`

这一层在 Phase C 先允许是轻量版，至少预留：

- `distance_to_sl`
- `distance_to_tp`
- `atr_multiple_from_entry`
- `range_location`
- `structure_bias`

### 3.7 `entry_context`

从 open ledger / factor snapshot 恢复，至少包含：

- `entry_decision_id`
- `entry_score`
- `entry_reason`
- `factor_set_version`
- `policy_version`
- `expected_holding_profile`
- `entry_regime`
- `entry_regime_confidence`

### 3.8 `runtime`

至少包含：

- `loop_running`
- `bridge_connected`
- `data_quality_state`
- `runtime_health`

---

## 4. 核心派生字段

`position_supervisor` 在 Phase C 后续子任务里必须稳定产出下面这些字段，C1 先把口径定住：

- `mfe`
- `mae`
- `giveback_ratio`
- `profit_capture_ratio`
- `time_in_profit`
- `holding_efficiency`
- `time_decay_score`
- `thesis_status`
- `regime_shift`

### 4.1 字段口径约定

- `mfe`: 持仓期间最大浮盈
- `mae`: 持仓期间最大浮亏
- `giveback_ratio`: 当前浮盈相对 `mfe` 的回吐比例
- `profit_capture_ratio`: 已实现或当前保留利润 / `mfe`
- `time_in_profit`: 持仓期间处于浮盈状态的时间占比或秒数
- `holding_efficiency`: 单位持仓时间对应的收益效率评分
- `time_decay_score`: thesis 随持仓时间衰减后的质量分
- `thesis_status`: `intact / weakening / broken`
- `thesis_break_ready`: thesis broken 已超过模板最小证据窗且效率低，但不代表一定可 full close
- `thesis_break_confirmed`: thesis broken 已获得强确认；强确认包括接近止损、regime confirmed、time decay、连续 broken 计数或信号反转
- `thesis_broken_confirmations`: 连续 thesis broken 证据计数，缺失时按 0 处理
- `signal_reversal`: 入场方向信号是否出现明确反转
- `regime_shift`: `none / mild / confirmed`

### 4.2 动作有效性边界

模板只提供策略参数，不能绕过监督智能体的基础证据门。所有模板、治理切换后的模板以及
模型建议都必须遵守：

- 盈利回吐类 `tighten / reduce` 必须同时满足价格、PnL、路径指标已知，并至少完成一根已收盘 bar；thesis-break 仍遵守模板声明的更长观察窗；
- `mfe` 必须达到内置 baseline 模板现有 `capture_policy.mfe_capture_failure_threshold`
  的非微量盈利底线（当前为 `0.15`）。治理模板的同名字段只能把底线抬高，不能降低；
- 未满足时统一输出 `hold`，并使用 `profit_protection_evidence_pending`，不进入 RiskPolicy，也不触达 broker；
- 模型只能在 `evidence.model_action_boundary_ready=true` 时把 `hold` 提升为 `tighten / reduce`；
- `reduce` 还必须通过 broker 最小手数和步进规划，最终动作以 `effective_action` 为准。

---

## 5. 输出 Contract

建议统一返回：

```python
{
  "position_id": "268046003",
  "decision_ts": 1760000000.0,
  "action": "tighten",
  "confidence": 0.78,
  "severity": "warn",
  "thesis_status": "weakening",
  "regime_shift": "mild",
  "summary_reason": "profit_giveback_after_mfe",
  "evidence": {...},
  "recommended_controls": {...},
  "requires_risk_verdict": true,
}
```

### 5.1 标准动作集

- `hold`: 继续持有，不建议修改保护
- `tighten`: 建议收紧止损、缩短容忍空间、提高保护级别
- `reduce`: 建议降低仓位规模
- `close`: 建议主动平仓

### 5.2 建议证据 `evidence`

至少包含：

- `holding_seconds`
- `holding_timeout_ratio`
- `mfe`
- `mae`
- `giveback_ratio`
- `profit_capture_ratio`
- `time_in_profit`
- `holding_efficiency`
- `time_decay_score`
- `thesis_status`
- `regime_shift`
- `trigger_tags`

### 5.3 执行建议 `recommended_controls`

用于给后续执行层和风控层传递可执行意图，至少支持：

- `target_stop_loss`
- `target_take_profit`
- `reduce_fraction`
- `close_reason`
- `protection_mode`

### 5.4 自适应监督输出与市场状态机

live 与 parity replay 共用同一套市场上下文构造：输入来自
`PortfolioCompositor._build_context_state()`，regime 由
`resolve_market_regime()` 解析。上下文必须保留以下事实：

- `trend_strength_state/score`
- `volatility_state/score`
- `event_window_state/score`
- `session_state`
- `regime_id/confidence/source/dimensions`

`atr_multiple_from_entry` 只有 canonical factor frame 明确提供真实价格空间 ATR 时才有效；
`range_location`、`structure_bias` 缺少事实源时保持 `None/unknown`，不能用 `0.0` 伪装有效。
MFE、MAE、giveback、profit capture 和 time-in-profit 仍只由
`position_metrics.update_position_path_metrics()` 累计写入。

监督 posture 固定为：

- `unknown_observe`：市场维度缺失、过期或只有低置信度 session fallback；只观察；
- `trend_hold`：强趋势且 thesis 未确认破坏；普通 near-TP、giveback 和 time decay 不收紧；
- `range_capture`：震荡/弱趋势且路径证据成熟；允许既有 profit protection 建议；
- `transition_confirming`：regime/thesis 变化尚未完成闭合 bar 确认；只观察；
- `exit_commit`：硬风险、timeout、确认反转或确认 thesis break；走既有 close/RiskPolicy 链路。

优先级固定为硬风险/timeout/确认退出，其次才是 posture-owned 的自适应保护。
高波动只影响闭合 bar 确认窗口，不直接等价于更快收紧；未确认 thesis break 继续 hold。

输出中的 `recommended_action` 是状态机原始建议，`requested_action` 是进入执行规划的请求，
`effective_action` 是最终允许送入 RiskPolicy 的动作。active template 只接受
`risk_boundary.adaptive_execution_mode=governed_execute`；`observation_only` 只表示历史记录或
learning-shadow 审计值，不得作为新的 live trace 执行边界。真实执行仍严格限定为
`stage=executed AND outcome=applied`，此时才可有 `execution_class=applied` 与
`is_real_execution=true`。

---

## 6. 与 `RiskPolicyService` 的关系

### 6.1 权力边界

- `position_supervisor` 只有建议权
- `RiskPolicyService` 保留最终裁决权
- 所有高影响动作都必须形成 `RiskVerdict`

### 6.2 Phase C 推荐动作映射

```text
supervisor.action = hold
  -> 不调用执行动作
  -> 可选写入 advisory evidence

supervisor.action = tighten
  -> 调用 RiskPolicyService.evaluate("tighten_position", context)

supervisor.action = reduce
  -> 调用 RiskPolicyService.evaluate("reduce_position", context)

supervisor.action = close
  -> 调用 RiskPolicyService.evaluate("close_position", context)
```

### 6.3 C1 对风控接口的结论

当前 `RiskPolicyService` 已正式支持：

- `open_trade`
- `close_position`
- `tighten_position`
- `reduce_position`
- `switch_position_supervisor_template`
- 治理类 action

当前约束：

- `tighten / reduce / close` 必须先形成 supervisor verdict，再交给 `RiskPolicyService`
- 风控拒绝时只写审计，不执行 broker 修改
- broker amend / close 失败时必须写入 lifecycle，不得把 action 记成成功
- supervisor 模板切换必须来自可审计治理建议；Demo 与未来实盘共用同一 V16/Admission/RiskPolicy/Coordinator
  mutation service，但普通持仓监督执行不依赖 `apply_demo` 或学习周期显式 apply。只要 active template
  已通过既有 authority，普通 `tighten/reduce/close` 就统一进入 governed supervisor executor；
  `autonomy_demo_auto_apply` 只控制治理 mutation 的应用，不授予或撤销 broker 执行权。

### 6.4 风控上下文扩展要求

后续 supervisor -> risk 的 context 至少应新增：

- `supervisor_action`
- `supervisor_confidence`
- `supervisor_reason`
- `supervisor_evidence`
- `supervisor_decision_ts`
- `entry_decision_id`
- `position_supervisor_template_id`
- `previous_template_id / target_template_id`（仅模板切换）

---

## 7. Ledger / Trace 写入要求

### 7.1 canonical decision event

supervisor 结论进入 `canonical_v2.event`，`entity_type=risk_decision`，建议 event_type 使用：

- `supervisor_hold`
- `supervisor_tighten`
- `supervisor_reduce`
- `supervisor_close`

写入规则：

- `action_reason`: 使用 supervisor 的结构化主原因，如 `profit_giveback_after_mfe`
- `action_json`: 写 supervisor verdict 原文
- `risk_state_json`: 如果已进入 `RiskPolicyService`，写入 `policy_verdict`
- `position_id` / `trade_id`: 必填

### 7.2 canonical position lifecycle

执行层真正落动作后，再写 `canonical_v2.event`，`entity_type=position_transition`：

- `tightened`
- `reduced`
- `closed`

其中 `details_json` 必须保留：

- `supervisor_action`
- `supervisor_reason`
- `risk_verdict_reason`
- `applied_controls`

### 7.3 canonical position supervisor trace

监督轨迹写入 `canonical_v2.event`，`entity_type=supervisor_trace`，是 supervisor 自治学习的永久事实。

它和 `risk_decision` event 的职责不同：

- `risk_decision` event 记录进入正式决策账本的 supervisor 动作；
- `supervisor_trace` event 记录每一次 supervisor 对仓位的处理结果，包括 `hold`、冷却跳过、风控拒绝、执行跳过、执行成功、执行失败和异常。

每条 trace 至少保留：

- `position_id / trade_id / decision_id`
- `trace_integrity`
- `config_version / config_hash / evolution_run_id`
- `action / summary_reason / confidence`
- `template_id / template_version`
- `stage / outcome`
- `risk_action / risk_allowed / risk_reason`
- `execution_status / execution_reason`
- `context_json`
- `verdict_json`
- `risk_verdict_json`
- `execution_json`

当请求动作无法执行时，trace 还必须区分：

- `requested_action`: supervisor 原始建议；
- `effective_action`: 经证据门和 broker 可执行性规划后的最终动作；
- `recommended_action`: 保留原始建议，不能被误读为 broker 已执行。

平仓后的学习样本还必须区分动作事实和后验建议：

- `observed_action`: 实际发生的 supervisor 动作；
- `action_semantics=counterfactual_recommendation`: 有成熟反事实证据时，`recommended_action` 才表示后验建议；
- `action_semantics=observed_action_without_counterfactual`: 没有反事实证据时，`recommended_action` 只是安全的临时占位，不是“原动作应该改成 hold”的结论；
- `counterfactual_status=available/unproven`、`recommended_action_provisional=true/false`：明确证据是否已经证明建议。

标准 `stage / outcome` 口径：

- `evaluated / hold`
- `learning_shadow / shadow`（learning worker 对已平仓事实的 recovered 非执行回放；绑定 candidate suggestion ID，不是 live 执行）
- `canary_shadow / shadow`（仅历史兼容读取；新 live loop 不再生产，不能满足 readiness/auto-unfreeze）
- `cooldown_skipped / skipped`
- `no_op_suppressed / skipped`（目标保护已生效；同一持久化 action fingerprint 只记录首次）
- `timeout_delegated / skipped`
- `risk_rejected / blocked`
- `execution_skipped / skipped`
- `executed / applied`
- `execution_failed / failed`
- `exception / failed`

展示和 API 消费方不得只根据 `action=tighten/reduce/close` 判断动作已经执行。`execution_json` 统一提供：

- `execution_class=applied/shadow/skipped/blocked/failed/observed`
- `is_real_execution=true` 仅允许出现在 `stage=executed AND outcome=applied`
- `recommended_action` 保留 supervisor 建议动作；shadow 建议还在 `shadow_recommendation` 中显式保留

Demo/实盘共用同一监督执行边界：

- active template 的普通 `tighten/reduce/close` 都必须走 supervisor verdict -> RiskPolicy -> broker ->
  lifecycle -> fresh reconcile -> trace；风控拒绝、未知回执、broker 失败或对账不完整不得记为 applied；
- `observation_only/superseded` 只保留为历史审计或 learning-shadow 记录，不再进入新的 live trace、
  `recently_applied` 或 broker cooldown；历史非真实 trace 通过幂等 repair terminalize 为
  `label_status=excluded`、`train_weight=0`、`allowed_uses=[audit, explainability]`；
- `recovery_position_state.recovery_meta` 继续记录 posture、episode、闭合 bar、fingerprint 和执行 class，
  但去重不能把真实执行降级为 observation，也不能把 observation 重复送回 pending。

`legacy_awe_trailing` 已从 active protection cycle 和 candidate writer 退役。旧 trace、close attribution
和 parity replay 仍可读取；generic executor 对新的 legacy candidate 明确拒绝，不生成新决策/trace，也不调用
RiskPolicy/broker。它不是 Demo 或未来实盘的备用执行链。

`tighten` 在进入 `RiskPolicyService` 前先比较 broker 当前 SL 与计划 SL。目标已经达到或没有形成更严格保护时，不创建 decision ledger、不调用 RiskPolicy、不触达 broker；首次写 `no_op_suppressed` trace，并把动作目标 fingerprint 保存到 `recovery_position_state.recovery_meta_json`，后续相同目标直接去重。目标变化或当前 SL 重新变得可收紧时会恢复正常风控和执行链路。

这张表服务于后续自治闭环：

- 识别同一类 supervisor 错误是否重复发生；
- 将 `premature_tighten / protection_too_tight / late_exit` 与当时的动作和模板绑定；
- 形成 `supervisor_execution_trace` 学习样本 (`autonomous_learning_sample.sample_type`，不是独立表)；
- 支持自动降权、模板调整、冷却窗口调整和回滚观察。

`supervisor_execution_trace` 只有真实 `stage=executed AND outcome=applied` trace 才进入 supervisor maturity；
非真实/观察/失败 trace 通过幂等 repair 记为 `label_status=excluded`、`train_weight=0`，仅允许 audit/explainability。
真实结果可进入按消费者隔离的 outcome learning；只有存在同一 decision_id 的真实 trace、完整 broker lifecycle/reconcile
和无硬污染 review 时，才可升级为 `supervisor_counterfactual`，并且 governance mutation 仍要求完整成熟且无污染证据。

系统支持三类 trace 来源：

- live trace：由 `live_service -> DecisionLedger.log_position_supervisor_trace()` 写入 canonical event，默认 `trace_integrity=full`
- candidate observation：由 learning worker 的 `materialize_position_supervisor_candidate_observations()` 基于已平仓 outcome 与成熟 counterfactual 回放，固定 `stage=learning_shadow / execution_status=observation_only / trace_integrity=recovered`；不调用 broker，也不进入 live action arbitration
- 历史 recovered trace：只从已明确标记的历史审计输入回填到 canonical event，标记为 `recovered / partial`；不再读取或重建旧事实表。

candidate observation 与 legacy trace 只用于审计/弱监督；前者必须与当前 suggestion ID 精确绑定后才可作为 candidate readiness 的一项输入，二者都不构成 live 控制授权。

### 7.4 supervisor trace 成熟化

`mature_position_supervisor_traces()` 会把 canonical `supervisor_trace` 与 `counterfactual_review` event 对齐，生成或更新 `sample_type=supervisor_execution_trace` 的学习样本。

统一标签口径：

- `protection_too_tight / premature_tighten / noise_stopout` -> `over_protected`
- `correct_stop` -> `correct_action`
- `missed_protection` -> `missed_protection`
- 证据不足 -> `inconclusive`

统一推荐动作：

- `hold`
- `tighten`
- `reduce`
- `close`
- `less_tighten`

成熟化规则：

- 有成熟 counterfactual 且 trace integrity 足够时，`label_status=matured`
- 证据不足时保持 `pending`
- `trace_integrity=recovered / partial` 自动降权
- `trace_integrity=missing` 权重为 0

### 7.5 trade trace

`/api/risk/trade-trace` 后续需要能同时回答：

- supervisor 最后一次建议是什么
- 风控是否批准
- 最终实际是否执行
- 如果没执行，是被风控拒绝、broker 失败，还是仍在等待

因此 trade trace 需要把 supervisor verdict 作为一等证据，而不只是 review 附注。

### 7.6 review / learning 写入要求

平仓复盘必须额外写入：

- `close_reason_source`
  - `supervisor_direct_close`
  - `supervisor_tighten_stopout`
  - `supervisor_reduce_partial_or_stopout`
  - `external_broker_close`
  - `restart_replay`
- `inferred_close_supervisor`
- `attribution_integrity`
  - `full`
  - `recovered`
  - `missing`

`attribution_integrity=missing` 的样本只能作为退出质量 / supervisor 学习证据，不应直接触发强因子降权。

恢复回放必须把两个含义分开：

- `recovery_observation_reason=position_missing_after_recovery_reconcile` 或
  `broker_position_not_found` 只表示恢复时观察到 broker 仓位消失；
- `close_reason` / `close_reason_source` 表示真实交易关闭原因。若有持久化的 supervisor close reason，优先使用
  `supervisor_direct_close`；否则只有保护单与权威成交匹配时才使用 `broker_close`；无法证明时保留
  `restart_replay`。不得用恢复观测原因覆盖交易原因。

---

## 8. supervisor 模板治理

当前内置模板：

- `position_supervisor:default.v1`
- `position_supervisor:conservative.v1`
- `position_supervisor:profit_protection.v1`

全局模板治理流程（只改变默认基线，不改写已经绑定的仓位）：

```text
supervisor review / counterfactual
  -> build_position_supervisor_advisories
  -> policy_suggestion(scope_type=position_supervisor_template)
  -> autonomous governance / manual override audit
  -> RiskPolicyService.evaluate("switch_position_supervisor_template", ...)
  -> RuntimeConfig.position_supervisor_template_id
  -> runtime_config_overlay / runtime_config_snapshot
  -> learning_application_log / learning_application_effect
```

记忆驱动的单仓选择流程独立于全局模板 mutation：

```text
canonical trace/counterfactual + experience/brain/posterior
  -> position_supervisor_governance
  -> runtime_kv[position_supervisor_selection.v1]
  -> 开仓成交前绑定，或稳定 regime 边界上的单仓切换
```

`position_supervisor_selection.v1` 只有现有 learning worker 的治理阶段可以生成和写入；live
backend 只读。普通 tick 不重新查询记忆，也不重新选择模板。

约束：

- `proposed` 建议不能直接切 live 模板
- 只有 `auto_approved` / `approved` 且通过风控的建议可以申请切换
- 模板 ID 必须来自内置模板列表或可从 evidence/application 恢复的生成型候选
- 自动部署只允许 `RuntimeConfig.autonomy_mode=demo_autonomous`
- 自动部署必须同时具备 replay summary 和 counterfactual summary
- 切换必须保留 `previous_template_id`，便于回滚审计
- 切换必须通过 `RuntimeConfigMutationService` 写入 `runtime_config_overlay / runtime_config_snapshot / evolution_decision / learning_application_log / learning_application_effect`
- 只调用 `config.runtime_config.patch()` 或只写 `runtime_config_snapshot` 属于临时内存变更，重启后不应被视为 active template
- 生成型 supervisor 模板必须能从 `policy_suggestion.evidence_json` 或已应用的 `learning_application_log.details_json` 恢复；active template ID 指向孤儿模板时应视为治理链路缺口

生成型候选不得由少量样本直接生成完整多阈值模板。每个 candidate 只能声明一个
`control` 和一个 `regime_stratum`，evidence 必须同时包含 base template 快照、单字段
`candidate_patch`、`generation_context`、replay summary 和 counterfactual summary。
候选模板可以保存完整恢复快照，但必须能证明控制区只有一个 scalar diff；缺少 candidate ID、
generation context、V16 bridge 或上述证据时只能 observation/superseded。approved suggestion
不等于 applied，实际生效仍以 application log、effect log、`applied_mutation_id` 和 V16
finalize 为准；普通 learning candidate 不能自动打开 `governed_execute`。

### 8.1 单仓策略绑定

模板是全局默认基线，不再是存量仓位唯一的实时策略来源。开仓选择一次、成交后绑定，绑定
存放在现有 `entry_protection_plan.supervisor_binding`，并随
`recovery_position_state.recovery_meta_json` 恢复，不新增表、服务或调度器。

绑定必须是 `position_supervisor_binding.v1`，至少包含：

```json
{
  "template_id": "position_supervisor:default.v1",
  "template_version": "default.v1",
  "template_hash": "sha256(normalized_template)",
  "template_snapshot": {},
  "binding_source": "static_baseline | governed_global_baseline | governed_selection_projection",
  "selection_status": "bound",
  "selection_key": {"symbol": "", "timeframe": "", "entry_regime": "", "current_regime": ""},
  "bound_at": 0,
  "posterior_fingerprint": "",
  "evidence_refs": {}
}
```

`template_snapshot` 是完整规范化模板；hash 使用规范化 JSON 计算。重启、恢复和每次监督评估
都要重新校验 ID、版本、快照和 hash：

- 缺失快照的旧仓位明确标为 `legacy_global_fallback`，不能补造历史绑定；
- 快照损坏、hash 不一致或来源不明时软策略进入 `unknown/hold`，硬风险收口仍继续；
- 全局模板变化只影响新仓位的默认基线，不改写已绑定仓位；
- 普通 `hold/tighten/reduce/close` 使用绑定快照，但最终动作仍必须通过 Safety/RiskPolicy。

### 8.2 受控选择与边界切换

`runtime_kv[position_supervisor_selection.v1]` 是唯一的记忆到 live 的选择投影。候选必须同时
满足当前 Coordinator mutation、完整干净成熟反事实、`causal_scope=supervisor`、同一模板
ID/version/hash、有效 application/effect 和既有 canary 门槛；`proposed`、`approved`、单独的
`brain_memory` 或 `inconclusive` 后验不能进入 live。没有合格候选、证据冲突或效果相同都返回
`no_change`，使用默认基线而不伪装成记忆判断。

当前配置字段及默认值为：

```text
position_supervisor_auto_selection_mode = off
position_supervisor_switch_min_stable_bars = 2
position_supervisor_switch_cooldown_bars = 3
position_supervisor_max_switches_per_position = 2
position_supervisor_selection_max_age_seconds = 900
```

允许的模式为 `off | shadow | demo_execute | live_execute`。`off` 只是没有合格证据时的安全
启动基线，不是等待人工打开的开关：learning worker 发现投影达到资格后，会自动通过既有
V16、RiskPolicy 和 Coordinator 切到有界 Demo。当前 `live_execute` 不准入；不再额外增加
人工审批闸门或第二套开关。

新仓位只在成交前读取一次选择结果。已有仓位只有在以下条件同时满足时才允许切换：状态变化、
连续达到配置的已收盘 bar 数、无未完成 broker intent/reconcile、价格/PnL/路径/市场上下文
均 known、无硬风险或优先风险收口、投影新鲜且匹配、未超过单仓次数且不在冷却期。切换只更新
该仓位 binding，保留旧 binding、写一条 `policy_switch` trace，下一次监督评估才使用新模板；
不修改全局 RuntimeConfig，也不触达 broker。

### 8.3 监督 trace 与后验引用

绑定存在时，每条新 trace 必须能回答：使用哪个模板、来自哪里、当时状态是什么、建议动作与
最终动作分别是什么、RiskPolicy 是否批准、broker 是否确认、reconcile 是否新鲜，以及是否为
no-op/cooldown/失败/superseded。至少保留 `template_id/template_version/template_hash`、
`binding_source`、`selection_event_id`、`current_regime`、`supervisor_posture`、
`requested_action/effective_action/applied_action`、`risk_policy_result`、
`broker_execution_result`、`reconcile_result` 和 `no_change_reason`。

平仓后的 counterfactual、training sample、`experience_memory`、`brain_memory` 和 effect 只能
携带同一绑定引用继续后验；旧数据不补造模板信息，也不能参与新模板自动准入。

### 8.4 监督经验与系统记忆

“进入记忆”分为四个层次，不能互相冒充：

1. canonical `supervisor_trace` / `counterfactual_review`：原始动作和后验事实；
2. `supervisor_execution_trace` / `supervisor_trajectory`：带证据资格的学习样本；
3. `experience_memory`：交易 lesson 投影，主要用于经验检索，不等于监督模板变更；
4. `brain_memory` / `posterior_arbitration`：有界、可重建的检索和最终后验索引，只能提供
   memory/critic/context，不能直接授权 live 动作。

只有完整、成熟、无污染且通过对应消费者资格的证据，才能进入 supervisor governance；
真正改变模板仍必须经过 policy suggestion、V16、RiskPolicy、Coordinator、application/effect
和可回滚的配置投影。记忆存在不等于策略已生效。

---

## 9. 与现有系统的最小落地关系

基于当前代码，稳定复用以下入口：

- 持仓时长口径复用 `backend/services/live_service.py` 里的 `_build_close_position_risk_context()` 与 `_holding_summary_for_position()`
- 风控裁决入口继续统一走 `risk/policy_service.py`
- 证据存储继续复用 `backend/ledger/service.py`
- 运维查询继续复用 `backend/api/risk.py` 的 `trade-trace`
- supervisor 反事实审计走 `backend.services.supervisor_counterfactual`
- supervisor 反事实/observation 调度走 `backend.services.supervisor_learning_scheduler`；该调度器不自动 materialize legacy advisory

这意味着后续不应重造平行 supervisor 链路，只应扩展这些既有骨架。

---

## 10. 当前完成标准

当前 contract 视为满足，当且仅当：

- 模块职责清楚
- 输入输出 schema 清楚
- 与 `RiskPolicyService` 的边界清楚
- ledger / trace 写入要求清楚
- review / learning 能回答“谁平的、该不该平、系统是否学到了”
- 每个存量仓位的策略来源、模板版本和快照 hash 清楚，模板切换不会无意改写存量仓位
- 记忆事实、学习资格、治理应用和 live 生效状态可以分别核验
- 模板切换必须有自治审计、可回滚配置快照和风控裁决

---

## 11. 后续扩展入口

后续扩展优先顺序建议为：

1. 为每个新仓位绑定可恢复的监督策略快照
2. 增加真实 `tighten / reduce / timeout` 样本覆盖
3. 提升 canonical `counterfactual_review` event 的标签置信度并闭合 application/effect
4. 增加受控 rollback / gray-release 展示
5. `time_decay_score / thesis_status / regime_shift`

后续快速反应应优先依赖当前事实、状态和证据等级，而不是继续堆叠不可解释的
`if/else` 阈值。事实缺失时保持 `unknown/hold`；经验只能通过有边界的后验治理改变
参数，不能直接改写执行权力。
