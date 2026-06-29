# Quant Trading Architecture

> Last updated: 2026-06-30
> Scope: current system, target full architecture, and the delivery roadmap we will follow.

本文现在是项目的主蓝图。后续讨论中形成的新架构结论，优先更新这里；`TODO.md` 只负责承接近期执行项和验证项。

---

## 1. 这套系统现在到底是什么

当前系统已经不是“因子出信号然后下单”的简单交易脚本，而是一条可审计、可复盘、可学习的闭环：

```text
市场数据
  -> 实时因子计算
  -> 信号归一化与多因子组合
  -> 执行闸门 / 风控裁决
  -> cTrader demo 执行
  -> 决策账本 / 订单与仓位生命周期
  -> 平仓复盘 / 经验沉淀
  -> 规则治理 / 离线模型流水线
```

当前维护中的前端是 `miniprogram_v2`，后端是 FastAPI 服务，执行通道是 cTrader demo。

一句话概括当前状态：

**Phase C / D / E 主链已完成，Phase H 自主进化地基已落地第一版；系统已进入“主动持仓管理 + 责任归因 + 参数治理 + 退出反事实审计 + 统一进化账本”并行观察阶段。**

2026-06-30 后的当前补充状态：

**系统已经具备 demo autonomous 下的自动样本物化、supervisor trace 永久记录、反事实成熟化、自动审批/应用/回滚账本和 RuntimeConfig 快照。当前重点是观察真实效果、补 freshness watchdog，并把配置/进化状态查询面继续收口成单一事实源。**

### 1.1 数据库治理基线

2026-06-26 起，数据库层不再只是“路径统一”，而是正式进入治理模式：

- `SQLite` 只负责运行时状态库
  - `data/state.db`
  - `data/experiments.db`
- `DuckDB` 只负责市场/分析型库
  - `data/ctrader_data.duckdb`
  - `data/ticks.duckdb`
  - `data/l2.duckdb` -> `data/l2_monthly/l2_YYYY_MM.duckdb`，由 L2 writer 跨月自动刷新
  - `data/trades.duckdb`
  - `data/events.duckdb`
- 业务代码禁止直接使用 `sqlite3.connect(...)` / `duckdb.connect(...)`
- 统一连接入口在 `backend/core/db.py`
  - `connect_sqlite(...)`
  - `connect_duckdb(...)`
- 启动与排障统一使用：
  - `python scripts/db_doctor.py --repair`

这样做的目的不是“形式统一”，而是避免下面三类历史问题再次发生：

- 拿 `sqlite3` 去打开 `.duckdb`
- 运行态 schema 漂移后没人迁移
- 不同脚本/服务对同一个库的引擎和结构假设不一致

### 1.2 现网数据源边界

2026-06-26 起，现网对数据源职责做了进一步收口：

- cTrader 是当前实盘唯一执行与实时状态源
  - spot
  - account
  - positions
  - execution / deals
- 第二数据源当前不再参与 live 开仓、风控放行或 broker 状态判断
- `L2 depth` 当前可以作为研究支路在 cTrader 主连接内采集，但默认不是实盘前置依赖；只有在 `risk_require_l2_depth=true` 时，才允许成为 live 开仓/风控门槛

这条边界的目的，是把“今天真要交易必须依赖的数据”与“未来研究可能有帮助的数据”彻底分开，避免研究支路反向拖垮实盘链路。

---

## 2. 当前系统已经做到哪一步

### 2.1 已经落地的能力

- 实时因子链路：`StreamingFactorEngine -> SignalNormalizer -> PortfolioCompositor`
- live 执行链路：`ExecutionGate -> live_service -> ctrader_bridge`
- 决策账本与生命周期：`signal / skip / open / close / order_failed / amend_failed`
- 平仓复盘与经验沉淀：`trade_outcome_review / experience_memory`
- 规则驱动学习闭环：`PolicySuggester -> Governor -> learning_application_log / effect`
- 持仓监督闭环：`position_supervisor -> RiskPolicyService -> cTrader amend/reduce/close -> ledger / trade-trace`
- 归因恢复与反事实退出审计：`AttributionEngine.restore_open -> trade_outcome_review.attribution_integrity -> supervisor_counterfactual_review`
- 学习证据契约：`learning_evidence_contract.v1 -> dataset/readiness/validator/train/shadow/inference audit`
- supervisor 模板治理：`position_supervisor_template -> policy_suggestion -> switch_position_supervisor_template`
- 统一进化账本：`evolution_run / evolution_decision / runtime_config_snapshot`
- supervisor trace 成熟化：`position_supervisor_trace -> supervisor_counterfactual_review -> supervisor_execution_trace`
- 离线模型流水线：dataset、readiness、validator、train、promotion gate、shadow、canary、advisory inference
- 风控统一裁决第一阶段：`RiskPolicyService.evaluate(action, context) -> RiskVerdict`
- 持仓时长记录第一阶段：`holding_seconds / holding_minutes / timeout_*`
- 运维证据链查询：`/api/risk/trade-trace`
- 三端协作流程：本地开发、GitHub 合并、服务器验证与同步

### 2.2 当前系统的真实定位

它已经是：

- 一个**规则驱动、证据可回放**的交易系统；
- 一个**会记录自己为什么做出决定**的系统；
- 一个**会在平仓后形成结构化经验**的系统；
- 一个**能把退出问题、时长问题、regime 问题与参数可疑责任分开记录**的系统；
- 一个**允许模型离线学习，但禁止模型直接接管实盘**的系统。

它仍然不是：

- 一个能完全自动调参和治理因子的系统；
- 一个有成熟元模型统一调度全局状态的系统；
- 一个多品种、全组合、全上下文的完全体。

但在 demo autonomous 范围内，它已经可以自动推进低风险治理动作：

- 自动物化 learning samples 和参数模板 recommendations；
- 自动审批白名单内、证据充分、可回滚的建议；
- 自动应用 `online_light` 参数模板和符合门禁的 supervisor 模板切换；
- 自动把应用和回滚写入 `evolution_decision`，并保留 `previous_template_id` / config snapshot。

---

## 3. 当前系统的核心短板

这几轮对话之后，短板已经从“缺核心层”转为“核心层已入位，需要继续补真实样本、自动审计和受控治理”。

### 3.1 持仓监督层已入位，但仍需继续观察真实样本

系统已经不再只是等待原始止盈止损。`position_supervisor` 已经能持续输出 `hold / tighten / reduce / close`，并经过 `RiskPolicyService` 统一裁决后执行。

当前仍需要继续补的不是“有没有 supervisor”，而是：

- 更多真实 `tighten / reduce / timeout` 执行样本；
- 更稳定地区分 `supervisor_tighten_stopout` 与外部 broker close；
- 在审批后安全切换 `position_supervisor_template`，而不是绕过风控。

### 3.2 归因层已进入主链，但重启恢复与证据完整性仍是重点

系统已经把平仓复盘、责任标签、因子贡献、supervisor 事件和反事实审计接入学习链路。

当前明确要求每笔 review 标记：

- `attribution_integrity`: `full / recovered / missing`
- `close_reason_source`: `supervisor_direct_close / supervisor_tighten_stopout / supervisor_reduce_partial_or_stopout / external_broker_close / restart_replay`
- `inferred_close_supervisor`: close 前最近一次 supervisor verdict

### 3.3 因子治理层仍然偏弱

当前系统会：

- 记录因子贡献
- 调整权重
- 通过规则建议做保守治理

但还没形成真正的“因子教练层”：

- 哪个因子适合哪类市场
- 哪个因子是公式问题，哪个是参数问题
- 参数该在线轻调，还是离线深调
- 何时拆版本、切模板、灰度上线、替换旧参数

### 3.4 元模型还没有正式入位

当前模型链路是 advisory-only，这很好，也符合现阶段安全边界。  
但未来完全体还需要一个更高层的“全局调度脑”，负责：

- 看整体状态，而不是只看单笔交易
- 协调因子、风控、执行、学习、模型
- 建议系统偏进攻还是偏防守
- 识别什么时候该降频、降权、冻结某类策略

---

## 4. 这套系统的正确权力结构

这是现在最需要固定下来的原则。

### 4.1 风控不是唯一的大脑，但它是最高裁决权

系统里可以有多个“会思考”的层：

- 因子层发现机会
- 归因层解释交易过程
- 因子治理层优化因子和参数
- 元模型层统筹全局状态

但这些层都不能绕过风控。

**风控不是负责产生 alpha，风控负责决定哪些 alpha 有资格活着进入执行。**

### 4.2 模型可以参与判断，但不能直接越权执行

未来模型可以：

- 估计风险
- 估计当前市场适配度
- 估计因子可信度
- 建议收紧风险预算
- 建议调整因子权重或参数模板

但模型不能直接：

- 提高硬风控上限
- 关闭熔断
- 绕过 Governor
- 直接替换 live 交易决策

### 4.3 硬风控不是“全是固定数字”

硬风控应该拆成两层：

#### 绝对硬边界

必须写死，谁都不能突破：

- 单笔最大风险
- 单日最大亏损
- 最大总回撤
- 最大仓位 / 最大杠杆 / 最大敞口
- 网络异常 / 数据异常 / broker 异常禁止开新仓
- kill switch / dry-run / emergency close

#### 动态硬边界

不是写死一个常数，而是写死一套裁决规则：

- 当前允许仓位 = 风险预算 x 波动率调节 x 市场状态调节
- 当前允许持仓时长 = 策略类型 x regime x 风险状态
- 当前止损结构 = 波动率 x 空间位置 x 流动性状态

关键点是：

**可以动态算，但动态算出来的结果也必须被硬执行。**

---

## 5. 完全体应该长什么样

未来正确的形态不是“一个超级模型控制所有”，而是“多层解释、多层治理、统一裁决、证据闭环”。

建议目标架构如下：

```text
市场 / Broker / 外部上下文
  -> 数据质量与市场状态层
  -> 时间/空间上下文层
  -> 因子执行层
  -> 多因子组合层
  -> 持仓监督与交易归因层
  -> 因子治理层
  -> 元模型/元策略层
  -> RiskGovernor / RiskPolicyService
  -> 执行路由
  -> Ledger 证据脊柱
  -> 复盘、学习、模型实验室
  -> 治理、灰度、回滚
```

---

## 6. 完全体的分层定义

### Layer 0: 执行宪法层（不可绕过硬风控）

职责：

- 最大亏损、最大回撤、最大敞口、最大杠杆
- 数据断流 / broker 断连 / 价格异常 / 磁盘异常 / loop 异常处理
- 熔断、强制 dry-run、emergency close

原则：

- 这一层不是为了赚钱，是为了**不死**
- 这一层不接受模型绕过

### Layer 1: 数据质量与市场状态层

职责：

- 行情是否新鲜
- spread / slippage / depth / bar lag 是否异常
- 当前市场更像趋势、震荡、假突破、波动扩散还是波动收缩
- 当前交易环境是否可交易

输出：

- `tradeability_state`
- `regime_state`
- `data_quality_state`

### Layer 2: 时间/空间上下文层

这是后续必须补强的抽象层。

职责：

- 时间上下文：交易时段、weekday、事件窗口、持仓时长、盈利持续时间、回撤持续时间
- 空间上下文：价格在区间/通道/支撑阻力/波动分位中的位置
- 多周期上下文：M1/M5/M15/H1 结构是否一致
- 相关性上下文：当前仓位与其他风险暴露是否冲突

输出：

- `temporal_context`
- `market_space_context`
- `multi_timeframe_context`

### Layer 3: 因子执行层

职责：

- 按当前公式版本和参数版本生成信号
- 输出方向、强度、适用 regime、预期持仓类型

这里要特别强调：

因子不是死的，但**单笔交易生命周期内最好保持版本稳定**，避免边交易边重写自己。

每个因子最终都应该是一个可解释对象，至少知道：

- 因子家族
- 公式版本
- 参数版本
- 当前参数值
- 适用市场
- 弱适用市场
- 预期持仓时长
- 典型失效模式

Phase E / E1 的正式字段 contract 见 [factor-card-schema.md](factor-card-schema.md)。

### Layer 4: 多因子组合层

职责：

- 因子归一化
- 因子分组
- tactical / macro / structural 组合
- 生成 composite score、direction、confidence

这一层只表达：

**“我想不想交易”**

它不能最终决定：

**“我可不可以交易”**

### Layer 5: 持仓监督与交易归因层

这是未来最关键的新层，建议正式命名为：

**`position_supervisor`**

Phase C / C1 的正式 contract 见 [position-supervisor-contract.md](position-supervisor-contract.md)。

职责：

- 持续观察仓位生命周期
- 判断 thesis 是否仍成立
- 识别市场是否切换
- 识别是否出现高浮盈回吐
- 识别持仓时间是否已经失去效率
- 给出继续持有 / 收紧 / 减仓 / 平仓的建议

这一层要显式判断：

- 入场质量 `entry_quality`
- 持仓效率 `holding_efficiency`
- 浮盈回吐比例 `giveback_ratio`
- 时间衰减评分 `time_decay_score`
- market regime 是否切换
- 继续持有是否值得占用风险预算

这层的核心不是“生成信号”，而是：

**理解一笔已经开的交易现在还值不值得继续活着。**

### Layer 6: 因子治理层

这是“谁来调参数、谁来优化因子”的正式答案。

职责：

- 判断某因子是逻辑问题、参数问题、市场不匹配，还是退出不匹配
- 统计因子在不同 regime 下的表现
- 决定是降权、换模板、调阈值、调 lookback，还是拆成新版本
- 负责在线轻调与离线深调的边界

Phase E / E1 的因子解释卡片 contract 见 [factor-card-schema.md](factor-card-schema.md)。  
Phase E / E2 的参数模板 contract 见 [parameter-template-contract.md](parameter-template-contract.md)。  
Phase E / E3 的在线/离线边界见 [parameter-tuning-boundary.md](parameter-tuning-boundary.md)。

建议拆成两类动作：

#### 在线轻调

- 权重调整
- 风险预算缩放
- 开仓阈值轻微调节
- 不同 regime 间切换预设参数模板

#### 离线深调

- 改核心公式
- 改核心 lookback
- 改主要阈值
- 加过滤条件
- 重新回测、walk-forward、灰度上线

原则：

**归因层负责发现“参数可疑”，治理层负责决定“参数怎么改”。**

### Layer 7: 元模型 / 元策略层

未来可以有元模型，但角色不是皇帝，而是全局调度员。

职责：

- 汇总因子、仓位、风险、执行、学习、模型状态
- 判断当前应该偏进攻还是偏防守
- 建议风险预算倍数、交易频率、可信因子族
- 决定哪些候选建议应该进入人审或 Governor

它不能：

- 直接下单
- 直接改硬风控上限
- 绕过 shadow / canary / Governor

### Layer 8: RiskGovernor / RiskPolicyService

这一层是整个系统的**最高执行裁决层**。

未来所有高影响动作都应该走它：

- `open_trade`
- `add_position`
- `reduce_position`
- `close_position`
- `update_weight`
- `switch_parameter_template`
- `promote_factor`
- `register_factor`
- `start_shadow_model`
- `start_canary_model`
- `apply_model_suggestion`

它接收来自：

- 硬规则
- 市场状态
- 时间/空间上下文
- 持仓监督建议
- 因子治理建议
- 元模型建议

然后输出唯一裁决：

```text
RiskPolicyService.evaluate(action, context) -> RiskVerdict
```

### Layer 9: 执行路由层

职责：

- 统一 broker metadata、volume、SL/TP、改单、撤单
- 严格执行经过批准的动作
- 不负责理解策略语义

### Layer 10: Ledger 证据脊柱

未来所有动作都应写入统一证据链：

- 当时看到了什么
- 因子想做什么
- 持仓监督怎么判断
- 元模型建议了什么
- 风控允许了什么
- 最终执行了什么
- 结果如何
- 后来学到了什么

2026-06-30 起，Ledger 证据脊柱进一步拆成两类：

- 交易事实账本：`decision_ledger / order_lifecycle_event / position_lifecycle_event / trade_outcome_review`
- 进化治理账本：`evolution_run / evolution_decision / runtime_config_snapshot`

其中：

- `evolution_run` 记录一次自治运行，例如样本物化、trace 回填、trace 成熟化、demo 自动治理周期；
- `evolution_decision` 记录这次运行内每个关键决策，例如自动审批、apply switch、rollback、样本成熟；
- `runtime_config_snapshot` 记录当时 RuntimeConfig 的稳定 hash 和版本，供交易、trace、sample、application 回放。

新的约束是：

**任何自动学习或自动治理动作，都必须能从 `evolution_decision` 追溯到证据、风控 verdict、前后状态和配置版本。**

### Layer 11: 学习与模型实验室

职责：

- dataset export / readiness / validation
- offline train / registry / promotion gate
- shadow / canary / advisory inference
- 回测、复盘、离线调参、相似案例检索

原则：

**先做解释和建议，再做受限影响，最后才可能有限介入 live policy。**

2026-06-30 起，学习样本统一执行 `learning_evidence_contract.v1`：

- `label_status=pending` 的样本不能声明 `supervised_training`
- `integrity=missing` 不进入强监督训练
- `recovered / partial` 样本必须降权
- `supervisor_execution_trace` 只有结合 review / counterfactual 成熟后，才允许升级为强训练候选

---

## 7. 三个最容易混淆的角色

这是后续开发时必须始终保持清楚的分工。

### 因子

负责：

- 发现机会
- 生成信号

不负责：

- 最终裁决
- 直接修改自己

### 交易大脑

这里不是单一模块，而是三层合起来：

- 持仓监督与归因层
- 因子治理层
- 元模型层

它们负责：

- 理解过程
- 解释问题
- 形成优化建议

### 风控

负责：

- 统一裁决
- 强制执行
- 记录原因

所以更准确的说法不是“风控是唯一大脑”，而是：

**风控是最高裁决权；交易大脑是解释和治理中枢。**

---

## 8. 我们要如何判断“因子错了”还是“退出错了”

这是未来归因体系的关键标准。

不能只看最终盈亏。  
必须看整条持仓路径。

建议每笔交易至少记录并用于归因：

- `entry_quality`
- `exit_quality`
- `mfe`
- `mae`
- `profit_capture_ratio`
- `giveback_ratio`
- `time_in_profit`
- `holding_efficiency`
- `regime_fit`
- `exit_reason`
- `thesis_status_at_exit`

然后给出责任标签，例如：

- `entry_good_exit_bad`
- `alpha_correct_but_capture_failed`
- `tp_too_far`
- `sl_too_tight`
- `holding_too_long`
- `regime_changed_during_hold`
- `factor_logic_ok_but_param_suspect`

这样系统后续才能真正区分：

- 是因子逻辑不适应
- 是因子参数不适应
- 是止盈止损不适应
- 是持仓时长不适应
- 是市场中途切换了

---

## 9. 当前系统与完全体之间，最关键的开发路线

下面这条路线是后续开发主线，默认按此推进。

## 9A. 数学模型和大语言模型应该接在哪里

这两类模型都应该进入系统，但角色完全不同，不能混用。

### 数学模型的定位

数学模型更适合做：

- 概率估计
- 风险评分
- regime 识别
- 因子排序
- 持仓质量评分
- 参数模板选择
- 异常检测

它更像系统里的**定量判断器**。

### 大语言模型的定位

大语言模型更适合做：

- 复盘解释
- 证据归纳
- 失败模式总结
- 治理建议草案
- 人审辅助
- 运维/风控/因子状态的人话说明

它更像系统里的**语义理解器和治理助理**。

### 数学模型应该接的层

#### 1. 接在持仓监督与归因层

用途：

- 评估继续持有是否仍有正期望
- 评估退出风险是否升高
- 评估时间衰减是否明显
- 评估浮盈回吐是否异常
- 评估当前持仓效率是否已经恶化

适合的模型：

- path scoring model
- survival / duration model
- exit quality model
- trade outcome probability model

输出进入：

- `position_supervisor`
- `RiskPolicyService` 的审计上下文和建议输入

#### 2. 接在因子治理层

用途：

- 判断因子在不同 regime 下的有效性
- 判断问题更像公式问题还是参数问题
- 评估 lookback、threshold、止盈止损模板是否失配
- 给出参数模板切换或降权建议

适合的模型：

- logistic regression
- xgboost / lightgbm
- ranking model
- regime classifier
- anomaly detector

输出进入：

- 因子治理工作流
- 参数模板候选
- 治理审批前的量化证据

#### 3. 接在元模型层

用途：

- 汇总全局市场、持仓、风险、学习、执行状态
- 建议当前系统偏进攻还是偏防守
- 建议风险预算倍数、交易频率、可信因子族
- 识别系统是否进入恢复期、防守期或异常期

它是未来“元模型”中的定量核心，但仍然只有建议权，没有执行特权。

### 大语言模型应该接的层

#### 1. 接在归因与复盘层

用途：

- 读取结构化 `trade_trace`
- 总结这笔交易为什么赢、为什么亏
- 把“因子问题 / 参数问题 / 退出问题 / 时长问题 / regime 问题”讲清楚
- 生成给人看的复盘摘要

它在这里更像：

**交易复盘分析师**

#### 2. 接在因子治理层

用途：

- 汇总某因子在一段时间内的表现证据
- 归纳该因子适用市场、弱适用市场、典型失败模式
- 生成治理建议草案
- 生成参数调整提案的解释文本

它在这里更像：

**治理报告生成器**

#### 3. 接在元治理层

用途：

- 汇总风控、归因、因子治理、数学模型建议
- 生成系统状态说明
- 生成 rollout / rollback 理由
- 支持人工审批和运维排障

它在这里更像：

**治理秘书长 / 审计助理**

### 两类模型都不能直接接到执行层

不管是数学模型还是大语言模型，都不应该直接拥有下面这些权力：

- 直接开仓
- 直接平仓
- 直接提高硬风控上限
- 直接关闭熔断
- 直接绕过 `RiskPolicyService`
- 直接启用 live-trading 模型

它们都只能通过：

- `position_supervisor`
- 因子治理层
- 元模型层
- `RiskPolicyService`
- shadow / canary / advisory 流程

间接影响系统。

### 最终关系

可以把两类模型和主链路的关系理解成这样：

```text
市场数据
  -> 因子层
  -> 组合层
  -> 持仓监督层
       <- 数学模型: 持仓评分 / 退出风险 / 时间衰减
       <- LLM: 复盘解释 / 失败归因总结
  -> 因子治理层
       <- 数学模型: 参数评估 / regime适配 / 因子排序
       <- LLM: 治理建议归纳 / 审查说明
  -> 元模型层
       <- 数学模型: 全局状态评分 / 风险预算建议
       <- LLM: 全局解释 / 治理摘要 / 人审辅助
  -> RiskPolicyService
  -> 执行层
```

一句话总结：

- 数学模型负责“算”
- 大语言模型负责“讲明白”
- 风控负责“拍板并执行”

### Phase A: 稳定闭环

状态：已完成

目标：

- 决策账本、复盘、经验、规则建议、效果跟踪稳定
- 手动平仓、重启恢复、补账无断链

成果：

- 已验证 open/close/review/experience 主闭环
- 已完成手动 broker close 验证

### Phase B: 风控统一

状态：已完成可用闭环

目标：

- 把分散风控收敛到 `RiskPolicyService`
- 让风险 verdict 成为统一可审计裁决

成果：

- 风控 summary / verdict / trade-trace 已可线上验证
- 持仓时长与 timeout 审计已落地
- 运维前端已开始做人话展示

### Phase C: 持仓监督闭环

状态：已完成主链，真实样本观察中

这是系统从“会开仓”走向“会管理仓位”的关键一步，当前主链已经在线上运行。

已完成：

1. 建立 `position_supervisor`
2. 为每个活跃仓位持续计算：
   - `holding_seconds`
   - `mfe / mae`
   - `giveback_ratio`
   - `time_decay_score`
   - `holding_efficiency`
   - `thesis_status`
   - `regime_shift`
3. 输出结构化建议：
   - `hold`
   - `tighten`
   - `reduce`
   - `close`
4. 把建议送入 `RiskPolicyService`
5. 所有动作写入 ledger 和 trade trace
6. 持仓监督阈值已模板化，当前内置：
   - `position_supervisor:default.v1`
   - `position_supervisor:conservative.v1`

验收标准：

- 不再只会死等止盈止损
- 对“曾经盈利但后来回吐”的仓位能给出可解释动作
- 每次平仓都知道是 stop、timeout、giveback、regime shift，还是 thesis failure
- supervisor 模板切换必须走 `policy_suggestion` 审批和 `RiskPolicyService.evaluate("switch_position_supervisor_template", ...)`

### Phase D: 归因升级与责任分离

状态：已完成主链，继续提升真实样本覆盖

目标：

- 把“入场错 / 退出错 / 时长错 / 参数错 / regime 错”正式分离

已完成：

1. 扩展 trade review contract
2. 引入统一 failure taxonomy v2
3. 建立责任归因标签
4. 把责任归因同时写入：
   - trade review
   - factor contribution review
   - position supervisor close reason
   - learning sample
5. 新增重启后归因恢复与 `attribution_integrity`
6. 新增 supervisor 退出反事实样本：
   - `supervisor_counterfactual_review`
   - `backend.services.supervisor_counterfactual`
   - `backend.services.supervisor_learning_scheduler`

验收标准：

- 单笔亏损不再粗暴归类为“因子失效”
- 系统能识别“因子方向对，但退出不好”
- 系统能识别“supervisor 平得对 / 平早了 / 保护太紧 / 噪音止损”

### Phase E: 因子治理与参数模板

状态：主链已完成，真实样本与灰度效果观察中

目标：

- 正式建立“因子教练层”

已完成主链：

1. 因子解释卡片标准化
2. 参数版本与模板系统
3. regime-aware 参数模板切换
4. 在线轻调与离线深调边界
5. 参数怀疑证据 -> 候选治理动作 -> 回测/灰度 -> 发布

验收标准：

- 系统知道某因子是“逻辑好但参数可疑”
- 因子不再只有权重变化，还能有版本和模板治理

### Phase F: 元模型旁路

状态：已完成后端旁路和审计，继续观察样本质量

目标：

- 让更高层的大脑开始看到全局，但仍不拥有执行特权

已完成主链：

1. 定义 `meta_context.v1`
2. 汇总市场、因子、持仓、风控、学习、模型状态
3. 输出 advisory meta decision
4. 把元模型建议写入 ledger
5. 只允许生成建议，不允许直接执行

验收标准：

- 系统能判断现在该进攻、观望、收缩还是恢复
- 元模型能建议降频、降权、冻结某些因子族

### Phase G: 元模型治理建议与前端交接

状态：已完成后端 contract、shadow report、治理建议和前端交接入口

目标：

- 让 meta shadow / governance suggestion 能被后端和小程序稳定查看
- 保持 advisory-only，不直接接 live

已完成：

- meta model context / shadow report / snapshot
- governance suggestion 入 `policy_suggestion`
- readiness 与 ops 聚合入口

### Phase H: 自治数据工厂与分级自动治理

状态：第一版地基已完成，后台观察中

目标：

- 在不突破安全边界的前提下，让系统自动做低风险调整

已完成：

- `autonomous_learning_sample`
- `position_supervisor_trace`
- `supervisor_counterfactual_review` 后台物化
- `evolution_run / evolution_decision / runtime_config_snapshot`
- demo autonomous 自动审批、自动应用、自动 rollback 账本
- strict evidence contract training gate

不允许自动化：

- 提高硬风控上限
- 关闭熔断
- 启用 live-trading 模型
- 绕过 Governor

### Phase I: 多品种完全体

目标：

- 从 XAUUSD+ 走向多品种、多风险池、全组合调度

---

## 10. 后续开发的默认顺序

如果没有新的外部强约束，当前默认按下面顺序推进：

1. 继续观察 Phase H 第一版自治地基的真实效果
2. 补 `evolution_run / evolution_decision` 的查询与告警体验
3. 补 shadow/model freshness watchdog
4. 补 supervisor template / parameter template 的效果阈值和自动 rollback 策略
5. 样本和治理稳定后，再推进 Phase I 多品种扩展

原因很简单：

- Phase C/D/E/F/G/H 的主链已经入位，接下来风险来自“自动系统是否持续可解释、可回滚、不过拟合”
- 没有 freshness watchdog，模型/影子报告可能过旧还被治理层误读
- 没有足够真实观察期，自动回滚阈值容易过早或过晚
- 多品种会放大所有治理问题，所以必须在单品种自治链路稳定后再扩展

---

## 11. 当前系统最重要的工程原则

1. 硬风控高于一切模型和策略
2. 风控是最高裁决层，不是唯一 alpha 来源
3. 单笔交易不能直接重写系统
4. 模型先 advisory，再 shadow/canary，再考虑受限影响
5. 持仓过程必须持续重评估，不能只等 TP/SL
6. 归因必须分清入场、退出、时长、参数、regime 的责任
7. 因子可以进化，但不能在实盘里无边界自我改写
8. 所有高影响动作都要可解释、可审计、可回滚
9. 前端展示以人话为主，机器状态只是底层证据，不是最终文案

---

## 12. 当前主要入口

### API

- `/api/live/status`
- `/api/live/positions`
- `/api/risk/summary`
- `/api/risk/policy/verdicts`
- `/api/risk/trade-trace`
- `/api/learning/dataset`
- `/api/learning/decision-dataset`
- `/api/learning/model/pipeline/run`
- `/api/learning/evolution/runs`
- `/api/learning/position-supervisor/traces/backfill`
- `/api/learning/position-supervisor/traces/materialize-labels`

### 数据

- `data/state.db`
- `data/experiments.db`
- `data/*.duckdb`

### 核心文档

- [README.md](../README.md)
- [TODO.md](../TODO.md)
- [development-workflow.md](development-workflow.md)
- [startup.md](startup.md)

---

## 13. 当前结论

这套系统现在已经有了“骨架、血管和第一版自治神经”。

下一阶段真正的突破点，不是急着让模型接管实盘，而是把下面三件事继续做稳：

1. 进化账本的长期连续性
2. supervisor / 参数模板自动治理的效果观察与回滚
3. 模型和 shadow 审计的新鲜度与准入门禁

这三层稳定后，模型更深参与和多品种扩展才有足够稳的地基。
