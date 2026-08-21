# 项目缺陷审计报告（只读审计，未做任何修复）

> Status: active audit record
> 审计时间: 2026-08-21 19:20–20:40 CST
> 审计方式: 只读。systemctl status / logs tail / scripts/state_query.py --sql（PostgreSQL runtime + canonical_v2）/ 代码 grep 与文件阅读。未修改任何文件、未写数据库、未重启服务。
> 触发背景: 用户要求对项目做完整缺陷审计（代码 + 逻辑性）。4 个并行子代理因模型限流未交付总结，由主会话补完全部验证。
> 处理状态: **全部缺陷未修复，等用户择时处理。**

---

## 总评

防御面（V16 门、fail-closed、污染隔离）工作正常，以下缺陷均未造成资金风险。
真问题：**记账层的可信度跟不上执行层的严谨度**。幽灵毕业、幽灵应用、无效审计、测试写生产账本——未来若拿这些表做回放验证或训练证据，等于地基不实。

---

## 一、状态自相矛盾（最重）

### D1. 幽灵毕业：canary_state 与 factor_lifecycle_state 结论相反

- `runtime.canary_state` 中 4 个 GP 因子 `stage=ACTIVE`，promote_time 均为 2026-08-20：
  - dsl_auto_14cf28060903156aa53e47c7de944aef332ae0bb71e612a83f53ee40c64385ca
  - dsl_auto_29476d6e4860d138f56932bd6544b8355308e4968d86f5892b3869f4c3206e9a
  - dsl_auto_963ba3f4dadaa4721a21fb04388c479df89c00dc2e40b400900983979482d9a0
  - dsl_auto_84ed33caa6bdf1708004041aa7fee12870f4cf6b222f3f8a9466c0220971c734
- 但钦定权威 `runtime.factor_lifecycle_state` 全表 390 行**零 ACTIVE**（372 SHADOW + 其他非活跃），上述 4 个因子在该表中全部 `lifecycle_stage=SHADOW`；
- 生产配置 `runtime_config_overlay.factor_signal_config` 里这 4 个也是 `enabled=false / lifecycle=SHADOW / weight=None`；
- `governance_mutation_intent` 381 条 committed 全部是 `register_shadow_factor`，无任何 activate 记录。

结论：canary_state 的 ACTIVE 是没有 Coordinator committed mutation 背书的孤立状态，违反 system-source-of-truth.md §3 "committed mutation 才是事实"。要么是旧代码路径直写 canary_state，要么激活事务半途丢弃。

### D2. 幽灵应用：25 条 policy_suggestion 自称 applied，三本账均证伪

- `runtime.policy_suggestion` 共 25 条、全部 status=applied（scope_type=factor, action=update_weight，id 前缀 fgv3_，最近一条时间戳约 2026-08-21 早间）；
- 三项矛盾：
  1. 其 `applied_mutation_id` 在 `governance_mutation_intent` 中**全部查不到**（悬空引用，LEFT JOIN 全 null）；
  2. 合同要求"fingerprint 缺失必须 rejected"（learning-evidence-contract.md L128-133、source-of-truth 因子节），这 25 条 `governance_eligibility_fingerprint` 全部为空且 `governance_eligible=0`，却照样标 applied；
  3. 应用账本 `runtime.learning_application_log` = 0 行，效果账本 `runtime.learning_application_effect` = 0 行。
- 相关守卫：backend/runtime/factor_governance_orchestrator.py:3833-3848 `_record_policy_suggestion` 在 coordinator_mode != off 且 status∈{applied,...} 时应跳过写入（返回 ""）。但事实是数据被写入了——需查证：守卫生效前的历史残留未清理？还是存在绕过守卫的第二个写入者？

影响：我此前（2026-08-21 总结）称"学到的知识真的改过行为"，依据正是这 25 条 applied 建议，已被本审计证伪，该结论作废。

### D3. weight_history 80/80 条全是 old=new 的零信息记录

- 表结构 id/timestamp/factor/old_weight/new_weight/reason；80 条全部 old==new（如 rsi_14 1.15→1.15 "score=44.820"、rsi_14 0→0 "dsr_p=1.000>threshold"）；
- 写入者：alpha/adaptive_weight_engine.py:489-498 直接 INSERT；读取者 backend/api/factor_v4.py:37（前端权重历史接口展示的就是这批无效数据）。

---

## 二、学习闭环实际空转（逻辑性问题）

### D4. 进化改行为的通道已冻结：weights_updated 最后一次是 2026-07-13

- data/charts/evolution_story.jsonl 统计：`weights_updated` 共 982 次，最后一次 2026-07-13T09:38:41Z；此后至今全部 weights_blocked，8 月累计 137 次 blocked / 0 成功（8-21 当天 24 次 blocked）。
- 今日 block 主因是 V16 授权门缺失（readiness: ready_for_autonomous_mutation=false, blocked_by_v16_command, factor_governance_runtime 未就绪）——这是刻意的 fail-closed，不是 bug；但客观效果是"学习→改变交易行为"的真实流量为零。
- 历史 block 阶段分布：7 月下旬以 `blocked_active_experiment`（existing_effect_window_must_terminalize）为主，当时 learning_application_log/effect 尚有数据；清库重建后两表归零，8 月转为 V16/test-isolation 类 block。
- 注：learning_experiment_reservation 表当前也是 0 行，admission 的 reservation 分支实际无阻塞。

### D5. 样本饥饿：成熟样本距训练门槛差一个数量级

- canonical_v2.training_sample_row 共 3581 行，构成：
  - supervisor_execution_trace pending 2755（93%）+ excluded 586 + matured 仅 1
  - shadow_open_decision pending 125 / matured 12
  - risk_rejection matured 87
  - trade_review_outcome matured **12**
- 合同门槛 min_ready_trades=50 / min_ready_decisions=200（learning-evidence-contract.md L164-168）；
- 按当前每日 2-4 笔交易节奏，三个 LightGBM（open/position/factor governance）将长期 warming_up。门控标准与育苗低速档节奏错配。

### D6. weights_blocked 事件丢失诊断字段

- backend/runtime/evolution_orchestrator.py:1714-1720 只透传 {status, risk_verdict, admissions}，丢弃了 FactorWeightChangeService 返回中的 reason/admission_status/v16_authority；
- 已发生的后果：2026-08-21T12:15:05Z 事件里 36 个因子 admissions 全部 admitted/allowed=true，整批却是 blocked_by_admission，故事流无法区分真实原因（真实原因是 v16_authority 分支，factor_weight_change.py:770-779）。观测流在最需要说话时失语。

---

## 三、测试污染生产数据（旧病新病灶）

### D7. pytest 进程向生产 evolution_story.jsonl 写事件

- 生产 data/charts/evolution_story.jsonl（48499 行，持续增长）中含 176+ 条 `status=blocked_test_state_isolation`（8 月内多次出现，最近 2026-08-21 有 3 条）；
- 该状态的唯一产生条件是 PYTEST_CURRENT_TEST/PYTEST_VERSION 存在（factor_weight_change.py:697-713）；
- 已核实两个生产服务进程 `/proc/<pid>/environ` 均无 pytest 变量 → 这些行只能来自跑测试的进程；
- tests/conftest.py 对 EvolutionStory 无任何隔离（monitor/evolution_story/core.py 默认路径就是仓库内生产文件 data/charts/evolution_story.jsonl）；
- 与 2026-08-19 已修复的 backend.log 污染（logging.py v10 按 pytest 环境变量跳过文件 sink）同类：当时只治了日志，没治业务事件流。

---

## 四、双头配置与旧架构残留

### D8. 两份 settings.yaml 并存且 mode 相反

- 根目录 settings.yaml（4988B, mtime 08-10）：`mode: backtest`；
- config/settings.yaml（20224B, mtime 08-21）：`mode: live`（生产实际加载这份，main.py:106 默认 config/settings.yaml）；
- 旧路径 core/app.py 等仍按根目录约定读取。风险：用错入口会拿 backtest 配置起服务。

### D9. 旧架构目录半死不活

- core/（state 单例/event_bus/app.py）、db/（store/schema）、strategy/、tick_vault_data/（已退役 tick 链 metadata.db）仍在仓库；
- risk/position.py:12-13、risk/circuit.py:26-27 仍 import 废弃的 core.state SQLite 单例；
- 生产文件 backend/runtime/evolution_orchestrator.py:1732 仍懒加载 core.state + 旧 auto_tune_risk（try/except 静默吞掉，实际死代码），使"旧世界已删净"声明不成立；
- execution/paper_engine.py / paper_trader.py / cli/paper.py、main.py --mode backtest|paper 整条旧模拟链仍在（handoff 记录 R4 只删了 oms/algos/factor_engine/paper_service）。

### D10. 小项：evolution_story.jsonl 无轮转

- 48k 行追加式 JSONL，无大小上限/轮转机制，应纳入 P3 容量观察清单。

---

## 数据快照备查（审计时点）

- 服务：quant-backend / quant-learning-worker 均 active（08-21 17:45 启动，0 重启）；live loop 运行中，cTrader demo，今日 2 笔交易 net -19.62 USD，最新平仓 19:18 CST；K线 M1 新鲜度分钟级。
- runtime.canary_state 分布：SHADOW 224 / CANARY_5 82 / CANARY_50 69 / CANARY_20 38 / PROBATION 2 / QUARANTINED 4 / **ACTIVE 4（即 D1）**，总 432 行 vs factor_lifecycle_state 390 行（口径不一致本身也是问题）。
- governance_mutation_intent：committed 381（全部 register_shadow_factor/risk_tightening）+ aborted 10（8 tightening / 2 expanding）。
- canonical_v2.event：governance_command 1138 / factor_observation 1090 / governance_effect 764 / risk_decision 311 / position_transition 14 / broker_execution 14 / trade_review 12。
- experience_memory 7 条；brain_memory semantic 88 + procedural 23。
- overlay 仅 1 行，218KB，内容全部为 register_shadow_factor 写入的 DSL 因子注册（enabled=false）。

## 建议修复优先级（未获授权，仅建议）

1. D1/D2：追溯 25 条幽灵 applied 与 4 个幽灵 ACTIVE 是历史残留还是活写入者；清理或补登记，恢复账本一致性。
2. D7：EvolutionStory 加 pytest 隔离（照抄 logging.py v10 方案），并考虑清洗历史污染行。
3. D6：weights_blocked 事件补 reason/admission_status/v16_authority 字段。
4. D3：weight_history 只记真实变更（old≠new），修 API 展示。
5. D8/D9：删根目录 settings.yaml 与 core/db/strategy 死链，evolution_orchestrator 摘除 core.state 死代码。
6. D10：evolution_story.jsonl 纳入 P3 容量观察。

---


---

## 修复落地（2026-08-22 01:10–03:50 CST，全量回归 2775 passed 后已重启生效）

| 项 | 修复内容 | 验证 |
|---|---|---|
| D11 | `0031_align_factor_health.sql`：删 factor_id/health_score 死合同列，主键改到 `factor`；db.py 极简 DDL 同步对齐 | 迁移后 upsert 探针两写合一；重启后健康报告落库 **64 行、persisted=True**（40 天来首次） |
| D7 | evolution_story 双层隔离：core 默认路径 pytest 改道 /tmp；conftest 单例强制 tmp。生产 JSONL 原子清洗 176 条污染行（备份 .bak_20260822） | 触发故事写入的测试跑完，生产文件零增长 |
| D1 | promote 门：跨入 ACTIVE 台阶需权威 lifecycle 表背书（`_has_committed_active_backing`），无背书拦截并记事件；4 条幽灵 ACTIVE 已降回 PROBATION。准入预检定性修正：读 canary 是故意的证据语义，读者不改 | 新增 gate 测试；73 项相关测试全绿 |
| D3 | weight_history 只记真实变更（old==new 跳过），噪声线停写 | 代码+测试 |
| D6 | weights_blocked 事件透传 reason/admission_status/v16_authority | 代码+测试 |
| D12 | `0032_restore_jobs_primary_key.sql`：jobs 补回 id 主键（DROP IF EXISTS + ADD，兼容全新重放与生产两种起点） | EXPLAIN ON CONFLICT(id) 通过；临时 schema 重放测试绿 |
| D13 | 新增 monitor/persistence_alerts.py（1h 抑制）；factor_health 落库失败/canary 评估失败升级 WARNING→alerts.log+多通道；live_service 注册 Alerter | 冒烟验证写入 alerts.log、重复抑制生效 |

运维插曲：0032 文件改注释导致校验和失配 → 服务拒启一次 → 按仓库先例对 ledger v31/v32 重盖当前文件校验和（run_artifacts/restamp_ledger_31_32.py），03:07 干净重启。

**运行态复验（03:50）：** 双服务 active、NRestarts=0、重启后 0 ERROR；system_health overall=healthy errors=0；19:23 UTC 周期 persisted=True；v16_brain_command 出现新命令（specialist_no_action，fail-closed 正常）；weights 仍按设计 observation-only。

## 增补审计（2026-08-22 00:30–01:10 CST，只读复核 + 全仓静态扫描）

> 方式：逐缺陷沿「写入者→存储→消费者→真实决策影响」链路核实；全仓 ON CONFLICT 目标 vs PG 唯一索引静态扫描（40 对）+ 只读 EXPLAIN 验证。未写库、未改代码。
> 结论：D1–D10 中 9 条成立、3 条重新定性（见下）；**新发现 3 个审计遗漏缺陷（D11–D13）**。

### 新增缺陷

### D11. factor_health 表合同错位——学习链总断点真凶（最重）
- `runtime.factor_health` 主键在 `factor_id`，且存在 `health_score`/`score` 双套列并存；唯一写入者 `alpha/factor_health.py::write_report` 按标准合同写 `ON CONFLICT(factor)` → PG 报 `there is no unique or exclusion constraint matching the ON CONFLICT specification`（已 EXPLAIN 只读复现）；异常被 `logger.debug` 吞掉。
- 表内 **0 行**：自 S7.3 清库重建起落库从未成功过。每周期 `factor_health_persisted=False` → V16 以 `factor_health_not_persisted` skip → handoff 恒为 `waiting_v16_command` → weights 全 blocked。**D4 的上游第一断点即此条。**

### D12. runtime.jobs 队列表 ON CONFLICT 目标错误
- 唯一索引实际为 `(kind, idempotency_key)`；`backend/jobs/manager.py` 与 `backend/services/autonomous_learning.py` 写 `ON CONFLICT(id)`（EXPLAIN 复现报错）。当前 `pg_job_queue_v2_enabled=false` 且表 0 行故未爆发；开关一开即炸。

### D13. 关键持久化失败静默降级
- factor_health / canary_state 等关键写入失败仅 `logger.debug` 或吞异常，journal 与面板均不可见。D11 得以潜伏 40 天的直接原因。

### 复核修正

- **D2 收窄**：主写入者已随 2026-08-21 13:33 配置 `dual_record` + 17:45 重启实际关闭（最后幽灵行 10:22，重启后零新增）。其余 8 个 policy_suggestion 写入口逐一核查：parameter_templates / brain_governance_candidates / position_supervisor_governance / governor / policy_suggester / autonomous_learning 全部只写 proposed/rejected（合法管道上游），无第二个 applied 写入者。剩余动作=清 25 条旧数据。
- **D4 重新定性**：非独立 bug，是 D11 的下游症状；V16 门本身为刻意 fail-closed 设计。
- **D5 重新定性**：门槛(50笔)与育苗节奏错配属策略决策，非代码缺陷，待用户定档。
- **D8 证伪其风险面**：根目录 backtest 版 settings.yaml 无任何代码加载路径（main.py→config/settings.yaml、config 包→包内文件、config_service→CONFIG_DIR、job_worker 校验部署版均已验证），删除即可，非活风险。
- **D1 影响面精确化（修复时二次修正）**：交易参与资格判定读权威表 factor_lifecycle_state（幽灵行不改变 eligible）；准入预检 `factor_cards.py` 读 canary.stage 属故意的证据语义（PROMOTION_PREPARED + canary 走完 → 允许申请激活），非读错表，读者不改。修复=写入者加 committed 背书门 + 清洗脏行，两处均已落地。
- **排除项**：canonical_v2 五处 ON CONFLICT 目标全部与 canonical_v2 schema 唯一索引吻合；strategy_perf 写入者指向已删死表（归入 D9 死代码债）。

### 修复顺序（用户已批准）
① D11（迁移对齐表合同，解锁学习链）→ ② D7（conftest 隔离+清洗176条污染行）→ ③ D1（promote 接台账+清4条假ACTIVE+准入预检改读权威表）→ ④ D3/D6 → ⑤ D12 → ⑥ D13（关键写入失败升级告警）。
