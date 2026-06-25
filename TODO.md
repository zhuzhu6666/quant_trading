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

- 🟡 Phase C：持仓监督闭环（进行中，C1 已完成）

### 当前系统一句话状态

系统现在已经具备：

- 实时因子链路
- 统一风控裁决第一阶段
- 决策账本与生命周期追踪
- 平仓复盘与经验沉淀
- 离线模型训练 / shadow / canary / advisory 流程

系统现在仍然缺：

- 持仓中的主动裁决能力
- “因子错 / 参数错 / 退出错 / 时长错 / regime 错”的正式责任拆分
- 因子参数治理与版本治理
- 元模型层的全局调度能力

### 当前唯一进行中主线

`Phase C / C6：真实案例验收与上线前核查`

### 下一步入口

继续 **Phase C / C6：真实案例验收与上线前核查**。

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

状态：`进行中`  
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
- 本地历史平仓样本已增强到 Phase C 路径口径：
  - `backend/services/learning_backfill.py` 现在会优先用 broker 开/平成交恢复 `holding_seconds`
  - 若本地 DuckDB 有对应 bars，则会进一步推导 `mfe / mae / giveback_ratio / profit_capture_ratio / holding_efficiency / thesis_status`
  - 已通过远程 `/api/market/bars` 回灌 2026-06-24 ~ 2026-06-25 的 `XAUUSD+ / M5` bars 到本地 DuckDB，当前本地真实 review 已识别出 `4` 个 `profit_giveback` 案例
- C6 验收口径已显式区分“直接证据”和“推断证据”：
  - `scripts/phase_c_supervisor_check.py` 现在会输出 `coverage`
  - `timeout_close_case` 与 `active_protection_case` 已拆成 `evidence / inferred_evidence`
  - `learning_backfill` 写入的 `close_reason_source=phase_c_inferred` 不再被当成“真实已执行 supervisor/timeout close”
- 真实案例覆盖现状：
  - 已直接覆盖：长持仓案例、盈利后回吐案例、broker/manual close 案例、活跃仓 supervisor `close / thesis_broken` 案例
  - 当前仅推断覆盖：历史 `profit_giveback_after_mfe` 一类退出问题样本
  - 远程现网历史 review 当前主要仍是 `historical_backfill / broker_close / restart_replay`，还没有现成的 `holding_timeout / supervisor_reduce / supervisor_tighten / thesis_broken` 已执行平仓样本可直接验收

当前阻塞点：

- 当前本地新增 review 虽已能补出 `holding_seconds / giveback_ratio / profit_capture_ratio / thesis_status`，但仍有部分旧样本缺 entry decision / regime 上下文，因此“责任拆分”还不够完整
- 远程线上接口当前仍未部署 Phase C 新 supervisor 字段，因此线上活跃长持仓仍只能看到旧口径 payload
- 当前仍缺少真实 `holding_timeout` 样本，以及“已执行并落账成 review 的 active protection close（reduce/tighten/close）”样本，C6 还不能正式收口

下一步：

- 把本地 Phase C 代码发布到服务器后，复跑 `/api/live/positions`、`/api/risk/trade-trace`、`python scripts/phase_c_supervisor_check.py --api-base ...`
- 至少覆盖长持仓回吐、手动关闭、超时关闭、主动保护关闭四类案例
- 优先补到能从真实 close review 中稳定看到 `holding_seconds / giveback_ratio / supervisor close_reason` 的完整证据
- 一旦线上出现 timeout 或 supervisor 执行平仓样本，立即回灌本地并复跑 C6 验收脚本，确认责任标签是否能落到 `时长问题 / 退出问题 / thesis_broken`

---

## 5. Phase D：归因升级与责任分离

状态：`未开始`  
优先级：`P0`  
前置条件：`Phase C 基本落地`

目标：让系统正式分清“问题到底出在哪”。

### D1：扩展 trade review contract

状态：`未开始`

新增核心字段：

- `entry_quality`
- `exit_quality`
- `holding_efficiency`
- `regime_fit`
- `thesis_status_at_exit`
- `profit_capture_ratio`
- `giveback_ratio`
- `time_in_profit`

### D2：失败分类体系 v2

状态：`未开始`

目标标签至少包括：

- `entry_good_exit_bad`
- `alpha_correct_but_capture_failed`
- `tp_too_far`
- `sl_too_tight`
- `holding_too_long`
- `regime_changed_during_hold`
- `factor_logic_ok_but_param_suspect`

### D3：责任回写链路

状态：`未开始`

要写回：

- `trade_outcome_review`
- `factor_contribution_review`
- learning sample
- trade trace
- 前端复盘页

完成标准：

- 任何一笔亏损都不能只被粗暴归成“因子失效”

---

## 6. Phase E：因子治理与参数模板

状态：`未开始`  
优先级：`P1`  
前置条件：`Phase D 基本落地`

目标：建立正式的“因子教练层”。

### E1：因子解释卡片 schema

状态：`未开始`

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

### E2：参数模板系统

状态：`未开始`

目标：

- 把“参数”从散点配置变成版本化模板
- 支持 regime-aware 参数切换

### E3：在线轻调 / 离线深调边界

状态：`未开始`

要明确：

- 哪些可以在线轻调
- 哪些必须离线验证后发布

### E4：因子治理工作流

状态：`未开始`

链路目标：

参数可疑证据 -> 治理建议 -> 回测/验证 -> 审批 -> 灰度 -> 发布/回滚

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

**Phase C / C6：真实案例验收与上线前核查**
