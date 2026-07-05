# System Source Of Truth

> Status: active
> Last verified: 2026-07-06
> Scope: authoritative sources for runtime state, configuration, governance, data, and frontend contracts.

本文回答一个问题：当文档、注释、接口、数据库和历史理解冲突时，到底以哪里为准。

## 1. 总规则

1. 运行态事实优先于历史注释。
2. 数据库事实优先于临时日志片段。
3. 当前服务入口优先于旧脚本。
4. `RiskPolicyService` 和 `DecisionPolicy` 的权力边界优先于旧自动化路径。
5. 文档只描述事实，不替代运行态审计。

## 2. 配置事实源

| 事项 | 权威来源 | 说明 |
|---|---|---|
| 静态默认配置 | `settings.yaml` / `config/runtime_config.py` | 基础配置，不由自治治理直接回写 |
| 自治配置覆盖 | PostgreSQL `runtime_config_overlay` | 自治层事实源，重启后恢复 |
| 配置快照 | PostgreSQL `runtime_config_snapshot` | 审计和回滚用，不临场推断 |
| 配置写入口 | `RuntimeConfigMutationService` / `DecisionPolicy` | 自治配置变更必须走统一写入口 |
| 启动恢复 | `RuntimeConfigStartupService` | base YAML + DB overlay 后替换内存配置 |

判断原则：

- 生产自治动作不应直接修改 `settings.yaml`。
- 看到内存配置异常时，先查 overlay 和 snapshot。
- 可疑测试 overlay 不应被生产启动恢复。

## 3. 因子事实源

| 事项 | 权威来源 | 说明 |
|---|---|---|
| 因子输入帧 | `FactorFrameBuilder` | live、health、evolution 统一 PIT 数据入口 |
| 因子角色 | `factor_signal_config.role` / registry fallback | `alpha/context/gate/sizing` |
| 方向评分 | `PortfolioCompositor` | 只使用 enabled、weight > 0、role=alpha 的因子 |
| 权重写入 | `DecisionPolicy` | 权重治理唯一写入口 |
| 因子生命周期 | `RegistryAdapter` / lifecycle event | lifecycle 权威来源 |
| 因子治理视图 | `Factor Catalog` | 聚合 registry、runtime config、weights、health、shadow、AWE、learning |
| 因子治理周期 | `FactorGovernanceOrchestrator` | 自治决策中枢 |

判断原则：

- `bb_width/adx/atr_ratio/keltner_width` 是 context，不是方向投票。
- context 可以影响状态、阈值、仓位，但不能直接改变多空方向。
- shadow 因子不直接交易，必须经治理晋升。
- disabled/DEAD 因子应被 engine、compositor、AWE、readiness 一致排除。

## 4. 风控与执行事实源

| 事项 | 权威来源 | 说明 |
|---|---|---|
| 动作裁决 | `RiskPolicyService.evaluate(...)` | 风控统一裁决入口 |
| 下单/改仓/平仓 | cTrader bridge + ledger | broker 执行事实与账本共同追溯 |
| 信号门槛 | `ExecutionGate` + context policy effect | gate 前应用有效阈值 |
| 仓位监督 | `PositionSupervisor` / `position-supervisor-contract.md` | 持仓期间动作建议和 trace |
| 事件缩放 | `execution/event_sizing.py` + `data/events.duckdb` | 事件窗口风控输入 |

判断原则：

- 风控不产生 alpha，但拥有最高执行裁决权。
- 因子治理、模板切换、回滚都不能绕过 RiskPolicyService。
- BB 不进入 ExecutionGate 作为硬过滤器。

## 5. 学习与自治事实源

| 事项 | 权威来源 | 说明 |
|---|---|---|
| 样本证据语义 | `learning-evidence-contract.md` | label、integrity、causal_level、allowed_uses |
| 学习样本统一表 | PostgreSQL `autonomous_learning_sample` | features、label、trace、evidence_contract、config hash |
| 样本来源事实 | `decision_ledger` / `position_supervisor_trace` / `trade_outcome_review` / `supervisor_counterfactual_review` / `factor_contribution_review` | 不能从模型输出反推原始事实 |
| 数据集就绪 | `/api/learning/dataset/readiness` | trade/decision schema、required fields、ready 样本数 |
| 数据精度健康 | `/api/learning/dataset/quality-health` | evidence contract 自洽性和 open context 覆盖率 |
| 模型权限 | `model_permission_audit` / `backend.services.model_permissions` | shadow/advisory guardrails |
| shadow 模型审计 | `*_shadow_audit` 表 | open、position、factor、meta 的 shadow inference 事实 |
| 治理动作记录 | `evolution_decision` | 每轮自治判断 |
| 治理运行记录 | `evolution_run` | 为什么运行、运行结果 |
| 后验效果 | `learning_application_effect` | 回滚判断事实源 |
| 应用日志 | `learning_application_log` | 动作应用状态 |
| 建议/审计状态 | `policy_suggestion` + normalized status | `proposed/auto_approved/applied/rolled_back/blocked_by_risk/superseded` |
| 智能单元总账 | `docs/rule-driven-intelligence-inventory.md` | 规则智能、影子模型、审计数据和精度口径 |

判断原则：

- 模型输出默认 advisory/shadow，不能直接接管实盘。
- `model_ready=true` 还必须配合 `allowed_uses` 包含 `supervised_training`，才可进入强监督训练。
- `train_weight` 由 `quality_score`、`integrity`、`causal_level`、`label_status` 共同决定。
- 历史缺失字段只能标 degraded/partial/missing，不能补造实时上下文。
- 强治理必须有证据等级、样本数量、风控通过和回滚点。
- 回滚只能使用当时 decision 的 rollback JSON，不临场猜测。

## 6. 数据事实源

| 数据 | 权威来源 | 说明 |
|---|---|---|
| K 线 | `data/bars_monthly/bars_YYYY_MM.duckdb` | `data/bars.duckdb` 是当前月兼容链接 |
| tick | `data/ticks_monthly/ticks_YYYY_MM.duckdb` | `data/ticks.duckdb` 是当前月兼容链接 |
| L2 | `data/l2_monthly/l2_YYYY_MM.duckdb` | 由 backend 内 cTrader 主连接采集 |
| 外部研究数据 | `data/external_data.duckdb` | COT/ETF/FRED/宏观，必须按 `release_at` 做 PIT |
| 经济事件 | `data/events.duckdb` | 风控事件缩放读取 |
| 运行态状态 | PostgreSQL `state_v1` | 不再使用 `data/state.db` |
| 状态库运维边界 | `docs/state-postgres-store.md` | PostgreSQL state store、迁移留痕和旧 SQLite 禁用边界 |

判断原则：

- live 实时执行状态以 cTrader 为准。
- 外部研究数据不能替代 broker 实时行情和执行状态。
- 不新增生产路径写入 SQLite state。

## 7. API 与前端事实源

| 事项 | 权威来源 | 说明 |
|---|---|---|
| 后端健康 | `/api/health` | 最小服务健康 |
| 运维就绪 | `/api/ops/backend-readiness` | readiness、overlay、governance freshness |
| 因子治理展示 | `/api/v4/catalog` | 实时 Catalog；支持 latest snapshot |
| 因子卡片 | `factor-card-schema.md` + Factor Cards API | 前端解释展示 |
| 前端职责 | `development-workflow.md` | 本地 Windows 负责小程序/Web 展示 |

判断原则：

- 前端展示不应重新推断因子角色。
- context 因子不应显示成多空投票。
- 旧字段保留兼容，但新语义以 V2/V3 字段为准。

## 8. 冲突处理顺序

当信息冲突时，按下面顺序判断：

1. 当前运行中的服务和数据库审计事实。
2. 当前代码入口和测试契约。
3. `docs/system-source-of-truth.md`、`docs/architecture.md`、对应 contract。
4. `docs/legacy-debt-register.md` 中的迁移说明。
5. 历史 planning 文档和旧注释。

历史 planning 文档和旧注释只能提供背景，不能单独作为实现依据。
