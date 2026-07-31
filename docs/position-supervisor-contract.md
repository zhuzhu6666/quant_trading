# Position Supervisor Contract

> Last updated: 2026-06-30
> Phase: C-H
> Status: implemented contract, live governance enabled, autonomous trace learning foundation enabled

本文定义并固化 `position_supervisor` 的运行 contract。Phase C 的持仓监督主链已经落地；后续修改应保持本文的权力边界、证据结构和治理入口不变。

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
- supervisor 模板切换必须来自可审计治理建议；demo autonomous 下可自动批准/应用，人工入口只作为覆盖和追责

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

### 7.1 decision ledger

supervisor 结论进入 `decision_ledger`，建议 event_type 使用：

- `supervisor_hold`
- `supervisor_tighten`
- `supervisor_reduce`
- `supervisor_close`

写入规则：

- `action_reason`: 使用 supervisor 的结构化主原因，如 `profit_giveback_after_mfe`
- `action_json`: 写 supervisor verdict 原文
- `risk_state_json`: 如果已进入 `RiskPolicyService`，写入 `policy_verdict`
- `position_id` / `trade_id`: 必填

### 7.2 position lifecycle

执行层真正落动作后，再写 `position_lifecycle_event`：

- `tightened`
- `reduced`
- `closed`

其中 `details_json` 必须保留：

- `supervisor_action`
- `supervisor_reason`
- `risk_verdict_reason`
- `applied_controls`

### 7.3 position supervisor trace

`position_supervisor_trace` 是 supervisor 自治学习的永久轨迹表。

它和 `decision_ledger` 的职责不同：

- `decision_ledger` 记录进入正式决策账本的 supervisor 动作；
- `position_supervisor_trace` 记录每一次 supervisor 对仓位的处理结果，包括 `hold`、冷却跳过、风控拒绝、执行跳过、执行成功、执行失败和异常。

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

`tighten` 在进入 `RiskPolicyService` 前先比较 broker 当前 SL 与计划 SL。目标已经达到或没有形成更严格保护时，不创建 decision ledger、不调用 RiskPolicy、不触达 broker；首次写 `no_op_suppressed` trace，并把动作目标 fingerprint 保存到 `recovery_position_state.recovery_meta_json`，后续相同目标直接去重。目标变化或当前 SL 重新变得可收紧时会恢复正常风控和执行链路。

这张表服务于后续自治闭环：

- 识别同一类 supervisor 错误是否重复发生；
- 将 `premature_tighten / protection_too_tight / late_exit` 与当时的动作和模板绑定；
- 形成 `supervisor_execution_trace` 学习样本 (`autonomous_learning_sample.sample_type`，不是独立表)；
- 支持自动降权、模板调整、冷却窗口调整和回滚观察。

`supervisor_execution_trace` 样本默认 `label_status=pending`，不能直接作为强收益标签；只有平仓 review 与 counterfactual 成熟后，才允许升级为更高权重训练/治理证据。

系统支持三类 trace 来源：

- live trace：由 `live_service -> DecisionLedger.log_position_supervisor_trace()` 写入，默认 `trace_integrity=full`
- candidate observation：由 learning worker 的 `materialize_position_supervisor_candidate_observations()` 基于已平仓 outcome 与成熟 counterfactual 回放，固定 `stage=learning_shadow / execution_status=observation_only / trace_integrity=recovered`；不调用 broker，也不进入 live action arbitration
- legacy trace：由 `backfill_position_supervisor_traces()` 从历史 `decision_ledger` 回填，标记为 `recovered / partial`

candidate observation 与 legacy trace 只用于审计/弱监督；前者必须与当前 suggestion ID 精确绑定后才可作为 candidate readiness 的一项输入，二者都不构成 live 控制授权。

### 7.4 supervisor trace 成熟化

`mature_position_supervisor_traces()` 会把 `position_supervisor_trace` 与 `supervisor_counterfactual_review` 对齐，生成或更新 `sample_type=supervisor_execution_trace` 的学习样本。

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

---

## 8. supervisor 模板治理

当前内置模板：

- `position_supervisor:default.v1`
- `position_supervisor:conservative.v1`

治理流程：

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

约束：

- `proposed` 建议不能直接切 live 模板
- 只有 `auto_approved` / `approved` 且通过风控的建议可以申请切换
- 模板 ID 必须来自内置模板列表
- 自动部署只允许 `RuntimeConfig.autonomy_mode=demo_autonomous`
- 自动部署必须同时具备 replay summary 和 counterfactual summary
- 切换必须保留 `previous_template_id`，便于回滚审计
- 切换必须通过 `RuntimeConfigMutationService` 写入 `runtime_config_overlay / runtime_config_snapshot / evolution_decision / learning_application_log / learning_application_effect`
- 只调用 `config.runtime_config.patch()` 或只写 `runtime_config_snapshot` 属于临时内存变更，重启后不应被视为 active template
- 生成型 supervisor 模板必须能从 `policy_suggestion.evidence_json` 或已应用的 `learning_application_log.details_json` 恢复；active template ID 指向孤儿模板时应视为治理链路缺口

---

## 9. 与现有系统的最小落地关系

基于当前代码，稳定复用以下入口：

- 持仓时长口径复用 `backend/services/live_service.py` 里的 `_build_close_position_risk_context()` 与 `_holding_summary_for_position()`
- 风控裁决入口继续统一走 `risk/policy_service.py`
- 证据存储继续复用 `backend/ledger/service.py`
- 运维查询继续复用 `backend/api/risk.py` 的 `trade-trace`
- supervisor 反事实审计走 `backend.services.supervisor_counterfactual`
- 自动物化调度走 `backend.services.supervisor_learning_scheduler`

这意味着后续不应重造平行 supervisor 链路，只应扩展这些既有骨架。

---

## 10. 当前完成标准

当前 contract 视为满足，当且仅当：

- 模块职责清楚
- 输入输出 schema 清楚
- 与 `RiskPolicyService` 的边界清楚
- ledger / trace 写入要求清楚
- review / learning 能回答“谁平的、该不该平、系统是否学到了”
- 模板切换必须有自治审计、可回滚配置快照和风控裁决

---

## 11. 后续扩展入口

后续扩展优先顺序建议为：

1. 增加真实 `tighten / reduce / timeout` 样本覆盖
2. 提升 `supervisor_counterfactual_review` 的标签置信度
3. 增加受控 rollback / gray-release 展示
4. `time_decay_score / thesis_status / regime_shift`
