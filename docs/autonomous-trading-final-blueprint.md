# Autonomous Trading Final Blueprint

> Status: active
> Last updated: 2026-07-08
> Scope: final operating blueprint for demo-nursery autonomous learning, multi-agent governance, proposal arbitration, execution boundaries, memory, and growth.

本文记录最终大纲。后续所有智能体链路、因子治理、学习记忆、风控和前端治理改动，都必须能解释自己服务于下面这条主线：

```text
demo nursery 自动采样
  -> 多智能体并行采证和建议
  -> 统一权责合同
  -> 统一提案总线
  -> 统一审查和反证
  -> RiskPolicy / DecisionPolicy 串行裁决
  -> 单一路径执行和审计
  -> 交易结果沉淀 lesson / memory / scorecard
  -> 下一轮 agent 生成建议前读取同一份上下文
```

一句话目标：

**系统不是靠一个万能智能体交易，而是让多个智能体各司其职、互相提供证据和反证，所有真实动作经过同一套裁决和审计路径，demo 资金用于自动学习，交易结果持续反哺记忆和 agent 可靠性，最终形成可成长、可回滚、可解释的自治交易系统。**

## 1. Demo Nursery

- `autonomy_mode=demo_nursery` 是自动学习育苗模式，不是真实资金风控旁路。
- demo 可以自动采样、自动应用已在安全范围内的治理建议，但底线风险仍硬拦。
- 学习门可以转为 observation，底线门不能转为 observation。
- 每笔交易必须能进入 trade review、lesson memory、agent attribution 和后续 scorecard。
- 初期亏损可接受，但亏损必须生成可检索、可反证、可用于下一轮约束的结构化经验。

## 2. Multi-Agent Contract

当前登记 agent 基线：

- `v16_brain`
- `autonomous_learning`
- `factor_governance`
- `factor_pruning_governance`
- `llm_advisory`
- `lightgbm_shadow_models`

规则：

- 未登记 agent 默认 `review_only`。
- LLM 永远 `advisory_only`。
- shadow model 永远没有执行权。
- agent 可以采证、建议、生成候选或写审计，但不能直接下单、不能直接改 runtime、不能绕过 RiskPolicy/DecisionPolicy。
- 高 score 只能提高审查优先级，不能扩大权限。
- 低 score、负反馈、合同违规必须提高证据要求或阻断自动桥接。

## 3. Proposal And Candidate Flow

目标链路：

```text
source evidence / candidate
  -> AgentAuthorityRegistryService.evaluate
  -> agent context snapshot
  -> ProposalRegistryService envelope
  -> Candidate Review / scorecard / briefing / counter-evidence
  -> controlled bridge
  -> legacy governor or runtime control plane
```

要求：

- 所有建议必须能映射到统一 proposal envelope。
- `policy_suggestion`、`brain_governance_candidate`、shadow/advisory audit、learning application、live autonomy event 都要被统一读模型收敛。
- `brain_governance_candidate` 进入 `policy_suggestion` 前必须有 candidate review。
- `governance_ready` 只表示可进入审查桥接，不等于可执行。
- Proposal Registry 只做展示、路由、可靠性、证据新鲜度和冲突检测，不审批、不应用。

## 4. Risk And Execution Boundary

真实动作只允许通过这些入口：

- `RiskPolicyService.evaluate(...)`
- `DecisionPolicy`
- `RuntimeConfigMutationService`
- runtime overlay/snapshot
- cTrader live execution pipeline
- release/replay/rollback evidence

禁止：

- agent 直接触达 broker。
- agent 直接写 runtime overlay。
- 前端重算策略或绕过后端裁决。
- LLM 或 shadow model 改变授权状态。
- 任何提案绕过 RiskPolicy/DecisionPolicy。

## 5. Factor Evolution

因子系统的目标不是越多越好，而是可解释、可裁剪、可反证、可回滚。

要求：

- 因子方向投票、context、gate、sizing 职责分清。
- 因子裁剪候选必须来自真实决策参与、贡献压力、弱健康或亏损归因。
- pruning 晋级前必须查 counter-evidence。
- pruning 桥接前必须查 candidate review、scorecard 和 briefing。
- 权重实际变化仍必须由 DecisionPolicy 和 RiskPolicyService 约束。
- 效果变差必须能通过 learning application effect 回滚或降权。

## 6. Position And Sizing

要求：

- 动态仓位由 equity、SL distance、Kelly/context policy、broker min/step/max 和 API volume 上限共同决定。
- context policy 或 Kelly 低于 broker 最小量时输出 0 volume，并由风控阻断，不能悄悄提升为最小仓位。
- position supervisor 负责持仓观察、close/reduce/tighten 建议，但最终动作仍由 RiskPolicyService 裁决。
- supervisor 不得因为年轻仓位短期波动或最小 reduce volume 不可交易就无条件 full close。

## 7. Memory And Growth

成长的定义：

- 不是模型声称学会了。
- 不是更多 agent 互相聊天。
- 是每个建议、动作、后验效果和交易结果都可追溯，并能改变下一轮证据要求、可靠性排序和候选审查严格度。

记忆要求：

- trade lesson 稳定写入 `experience_memory`。
- lesson 必须带 market state、entry rationale、risk observations、execution action、outcome、failure tags、recommended action、allowed uses 和 confidence。
- memory 必须能被 brain、candidate review、scorecard、briefing 和后续 agent prompt/context 读取。
- 负反馈优先作为反证和证据门，不作为无审查自动惩罚。

## 8. Frontend And Operator Surface

前端最终目标是展示链路，而不是制造第二套决策系统。

要求：

- 前端不重算策略、不重算风控、不直接授权。
- 操作台展示：agent contract、scorecard、briefing、proposal registry、candidate review、risk verdict、DecisionPolicy preview、memory feedback、rollback evidence。
- 小程序保持简洁状态界面；复杂治理放 Web。
- 所有按钮只触发后端受控 API。

## 9. Deviation Guard

每一步推进必须回答：

1. 是否服务于 demo nursery 自动学习、多智能体治理或单一路径审计？
2. 是否扩大了某个 agent 的执行权？如果是，停止。
3. 是否绕过 RiskPolicyService、DecisionPolicy、runtime overlay/snapshot 或 cTrader 执行链路？如果是，停止。
4. 是否新增了孤立表、孤立状态或第二套事实源？如果是，必须有明确迁移理由。
5. 是否让结果可被 readiness、proposal registry、memory 或 scorecard 观测？如果不能观测，优先补观测而不是继续加智能。

允许自动推进的工作：

- 收紧绕路。
- 增强只读审计。
- 补齐 agent context、scorecard、briefing 和 review coverage。
- 将旧路径映射到统一 proposal/candidate/review 口径。
- 增加测试和文档事实源。

需要人工确认的工作：

- 改真实资金 live autonomy 权限。
- 放宽底线风控。
- 新增 agent 执行权限。
- 改 broker 执行语义。
- 大改前端信息架构。
- 删除历史数据或迁移生产表。
