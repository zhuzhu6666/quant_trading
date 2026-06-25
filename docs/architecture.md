# Quant Trading Architecture

> Last updated: 2026-06-25
> Scope: current maintained architecture and target full architecture.

本文是当前项目的主架构文档。历史蓝图、旧 Web Console 设计、MT5 时代说明和重复 proposal 不再作为运行依据。

## 1. 当前系统定位

当前系统是 XAUUSD+ 为主的 Factor Takeover v4 量化交易栈。

它已经不是单纯的因子回测系统，而是一条可审计闭环：

```text
行情数据
  -> 因子实时计算
  -> 信号归一化
  -> 多因子组合
  -> 执行闸门 / 风控闸门
  -> cTrader demo 执行
  -> 决策账本 / 订单与仓位生命周期
  -> 平仓复盘 / 经验记忆
  -> 规则治理 / 模型数据集 / 离线模型流水线
```

维护中的前端是 `miniprogram_v2`。后端是 FastAPI API 服务。旧浏览器 Web Console、MT5 文档和早期设计稿不再是维护目标。

## 2. 当前主链路

### 2.1 实时交易链路

```text
DataStore / cTrader bars
  -> backend/services/live_service.py
  -> alpha/streaming_factor_engine.py
  -> alpha/signal_normalizer.py
  -> alpha/portfolio_compositor.py
  -> alpha/execution_gate.py
  -> backend/services/live_service.py risk checks
  -> execution/ctrader_bridge.py
  -> backend/ledger/service.py
```

核心职责：

- `StreamingFactorEngine`：每根 bar 增量计算因子。
- `SignalNormalizer`：把原始因子值变成统一方向和强度的信号。
- `PortfolioCompositor`：合成 tactical / macro / composite score。
- `ExecutionGate`：处理信号阈值、冷却期、NFP/GVZ 事件过滤，并预留 `RiskGovernor` 裁决入口。
- `live_service`：读取账户与仓位缓存，执行 VaR gate、Kelly sizing、仓位数量/总量控制、SL/TP 修正、cTrader 下单。
- `DecisionLedger`：记录 signal、skip、open、close、order_failed、amend_failed 等事件。

当前执行安全边界：

- cTrader demo 是唯一维护中的执行通道。
- `ctrader_send_orders` 和 `factor_dry_run` 决定是否真正发单。
- 下单前必须经过 `ExecutionGate`。
- 下单前会经过 VaR gate。
- Kelly 只影响仓位大小，不负责绕过风控。
- 仓位数量和总 API volume 会再次拦截。
- 订单失败、SL/TP amend 失败、风控 skip 都会写入 ledger，供后续学习。

### 2.2 风控体系现状

当前风控是“分层硬规则 + 总裁决器雏形”。

```text
ExecutionGate
  -> signal threshold / cooldown / event filters
  -> optional RiskGovernor allow_trade

live_service pre-order risk
  -> VaR gate
  -> Kelly position sizing
  -> max position count
  -> max total API volume
  -> broker metadata / min volume / step volume

risk/
  -> RiskGovernor
  -> circuit breaker
  -> position monitor
  -> concentration monitor
  -> stress tester
  -> VaR engine
  -> Kelly position sizer
  -> regime detector
  -> cross asset covariance
```

`RiskGovernor` 目前是最高层风控裁决器的接口形态，已支持：

- `allow_trade`
- `allow_weight_update`
- `allow_promotion`
- `allow_new_factor`
- `force_dry_run`
- `force_deleverage`

但它还没有完全成为实时链路唯一入口。当前 live path 仍是显式执行 `ExecutionGate + VaR + Kelly + 仓位控制`。未来需要把这些分散检查收敛到统一的 `RiskGovernor` / `RiskPolicyService` facade。

当前需要注意的结构债：

- `risk/` 和 `backend/risk/` 同时存在。
- API 使用 `backend/risk` 的轻量版本。
- 更完整的 Governor、压力测试、集中度、跨品种风控在根目录 `risk/`。
- 后续应统一为一个稳定 facade，避免 live、API、learning 各自读不同实现。

### 2.3 决策账本与学习样本

当前系统已经把“交易”扩展成“可学习事件”。

```text
decision_ledger
  + factor snapshots
  + order lifecycle
  + position lifecycle
  + trade_outcome_review
  + experience_memory
  + learning_application_log
  + learning_application_effect
  -> LearningFeatureProvider
  -> learning_sample.v1 / decision_sample.v1
```

两类样本：

- `learning_sample.v1`：平仓后的交易级样本，包含 PnL、归因、复盘、经验、治理上下文。
- `decision_sample.v1`：决策级样本，覆盖 signal、skip、hold、open、order_failed、amend_failed。

样本关键字段：

- `factor_outcomes`
- `attribution_alignment`
- `execution_trace`
- `application_context`
- `risk_state`
- `portfolio_state`
- `llm_context`
- `explainability`

这意味着未来模型不只是学习“什么信号赚钱”，也能学习：

- 为什么没有交易。
- 哪一层 gate 拦截。
- 是否是风控拦截。
- 是否是 broker 执行失败。
- 哪些因子在入场时看似正确、平仓后实际有害。
- 某次规则应用之后效果是否变好。

### 2.4 规则驱动学习闭环

当前学习闭环已经落地，但处于 demo 实盘联调和稳定性验证阶段。

```text
closed position
  -> TradeReviewer
  -> ExperienceBuilder
  -> PolicySuggester
  -> RuleEvolutionGovernor
  -> approved / rejected / rolled_back
  -> learning application log
  -> learning application effect tracking
  -> adaptive weights / later review
```

当前规则治理原则：

- 单笔交易不会直接改变系统。
- 经验先沉淀成 pattern。
- pattern 达到样本数量和收益阈值后才形成 suggestion。
- suggestion 需要 Governor 审核。
- 已应用建议需要持续观察。
- 后续证据反转时可以 rollback / ineffective / reinforced。

这套系统当前是“规则驱动学习”，不是“模型驱动自我进化”。

### 2.5 离线模型流水线

当前模型链路已经建立，但所有模型能力都是离线和 advisory-only。

```text
LearningDatasetBuilder
  -> LearningDatasetReadiness
  -> LearningDatasetValidator
  -> DatasetSummaryAdapter
  -> LearningStatisticalTrainer
  -> ModelPromotionGate
  -> ModelShadowQueue
  -> ModelShadowRunner
  -> ModelCanaryReviewer
  -> ModelInferenceContract
  -> ModelCanaryExecutor
  -> LearningModelPipeline
```

安全边界：

- 模型 artifact 不允许声明 `live_trading=true`。
- `ModelPromotionGate` 只允许进入 `shadow_candidate`。
- `ModelShadowRunner` 只做离线影子验证。
- `ModelCanaryReviewer` 只把候选推进为 `canary_ready` 或 `canary_rejected`。
- `ModelInferenceContract` 只接受 `canary_ready` 模型，输出 `review_only` advice。
- `ModelCanaryExecutor` 只做受控 advisory trial，不下单、不改权重。

当前模型的定位：

```text
模型 = 解释、评分、排序、审查建议
模型 != 下单者
模型 != 风控绕过者
模型 != 权重直接修改者
```

## 3. 当前系统成熟度

### 已经落地

- 因子实时链路。
- composite signal 和 execution gate。
- cTrader demo 执行通道。
- 决策账本和订单/仓位 lifecycle。
- 平仓复盘。
- 经验记忆。
- 规则建议。
- Governor 审批 / 拒绝 / 回滚。
- 学习应用日志和效果跟踪。
- 重启后 learning backfill。
- 模型就绪样本导出。
- 离线快照、readiness、validator。
- 离线训练、model card、promotion gate。
- shadow queue / shadow run / canary review。
- advisory-only inference。
- controlled canary trial。
- 小程序 V2 作为唯一维护前端。

### 仍需验证

- demo 实盘连续运行下，平仓复盘是否稳定无漏记。
- 重启中开仓/平仓场景是否能可靠恢复。
- learning application effect 是否能在真实流中正确推进。
- 自动 rollback / reinforced 是否和真实收益一致。
- 小程序学习页是否准确展示所有状态。

### 当前主要技术债

- `risk/` 与 `backend/risk/` 风控实现需要统一 facade。
- `RiskGovernor` 尚未成为所有风控动作的唯一裁决入口。
- 多品种风险预算尚未完全接入 live path。
- 历史重复 learning application 需要一次清理脚本。
- 部分旧模块、旧桥接、旧脚本仍在 TODO 中待清理。

## 4. 未来完全体架构

未来完全体不是“让一个模型控制一切”，而是“硬风控不可绕过，规则治理可审计，模型和元模型提供建议与协调”。

推荐目标形态：

```text
Market / Broker / External Data
  -> Data Quality Gate
  -> Feature and Factor Layer
  -> Signal and Portfolio Layer
  -> Meta Decision Layer
  -> Risk Governor
  -> Execution Router
  -> Ledger and Lifecycle Spine
  -> Review / Learning / Model Lab
  -> Governance and Rollback
```

### 4.1 完全体分层

#### Layer 0: 不可绕过硬风控

这一层是系统宪法，任何模型、元模型、规则建议都不能绕过。

包括：

- 最大日亏损。
- 最大总回撤。
- 单笔最大风险。
- 最大仓位数量。
- 最大净/毛敞口。
- 最大品种相关性敞口。
- broker 断连处理。
- 数据延迟处理。
- 价格异常处理。
- emergency close / kill switch。
- 强制 dry-run。

#### Layer 1: 数据质量和市场状态

负责判断当前数据是否可交易：

- bar/tick 是否新鲜。
- 价格是否卡死。
- spread/slippage 是否异常。
- 交易时段是否允许。
- 重大事件风险。
- regime 是否发生明显切换。
- 多数据源是否互相矛盾。

输出不是交易信号，而是 `market_context` 和 `tradeability_state`。

这一层还需要补齐两类完全体上下文：

- `temporal_context`：交易时段、星期/日期、重大事件前后、bar 生命周期、开仓时间、持仓时长、持仓是否超时、不同持仓阶段的收益/回撤效率。
- `market_space_context`：价格在近期区间、趋势通道、支撑阻力、波动分位、成交/深度状态、多周期结构、跨品种相关性中的位置。

当前系统已经开始在 review / experience 中记录 `entry_ts`、`close_ts`、`holding_seconds`、`holding_minutes`，但时间/空间上下文仍未形成统一抽象层。后续应把它们作为可复盘、可训练、可风控的上下文输入，而不是散落在单个因子或日志字段里。

#### Layer 2: 因子与组合信号

负责生成可解释的交易意图：

- 因子计算。
- 因子归一化。
- 因子分组。
- tactical / macro 组合。
- 因子贡献快照。
- composite score。
- direction。
- confidence。

这一层只能表达“想不想交易”，不能决定“可不可以交易”。

时间/空间上下文进入本层时，应采用“上下文调制”，而不是简单替代信号。例如：

- 同一个趋势因子，在高波动突破空间和低波动震荡空间中权重不同。
- 同一个入场信号，在亚洲盘、伦敦盘、美盘和重大事件前后的可信度不同。
- 同一笔持仓，开仓后 5 分钟、30 分钟、2 小时的出场逻辑应不同。
- 因子贡献复盘应区分 entry contribution、hold contribution、exit contribution 与 holding-time efficiency。

这部分是当前完全体路线中的未完成项，应在 Phase B/C 之间逐步补齐。

#### Layer 3: 元决策层

这是未来元模型/元策略所在层。

职责：

- 判断当前应该偏防守还是进攻。
- 判断当前哪些因子族更可信。
- 调整建议风险预算。
- 给出是否应该降低频率、降低仓位、暂停学习的建议。
- 给不同模型/规则输出做 routing。
- 给出人类可读解释。

但它不能：

- 直接下单。
- 直接提高硬风控上限。
- 直接绕过 Governor。
- 直接把模型接入 live execution。

#### Layer 4: RiskGovernor / RiskPolicyService

未来应成为所有高影响动作的唯一裁决入口。

统一裁决：

- 是否允许开仓。
- 是否允许加仓。
- 是否允许平仓。
- 是否允许权重更新。
- 是否允许因子晋升。
- 是否允许新因子注册。
- 是否允许模型进入 shadow。
- 是否允许模型进入 canary。
- 是否允许某个建议应用到 live policy。

推荐接口：

```text
RiskPolicyService.evaluate(action, context) -> Verdict

action:
  - open_trade
  - add_position
  - close_position
  - update_weight
  - promote_factor
  - register_factor
  - start_shadow_model
  - start_canary_model
  - apply_model_suggestion

verdict:
  - allowed
  - reason
  - severity
  - max_size
  - required_mode
  - audit_payload
```

这一层应该收敛现在分散的 VaR、Kelly、仓位、熔断、集中度、回撤、数据延迟、多品种敞口判断。

#### Layer 5: 执行路由

职责：

- 根据 Governor verdict 执行。
- 统一处理 broker metadata。
- 统一处理 volume 转换。
- 统一处理 SL/TP amend。
- 统一记录 order lifecycle。
- 支持未来 paper/demo/live 分层。

执行层只执行已经批准的动作，不自行解释策略。

#### Layer 6: Ledger Spine

未来所有重要动作都应写入一条统一证据脊柱。

包括：

- 市场上下文。
- 因子快照。
- composite signal。
- gate 结果。
- risk verdict。
- model advice。
- meta-model advice。
- final action。
- broker lifecycle。
- position lifecycle。
- post-trade review。
- learning application。
- rollback / reinforce。

目标是任何一笔交易都能回放：

```text
当时看到了什么
系统想做什么
模型建议了什么
风控允许了什么
最后执行了什么
结果如何
后来学到了什么
有没有回滚
```

#### Layer 7: 学习与模型实验室

这一层可以越来越强，但必须隔离 live 权限。

包括：

- dataset export。
- readiness audit。
- dataset validation。
- offline training。
- model registry。
- promotion gate。
- shadow validation。
- canary review。
- advisory inference。
- controlled canary trial。
- human review。

任何模型要影响 live policy，必须经过：

```text
offline validated
  -> shadow passed
  -> canary ready
  -> advisory trial passed
  -> Governor approved
  -> limited rollout
  -> monitored application effect
  -> rollback capable
```

## 5. 元模型的最终位置

未来可以有元模型，但它不是皇帝，而是调度员、审计员和风险参谋。

### 元模型应该做什么

- 汇总因子、风控、执行、学习、模型输出。
- 识别当前系统状态：正常、过热、失真、防守、恢复期。
- 决定是否建议降低交易频率。
- 决定是否建议降低风险预算。
- 决定哪些模型/因子当前更可信。
- 给出可解释的建议。
- 帮助发现风控规则过严或过松。
- 帮助生成待审核的 policy suggestion。

### 元模型不应该做什么

- 不直接下单。
- 不直接修改风控上限。
- 不直接关闭熔断。
- 不直接提升仓位。
- 不绕过 `RiskGovernor`。
- 不绕过 shadow/canary。
- 不在证据不足时自动晋升模型。

### 推荐元模型输出格式

```json
{
  "regime": "defensive",
  "confidence": 0.78,
  "risk_budget_multiplier_suggestion": 0.5,
  "trade_frequency_suggestion": "reduce",
  "trusted_factor_groups": ["macro", "volatility"],
  "untrusted_factor_groups": ["short_momentum"],
  "recommended_actions": [
    {
      "action": "freeze_weight_update",
      "reason": "drawdown approaching limit and recent model advice unstable",
      "requires_governor_approval": true
    }
  ],
  "must_not_override": [
    "max_daily_loss",
    "circuit_breaker",
    "broker_disconnect",
    "data_lag"
  ]
}
```

元模型输出应进入 ledger，并由 `RiskGovernor` 或 `RuleEvolutionGovernor` 审批后才能影响系统。

## 6. 当前到完全体的路线

### Phase A: 稳定当前闭环

目标：证明现有规则驱动闭环在 demo 实盘里可靠。

要完成：

- 连续运行验证 signal/open/close/skip/order_failed/amend_failed 都能落账。
- 验证平仓复盘和真实 PnL 对齐。
- 验证 learning application effect 能推进。
- 验证 rollback / reinforced 不误触发。
- 验证重启恢复和 delayed backfill。

### Phase B: 风控统一

目标：让风控从“分散检查”变成“统一裁决”。

当前进展：

- 已新增 `RiskPolicyService.evaluate(action, context) -> RiskVerdict` 作为统一 facade。
- live 开仓路径已先接入 `open_trade`，把 VaR、仓位数量、API volume、金字塔检查统一成一个可审计 verdict。
- 后续还需要把 close、weight update、factor promotion、model shadow/canary 等高影响动作也逐步接入。

要完成：

- 合并 `risk/` 与 `backend/risk/` 的职责边界。
- 增加 `RiskPolicyService` facade。
- live_service 所有下单前风险判断统一走 facade。
- API 风控面板读取同一套状态。
- ledger 记录完整 risk verdict。
- 将 `temporal_context` 纳入风控裁决：持仓时长、超时、事件窗口、交易时段、日内连续亏损节奏。
- 将 `market_space_context` 纳入风控裁决：价格空间位置、波动分位、结构冲突、相关性敞口。

### Phase C: 模型建议进入实时旁路

目标：让模型能看 live context，但仍不能执行。

要完成：

- live path 可调用 `ModelInferenceContract` advisory score。
- advisory score 写入 ledger。
- 小程序显示模型建议与置信度。
- 模型建议只作为 review context，不改变订单。
- 模型样本显式携带时间/空间上下文，先用于解释和离线训练，不直接驱动 live 风控。

### Phase D: 元模型旁路

目标：增加元模型对系统状态的统一判断。

要完成：

- 定义 `meta_context.v1`。
- 汇总 market、factor、risk、execution、learning、model 状态。
- 元模型输出 advisory meta decision。
- 输出进入 ledger。
- 输出可生成 policy suggestion，但不能直接应用。

### Phase E: 受限自动调参

目标：在严格权限下，让系统自动应用低风险建议。

允许自动化的范围：

- 小幅降低风险预算。
- 暂停权重更新。
- 暂停新因子注册。
- 降低交易频率。
- 标记某类模式为 watch。

不允许自动化的范围：

- 提高最大亏损阈值。
- 关闭熔断。
- 提高最大仓位。
- 未经审批启用 live_trading 模型。
- 跳过 canary。

### Phase F: 多品种完全体

目标：从 XAUUSD+ 单品种扩展到多品种组合风控。

要完成：

- 每品种独立 factor pipeline。
- 全局 `RiskGovernor` 聚合风险预算。
- 跨品种相关性和风险平价。
- 品种级、策略级、账户级三层限制。
- 多品种 ledger 和 dataset contract。

## 7. 最终原则

1. 硬风控永远高于模型。
2. Governor 是执行权限边界。
3. 模型先离线，再 shadow，再 canary，再 advisory，最后才可能受限影响 live policy。
4. 元模型只协调，不独裁。
5. 所有高影响动作必须可审计、可解释、可回滚。
6. 单笔交易不能直接改变系统。
7. 经验必须经过样本数、收益、失败标签和治理审查。
8. 未来越智能，权限越要清晰。

## 8. 主要 API

Learning endpoints:

- `/api/learning/dataset`
- `/api/learning/decision-dataset`
- `/api/learning/dataset/export`
- `/api/learning/dataset/readiness`
- `/api/learning/dataset/validate`
- `/api/learning/dataset/model-card`
- `/api/learning/dataset/train`
- `/api/learning/model/promotion-gate`
- `/api/learning/model/shadow-queue`
- `/api/learning/model/shadow-run`
- `/api/learning/model/canary-review`
- `/api/learning/model/inference`
- `/api/learning/model/canary-trial`
- `/api/learning/model/pipeline/run`

Risk endpoints:

- `/api/risk/summary`
- `/api/risk/var`
- `/api/risk/kelly`
- `/api/risk/stress`
- `/api/risk/concentration`

## 9. 数据库

- `data/state.db`：运行状态、决策账本、复盘、经验、学习应用、cTrader deals。
- `data/experiments.db`：实验、模型注册、shadow/canary 模型工作流。
- `data/*.duckdb`：bars、ticks、L2、trades、events 等行情数据。

## 10. 高信号测试

```bash
python -m pytest tests\research\test_rule_learning_pipeline.py tests\research\test_model_registry.py -q
python -m pytest tests\research tests\alpha\test_portfolio_compositor.py tests\test_live_service_lifecycle.py tests\test_evolution_closure_fixes.py tests\deployment\test_deployment.py tests\test_backend_jobs_manager.py tests\test_backend_jobs_state.py -q
```
