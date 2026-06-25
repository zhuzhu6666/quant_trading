# TODO — 完整开发路线与当前进度

本文档现在是项目的**执行总面板**。  
以后每完成一项、暂停一项、发现一项新缺口，都必须先更新这里，再继续开发。

目标：

- 让任何一次新对话都能先读 `TODO.md`，快速知道系统做到哪了
- 让后续开发严格按顺序推进，而不是想到哪做到哪
- 让“当前状态、下一步、为什么还没做”都能落在文档里，而不是只留在聊天记录

长期架构蓝图见 [docs/architecture.md](docs/architecture.md)。

---

## 0. 使用规则

### 每次开发前

先看：

1. 本文档的“当前阶段”
2. 本文档的“当前唯一进行中项”
3. 本文档的“下一步入口”

### 每次开发后

必须更新本文档中的以下内容：

- 对应任务状态：`未开始 / 进行中 / 已完成 / 阻塞 / 观察中 / 延后`
- 完成日期
- 产出物
- 验证结果
- 新发现的缺口或后续子任务

### 状态约束

- 同一时间只允许一个“主开发阶段”处于 `进行中`
- 同一阶段内可以有多个子任务，但要标清主次
- 发现新想法时，先写进“新增发现 / 待设计项”，不要直接打乱当前顺序

---

## 1. 当前阶段总览

### 已完成阶段

- ✅ Phase A：稳定闭环
- ✅ Phase B：风控统一（达到可用闭环）

### 当前主阶段

- ✅ Phase E：因子治理与参数模板（主链已完成，远程验收通过 / 真实样本观察）

### 当前系统一句话状态

系统现在已经具备：

- 实时因子链路
- 统一风控裁决第一阶段
- 决策账本与生命周期追踪
- 平仓复盘与经验沉淀
- 正式责任归因与责任回写链路
- 离线模型训练 / shadow / canary / advisory 流程

系统现在仍然缺：

- 元模型层的全局调度能力
- 更多真实样本下的参数模板灰度发布 / 回滚观察
- 更细粒度的退出责任细分与模板化治理动作（后续增强）

### 当前唯一进行中主线

`Phase E 完成：真实样本观察 / Phase F 准备`

### 下一步入口

Phase E 主链已完成并通过远程验收。下一步默认进入 **Phase F：数学模型与大语言模型分层接入**；Phase E 只保留真实样本观察与后续模板覆盖增强。

---

## 2. 总开发顺序

后续默认严格按下面顺序推进，除非有线上事故或用户显式改优先级。

1. Phase C：持仓监督闭环
2. Phase D：归因升级与责任分离
3. Phase E：因子治理与参数模板
4. Phase F：数学模型与大语言模型分层接入
5. Phase G：元模型旁路
6. Phase H：受限自动治理
7. Phase I：多品种完全体

说明：

- **Phase C** 不做，系统持仓中仍然是“睡着的”
- **Phase D** 不做，系统就分不清问题到底出在 entry、exit、timing、param 还是 regime
- **Phase E** 不做，因子优化就只能靠零散人工干预
- **Phase F/G** 是把模型系统化接入，但前提是前面几层已经清楚

---

## 3. Phase A / Phase B 已完成记录

### Phase A：稳定闭环

状态：`已完成`  
完成日期：`2026-06-25`

已完成：

- 决策账本、平仓复盘、经验沉淀、规则建议、治理审批主链路打通
- 手动 broker close 已验证能进入复盘与经验链
- learning backfill 与重启恢复主链路打通
- `holding_seconds / holding_minutes` 已开始沉淀

主要验证：

- `scripts/phase_a_health_check.py`
- 6 小时窗口健康检查通过
- open / close / review / experience 无断链

### Phase B：风控统一

状态：`已完成（可用闭环）`  
完成日期：`2026-06-25`

已完成：

- `RiskPolicyService.evaluate(action, context) -> RiskVerdict`
- `open_trade / close_position / update_weight / promote_factor / register_factor / start_shadow_model / start_canary_model`
- runtime health 第一版进入 live 开仓裁决
- `temporal_context` 第一版进入风控审计上下文
- 持仓 timeout 审计字段落地
- `/api/risk/summary`
- `/api/risk/policy/verdicts`
- `/api/risk/trade-trace`
- 运维前端开始做人话化展示

主要验证：

- GitHub / 服务器已同步到 `264a2944`
- 线上验证 `/api/risk/summary`、`/api/live/status`、`/api/live/positions`、`/api/risk/trade-trace`
- 服务重启后可恢复到 `ready`

---

## 4. Phase C：持仓监督闭环

状态：`观察中（主链已完成，真实样本持续补充）`  
优先级：`P0`  
目标：让系统从“会开仓”走向“会管理已开仓位”。

### 为什么先做这个

当前最明显的缺口是：

- 仓位曾经盈利过
- 后来又回吐
- 持仓时间已经很长
- 系统仍然只会等待原始止盈止损

这说明当前系统没有真正的“持仓裁决层”。

### C1：定义 `position_supervisor` contract

状态：`已完成`

完成日期：`2026-06-25`

要产出：

- 模块职责定义
- 输入上下文字段
- 输出动作字段
- 与 `RiskPolicyService` 的接口关系
- ledger / trace 写入要求

至少要回答：

- supervisor 什么时候跑
- 它读哪些仓位状态和市场状态
- 它输出哪些建议
- 哪些建议必须再交给风控拍板

完成标准：

- 有明确 schema / contract 文档
- 能指导后续代码落地，不再口头讨论

产出物：

- `docs/position-supervisor-contract.md`
- `docs/architecture.md` 中 Layer 5 增加 contract 入口

验证结果：

- contract 已对齐现有 `RiskPolicyService.evaluate(action, context)`、`decision_ledger`、`position_lifecycle_event`、`/api/risk/trade-trace` 的现有接口形态
- 已明确 C4 之前 `tighten / reduce` 仅能先作为 advisory verdict 存证，`close` 可优先复用现有 `close_position`

新发现的缺口 / 后续子任务：

- C4 需要为 `tighten_position` / `reduce_position` 增加正式 risk action
- C5 需要让 `trade-trace` 显式展示 supervisor verdict，而不是只依赖 review 或 action_json 旁带

### C2：补齐持仓路径核心字段

状态：`已完成`

完成日期：`2026-06-25`

要补齐或统一：

- `mfe`
- `mae`
- `giveback_ratio`
- `profit_capture_ratio`
- `time_in_profit`
- `holding_efficiency`
- `time_decay_score`
- `thesis_status`
- `regime_shift`

完成标准：

- 活跃仓位与已平仓仓位都能稳定计算核心字段
- 字段能进入 API / trace / review 基础结构

产出物：

- `backend/services/position_metrics.py`
- `backend/services/live_service.py` 持仓路径指标接入
- `alpha/reflection/reviewer.py` 平仓复盘指标接入

验证结果：

- 活跃仓位路径已能稳定输出 `mfe / mae / giveback_ratio / profit_capture_ratio / time_in_profit / holding_efficiency / time_decay_score / thesis_status / regime_shift`
- `/api/live/positions` 基础结构已通过 `live_service.get_positions()` 挂载这些字段
- 平仓 review 与 `/api/risk/trade-trace` 的 review 基础结构已可带出这些字段
- 测试通过：
  - `python -m pytest tests/test_live_service_account_refresh.py tests/test_live_service_lifecycle.py tests/research/test_rule_learning_pipeline.py -q`
  - `python -m pytest tests/risk/test_risk_api_policy.py -q`

新发现的缺口 / 后续子任务：

- 当前 `regime_shift` 仍是保守版，只有显式 regime 证据时才会给出 `confirmed`
- C3 需要基于这些字段把 `hold / tighten / reduce / close` 的动作门槛正式结构化
- C5 之后前端需要把这些字段翻译成人话，而不只是裸数值

### C3：定义持仓动作分级

状态：`已完成`

完成日期：`2026-06-25`

要形成统一动作集：

- `hold`
- `tighten`
- `reduce`
- `close`

并明确每种动作的触发条件、证据字段、执行限制。

完成标准：

- supervisor 输出不再是模糊文字，而是结构化动作建议

产出物：

- `backend/services/position_supervisor.py`

验证结果：

- `position_supervisor` 已稳定输出 `hold / tighten / reduce / close`
- 已包含结构化 `summary_reason / evidence / recommended_controls / human_summary`
- 测试通过：
  - `python -m pytest tests/test_position_supervisor.py -q`

### C4：接入 `RiskPolicyService`

状态：`已完成`

完成日期：`2026-06-25`

要完成：

- 持仓监督建议进入风控裁决
- 风控能基于 supervisor 建议做最终放行、收紧或拒绝
- 原因写入 `risk_verdict`

完成标准：

- 持仓中的减仓、收紧、平仓都可追到 supervisor 与 risk verdict 链路

产出物：

- `risk/policy_service.py`
- `backend/services/live_service.py`

验证结果：

- `tighten_position / reduce_position / close_position` 已进入统一 risk verdict
- live tick 已接入 supervisor -> risk -> execute 链路
- 测试通过：
  - `python -m pytest tests/risk/test_policy_service.py tests/test_live_service_lifecycle.py -q`

### C5：接入 trade trace / 前端展示

状态：`已完成`

完成日期：`2026-06-25`

要完成：

- `/api/risk/trade-trace` 可展示 supervisor 结论
- 前端能用人话说明“为什么继续拿 / 为什么收紧 / 为什么平仓”

完成标准：

- 不再只显示机器字段
- 用户能直观看懂系统对持仓的判断

产出物：

- `backend/api/risk.py`
- `miniprogram_v2/pages/trading/index.js`
- `miniprogram_v2/pages/trading/index.wxml`

验证结果：

- `/api/risk/trade-trace` 已显式暴露 `position_supervisor.latest/events`
- 活跃持仓 API 已输出 `supervisor_label / supervisor_summary`
- 当前持仓页已增加“持仓监督结论”人话卡片
- 测试通过：
  - `python -m pytest tests/test_position_supervisor.py tests/risk/test_risk_api_policy.py -q`

### C6：用真实案例验收

状态：`进行中`

目标案例：

- 近期那两个长持仓
- “曾经盈利但未止盈，后续回吐甚至止损”的案例
- 手动关闭、超时关闭、主动保护关闭等案例

完成标准：

- 能明确给出：是退出问题、时长问题、regime 切换问题，还是因子/参数问题

当前进展：

- 已补典型场景自动化验收：
  - 大浮盈回吐 -> `reduce`
  - 持仓超时 -> `close`
  - trace 中可追 supervisor verdict
- 已新增真实数据核查脚本：
  - `scripts/phase_c_supervisor_check.py`
- 本地真实样本链路已打通：
  - 修复 Windows 本地 cTrader TLS 证书链问题后，`scripts/backfill_ctrader_deals.py --days 1 --max-rows 20` 已可成功回填真实成交
  - `scripts/backfill_learning_reviews.py --limit 50 --allow-partial` 已可基于这些成交生成本地 `trade_outcome_review`
  - `python scripts/phase_c_supervisor_check.py --limit 30` 已可在本地直接读取真实 review 样本完成结构核查
- 本地当前活跃仓验收入口已打通：
  - `python scripts/phase_c_supervisor_check.py --direct-broker` 已可直接连本地 cTrader，读取真实 open positions，并复用 Phase C 的持仓路径指标 + supervisor 判定
  - 当前真实样本中，2 笔 open position 都已被识别为 `long_hold`
  - 其中 `268085757` 当前已被 supervisor 给出 `close / thesis_broken` 结论，说明 C6 已具备“真实活跃仓 -> supervisor 判断”本地验收能力
- 线上服务器已完成 Phase C 发布与重启验收：
  - GitHub `main`、服务器 `/home/ubuntu/quant_trading`、本地代码已同步到 `4a12eaf`
  - 服务器 `.venv` 已通过 Phase C 相关测试：`57 passed`
  - `quant-backend.service` 重启后，`/api/live/status` 已进入 `bridge_ready/account_ready`，并完成真实持仓恢复
  - 服务器日志已出现真实主动保护执行证据：`position_id=268085757` 被 live loop 发出 `supervisor close`，原因为 `thesis_broken`
  - 该仓位随后已生成真实 close review：`review_b4e1413da0cb489e`，`close_reason=thesis_broken`，说明“真实持仓 -> supervisor 判定 -> risk verdict -> 执行 -> review”链路已在线上跑通
- 本地历史平仓样本已增强到 Phase C 路径口径：
  - `backend/services/learning_backfill.py` 现在会优先用 broker 开/平成交恢复 `holding_seconds`
  - 若本地 DuckDB 有对应 bars，则会进一步推导 `mfe / mae / giveback_ratio / profit_capture_ratio / holding_efficiency / thesis_status`
  - 已通过远程 `/api/market/bars` 回灌 2026-06-24 ~ 2026-06-25 的 `XAUUSD+ / M5` bars 到本地 DuckDB，当前本地真实 review 已识别出 `4` 个 `profit_giveback` 案例
- C6 验收口径已显式区分“直接证据”和“推断证据”：
  - `scripts/phase_c_supervisor_check.py` 现在会输出 `coverage`
  - `timeout_close_case` 与 `active_protection_case` 已拆成 `evidence / inferred_evidence`
  - `learning_backfill` 写入的 `close_reason_source=phase_c_inferred` 不再被当成“真实已执行 supervisor/timeout close”
- 真实案例覆盖现状：
  - 已直接覆盖：长持仓案例、broker/manual close 案例、线上已执行 active protection `close / thesis_broken` 案例
  - 本地已直接覆盖：盈利后回吐案例、活跃仓 supervisor `close / thesis_broken` 判定案例
  - 当前仅推断覆盖：历史 `profit_giveback_after_mfe` 一类退出问题样本
  - 远程现网当前仍缺少可直接验收的 `holding_timeout` 样本，以及 `supervisor_reduce / supervisor_tighten` 已执行样本

当前阻塞点：

- 当前本地新增 review 虽已能补出 `holding_seconds / giveback_ratio / profit_capture_ratio / thesis_status`，但仍有部分旧样本缺 entry decision / regime 上下文，因此“责任拆分”还不够完整
- 远程 `/api/live/positions` 在仓位刚被主动平仓后会短暂回到 `positions_empty`，当前更多依赖 `trade-trace` / `policy verdicts` / review 来确认主动保护已落账
- 当前仍缺少真实 `holding_timeout` 样本，以及 `supervisor_reduce / supervisor_tighten` 已执行样本，C6 还不能正式收口

临时决策：

- 上述“真实 `holding_timeout` / `supervisor_reduce` / `supervisor_tighten` 样本不足”先标记为 `观察中`，暂不阻塞后续主线推进
- 只要不影响后续 Phase D 的 contract 设计与基础落地，就先按“已有 direct evidence + 持续观察补样本”处理
- 一旦线上自然出现这些样本，再回补 C6 验收，不单独为补样本停住开发节奏

下一步：

- 继续观察线上，优先等待并验收真实 `holding_timeout` 与 `supervisor_reduce / supervisor_tighten` 样本
- 继续补远程真实 `profit_giveback` close review，确认 `giveback_ratio / profit_capture_ratio / close_reason` 能在线上稳定落到 review
- 对刚执行的线上 `thesis_broken` 样本，继续用 `trade-trace`、`policy verdicts`、`phase_c_supervisor_check.py --api-base ...` 做回放，确认链路长期稳定
- 一旦线上出现 timeout 或更多 supervisor 执行平仓样本，立即回灌本地并复跑 C6 验收脚本，确认责任标签是否能落到 `时长问题 / 退出问题 / thesis_broken`

---

## 5. Phase D：归因升级与责任分离

状态：`已完成`  
优先级：`P0`  
前置条件：`Phase C 基本落地`

目标：让系统正式分清“问题到底出在哪”。

### D1：扩展 trade review contract

状态：`已完成`

完成日期：`2026-06-25`

新增核心字段：

- `entry_quality`
- `exit_quality`
- `holding_efficiency`
- `regime_fit`
- `thesis_status_at_exit`
- `profit_capture_ratio`
- `giveback_ratio`
- `time_in_profit`

产出物：

- `backend/services/review_contract.py`
- `alpha/reflection/reviewer.py`
- `backend/services/learning_backfill.py`
- `backend/api/learning.py`
- `backend/api/risk.py`

验证结果：

- 新旧 review 现在都会统一走 `phase_d.v1` contract
- `trade_outcome_review.review_json` 已正式补齐：
  - `entry_quality`
  - `hold_quality`
  - `exit_quality`
  - `regime_fit_score / regime_fit`
  - `thesis_status_at_exit`
  - `regime_shift_at_exit`
  - `profit_capture_ratio`
  - `giveback_ratio`
  - `time_in_profit`
  - `holding_efficiency`
- `/api/learning/reviews` 与 `/api/risk/trade-trace` 已统一把这些字段作为稳定出口暴露
- 测试通过：
  - `python -m pytest tests/test_learning_backfill.py tests/test_review_contract_api.py tests/risk/test_risk_api_policy.py -q`
  - `python -m compileall alpha/reflection/reviewer.py backend/services/learning_backfill.py backend/services/review_contract.py backend/api/learning.py backend/api/risk.py`

新发现的缺口 / 后续子任务：

- D2 需要在现有 contract 上把 `entry_good_exit_bad / alpha_correct_but_capture_failed / holding_too_long / regime_changed_during_hold` 等责任标签正式结构化
- 旧样本虽然已经能被 API 正常规范化输出，但部分历史数据仍缺足够上下文，D2 需要允许“部分证据下的保守归因”

### D2：失败分类体系 v2

状态：`已完成`

完成日期：`2026-06-25`

目标标签至少包括：

- `entry_good_exit_bad`
- `alpha_correct_but_capture_failed`
- `tp_too_far`
- `sl_too_tight`
- `holding_too_long`
- `regime_changed_during_hold`
- `factor_logic_ok_but_param_suspect`

产出物：

- `backend/services/failure_taxonomy.py`
- `alpha/reflection/reviewer.py`
- `backend/services/learning_backfill.py`
- `backend/api/learning.py`
- `backend/api/risk.py`

验证结果：

- review 现在会正式生成：
  - `primary_responsibility`
  - `responsibility_labels`
  - `failure_taxonomy`
- taxonomy 已能保守识别：
  - `entry_good_exit_bad`
  - `alpha_correct_but_capture_failed`
  - `holding_too_long`
  - `regime_changed_during_hold`
  - `factor_logic_ok_but_param_suspect`
- `/api/learning/reviews` 与 `/api/risk/trade-trace` 已统一暴露上述责任标签
- 现有 `failure_tags` 未被推翻，而是在其上兼容追加正式责任标签，避免下游学习链断裂
- 测试通过：
  - `python -m pytest tests/test_failure_taxonomy.py tests/test_learning_backfill.py tests/test_review_contract_api.py tests/risk/test_risk_api_policy.py tests/research/test_rule_learning_pipeline.py -q`
  - `python -m compileall backend/services/failure_taxonomy.py alpha/reflection/reviewer.py backend/services/learning_backfill.py backend/api/learning.py backend/api/risk.py`

新发现的缺口 / 后续子任务：

- D3 需要把 `primary_responsibility / responsibility_labels / failure_taxonomy` 进一步稳定写回 learning sample、factor contribution review 与前端复盘出口
- 当前 taxonomy 仍是保守版，像 `tp_too_far / sl_too_tight` 这类更细粒度退出责任，还需要 D3/D4 后结合更多执行细节再细分

### D3：责任回写链路

状态：`已完成`

完成日期：`2026-06-25`

要写回：

- `trade_outcome_review`
- `factor_contribution_review`
- learning sample
- trade trace
- 前端复盘页

完成标准：

- 任何一笔亏损都不能只被粗暴归成“因子失效”

产出物：

- `alpha/reflection/reviewer.py`
- `backend/services/learning_backfill.py`
- `backend/api/learning.py`
- `backend/api/risk.py`
- `research/learning/experience_builder.py`
- `research/features/feature_provider.py`
- `miniprogram_v2/pages/learning/index.js`
- `miniprogram_v2/pages/learning/index.wxml`

验证结果：

- `trade_outcome_review.review_json` 现在会稳定落出：
  - `primary_responsibility`
  - `responsibility_labels`
  - `failure_taxonomy`
- `factor_contribution_review` 已通过结构化 `notes` 回写责任归因上下文，支持带出：
  - `primary_responsibility`
  - `responsibility_labels`
  - `factor_role`
  - `thesis_status_at_exit`
- learning sample / experience / factor outcome 对齐后，责任标签已能进入研究侧样本：
  - `decision_context_json`
  - `target`
  - `outcome_contribution`
  - `experience`
- `/api/risk/trade-trace` 与 `/api/learning/reviews` 已统一把 Phase D 责任字段作为稳定出口暴露
- 小程序复盘页已增加“主要责任 / 退出结论 / 责任标签”人话展示，不再只显示裸 review 字段
- 测试通过：
  - `python -m pytest tests/test_failure_taxonomy.py tests/test_learning_backfill.py tests/test_review_contract_api.py tests/risk/test_risk_api_policy.py tests/research/test_rule_learning_pipeline.py -q`
  - `python -m compileall alpha/reflection/reviewer.py backend/services/learning_backfill.py backend/services/failure_taxonomy.py backend/services/review_contract.py backend/api/learning.py backend/api/risk.py research/learning/experience_builder.py research/features/feature_provider.py`

新发现的缺口 / 后续子任务：

- 当前 taxonomy 仍保持保守阈值，部分历史样本在上下文不全时会落到 `unclear / partial`
- 更细粒度的 `tp_too_far / sl_too_tight` 等退出责任，还需要在 Phase E 的参数模板治理里继续细分

---

## 6. Phase E：因子治理与参数模板

状态：`已完成（远程验收通过，真实样本观察中）`  
优先级：`P1`  
前置条件：`Phase D 基本落地`

目标：建立正式的“因子教练层”。

### E1：因子解释卡片 schema

状态：`已完成`

完成日期：`2026-06-25`

每个因子至少要有：

- `factor_id`
- `factor_family`
- `formula_version`
- `parameter_version`
- `parameters`
- `expected_regimes`
- `weak_regimes`
- `expected_holding_profile`
- `failure_modes`

产出物：

- `docs/factor-card-schema.md`
- `backend/services/factor_cards.py`
- `backend/api/learning.py`

验证结果：

- `factor_card.v1` 已正式固化：
  - `factor_id / factor_family / formula_version / parameter_version`
  - `expected_regimes / weak_regimes / expected_holding_profile`
  - `failure_modes / governance_state / evidence_summary`
- `/api/learning/factor-cards` 已可稳定输出首批只读 factor card 列表
- factor card 当前已能复用并汇总：
  - `factor_registry`
  - `registry_adapter._meta`
  - `registry_adapter._lifecycle_statuses`
  - `factor_health`
  - `decision_factor_snapshot`
  - `factor_contribution_review`
  - `policy_suggestion`
  - `learning_application_log / effect`
- 已完成首批 `factor_family / regime / holding_profile` 枚举映射
- 测试通过：
  - `python -m pytest tests/test_factor_cards_api.py tests/test_review_contract_api.py tests/test_failure_taxonomy.py tests/test_learning_backfill.py tests/research/test_rule_learning_pipeline.py -q`
  - `python -m compileall backend/services/factor_cards.py backend/api/learning.py`

新发现的缺口 / 后续子任务：

- 当前 factor family 与参数推断仍以启发式为主，后续需要为重点因子补更明确的手工元数据
- E2 需要把 factor card 上的基础参数继续抽成正式的 parameter template 对象，并接上风控动作与审批链

### E2：参数模板系统

状态：`主链已完成，扩展覆盖观察中`

目标：

- 把“参数”从散点配置变成版本化模板
- 支持 regime-aware 参数切换

当前进展：

- 已固化 `parameter_template.v1` contract：
  - `docs/parameter-template-contract.md`
- 已新增首批只读模板装配服务：
  - `backend/services/parameter_templates.py`
- `/api/learning/parameter-templates` 已能基于 `factor_card.v1` 输出：
  - `default.v1`
  - `conservative.v1`
  - `aggressive.v1`
- 模板现在已带出：
  - `template_role`
  - `formula_version`
  - `base_parameter_version`
  - `applicable_regimes / avoid_regimes`
  - `holding_profile_hint`
  - `tuning_bias`
  - `evidence.last_primary_responsibility / recent_responsibility_labels`
- `RiskPolicyService` 已正式支持：
  - `switch_parameter_template`
  - 当前先复用治理侧 `allow_weight_update` 阈值作为首版 guardrail，保证模板切换动作能先进入统一风控审计入口
- 参数模板已从派生只读对象推进到可持久化对象：
  - `backend/core/db.py` 新增：
    - `parameter_template_registry`
    - `parameter_template_active`
    - `parameter_template_switch_log`
- 已补参数模板治理链最小闭环：
  - `POST /api/learning/parameter-templates/upsert`
  - `POST /api/learning/parameter-templates/suggest-switch`
  - `POST /api/learning/parameter-templates/apply-switch`
  - `GET /api/learning/parameter-templates/active`
  - `GET /api/learning/parameter-templates/switch-logs`
- 模板切换现在已支持：
  - 先生成 `policy_suggestion(scope_type='parameter_template')`
  - 经现有 governor 审批后再执行
  - 执行时进入统一 `switch_parameter_template` risk verdict
  - 激活状态与切换证据会正式落库
- `learning_application_log` 已开始记录 `scope_type='parameter_template'` 的应用日志，后续 E4 可继续在此基础上补审批与回滚策略
- 重点因子已补首批手工模板库，当前至少包括：
  - `rsi_14`
  - `macd_hist`
  - `adx`
  - 手工模板现在会优先于启发式派生模板输出
- `parameter_template` 的 application effect 已进入正式观测与回滚逻辑：
  - governor 现在会对 `scope_type='parameter_template'` 计算 post/baseline reward delta
  - 当模板切换后效果恶化时，会自动：
    - 回滚对应 `policy_suggestion`
    - 回切 `parameter_template_active`
    - 追加 `parameter_template_switch_log(status='rolled_back')`
  - 当效果足够正向时，也会生成对应的 reinforce suggestion
- 测试通过：
  - `python -m pytest tests/test_factor_cards_api.py tests/risk/test_policy_service.py tests/test_review_contract_api.py tests/test_failure_taxonomy.py tests/test_learning_backfill.py tests/research/test_rule_learning_pipeline.py tests/research/test_rule_evolution_governor.py -q`
  - `python -m compileall backend/core/db.py backend/services/factor_cards.py backend/services/parameter_templates.py backend/api/learning.py risk/policy_service.py research/learning/governor.py`

收口结论：

- 参数模板已经从只读派生对象进入持久化、审批、激活、运行态同步与回滚链路
- 首批重点因子和 runtime override 已覆盖主路径，后续“更多手工模板 / 更多 regime-specific 模板”不再阻塞 Phase E 收口
- 继续扩覆盖面归为观察 / 后续增强项

### E3：在线轻调 / 离线深调边界

状态：`主链已完成，真实样本观察中`

要明确：

- 哪些可以在线轻调
- 哪些必须离线验证后发布

当前进展：

- active parameter template 现在已正式接入运行态：
  - `activate_template()` 会同步 patch `RuntimeConfig.factor_signal_config`
  - app 启动时也会从 DB 的 active template 重新同步回 RuntimeConfig
- `StreamingFactorEngine` 已支持读取 `factor_signal_config.parameter_overrides`，当前首批已接线：
  - `rsi_14`
  - `macd_hist`
  - `adx`
- live factor pipeline 的 RuntimeConfig 订阅现在会热更新：
  - engine runtime config
  - normalizer configs
  - compositor merged configs
- 已新增边界判定入口：
  - `POST /api/learning/parameter-templates/boundary-check`
- `suggest-switch / apply-switch` 现在已正式接入边界闸门：
  - `create_switch_suggestion` 会把 `boundary / approval_path` 写进 suggestion evidence
  - `apply-switch` 对 `offline_deep` 会正式阻断直接上线，避免绕过离线验证链
- 已新增离线验证作业入口：
  - `POST /api/learning/parameter-templates/offline-validate`
  - 会创建 `parameter_template_validation` job
  - 已复用现有 backtest sweep
  - 已补 `purged walk-forward` 报告
  - 已把通过离线验证的结果登记为 `pending_review` 的 gray-release candidate
- 已新增离线候选查询入口：
  - `GET /api/learning/parameter-templates/offline-candidates`
- 已新增 gray-release candidate 审批/发布/回滚入口：
  - `POST /api/learning/parameter-templates/offline-candidates/review`
  - `POST /api/learning/parameter-templates/offline-candidates/release`
  - `POST /api/learning/parameter-templates/offline-candidates/rollback`
  - 发布动作底层仍复用 `switch_parameter_template` risk verdict 与 runtime sync
- gray-release candidate 现已进入 learning summary 与学习页展示：
  - `/api/learning/summary` 已增加 `parameter_template_candidates / latest_parameter_template_candidate`
  - `miniprogram_v2/pages/learning` 已可查看模板候选列表与详情
- 参数模板建议现已进入学习页审批详情：
  - 建议详情会显示 `在线轻调 / 离线深调` 边界结论
  - 会显示边界原因与建议应走的审批路径
- gray-release candidate 的审批结论现已进入统一生命周期与因子治理态：
  - `parameter_template_validation` 会把 `registered / reviewed / deployed / rolled_back` 写入 `lifecycle_events`
  - `FactorCardService.governance_state.template_state` 已可反映 `review_pending / review_approved / deployed / rolled_back`
- 已固化边界文档：
  - `docs/parameter-tuning-boundary.md`
- 当前边界规则已正式区分：
  - `online_light`
  - `offline_deep`
- 首版 `online_light` 约束已经写死：
  - 仅限当前 runtime-tunable 因子
  - `formula_version` 不变
  - `factor_family` 不变
  - 参数跳变不超过 35%
  - 模板角色属于 `default / conservative / aggressive`
- runtime parameter override 覆盖已从首批 3 个因子扩到：
  - `rsi_14 / macd_hist / adx`
  - `stoch_k / ema_slope / bb_width / obv_slope / vol_ma_ratio / supertrend_str / keltner_width`
- 测试通过：
  - `python -m pytest tests/test_factor_cards_api.py tests/alpha/test_streaming_factor_engine.py tests/test_runtime_config.py tests/risk/test_policy_service.py tests/test_review_contract_api.py tests/test_failure_taxonomy.py tests/test_learning_backfill.py tests/research/test_rule_learning_pipeline.py tests/research/test_rule_evolution_governor.py -q`
  - `python -m compileall alpha/streaming_factor_engine.py alpha/signal_normalizer.py alpha/portfolio_compositor.py backend/services/parameter_templates.py backend/services/live_service.py backend/app.py research/learning/governor.py backend/api/learning.py`

收口结论：

- 在线轻调 / 离线深调边界已正式写死并接入 `suggest-switch / apply-switch / recommendation materialize`
- `offline_deep` 已阻断直接上线，并要求先走离线验证、候选审批、灰度发布
- runtime parameter override 已覆盖首批主路径因子；更复杂离散/事件类因子是否模板化归为后续评估，不阻塞 Phase E 主链

### E4：因子治理工作流

状态：`主链已完成，运维入口继续观察`

链路目标：

参数可疑证据 -> 治理建议 -> 回测/验证 -> 审批 -> 灰度 -> 发布/回滚

已完成：

- `ParameterTemplateService.list_recommendations()` 已能从 factor card 的参数可疑证据生成模板推荐：
  - 识别 `primary_responsibility=parameter`
  - 识别 `factor_logic_ok_but_param_suspect`
  - 输出目标模板、边界结论、审批路径、建议动作
- `GET /api/learning/parameter-templates/recommendations` 已可供前端读取
- 学习页已开始展示“参数模板建议”，把参数可疑证据直接接到模板治理入口
- `create_switch_suggestion(...)` 现在会携带 `factor_card_evidence / evidence_context`
- 推荐项现在已可 materialize 成正式 `switch_parameter_template` suggestion：
  - `POST /api/learning/parameter-templates/recommendations/materialize`
  - 学习页“参数模板建议”详情已可直接触发“生成治理建议”
- `recommendations/materialize` 现在已按边界自动分流：
  - `online_light` -> 生成正式 `switch_parameter_template` suggestion
  - `offline_deep` -> 直接创建 `parameter_template_validation` job
- `offline_deep` recommendation 现在会把来源 trace 带进：
  - validation report `recommendation_context`
  - release candidate `validation_summary.recommendation_source`
  - 学习页模板候选的人话展示
- factor card `governance_state` 现在也会暴露：
  - `latest_template_candidate_trace`
  - `latest_template_recommendation`
- learning lifecycle 聚合现在也会为参数模板候选事件附带：
  - `metrics.candidate_trace.recommendation_id`
  - `metrics.candidate_trace.responsibility`
  - `metrics.candidate_trace.approval_path`
- `/api/learning/summary` 现已新增统一人话运维摘要：
  - `parameter_template_ops_summary`
  - 会把 recommendation / candidate / approval path 压成单句摘要，供 overview 等聚合入口直接消费
- 概览页“参数治理”现已改为直接消费统一摘要，不再在前端各自拼 recommendation 与 trace 文案
- 学习页现已同步接入参数治理统一摘要，并新增“参数治理轨迹”：
  - 直接消费 `summary.parameter_template_ops_summary`
  - 把 lifecycle 里的 `parameter_template` 事件翻译成推荐来源 / 审批路径 / 生命周期详情
- `trade-trace` 现已新增参数治理上下文：
  - 会按 `factor_contribution_review` 中的 `parameter` 责任与 `factor_logic_ok_but_param_suspect` 线索识别当前最值得治理的因子
  - 会继续串出对应模板候选、来源 recommendation 与人话 `ops_summary`
- 小程序 `ops` 页现已新增单笔交易证据查询入口：
  - 支持按 `position_id / decision_id` 查询 `/api/risk/trade-trace`
  - 可直接查看 `parameter_governance.ops_summary`、推荐来源与最新模板候选状态
- 小程序交易页现已可直接跳转到对应证据链：
  - 持仓卡片新增“查看证据链”
  - 会把当前 `position_id` 直接带到独立 `trade-trace` 页面
- `trade-trace` 页面现已具备完整逐步证据面板：
  - 决策账本
  - 持仓监督
  - 复盘归因
  - 参数治理 / 因子贡献
- `trade-trace` 页面现已继续补齐执行与恢复证据：
  - 仓位生命周期
  - 订单执行
  - 恢复状态
- `trade-trace` 页面现已增加分段折叠：
  - 核心摘要默认展开
  - 长列表证据（账本 / 因子 / 仓位 / 订单 / 恢复）可按段展开，避免页面过重
- `trade-trace` 页面现已新增统一时间线：
  - 会把 ledger / supervisor / position / order / review / governance 按时间合并
  - 用户可以先看顺序，再按段展开细节证据
- `ops` 页现已保留最近查询记录：
  - 最近查询过的 `position_id / decision_id` 会保留在页面上
  - 支持一键回放对应证据链
- `ops` 页现已保留最近交易样本列表：
  - 后端会提供最近 review 样本索引
  - 前端可直接按样本打开对应 trace，并带出责任归因与参数治理提示
- `ops` 页中的最近交易样本现已支持轻筛选：
  - `全部 / 参数问题 / 退出问题 / 带治理提示`
  - 方便快速定位参数治理相关样本
- `ops` 页中的最近交易样本现已支持文本搜索：
  - 可按 `position_id / trade_id / 摘要 / 因子` 搜索
  - 可与责任筛选组合使用
- `trade-trace` 现已独立成专门页面：
  - Ops 页保留最近样本、搜索筛选、手动查询与最近查询入口
  - Trading 页与 Ops 页都会直接跳到独立证据链页面查看完整 timeline / ledger / supervisor / review / governance 细节
- 学习页 / 因子页现已补上参数治理到交易证据的反查联动：
  - recommendation / offline candidate / parameter lifecycle event 会携带统一 `trace_locator`
  - 可从 Learning / Factors 详情直接打开来源 `trade-trace`，把治理对象重新钉回真实交易样本
- Factors 页现已继续补上治理阶段与治理对象入口：
  - lifecycle 卡片和详情里会直接显示 `在线轻调 / 离线深调 / 待审候选 / 等待发布 / 发布观察 / 已回滚`
  - 也会标出当前对应的是 `模板候选` 还是 `参数推荐`
  - 用户可从 lifecycle 详情直接跳到对应的 Learning 治理对象，不必先转回别的聚合页
- 独立 `trade-trace` 页面现已补上反向治理跳转：
  - 当证据链里识别到 recommendation / candidate 时，可直接切到 Learning 页并自动展开对应治理对象
  - 形成“治理页 -> 证据链 -> 治理详情”的双向往返闭环
- Overview / Ops 现已补上更广的参数治理入口：
  - Overview 可直接打开最新参数治理对象
  - Ops 最近交易样本若带治理提示，也可一键进入对应 Learning 审批详情
- Overview / Ops 现已开始直接暴露参数治理待办：
  - 可看到待审候选数量、当前推荐数量
  - 可一键打开首个待审 candidate / 推荐项，不必先进入 Learning 再手动查找
- Overview / Ops 现已把参数治理待办继续分层：
  - 显式区分 `待审候选 / 在线轻调推荐 / 离线深调推荐`
  - 用户能更快知道当前是“可直接推进上线”，还是“必须先走离线验证链”
- Learning / Overview / Ops 现已继续把参数治理待办翻译成“下一步动作摘要”：
  - recommendation / candidate / lifecycle detail 会直接显示下一步该走的审批或验证动作
  - Overview / Ops 不只显示数量，也会提示“先人工审核 / 先离线验证 / 先生成治理建议”
- Overview 现已继续补上聚合态的人话治理摘要：
  - 首页治理状态会优先聚焦当前最重要的对象：`候选待审 / 在线轻调 / 离线深调 / 待审核经验 / 已形成可用经验`
  - 参数治理待办里的 hint 也已显式带上 `待审候选 / 在线轻调 / 离线深调 / 发布观察` 这类阶段词，减少只看数量时的信息损失
- 独立 `trade-trace` 页面现已把治理阶段和下一步动作也抬到证据汇总：
  - 用户在证据链里就能直接看到当前是“在线推荐 / 离线推荐 / 待审候选 / 已发布观察”
  - 并能据此决定是先看 recommendation 还是 candidate，而不用先切回 Learning 再判断
- Ops 最近交易样本现已直接显示治理阶段与下一步动作：
  - `trade-trace/recent` 索引已带出统一 `stage / next_step / entry_type`
  - 运维入口可在样本列表里直接看到“等审核 / 等发布 / 先离线验证 / 先生成治理建议”
- Ops 最近交易样本现已支持按治理阶段继续筛选：
  - 可直接筛 `待审候选 / 推荐阶段 / 发布观察`
  - 样本卡片上的治理按钮也会按当前入口切成“看候选 / 看建议”
- Ops 顶部“参数治理待办”卡片现已带阶段和动作按钮：
  - 待办卡片会直接显示当前是“候选待审 / 等待灰度发布 / 在线轻调推荐 / 离线深调推荐”
  - 按钮文案会按当前动作切成“去审核 / 去发布 / 去生成建议 / 去做验证”
- Ops 最近交易样本筛选现已进一步区分 `在线轻调 / 离线深调`：
  - 不再只停在笼统“推荐阶段”，而是可直接分辨当前该走在线审批还是离线验证链
- Ops 最近交易样本卡片现已继续补上审批态和动作摘要：
  - 卡片会直接显示 `在线轻调 / 离线深调 / 待审候选 / 等待发布 / 发布观察 / 已回滚`
  - 也会显式标出当前对应的是 `模板候选` 还是 `参数推荐`
  - 治理按钮文案已按当前阶段细化成 `去审候选 / 去发布 / 看观察 / 去审建议 / 去做验证`
- Learning 详情里的离线候选现已接入真实动作按钮：
  - `pending_review / approved / deployed` 会分别显示 `批准/拒绝 / 发布 / 回滚`
  - recommendation 详情也已补上“已生成后下一步该去哪看”的状态提示，减少重复点击
- Learning 里的 recommendation 详情现已接入“完成态”联动：
  - 会自动识别这条推荐是否已经生成 suggestion、落成 candidate，或进入 lifecycle
  - 已处理完成的推荐按钮会自动禁用，并提供直达对应建议 / 候选 / 轨迹的入口
- Learning 里的 lifecycle 详情现已补上回跳入口：
  - lifecycle trace 会显式带出 `candidate_id`
  - 用户可从 lifecycle 直接回到对应 candidate 或 recommendation，不必手动翻列表
- Learning 里的 suggestion 详情现已接入来源 / 后续回跳：
  - 若 suggestion 来自参数 recommendation，会显示“回到来源推荐”
  - 若该 recommendation 已继续落成 candidate，也可直接从 suggestion 跳到后续候选
- 独立 `trade-trace` 页面现已补上更细的审批联动入口：
  - 页面会结合 recommendation / suggestion / candidate / lifecycle 当前状态，自动判断最该跳去的治理对象
  - 若 recommendation 已 materialize 成 suggestion，可直接从证据链跳到 Learning 的 suggestion 审批详情
  - Learning 页也已支持按 `suggestion` 类型做聚焦展开，形成 `trade-trace -> suggestion 审批` 的闭环
- 独立 `trade-trace` 页面现已补上页内细筛选：
  - 时间线支持按 `全部 / 治理相关 / 决策监督 / 执行落地` 切换
  - 每条时间线事件会显式标注它属于 `治理推进 / 复盘归因 / 决策与监督 / 执行落地`
  - recovery 状态也已并入统一时间线，方便把重启恢复证据和下单/仓位事件放在一起看
- 独立 `trade-trace` 页面现已把治理跳转继续下沉到时间线项：
  - governance 事件会直接显示当前对应的治理对象类型，并提供直达按钮
  - 若这笔交易的 review 已收敛到参数问题，复盘事件本身也可直接跳去 suggestion / candidate / recommendation
  - 用户不必先回到顶部摘要，就能在时间线里边看边推进审批链
- 独立 `trade-trace` 时间线现已进一步显式标注审批态：
  - 治理相关事件会直接显示 `在线轻调 / 离线深调 / 待审候选 / 等待发布 / 发布观察 / 已回滚`
  - 复盘事件若已进入参数治理，也会同步带出当前审批态，减少“先看完再判断该去哪”的心智负担
- 独立 `trade-trace` 时间线现已支持治理态二级筛选：
  - 在“治理相关”视图下，可继续细分筛 `在线轻调 / 离线深调 / 待审候选 / 等待发布 / 发布观察 / 已回滚`
  - 每个二级筛选会带自己的数量与人话摘要，页面更接近真正的参数治理待办视图
- 小程序治理前端现已抽出统一文案层：
  - 新增 `miniprogram_v2/utils/governance.js`
  - `overview / ops / factors / trade-trace` 现统一复用阶段标签、阶段摘要、对象类型与动作按钮映射
  - 已消除 `等待灰度发布 / 发布后观察 / 已回滚待复核 / 在线轻调推荐` 这类页面间漂移说法，后续继续扩审批态时不必重复维护
- 独立 `trade-trace` 摘要区现已继续前进一步做成治理待办队列：
  - 会把当前最该处理的治理对象单独抬成“主任务”，明确说明为什么先看它
  - recommendation / suggestion / candidate / lifecycle 若同时存在，也会以“备选入口”并排给出
  - 页面不再只是“能跳到治理对象”，而是开始直接给出参数治理推进顺序
- Ops 最近交易样本现已开始按治理优先级聚合：
  - 列表会优先把 `待审候选 / 等待发布 / 离线深调 / 在线轻调 / 发布观察 / 已回滚` 这类样本顶到前面
  - 交易证据查询区会额外抬出“当前最该处理的样本”卡片，直接给出优先动作
  - 运维入口开始从“看到治理提示”进一步变成“先处理哪条样本”的轻量待办视图
- 参数治理前端现已开始统一“待办优先级”规则：
  - `miniprogram_v2/utils/governance.js` 新增共享的优先级与待办构建 helper
  - `ops / trade-trace` 不再各自维护一套“谁更该先处理”的散落排序逻辑
  - 后续若把同样的优先级提示继续扩到 `overview / learning`，可以直接复用同一套规则
- `overview / learning` 现也已开始接共享治理优先级：
  - Overview 首页参数治理区会直接抬出“当前主待办”，不再只展示分散的候选/推荐数量
  - Learning 里的参数推荐与离线候选列表会按同一套优先级规则排序，并显示优先级标签
  - 首页聚合入口与治理详情页开始对齐“谁最该先处理”的判断
- 后端 `trade-trace/recent` 索引现已开始正式输出治理优先级字段：
  - `parameter_governance_stage / target_type / action_label / priority_score / priority_label / priority_summary` 已直接由后端生成
  - `ops` 页现优先消费后端 recent 索引给出的治理优先级与动作，不再完全依赖前端自行推断
  - 前后端在 recent 样本这条链路上的治理排序与动作语义开始真正对齐
- 单笔 `trade-trace` 明细现也开始对齐同一套治理派生语义：
  - `parameter_governance` 自身已直接带 `stage_label / target_type / action_label / priority_label / priority_summary`
  - `trade-trace` 摘要区现优先消费后端明细给出的治理阶段、动作与优先级标签
  - recent 列表、首页聚合与单笔证据详情开始逐步共用同一套后端治理语义
- 单笔 `trade-trace` 的后端治理对象现已继续补齐 suggestion / lifecycle：
  - `parameter_governance` 会直接带最新 `suggestion` 与 `lifecycle_event`
  - `trade-trace` 前端现优先使用后端明细里给出的 suggestion / lifecycle，再回退到本地 learning store 搜索
  - 参数治理详情页继续从“前端本地拼链路”往“后端直接给出链路对象”收口
- `ops / overview` 顶部主待办卡片现已开始直接消费后端 summary 聚合对象：
  - `/api/learning/summary` 新增 `parameter_template_todo`
  - Overview 首页与 Ops 参数治理待办区会优先使用这个后端聚合对象，而不是完全在前端从候选/推荐列表里重拼
  - 参数治理的顶部入口开始进一步从“前端聚合”向“后端统一出语义对象”收口
- Learning 页摘要区现也开始直接消费后端 `parameter_template_todo`：
  - 学习页“当前主待办”卡片会优先展示后端统一给出的治理对象、阶段、动作与优先级摘要
  - 闭环步骤里的“模板候选”阶段说明也会优先复用这张待办卡，而不是只按本地列表数量推断
  - `overview / ops / learning` 顶部治理入口开始共享同一份后端待办语义，进一步减少前端重复拼装
- Learning 页中的 recommendation / offline candidate 列表现也开始优先消费后端治理语义：
  - `/api/learning/parameter-templates/recommendations` 现已补出 `governance / progress / suggestion / latest_candidate / lifecycle_event`
  - `/api/learning/parameter-templates/offline-candidates` 现已补出统一 `governance` 字段
  - 学习页 recommendation / candidate 的阶段、下一步、优先级与完成态开始优先使用后端给出的聚合语义，前端本地推导退居 fallback
- `trade-trace` 页面现也开始把“该跳去哪个治理对象”继续下沉到后端：
  - `parameter_governance` 现已补出 `governance_jump / governance_todo_queue`
  - 后端会直接给出当前主跳转对象、按钮文案，以及主待办/次待办队列
  - `trade-trace` 前端现优先消费这组后端 jump/todo 语义，只在缺字段时才回退到本地 recommendation/candidate/suggestion 推断
- `trade-trace` 时间线项上的治理提示也开始继续下沉到后端：
  - `parameter_governance` 现已补出 `timeline_context`
  - 后端会直接给出 governance/review 时间线项该显示的阶段标签、阶段摘要、跳转按钮与跳转摘要
  - 时间线里的治理标签与“按复盘继续治理”文案开始优先使用后端聚合语义，前端本地分支进一步收缩
- `trade-trace` 顶部治理入口现也开始继续改成后端驱动：
  - `parameter_governance` 现已补出 `entry_context / quick_actions`
  - 顶部“建议入口 / 查看治理建议 / 查看模板候选”开始优先绑定后端给出的入口对象和快捷动作
  - `trade-trace` 顶部区域不再完全依赖前端自己拼 recommendationId / candidateId，治理入口语义进一步统一
- `trade-trace` 顶部治理摘要现也进一步收口到后端聚合字段：
  - `describeGovernanceAction` 现优先直接消费 `entry_context + stage_label/next_step_summary`
  - 前端已移除对 candidate / recommendation 状态的大段本地逐分支复刻，只保留通用 fallback
  - 顶部治理摘要与按钮区开始更明确地以同一份后端 `parameter_governance` 聚合对象为准
- `trade-trace` 对 learning store 的参数治理补链依赖现已进一步移除：
  - `resolveGovernanceTargets / describeGovernanceJump / buildGovernanceTodo` 现在直接以 `parameter_governance` 中的后端聚合对象为准
  - 查询单笔 `trade-trace` 时，前端已不再为 suggestion / lifecycle fallback 额外刷新 learning store
  - 参数治理证据链页面开始更接近纯消费后端聚合结果，前端跨页补链逻辑继续收缩
- `trade-trace` 的 jump/todo fallback 现也继续压缩到最小：
  - `describeGovernanceJump / buildGovernanceTodo` 在后端未返回完整对象时，只保留最小主入口 fallback
  - 前端已不再本地重建完整 suggestion / recommendation / candidate / lifecycle 待办队列
  - 参数治理待办优先级和入口排序继续收口到后端 `governance_jump / governance_todo_queue`
- `trade-trace` 时间线项的本地阶段判断现也继续缩薄：
  - `attachTimelineGovernanceLink` 现优先直接使用后端 `timeline_context`
  - governance/review 时间线项在缺少后端细字段时，只回退到统一 `governanceAction.stageLabel`，不再本地按 candidate/recommendation 逐状态推导
  - 时间线里的治理标题、摘要和跳转提示开始进一步与后端聚合语义保持单源一致
- `trade-trace` 顶部摘要现也开始统一吃后端 `governance_overview`：
  - `parameter_governance` 现已补出 `overview`
  - 顶部 `ops_summary / target_type / action_label / priority / candidate trace` 等散落字段开始优先从这份总览对象读取
  - 证据汇总区的治理文案进一步从“多字段拼装”往“单一后端聚合对象”收口
- `trade-trace` 顶部模板层现也开始直接绑定 `governanceOverview`：
  - 顶部卡片里的 `ops_summary / priority / target_type / action_label / candidate trace / latest candidate status / entry_label` 已优先走总览对象
  - WXML 对散落 view-model 字段的依赖继续下降，页面层更接近直接消费后端总览语义
- `trade-trace` 页面层现已补上统一 `governanceOverviewView`：
  - 会把后端 `overview` 与必要 fallback 收成单一 view-model
  - 顶部 WXML 不再写大量 `A || B || C` 绑定表达式，模板层继续从“散字段”往“单对象”收口
- `trade-trace` 模板层现已进一步摆脱 `governanceAction` 直绑：
  - 顶部治理阶段卡、最新候选摘要、建议入口说明现统一从 `governanceOverviewView` 读取
  - `governanceAction` 继续只留在页面逻辑里做最小 fallback，不再暴露成模板主绑定面
- `trade-trace` 时间线筛选态现已补上统一 `timelineView`：
  - 筛选按钮 active 态、筛选摘要、治理态二级筛选摘要与可见事件列表，现统一收进 `timelineView`
  - 页面 data 不再单独散落维护 `visibleTimelineItems / timelineFilterSummary / governanceStageFilterSummary`
- `trade-trace` 时间线筛选的人话语义现也开始继续下沉到后端：
  - `parameter_governance` 现已补出 `timeline_filter_context`
  - 后端会统一给出 `全部 / 治理相关 / 决策监督 / 执行落地` 以及各治理态筛选的标签与摘要模板
  - 前端现优先消费这组筛选语义，只负责结合当前计数完成展示，不再完全本地硬编码整段筛选说明
- `trade-trace` 页面逻辑里的 `governanceAction` 现也已进一步退场：
  - `governanceOverviewView` 现直接吸收 `overview / entry_context / parameter_governance` 的 fallback 语义
  - `governanceTodo` fallback、时间线治理 fallback、governance 原始事件标题与摘要，现统一改读 `governanceOverviewView`
  - 页面返回对象已不再暴露独立 `governanceAction`，前端本地治理中间层继续收缩
- Overview 首页参数治理摘要现也继续往后端收口：
  - `/api/learning/summary` 现已补出 `parameter_template_overview`
  - 后端会统一给出首页治理 headline、`待审候选 / 在线轻调 / 离线深调` 三条 hint，以及对应 candidate/recommendation 入口 locator
  - Overview 页现优先消费这份 overview 聚合对象，只在缺字段时才回退本地 `offlineCandidates / templateRecommendations` 推断
- Ops 页参数治理卡片现也开始复用同一份 summary overview：
  - 运维入口里的 `待审候选 / 在线轻调 / 离线深调` 三张卡现优先消费 `parameter_template_overview`
  - 卡片标题、阶段摘要、动作按钮与跳转 locator 不再完全依赖本地 `offlineCandidates / templateRecommendations` 首项推断
  - Overview / Ops 顶部参数治理入口开始共享同一份后端 hint 语义，页面间说法进一步收敛
- Ops 最近交易样本区域现也继续压缩本地治理 fallback：
  - 样本卡片里的 `stage / stage_summary / next_step / target_type / action_label / priority` 现优先直接读后端 recent 索引字段
  - 前端本地 `describeGovernance*` 推导已退到缺字段兜底，不再作为最近样本文案的主来源
  - 运维页 recent 列表与后端 `trade-trace/recent` 的治理语义进一步接近单源一致
- Learning 页里的候选 / 推荐 next-step 文案现也继续退到 fallback：
  - `offline candidate / template recommendation` 详情与列表现优先直接消费后端 `governance.stage_summary / next_step / action_label / priority`
  - 前端本地 next-step 叙事现主要只保留给缺字段 fallback
  - Learning 页里最重的参数治理人话逻辑开始进一步让位给后端聚合语义
- Learning 页里的 lifecycle 事件 next-step 现也开始后端化：
  - `/api/learning/lifecycle` 的参数模板事件现已补出 `governance.stage_label / stage_summary / next_step / action_label`
  - lifecycle 列表与详情现优先直接消费这组后端治理语义，不再默认走前端本地事件状态映射
  - Learning 页参数治理三条主对象链 `recommendation / candidate / lifecycle` 的 next-step 文案开始进一步对齐成后端单源
- Learning 页里的 suggestion progress 语义现也开始后端化：
  - `/api/learning/suggestions` 的参数模板建议现已补出 `progress`
  - 后端会统一给出 suggestion 当前是“来自来源推荐 / 已进入模板候选 / 可回跳对象”的状态摘要与回跳 target
  - Learning 页 suggestion 详情现优先消费这组后端 progress 语义，前端本地 recommendation/candidate 关系拼装继续退到 fallback
- Overview 首页遗留的治理 fallback helper 现也继续缩薄：
  - `buildOverviewGovernanceTodo` 不再本地重算 candidate / recommendation 的阶段、动作和优先级
  - 即使走 fallback，首页也会优先复用候选 / 推荐对象自带的后端 `governance` 字段
  - 首页参数治理入口从主路径到 fallback 路径都进一步朝“后端语义单源”靠拢
- Overview 首页参数治理入口现也继续收成后端 hint/todo 单源：
  - 首页不再在页面层本地扫描 `offlineCandidates / templateRecommendations` 去补 `governanceTodoCard`
  - `待审候选 / 在线轻调 / 离线深调` 三个入口缺少后端 hint 时默认直接留空，不再按 `status / recommended_scope` 重新挑第一个对象
  - 首页参数治理从“后端 hint + 本地列表兜底”继续收口到“后端 summary.overview / todo 单源驱动”
- Overview / Ops 三张参数治理 hint 卡的动作标签也继续统一：
  - `parameter_template_overview.pending_candidate_hint / online_light_hint / offline_deep_hint` 的 `action_label` 现直接复用对应 `governance.action_label`
  - 不再在 summary overview helper 里单独维护“去审核 / 去生成建议 / 去做验证”这套按钮文案
  - 首页与运维页入口卡继续从“overview hint 自己拼动作”往“统一 governance 动作语义”收口
- Overview / Ops 的参数治理计数现也统一改吃后端 summary：
  - 首页与运维页不再本地按 `pending_review / online_light / offline_deep` 过滤列表去算数量
  - `pendingTemplateCandidateCount / pendingTemplateRecommendationCount / onlineLightRecommendationCount / offlineDeepRecommendationCount` 现统一直接来自 `summary.parameter_template_candidates / parameter_template_recommendations`
  - 参数治理聚合入口继续从“后端 hint + 前端本地计数”往“后端 summary 全量聚合”收口
- Overview 首页参数治理摘要现也继续去掉页面层本地拼句：
  - `templateOpsSummary` 现直接以 `summary.parameter_template_ops_summary` 为准
  - 首页不再在缺字段时本地拼“当前模板推荐 N 条，其中在线 X / 离线 Y”这类数量摘要
  - 参数治理首页摘要继续从“后端给 counts + 前端补一句话”往“后端直接给 ops summary”收口
- Overview / Learning 的参数治理空态摘要现也继续退场：
  - 首页 `governanceHeadlineSummary` 与 Learning 页 `templateOpsSummary` 现直接绑定后端 summary 默认文案
  - 页面层不再额外兜底“当前还没有新的参数治理对象 / 当前还没有新的参数模板推荐或候选发布动作”
  - 参数治理聚合页继续从“后端给主语义 + 前端补空态文案”往“后端 summary 单源”收口
- `trade-trace` 顶部对象级空态现也继续后移到后端：
  - 当单笔交易没有参数治理对象时，后端现也会返回最小 `parameter_governance.overview` 空态对象
  - 页面层已删掉 `未进入治理链 / 当前这笔交易还没有进入参数治理链` 本地兜底文案
  - 单笔证据页参数治理总览现进一步做到“即使是空态，也由后端 overview 单源给出”
- Learning 后端里的 progress 关系拼装现也开始统一收敛：
  - recommendation progress 已抽到共享 helper，不再在接口里单独手写 candidate / suggestion / lifecycle 关系分支
  - suggestion / recommendation 两条对象链的“后续落到哪里、该回跳去哪”语义开始更接近同一套后端拼装方式
  - 后续若继续收前端 fallback，可直接围绕这组后端 progress helper 扩展，而不必重复散落实现
- Overview / Learning 两处页面层 fallback 现也继续缩薄：
  - Overview 首页 headline 与三条 hint 现默认直接消费后端 `parameter_template_overview`，不再在页面层重建本地阶段摘要兜底
  - Learning 页 suggestion / recommendation progress 现优先依赖对象自带的后端 `progress / suggestion / latest_candidate / lifecycle_event`
  - Learning lifecycle 详情的 next-step 现也默认直接读后端 `governance.next_step_*`，前端跨列表关系拼装与事件状态映射进一步退场
- Ops 页参数治理 fallback 现也继续往后端 recent / governance 字段收口：
  - recent trade trace 样本卡片里的 `stage / stage_summary / action_label / target_type / priority` 现直接以前端拿到的后端 recent 字段为主，不再在页面层重推一套文案
  - `待审候选 / 在线轻调 / 离线深调` 三张卡在 summary hint 缺失时，也会优先复用对象自带 `governance` 字段，而不是重新按 `status / boundary` 拼人话 next-step
  - Ops 运维页参数治理区继续从“本地 helper 推断”往“summary + recent + item.governance 单源消费”收口
- Ops recent trace 的治理入口提示也继续后端化：
  - `trade-trace/recent` 现已新增 `parameter_governance_entry_hint_text`
  - Ops 样本卡片不再按 `parameter_governance_entry_type` 本地分叉成“建议先看模板候选 / 建议先看治理建议”
  - 运维页最近样本入口继续从“后端给类型 + 前端拼提示”往“后端直接给展示级入口提示”推进
- Ops 运维页参数治理卡片现也继续缩掉列表兜底：
  - 三张参数治理卡片现只直接消费后端 `parameter_template_overview.*_hint`
  - 页面层不再本地从 `offlineCandidates / templateRecommendations` 挑第一条对象去兜底卡片内容
  - 运维页参数治理卡片入口继续朝“后端 summary hint 单源、前端只渲染”推进
- `trade-trace` 时间线治理 fallback 现也继续缩薄：
  - 治理态二级筛选摘要现优先直接消费后端 `timeline_filter_context.governance_stage_filters.*.summary`，前端不再本地补一套阶段摘要文案
  - 页内治理待办在缺失后端 `governance_todo_queue` 时不再本地重建主任务队列，默认等待后端统一给出待办语义
  - 时间线里的 governance/review 跳转提示现主要只认后端 `timeline_context`；缺少该对象时，前端只保留空态或总览对象级别的最小兜底，不再本地复刻完整治理跳转解释
- Factors 页 lifecycle 的参数治理审批态现也开始后端优先：
  - lifecycle 详情里的 `治理阶段 / 治理对象 / 动作按钮` 现优先直接消费事件自带的 `governance`
  - 页面不再按 `event / status / approval_path` 本地推一整套 candidate / recommendation 审批态文案
  - factor lifecycle 与 learning lifecycle 的参数治理人话语义进一步往同一份后端治理对象对齐
- `/api/learning/lifecycle` 的参数模板治理语义现也继续补完整对象：
  - `governance` 除了 `stage / next_step / action_label` 外，现已补出 `target_type / target_id / jump_type / button_text / candidate_id / recommendation_id`
  - Learning / Factors 生命周期详情现可以直接依赖这组后端跳转语义，而不必继续从 `candidate_trace` 本地拆目标对象
  - 参数模板 lifecycle 从“后端给阶段说明”进一步推进到“后端直接给阶段 + 跳转目标 + 按钮语义”的单源对象
- `trade-trace` 顶部治理总览现也继续削掉候选人话 fallback：
  - `governanceOverviewView` 里的 `latest_candidate_status_text / latest_candidate_trace_text / entry_label` 现默认直接以后端 `overview` 为准
  - 页面已不再本地根据 `latest_candidate.status / trace.responsibility` 重建“最新候选状态 / 来源推荐”说明
  - 单笔证据页顶部总览继续从“后端给阶段 + 前端补候选人话”往“后端 overview 单源对象”收口
- `trade-trace` 后端 `overview` 现也开始直接输出展示级语义：
  - `parameter_governance.overview` 现已补出 `entry_hint_text / latest_candidate_summary_text / show_stage_card`
  - `trade-trace` 顶部总览不再在页面层本地拼“建议入口 / 最新模板候选”展示文案，而是直接消费这组后端展示字段
  - 单笔参数治理总览继续从“后端给原始字段 + 前端组装展示字符串”往“后端直接给展示级 overview 对象”收口
- `trade-trace` 顶部与时间线里的治理默认文案现也继续后移到后端：
  - `parameter_governance.overview` 现会在已有治理上下文时直接给出默认 `stage / next_step` 展示文案
  - 页面层已删掉这组“参数问题待收敛 / 继续收敛证据”本地治理话术 fallback，改为直接消费后端 overview / timeline_context
  - 单笔证据页顶部卡片与 review 时间线项继续朝“后端给完整展示语义，前端只渲染”收口
- `trade-trace` 时间线筛选摘要也继续收口到后端：
  - 没有参数治理对象时，后端 `parameter_governance` 现在也会返回 `timeline_filter_context`
  - 页面层已删掉 `全部 / 治理相关 / 决策监督 / 执行落地` 筛选摘要的本地长句 fallback，默认用后端 `summary_template / empty_summary`
  - 单笔证据页时间线筛选继续从“前端按 count 拼摘要”往“后端提供筛选语义，前端只替换 count”推进
- Learning 页 candidate / recommendation 卡片的治理 fallback 现也继续退场：
  - `offline candidate / template recommendation` 卡片不再本地重建 `stage_summary / next_step / action_label / priority`
  - 页面现默认直接消费对象自带的后端 `governance` 字段，只保留状态说明和按钮呈现这类纯 UI 文案
  - Learning 参数治理主对象链继续从“前端 helper 推断”往“后端 governance 单源对象”收口
- Learning 页 recommendation / candidate 的展示 tone 与动作说明现也继续后移：
  - `/api/learning/*` 的参数模板 `governance` 现已补出 `stage_tone`，推荐对象额外补出 `action_summary / followup_hint`
  - Learning 页推荐/候选卡片不再按 `recommended_scope / status` 本地派生 `statusTone / actionText / actionDoneText`
  - 参数模板推荐详情继续从“后端给阶段 + 前端补边界人话”往“后端直接给展示语义”收口
- Learning 页 recommendation / candidate 的状态标签现也继续后移：
  - 参数模板 `governance` 现已补出 `status_label`
  - Learning 页 recommendation/candidate 卡片的 `statusLabel / governanceStageLabel` 现直接绑定后端治理对象，不再回退到本地 `humanizeBoundaryScope / humanizeCandidateStatus`
  - 参数模板状态展示继续从“后端给阶段字段 + 前端补状态标签”往“后端直接给完整状态语义”收口
- Learning 页 offline candidate 的动作按钮现也开始后端化：
  - 参数模板 candidate `governance` 现已补出 `action_buttons`
  - Learning 页候选详情不再按 `status` 本地决定 `approve / reject / release / rollback` 按钮集合
  - 离线候选动作语义继续从“前端按状态分叉”往“后端 governance 单源对象”推进
- Learning 页 offline candidate 的审核/发布展示现也继续后移：
  - 参数模板 candidate `governance` 现已补出 `review_display / deployment_display / rollback_display`
  - Learning 页候选详情不再按 `review.status / deployment.status / rollback.status` 本地拼人话说明
  - 离线候选详情继续从“前端拆 validation_summary 再解释”往“后端 governance 单源对象”收口
- Learning 页 recommendation / suggestion 的 progress fallback 现也继续退场：
  - recommendation / suggestion 详情现默认只消费后端 `progress`
  - 页面层不再在缺字段时本地拼“已进入候选 / 已生成建议 / 已进入轨迹 / 来自参数推荐”等后续状态语义
  - 参数模板后续落点继续从“后端 progress + 前端关系兜底”往“后端 progress 单源对象”收口
- Learning 页 lifecycle 的状态标签与 tone 现也继续后移：
  - 参数模板 lifecycle `governance` 现已补出 `status_label / stage_tone`
  - lifecycle 详情不再按 `registered / reviewed / deployed / rolled_back` 本地映射 event label 与 tone
  - 参数模板 lifecycle 展示继续从“前端按 event 解释”往“后端 governance 单源对象”推进
- `/api/learning/lifecycle` 的参数模板事件识别已对齐真实入库事件名：
  - 后端 governance helper 现在会把 `parameter_template_candidate_registered/reviewed/deployed/rolled_back` 归一到对应阶段
  - lifecycle 不再因为事件名前缀走 `neutral` 回退，而是直接返回 `待审候选 / 等待发布 / 发布观察 / 已回滚` 的阶段、tone 与动作语义
  - 已用 `test_learning_lifecycle_includes_parameter_template_candidate_events` 锁住真实事件名到治理阶段的映射
- Phase E 参数治理后端契约测试现已补齐一轮：
  - `tests/test_factor_cards_api.py` 已覆盖 recommendation / offline candidate / summary / lifecycle 的 `governance` 聚合字段
  - `tests/risk/test_risk_api_policy.py` 已覆盖单笔 `trade-trace` 的 `parameter_governance.overview` 展示级字段
  - 已定向通过 `pytest tests/test_factor_cards_api.py -k "parameter_template_recommendations_surface_parameter_suspicion or parameter_template_offline_candidates_endpoint_lists_release_candidates or learning_summary_includes_parameter_template_candidate_stats or learning_lifecycle_includes_parameter_template_candidate_events"` 与 `pytest tests/risk/test_risk_api_policy.py -k "trade_trace_includes_parameter_governance_context"`
- Learning 页 recommendation 按钮文案现也继续去掉本地 action fallback：
  - recommendation 详情里的 `actionButtonText` 现直接绑定后端 `governance.action_button_text`
  - 页面层不再按 `recommended_action / recommended_scope` 本地回退成“创建离线验证 / 生成治理建议”
  - 参数模板 recommendation 详情继续朝“后端 governance 单源、前端只渲染按钮语义”推进
- Learning 页 recommendation materialize 的成功反馈也继续后端化：
  - `/api/learning/parameter-templates/recommendations/materialize` 现返回 `result_label / result_summary`
  - 在线轻调会返回“已生成治理建议”，离线深调会返回“已创建离线验证”
  - Learning 页 toast 不再按 `result.mode` 本地判断在线/离线结果文案，默认直接消费后端结果标签
- Learning 页 offline candidate 操作反馈也继续后端化：
  - `offline-candidates/review / release / rollback` 现统一返回 `result_label / result_summary`
  - approve / reject / release / rollback / blocked 的结果文案由后端统一给出
  - Learning 页候选操作 toast 不再按 action 本地拼“已批准候选 / 已执行发布 / 已执行回滚”等业务文案
- Learning 页 suggestion 审批反馈也继续后端化：
  - `/api/learning/review` 现返回 `result_label / result_summary`
  - approve / reject 的建议审批结果文案由后端统一给出
  - Learning 页建议审批按钮不再静默刷新，而是直接展示后端返回的审批结果标签
- Learning 页手动运行治理的结果反馈也继续后端化：
  - `/api/learning/govern/run` 现返回 `result_label / result_summary`
  - `auto_actions` 数量转成“已处理 N 条 / 没有新动作”的展示标签由后端统一给出
  - Learning 页运行治理 toast 不再按 `auto_actions` 本地拼结果文案
- Learning 页 suggestion 的边界展示现也开始后端化：
  - `/api/learning/suggestions` 的参数模板建议现已补出 `parameter_template_display`
  - 后端会统一给出 `boundary_scope_label / boundary_reason_text / approval_path_text / impact_text / evidence_text`
  - Learning 页 suggestion 详情与列表现优先消费这组后端展示字段，页面层边界人话 helper 继续退到 fallback
- Learning 页 suggestion 的审批/影响文案现也继续缩掉本地 scope 分叉：
  - 模板切换建议详情里的 `approvalPathText / impactText / evidenceText` 现直接绑定后端 `parameter_template_display`
  - 页面层不再按 `offline_deep / online_light` 本地补“先验证再发布 / governor 审批后切换”等审批链说明
  - suggestion 详情继续从“后端给边界数据 + 前端补审批人话”往“后端直接给完整展示语义”收口
- Learning 页参数治理空态文案也继续后端化：
  - `/api/learning/summary` 现新增 `parameter_template_empty_states`
  - 参数模板候选 / 参数治理轨迹 / 参数模板建议 三块空态默认直接消费后端 summary
  - Learning 页模板层不再硬编码这三类 Phase E 空态文案
- Learning 页摘要里的参数治理任务卡也继续后端化：
  - `/api/learning/summary` 现新增 `parameter_template_task_cards`
  - 模板候选 / 治理轨迹 / 参数模板建议 三张进度卡由后端统一给出 `title / note / tone`
  - Learning 页摘要区不再按候选数、轨迹数、推荐数本地拼这三张 Phase E 任务卡
- Learning 页候选与 lifecycle 的来源链路展示也继续后端化：
  - offline candidate `governance` 现补出 `source_summary / approval_path_text`
  - lifecycle `governance` 现补出 `source_summary / approval_path_text`
  - Learning 页不再本地翻译参数治理来源推荐、主要责任与审批路径文案
- Learning 页 offline candidate 的证据摘要也继续后端化：
  - candidate `governance` 现补出 `evidence_display`
  - Walk-forward IC / 基线 IC / Δ 的摘要句由后端统一生成
  - Learning 页候选列表与详情不再本地计算这句离线验证摘要
- 独立 `trade-trace` 页的参数治理时间线联动继续增强：
  - `parameter_governance.timeline_context` 现新增 `governance_actions`
  - 时间线里的治理/复盘事件可同时带出候选、推荐、suggestion、lifecycle 等关联动作
  - `trade-trace` 页面时间线从单一 jump 按钮升级为多治理动作按钮，继续向“证据链直接进入审批/候选链路”推进
- `trade-trace` 时间线治理动作继续补齐定位上下文：
  - `governance_actions` 现统一带出 `factor_id / source`
  - 从 trade-trace 跳 Learning 的 staged focus 也会保留 `factorId / source`
  - 后续审批、候选发布或轨迹详情可知道动作来自证据链时间线，而不是只靠对象 ID 反查
- 从 `trade-trace` 进入 Learning 的治理 focus 继续增强：
  - Learning 页消费 staged focus 时先按对象 ID 精确打开
  - 如果对应对象暂时不在当前列表，会按 `factorId` 降级打开同因子的推荐/候选/轨迹
  - 若仍找不到对象，会给出来源感知 toast，不再从证据链入口静默失败

收口验证：

- 已通过 Phase E 收口回归：
  - `pytest tests/test_factor_cards_api.py tests/alpha/test_streaming_factor_engine.py tests/test_runtime_config.py tests/risk/test_policy_service.py tests/risk/test_risk_api_policy.py tests/research/test_rule_evolution_governor.py tests/research/test_rule_learning_pipeline.py tests/test_learning_backfill.py tests/test_review_contract_api.py tests/test_failure_taxonomy.py -q`
  - 结果：`114 passed`
- 已完成远程服务器验收：
  - GitHub `main` 已推送至 `49a6b150`
  - 服务器 `/home/ubuntu/quant_trading` 已 fast-forward 到 `49a6b15`
  - 远程 `.venv/bin/python -m pytest ... -q` 结果：`114 passed, 1 warning`
  - warning 为 pytest 配置项 `asyncio_mode` 未识别，不影响本轮 Phase E 测试通过
- 已通过前端语法检查：
  - `node --check miniprogram_v2/pages/learning/index.js`
  - `node --check miniprogram_v2/pages/overview/index.js`
  - `node --check miniprogram_v2/pages/ops/index.js`
  - `node --check miniprogram_v2/pages/trade-trace/index.js`
  - `node --check miniprogram_v2/pages/factors/index.js`
  - `node --check miniprogram_v2/services/learning.js`
  - `node --check miniprogram_v2/services/ops.js`
- 已通过后端 compileall：
  - `backend/api/learning.py`
  - `backend/api/risk.py`
  - `backend/services/factor_cards.py`
  - `backend/services/parameter_templates.py`
  - `backend/services/parameter_template_validation.py`
  - `risk/policy_service.py`
  - `research/learning/governor.py`
  - `alpha/streaming_factor_engine.py`
  - `alpha/signal_normalizer.py`
  - `alpha/portfolio_compositor.py`
  - `backend/services/learning_backfill.py`
  - `backend/services/review_contract.py`
  - `backend/services/failure_taxonomy.py`

系统完成前回补清单（不阻塞 Phase F）：

- 回补更多手工参数模板与 regime-specific 模板，优先覆盖真实交易中频繁触发、且当前仍依赖启发式推断的重点因子
- 基于真实运行样本评估复杂离散 / 事件类因子是否值得参数模板化；若收益不清晰，只保留解释和证据链，不强行模板化
- 观察真实候选从建议、审批、灰度发布到回滚 / reinforce 的完整样本，确认各页面状态、后端 contract 与审计日志一致
- 整理服务器运行期数据策略，包括 `state.db`、`data/charts/*`、验证报告与备份文件的 ignore / 归档 / 清理边界
- 系统整体完成前再回到 Phase E 做一次补样本验收；若上述观察没有暴露阻塞问题，Phase E 保持完成态

---

## 7. Phase F：数学模型与大语言模型分层接入

状态：`未开始`  
优先级：`P1`  
前置条件：`Phase C/D/E 的基础 contract 已明确`

目标：把模型接入位置写进系统，不让其悬空或越权。

### F1：数学模型接入持仓监督层

状态：`未开始`

候选能力：

- 持仓质量评分
- 退出风险评分
- 时间衰减评分
- 持仓继续持有概率评估

### F2：数学模型接入因子治理层

状态：`未开始`

候选能力：

- regime-aware 因子排序
- 参数失配检测
- 阈值与模板效果比较

### F3：大语言模型接入归因与治理层

状态：`未开始`

候选能力：

- 复盘解释
- 治理建议归纳
- 审查说明
- 人话运维与风控摘要

### F4：模型权限边界固化

状态：`未开始`

必须明确禁止：

- 直接下单
- 直接平仓
- 直接提高硬风控上限
- 直接绕过 `RiskPolicyService`

---

## 8. Phase G：元模型旁路

状态：`未开始`  
优先级：`P2`

目标：让系统拥有全局调度脑，但仍然只有建议权。

### G1：定义 `meta_context.v1`

状态：`未开始`

### G2：元模型输出 contract

状态：`未开始`

至少包括：

- 当前系统状态
- 风险预算建议
- 交易频率建议
- 可信因子族
- 冻结/观察建议

### G3：接入 Governor 审批链

状态：`未开始`

完成标准：

- 元模型建议进入 ledger
- 元模型建议不能直接执行

---

## 9. Phase H：受限自动治理

状态：`未开始`  
优先级：`P2`

目标：让系统自动应用低风险调整，但绝不越过硬风控。

允许自动化：

- 降低风险预算
- 降低交易频率
- 切换到保守参数模板
- 暂停某些弱势因子

禁止自动化：

- 提高最大亏损阈值
- 关闭熔断
- 提高最大仓位
- 直接启用 live-trading 模型

---

## 10. Phase I：多品种完全体

状态：`未开始`  
优先级：`P3`

目标：从 XAUUSD+ 扩展到多品种、多风险池、全组合调度。

---

## 11. 观察项 / 并行但不打断主线

| ID | 项 | 状态 | 说明 |
|---|---|---|---|
| O1 | `learning_application_effect` 真实流观察 | 观察中 | 跟踪 observing / reinforced / ineffective 是否稳定推进 |
| O2 | 重启恢复回归测试 | 待做 | 覆盖开仓后重启、重启期间平仓、延迟恢复 |
| O3 | 运行环境健康专项 | 待做 | `l2_depth`、`disk_space` 继续治理 |
| O4 | 风控运维页搜索入口 | 待做 | 继续强化按 `position_id / decision_id` 的查询体验 |
| O5 | 历史重复 application 清理脚本 | 待做 | 清理旧数据噪声 |

---

## 12. 已知结构缺口

| ID | 缺口 | 当前情况 | 归属阶段 |
|---|---|---|---|
| G-1 | 持仓中没有主动裁决层 | 当前更多是硬闸门 + TP/SL | Phase C |
| G-2 | 时间/空间上下文仍未统一抽象 | 已有 `holding_seconds`，但远未成体系 | Phase C / D |
| G-3 | 因子参数治理缺失 | 当前主要是权重治理 | Phase E |
| G-4 | 归因结果未完整喂回 live 风控 | 复盘和风控仍偏分离 | Phase C / D |
| G-5 | 数学模型和 LLM 接入层已写文档，但未正式落地 | 目前仍偏离线和辅助 | Phase F |
| G-6 | 元模型尚未正式入位 | 仍无统一全局调度层 | Phase G |

---

## 13. 已知技术债

### P1

| ID | 文件/模块 | 问题 |
|---|---|---|
| TD1 | `execution/ctrader_bridge.py` | 价格除数 `10**5` 仍偏 XAUUSD 定制 |
| TD2 | `execution/market_impact.py` | 成本计算默认金价硬编码 |
| TD3 | `config/__init__.py` | MT5 相关残留仍需清理 |
| TD4 | `backend/core/db.py` vs `research/experiment_tracker.py` | `experiments.db` schema 漂移 |

### P2

| ID | 文件/模块 | 问题 |
|---|---|---|
| TD5 | `monitor/evolution_story.py` vs `evolution_story/` | 文件与目录冲突 |
| TD6 | `./nul` | Windows 设备名误文件 |
| TD7 | `alpha/factor_attribution.py` | 旧版归因残留 |
| TD8 | `execution/paper_bridge.py` | 旧版模拟盘桥接残留 |
| TD9 | `live/` 目录 | 部分旧监控仅被旧 `main.py` 引用 |
| TD10 | 根目录调试脚本 | 一次性脚本应移入 `scripts/debug/` |

---

## 14. 下次开工时怎么开始

以后不管在哪个对话继续，默认启动顺序如下：

1. 先读 [TODO.md](TODO.md)
2. 确认“当前唯一进行中主线”
3. 如果为空，就从“下一步入口”开始
4. 开发前先更新对应任务为 `进行中`
5. 完成后更新为 `已完成`，并补验证结果与新发现

当前默认下一步：

**Phase F：数学模型与大语言模型分层接入**
