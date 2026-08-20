# API Fact Contract

> Status: active
> Last verified: 2026-08-13
> Scope: additive `fact.v1` provenance and freshness contract for public API and WebSocket read models.

本文只定义“这个值来自哪里、观测于何时、现在是否可信”，不改变各端点原有业务字段。运行与治理权力边界仍以 `system-source-of-truth.md` 为准。

## 1. 信封结构

已迁移端点在保留所有旧顶层字段的同时新增：

```json
{
  "_fact": {
    "envelope": "fact.v1",
    "contract": "live.account.v2",
    "state": "known",
    "source": "ctrader",
    "observed_at": 1784390400.0,
    "generated_at": 1784390401.0,
    "stale_after_sec": 30.0,
    "reason_code": null,
    "components": {}
  }
}
```

| 字段 | 固定语义 |
|---|---|
| `envelope` | 当前固定为 `fact.v1` |
| `contract` | 端点级契约 ID，客户端必须显式按它解析，不得递归搜索任意 `status/ok/items` |
| `state` | 只允许 `known/unknown/stale/error` |
| `source` | 真实观测或持久化来源；`none/unknown/unavailable/not_registered/degraded_cache` 都不是 known 权威源 |
| `observed_at` | 业务事实或持久化记录的观测时间，缺失代表 unknown；不得用请求生成时间伪装旧记录新鲜 |
| `generated_at` | 当次响应生成时间，只用于计算年龄 |
| `stale_after_sec` | 该 contract 的新鲜度上限 |
| `reason_code` | unknown/stale/error 的稳定机器可读原因 |
| `components` | 组合端点的子事实；子事实不会因顶层 `ok=true` 被自动提升为 known |

状态判定顺序为：显式源错误 → `error`；缺 `observed_at` 或不可用 source → `unknown`；超过 freshness → `stale`；其余才是 `known`。业务判断为 false（例如 readiness 未通过）可以是 known；它不等于数据源错误。

## 2. 默认新鲜度

| 事实类型 | `stale_after_sec` |
|---|---:|
| WS transport heartbeat | 5 秒 |
| `live.state.v2` 组合 state | 30 秒展示窗口；由 account/positions/loop 必需子事实聚合 |
| spot / account / positions / loop | 30 秒展示窗口 |
| `live.safety-freshness.v1` | 20 秒；安全心跳和新增风险准入硬门 |
| risk.inputs / live safety | 20 秒；风险输入与安全心跳共用同一后端事实门 |
| session / 风险性治理投影 | 30 秒 |
| system runtime health | 75 秒 |
| auto-recovery | 75 秒 |
| readiness / learning / ops 账本 | 180 秒 |
| market bars | 1800 秒；与 M15 月库质量门一致 |

个别端点可以更严，但必须在 `_fact.stale_after_sec` 中自描述，客户端不得另维护一套隐式时限。

展示 freshness 与安全 freshness 必须分开理解：`live.state.v2` 及其 account/positions/loop
子事实允许在 30 秒内继续展示最后一次真实 broker 对账；`live.safety-freshness.v1` 与
`risk.inputs.v1` 共用 20 秒后端事实门判断安全和风险输入是否可接受。前端不得用展示事实的 `known` 推导
`accepting_new_risk=true`，也不得用 WS 已连接替代安全心跳。

## 3. 核心运行端点

| 端点/输送 | `_fact.contract` |
|---|---|
| `GET /api/health` | `system.health.v2` |
| `GET /api/system/db-health` | `system.db-health.v2` |
| `GET /api/live/status` | `live.status.v2`，components 含 broker/account/positions/loop |
| `GET /api/live/loop-status` | `live.loop.v2` |
| `GET /api/live/account` | `live.account.v2` |
| `GET /api/live/positions` | `live.positions.v2` |
| `GET /api/live/strategy-status` | `live.strategy.v2` |
| `GET /api/live/session-stats` | `live.session-risk.v2` |
| `GET /api/live/realized-pnl-series` | `live.realized-pnl.v2` |
| WebSocket state snapshot | `live.state.v2`，components 含 account/positions/loop/safety/spot/session/strategy/risk_inputs/risk_health |
| `GET /api/risk/summary` | `risk.summary.v2`，components 含 `system.runtime-health.v1` 和 `risk.inputs.v1` |
| `GET /api/risk/policy/verdicts` | `risk.policy-verdicts.v2`；成功 PostgreSQL 查询的 `observed_at` 是本次读取时间，item `decision_ts` 只表示事件发生时间，历史长期无新事件不得使当前列表变 stale；`items` 只含已到 `RiskPolicyService` 的裁决，`pre_policy_skips` 单独投影 `decision_ledger` 中 `skip_stage=before_candidate` 且 `risk_policy_reached=false` 的开仓前置拦截，包含 `admission_owner`、`blockers`、`execution_intent_created`，不计入政策允许/拦截统计 |
| `GET /api/risk/trade-trace/recent` | `risk.trade-trace-recent.v2` |
| `GET /api/sync/status` | `ops.sync-status.v2` |
| `GET /api/ctrader/token-status` | `ops.ctrader-token-status.v2` |
| `GET /api/data/external-status` | `ops.external-data-status.v2` |
| `GET /api/v4/catalog` | `factor.catalog.v4` |
| `GET /api/market/bars` | `market.bars.v1`；默认 `source=monthly` 使用 `bars_monthly`，显式 `source=live` 只读 cTrader bridge 的 `ctrader_live_trendbar` 在线缓存；两者都用最近一根 K 线作为 `observed_at`，实时源不可用时为 `unknown/ctrader_live_trendbar_unavailable`，不回退月库 |
| `GET /api/ops/alerts` | `ops.alerts.v2`，components 含 `ops.alert-delivery.v1` |
| `GET /api/ops/recovery` | `ops.auto-recovery.v2` |
| `GET /api/ops/recovery/history` | `ops.auto-recovery-history.v2` |
| `GET /api/ops/backend-readiness` | `ops.backend-readiness.v2` |
| `GET /api/ops/autonomy/live-status` | `ops.live-autonomy-status.v2` |
| `POST /api/ops/autonomy/live-unlock/evaluate` | `ops.live-autonomy-unlock-evaluation.v2` |

account/positions 的 `observed_at` 与 reconcile ID 必须来自显式 fresh broker RPC；HTTP 读取和 cTrader push event 都不得刷新。push event 只进入 `event_projection` 子事实，供兼容展示和诊断，不能满足 startup、safety 或新增风险 admission。

live loop 的串行 broker owner 在空仓时也必须以 5 秒为目标周期完成 freshness 所需的显式对账，处理和 broker RPC 耗时计入该周期；本轮处理不得用 transport heartbeat 冒充完成事实。20 秒是安全对账、watchdog 和新增风险 admission 的硬门；桌面展示使用 30 秒窗口，避免短暂调度延迟制造“已连接但整屏过期”的假象。Web 端全应用只保留一个 `/ws/state` 连接；页面切换不得重建连接，每条消息都是完整快照。该连接由 canonical live state 写入事件驱动，不发送定时空快照，不做 HTTP fallback、旧快照合并或 live endpoint 轮询。WS 只有在真实断线或认证失败时才清空实时业务值，并按有界 backoff 重连；恢复后以第一条完整 WS 快照重新显示。非实时学习、治理和历史页面只在进入页面或用户明确刷新时读取各自 HTTP 事实。

cTrader bridge 的报价快照固定携带 `source=ctrader_spot`。有来源但超过 30 秒的桌面展示报价表现为 stale 并保留数值与时间；安全心跳或最终开仓事实准入在 20 秒内独立判断；只有从未收到报价或来源不可用时才是 unknown。不得用展示报价窗口放宽最终开仓事实准入。

Web 概览的 `system.health.v2` 只在页面进入或用户明确刷新时读取；不得通过轮询制造“接口正常/接口未知”周期抖动。交易运行态、账户、持仓、会话、策略和风险输入统一使用同一条 `/ws/state` 快照。

`risk.summary.v2` 是组件级 fail-closed 组合事实：`system.runtime-health.v1` 由每分钟健康检查产出，允许 75 秒新鲜度以覆盖调度抖动；`risk.inputs.v1` 与安全心跳共用 20 秒后端事实门。父级使用 75 秒自描述窗口，但任一组件为 `error/unknown/stale` 时必须投影为同类非 known 状态，不能用较宽的健康检查窗口掩盖风险输入过期。

`live.positions.v2` 进一步公开 `broker_reconcile.identity/protection/price/pnl` 四个 `fact.v1` 子事实：identity/volume/SL/TP 来自全量 position reconcile；current price 只来自真实 cTrader spot；PnL 来自独立 broker PnL RPC。桌面展示 freshness 为 30 秒，安全对账和最终新增风险 admission 仍独立使用 20 秒门。fresh 明确空仓不要求四个子组件并可保持 known；非空仓缺必需组件为 unknown，显式组件失败为 error；旧快照已经超过 30 秒时优先保持 stale 和原 `observed_at`。未知 price/PnL 不得用 entry price、账户差额或零值补齐，但 timeout、entry repair、close/reduce/tighten 仍可继续。

live loop 的同一串行 tick 只允许有一份 broker 持仓快照：`reconcile_positions()` 产生的完整 `PositionReconcileResult` 原样传给 `reconcile_account()`；当 PnL component 和每个 position 的 `pnl_state` 都明确为 `known` 时，账户复用该 PnL 总和，不再发第二次 `ProtoOAGetPositionUnrealizedPnLReq`。组件未知或失败仍走原 RPC/失效闭锁，不能用复用优化把未知提升为 known。

`live.loop.v2` 的 `observed_at` 只在串行 tick（包括风险指标更新）完整结束后推进；tick 开始、阶段切换和 transport heartbeat 都不是完成心跳。Safety heartbeat 仍通过独立的 `live.safety-freshness.v1` 作为 fail-closed 授权事实。因此“WS/经纪商已连接”与“账户/持仓事实待刷新”可以同时出现；展示可在 30 秒内保留最后一次真实事实，但安全心跳超过 20 秒必须阻止新增风险并显示安全门原因。

session 只有 `source` 为权威 `ctrader_deals*` 时才可 known；`degraded_cache` 必须 unknown。session 投影中的 `session_circuit_observation.triggered/reason/enforced` 区分“达到熔断阈值”和“实际阻断”：仅在 autonomy mode 与 broker 环境都确认是 Demo 时允许 `triggered=true, enforced=false`，且此时 `circuit_breaker=false`；非 Demo 必须保持 `triggered=true, enforced=true`。AutoRecovery status/history 读取都不得为取数而隐式构造或启动实例；未注册时固定为 `unknown/not_registered`，不得伪报健康。

## 4. Learning 端点级契约

`backend.services.learning_fact_views` 对每个读模型显式声明时间字段，不递归猜测 payload。

| 端点 | `_fact.contract` | 权威观测 |
|---|---|---|
| `GET /api/learning/suggestions` | `learning.suggestions.v2` | 最新 `reviewed_at/created_at` |
| `GET /api/learning/summary` | `learning.summary.v2` | `state_v1` 聚合源的最新持久化时间 |
| `GET /api/learning/reviews` | `learning.reviews.v2` | 最新 review 持久化时间 |
| `GET /api/learning/autonomous/samples` | `learning.autonomous-samples.v2` | 最新 sample 持久化时间 |
| `GET /api/learning/parameter-templates/active` | `learning.parameter-templates-active.v2` | 最新 `updated_at/activated_at` |
| `GET /api/learning/applications` | `learning.applications.v2` | 最新 `last_review_at/created_at/cycle_ts` |
| `GET /api/learning/lifecycle` | `learning.lifecycle.v2` | 最新 `ts` |
| `GET /api/learning/dataset/readiness` | `learning.dataset-readiness.v2` | 源表最新持久化时间 |
| `GET /api/learning/dataset/quality-health` | `learning.dataset-quality-health.v2` | 源表最新持久化时间 |
| `GET /api/learning/model/shadow-queue` | `learning.model-shadow-queue.v2` | 最新 `updated_at/created_at` |
| `GET /api/learning/model/canary-review` | `learning.model-canary-reviews.v2` | 最新 `created_at` |
| `GET /api/learning/model/inference` | `learning.model-inference-audits.v2` | `model_registry` 最新持久化时间 |
| `GET /api/learning/model/permissions/audits` | `learning.model-permission-audits.v2` | 最新 `created_at` |
| `GET /api/learning/model/position-quality-lightgbm/audits` | `learning.model-position-quality-audits.v2` | 最新 audit `created_at` |
| `GET /api/learning/model/open-quality-lightgbm/audits` | `learning.model-open-quality-audits.v2` | 最新 audit `created_at` |
| `GET /api/learning/model/factor-governance-lightgbm/audits` | `learning.factor-governance-lightgbm-audits.v2` | 最新 audit `created_at` |
| `GET /api/learning/model/factor-governance-lightgbm/advisories` | `learning.factor-governance-lightgbm-advisories.v2` | 继承源 audit 观测时间，不使用 advisory render 时间 |
| `GET /api/learning/model/offmarket-high-load/audits` | `learning.model-offmarket-high-load-audits.v2` | 最新 audit 持久化时间 |

权威查询证明结果集为空时，可在首次查询时记为 known empty；30 秒缓存重新渲染必须保留首次 `observed_at`，不得每次续鲜。非空记录缺持久化时间必须 unknown；返回 last-good 但当次源读失败时必须 error，旧业务字段仍保留。

## 5. Ops / governance 端点级契约

`backend.services.ops_governance_fact_views` 区分只读账本、持久化写结果和治理 mutation：

- 账本读只信显式领域时间或 item 时间；“没有记录”不会因 `ok=true` 变成当前事实。
- 对成功完成的只读 read-model 查询，endpoint 可以显式传入 `query_observed_at` 作为本次
  查询事实的观测时间；这只表示“查询已完成/当前结果为空或已读取”，不改变记录自身的
  `created_at/updated_at`，也不得把查询时间写回历史记录。
- 写结果只有返回可识别的 durable ID 和真实 commit 时间才是 known。
- 治理变更只有 `status=committed` 且存在 `mutation_id` 才是 known；legacy `applied/ok` 不是 committed 证据。本地 safety latch 作为独立 component 可在 PG mutation unknown 时仍然 known。
- 兼容端点若只返回进程内配置、请求时聚合或未回读的写结果，必须使用 `source=none + state=unknown + reason_code`；其中的 `generated_at/created_at` 不得直接充当事实时间。
- `backend/api/ops.py` 当前全部 68 个 HTTP 路由都必须经过端点级 `_fact` helper；除 `/backend-readiness` 自身外，禁止用 readiness `_fact` 替代其他端点的来源和时间。

| 端点 | `_fact.contract` |
|---|---|
| `GET /api/ops/autonomy/proposals` | `ops.autonomy-proposals.v2` |
| `GET /api/ops/autonomy/proposals/{proposal_id}` | `ops.autonomy-proposal.v2` |
| `POST /api/ops/autonomy/proposals/refresh` | `ops.autonomy-proposals-refresh.v2` |
| `POST /api/ops/autonomy/proposals/{proposal_id}/review` | `ops.autonomy-proposal-review.v2` |
| `GET /api/ops/brain/state` | `ops.v16-brain-state.v2` |
| `GET /api/ops/brain/memory` | `ops.v16-brain-memory.v2` |
| `GET /api/ops/brain/action-plans` | `ops.v16-action-plans.v2` |
| `GET /api/ops/brain/action-plan-evals` | `ops.v16-action-plan-evals.v2` |
| `GET /api/ops/brain/low-impact-executions` | `ops.v16-low-impact-executions.v2` |
| `POST /api/ops/brain/low-impact-executions/run` | `ops.v16-low-impact-execution-run.v2` |
| `GET /api/ops/brain/medium-impact-governance` | `ops.v16-medium-impact-governance.v2` |
| `POST /api/ops/brain/medium-impact-governance/materialize` | `ops.v16-medium-impact-governance-materialize.v2` |
| `GET /api/ops/brain/governance-candidate-reviews` | `ops.v16-governance-candidate-reviews.v2` |
| `POST /api/ops/brain/governance-candidates/review` | `ops.v16-governance-candidate-review-run.v2` |
| `GET /api/ops/brain/live-ready-guardrails` | `ops.v16-live-ready-guardrails.v2` |
| `POST /api/ops/brain/live-ready-guardrails/evaluate` | `ops.v16-live-ready-guardrail-evaluate.v2` |
| `POST /api/ops/brain/live-ready-guardrails/tighten` | `ops.v16-live-ready-guardrail-tighten.v2` |
| `GET /api/ops/incident-control` | `ops.incident-control.v2` |
| `POST /api/ops/incident-control` | `ops.incident-control-mutation.v2` |
| `GET /api/ops/incident-playbook/latest` | `ops.incident-playbook-latest.v2` |
| `GET /api/ops/autonomy-health/scope-approvals/latest` | `ops.autonomy-scope-approval-latest.v2` |
| `POST /api/ops/autonomy-health/scope-approvals` | `ops.autonomy-scope-approval-event.v2` |
| `GET /api/ops/autonomy-health/scope-enforcements/latest` | `ops.autonomy-scope-enforcement-latest.v2` |
| `POST /api/ops/autonomy-health/scope-enforcements` | `ops.autonomy-scope-enforcement.v2` |
| `GET /api/ops/v15/phase0` | `ops.v15-phase0-completion.v2` |
| `GET /api/ops/release/latest` | `ops.release-latest.v2` |
| `POST /api/ops/release/start` | `ops.release-start.v2` |
| `GET /api/ops/release/{run_id}/approvals` | `ops.release-approval-trail.v2` |
| `POST /api/ops/release/{run_id}/approvals` | `ops.release-approval-event.v2` |

其余 Ops 路由的端点级契约如下；表中兼容 unknown 不代表旧业务字段不可展示，只表示客户端不得把它用于绿色状态或放松控制：

| 端点 | `_fact.contract` | 权威边界 |
|---|---|---|
| `GET /api/ops/agent-authority` | `ops.agent-authority.v2` | 未暴露持久化观测，兼容 unknown |
| `GET /api/ops/agent-scorecard` | `ops.agent-scorecard.v2` | agent governance ledgers 最新活动时间 |
| `GET /api/ops/agent-briefing` | `ops.agent-briefing.v2` | briefing 中持久化 agent/experience item 时间 |
| `GET /api/ops/agent-trade-attribution` | `ops.agent-trade-attribution.v2` | `trade_outcome_review.created_at` |
| `GET /api/ops/agent-chain-health` | `ops.agent-chain-health.v2` | 聚合层未暴露来源时间，兼容 unknown |
| `GET /api/ops/autonomy/evolution-cycle` | `ops.autonomous-evolution-cycle.v2` | evidence/effect ledger 最新持久化时间 |
| `POST /api/ops/autonomy/evolution-cycle/run` | `ops.autonomous-evolution-nursery-run.v2` | run 未完成写后回读，兼容 unknown |
| `GET /api/ops/autonomy/demo-apply-plan` | `ops.autonomous-demo-apply-plan.v2` | request-time plan，兼容 unknown |
| `POST /api/ops/autonomy/demo-apply-step` | `ops.autonomous-demo-apply-step.v2` | durable evolution run ID + start/finish 时间 |
| `POST /api/ops/autonomy/live-unlock` | `ops.live-autonomy-unlock.v2` | committed governance mutation |
| `GET /api/ops/autonomy/governance-expansion-control` | `ops.governance-expansion-control.v2` | process projection 未暴露 commit 时间，兼容 unknown |
| `POST /api/ops/autonomy/governance-expansion-control` | `ops.governance-expansion-control-mutation.v2` | committed governance mutation |
| `POST /api/ops/autonomy/live-unlock/revoke` | `ops.live-autonomy-revoke.v2` | committed governance mutation；local latch 为独立 component |
| `GET /api/ops/brain/commands` | `ops.v16-brain-commands.v2` | `v16_brain_command.created_at` |
| `POST /api/ops/factor/pruning-governance/materialize` | `ops.factor-pruning-governance-materialize.v2` | 未写后回读，兼容 unknown |
| `POST /api/ops/factor/pruning-governance/promote-ready` | `ops.factor-pruning-governance-promote-ready.v2` | 未写后回读，兼容 unknown |
| `POST /api/ops/factor/pruning-governance/bridge-ready` | `ops.factor-pruning-governance-bridge-ready.v2` | 未写后回读，兼容 unknown |
| `GET /api/ops/factor/governance-effects` | `ops.factor-governance-effects.v2` | application/effect ledger item 时间 |
| `POST /api/ops/factor/governance-effects/reconcile` | `ops.factor-governance-effects-reconcile.v2` | reconcile 后 effect ledger item 时间 |
| `GET /api/ops/brain/governance-candidates` | `ops.v16-governance-candidates.v2` | candidate `updated_at/created_at`；状态同时区分 `reviewable_candidate_count` 与 `execution_pending_count`，pending bridge 不解释为 stale review |
| `POST /api/ops/brain/governance-candidates/{candidate_id}/submit` | `ops.v16-governance-candidate-submit.v2` | suggestion ID + candidate readback 时间；bridge 后 candidate 为 `bridge_pending`，approved 后为 `awaiting_execution`；blocked 为 unknown |
| `GET /api/ops/replay/latest` | `ops.replay-latest.v2` | `replay_report.created_at` |
| `POST /api/ops/replay/run` | `ops.replay-run.v2` | durable replay report ID + 时间 |
| `POST /api/ops/replay/bar-run` | `ops.replay-bar-run.v2` | durable replay report ID + 时间 |
| `POST /api/backtest/run` | 无 `fact.v1`；返回持久任务 ID | 唯一 Parity 历史回测入口；任务结果只返回指标、样本计数与工件位置，完整交易/事件/训练样本保存在已校验回放工件 |
| `POST /api/ops/replay/bar-preview` | `ops.replay-bar-preview.v2` | 明确不持久化，兼容 unknown |
| `GET /api/ops/replay/bar-decisions` | `ops.replay-bar-decisions.v2` | `decision_ledger.decision_ts`；每条开仓选择同时只读投影 `entry_ts`、已确认平仓才有的 `exit_ts`/`holding_seconds`，以及 `exit_decision_id`/`close_reason`；平仓事实按 `trade_outcome_review`、`position_lifecycle_event`、`recovery_position_state` 的既有只读投影顺序回退；`system_view` 只汇总服务端已有方向、评分、动作理由和后验事实 |
| `POST /api/ops/incident-playbook/run` | `ops.incident-playbook-run.v2` | durable playbook ID + 时间 |
| `GET /api/ops/incident-playbook/{playbook_id}/events` | `ops.incident-playbook-events.v2` | event ledger item 时间 |
| `POST /api/ops/incident-playbook/{playbook_id}/events` | `ops.incident-playbook-event.v2` | durable event ID + 时间 |
| `POST /api/ops/release/{run_id}/finish` | `ops.release-finish.v2` | durable release ID + `updated_at` |
| `GET /api/ops/reports/weekly` | `ops.weekly-reports.v2` | filesystem report mtime；无记录为 unknown |
| `POST /api/ops/reports/weekly/generate` | `ops.weekly-report-generate.v2` | generator 未注册，兼容 unknown |

proposal refresh 必须在写后重读投影才能 known；release approval trail 为空时必须用对应 release row 证明权威空集。incident status 的顶层事实由 runtime overlay 与 local latch 的最新观测组成；请求时生成的 legacy `updated_at` 不是权威时间。Parity replay 只有在 artifact path/hash 已返回时才可 known；`persist_artifact=false` 必须保持 unknown。

## 6. 客户端和兼容边界

1. 只有 `_fact.state=known` 且业务值正常时才允许绿色。缺 `_fact` 的新客户端固定按 unknown 处理。
2. stale 可保留最后 known 值和时间；unknown/error 不得用零值覆盖。`source=none` 不得展示零元账户。
3. unknown/stale/error 必须禁用 start/unlock/治理放松，但不得禁用 stop/emergency/close/reduce/tighten。
4. Web 使用 endpoint-level schema 和统一 `FactBoundary`；小程序按来源独立合并，全部失败只推进 `lastAttemptAt`，不推进 `lastSuccessAt`。
5. 本文未列出且响应中缺 `_fact` 的端点仍处于兼容迁移期；新客户端不得回退到 recursive compat。旧顶层字段保留两个小程序版本或 30 天，取更长者；观察和稳定发布完成前不删除。

## 7. 变更门禁

新增或修改 `_fact` 时必须同时完成：

- 端点级 contract/source/timestamp 声明；
- known/unknown/stale/error 与 authoritative-empty 定向测试；
- OpenAPI snapshot；
- Web 端点 schema/必要的 FactBoundary 测试；
- 小程序来源 reducer 测试（如该端点被小程序消费）；
- `system-source-of-truth.md`、`legacy-debt-register.md` 和 `change-impact-checklist.md` 同步。
