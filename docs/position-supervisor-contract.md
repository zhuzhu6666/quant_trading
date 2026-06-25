# Position Supervisor Contract

> Last updated: 2026-06-25
> Phase: C1
> Status: approved contract for implementation

本文定义 `position_supervisor` 的第一版 contract，用来指导 Phase C 后续编码落地。

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
- 直接平仓
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
- `regime_shift`: `none / mild / confirmed`

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

现状里 `RiskPolicyService` 只正式支持：

- `open_trade`
- `close_position`
- 治理类 action

因此 C1 明确：

- C4 需要为 `tighten_position` / `reduce_position` 新增正式 action
- 在 C4 完成前，supervisor 的 `tighten` / `reduce` 只能先作为 advisory verdict 存证，不能直接执行
- `close` 可以最先复用现有 `close_position` 通道

### 6.4 风控上下文扩展要求

后续 supervisor -> risk 的 context 至少应新增：

- `supervisor_action`
- `supervisor_confidence`
- `supervisor_reason`
- `supervisor_evidence`
- `supervisor_decision_ts`
- `entry_decision_id`

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

### 7.3 trade trace

`/api/risk/trade-trace` 后续需要能同时回答：

- supervisor 最后一次建议是什么
- 风控是否批准
- 最终实际是否执行
- 如果没执行，是被风控拒绝、broker 失败，还是仍在等待

因此 trade trace 需要把 supervisor verdict 作为一等证据，而不只是 review 附注。

---

## 8. 与现有系统的最小落地关系

基于当前代码，C1 先明确以下复用点：

- 持仓时长口径复用 `backend/services/live_service.py` 里的 `_build_close_position_risk_context()` 与 `_holding_summary_for_position()`
- 风控裁决入口继续统一走 `risk/policy_service.py`
- 证据存储继续复用 `backend/ledger/service.py`
- 运维查询继续复用 `backend/api/risk.py` 的 `trade-trace`

这意味着 C2-C5 不需要重造链路，只需要把 supervisor 补进现有骨架。

---

## 9. C1 完成标准

本 contract 在 C1 阶段视为完成，当且仅当：

- 模块职责清楚
- 输入输出 schema 清楚
- 与 `RiskPolicyService` 的边界清楚
- ledger / trace 写入要求清楚
- 能直接指导 C2 / C3 / C4 编码，而不再依赖口头解释

---

## 10. C2 的直接入口

C2 将按本文 contract 补齐实际计算字段，优先顺序建议为：

1. `mfe / mae`
2. `giveback_ratio / profit_capture_ratio`
3. `time_in_profit / holding_efficiency`
4. `time_decay_score / thesis_status / regime_shift`

