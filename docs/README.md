# 项目总览与当前状态

> Status: canonical
> Last verified: 2026-08-22 (缺陷批 D1–D13 部署验收完成、migration ledger v32、双服务周末休市期稳定运行)
> Scope: 新对话、实施、排障和发布的唯一文档入口。

读完本页即可知道项目当前处于什么阶段、系统怎样运行、哪些事情禁止做。只有准备修改某个领域时，才继续读对应合同。

## 1. 当前结论

- 开发基线为 `main`；发布流程会从该基线创建临时发布分支。
- **当前姿态（2026-08-22）**：生产运行态统一使用 PostgreSQL `runtime`，不可变事实与学习样本统一使用 `canonical_v2`；migration ledger 已到 v32（0031 factor_health 合同对齐、0032 jobs 主键恢复），旧 runtime 事实表已退役。
- **旧库清理（已完成）**：旧 `state_v1`、`public`、`legacy_mapping` 和本地 SQLite `data/state.db` 运行路径均已退役；生产代码不再读取、写入或重建这些路径。
- **运行态迁移门（已完成，运行时风险门仍独立生效）**：service-backed cleanup 已清理旧 runtime 事实，普通监督执行使用 `governed_execute -> RiskPolicy -> cTrader -> lifecycle -> fresh reconcile` 单轨链。当前若 readiness 阻断，只能来自实时 market/session、Safety、incident 或 broker 事实，不代表迁移回退或兼容路径仍在。
- **A 类结构修复（已完成）**：A1 trade_review 实时写入器 ✅ / A2 label 单一口径 ✅ / A3 posterior 触发放宽 ✅ / A4 win 单正反馈 ✅ / A5 effect 归因链代码就绪 ✅ / A6 supervisor_trace 成熟链 ✅。
- **缺陷批 D1–D13（2026-08-22 已部署生效，详见 planning/audit-defects-2026-08-21.md）**：D11 factor_health 表合同错位（学习链总断点）、D12 jobs 主键、D7 测试污染生产 JSONL、D1 幽灵 canary 毕业、D3/D6 权重噪声与诊断字段、D13 关键写失败静默。全量回归 2775 passed 后受控重启，重启后 60 分钟 0 ERROR；factor_health 健康报告首次落库成功（64 行，40 余天来首次）。学习链断点已解除。
- **闭环证据现状（2026-08-22 只读复核）**：历史 9 笔平仓中仅 `284485647` 拿到完整合格样本（integrity=full、权重 1.0，8/21 重启后重算转正）；其余 8 笔因重启回放降级为审计用途（train_weight=0），按硬边界保留不洗白。S7.6 验收标准 = 部署之后新开仓位完整走通 `open -> protection -> close -> deal sync -> review -> sample` 且样本直接 full/1.0；当前挂单 `284602893` 为部署前旧仓不计入。
- **Git 基线（已解除风险）**：截至 2026-08-22 全部收敛成果已提交并推送至 origin/main（最新 ef989d7），本地无未提交改动；handoff §7 的"186 文件 uncommitted"表述已过时。
- **代码收敛（S2/S3 完成）**：db_helpers 公共层（33 文件）+ 四域清扫（9 空壳 + EvolutionKernel + 零调用转发 + consume 包装器）+ A1–A6 / B1–B5 结构修复；历史全量回归记录保留在对应 rollout 文档，当前发布以本批新鲜针对性/全量验证为准。
- legacy 事实迁移的旧表述（P1/P2/P4/P5、1,702 处、schema version 20）已被"全库清空重建"取代，仅历史参考。
- 前端（miniprogram_v2 / web_frontend）在 Windows 本地维护；服务器后端-only sparse checkout，只提供 API 与 `/ws/state`。

每次回答“现在能否交易/发布”前，都必须重新查询服务、PostgreSQL、`runtime_kv`、日志和 broker；本页不保存逐时运行流水。

## 2. 当前生产结构

```text
cTrader spot/account/positions/execution
  -> serial live loop
     -> closed-bar factors and signal
     -> canonical RiskPolicy / RiskGovernor
     -> broker execution intent and reconcile
     -> position protection / emergency reduction
  -> PostgreSQL runtime operational state
  -> canonical_v2 immutable events / samples
     -> read-only readiness and fact.v1 APIs
     -> Tauri desktop full console / mini-program status surface

learning worker
  -> observation, learning, factor and governance evidence
  -> typed governance mutation path
  -> committed runtime projection
```

唯一权力边界：

| 领域 | 唯一事实或执行权 |
|---|---|
| broker 账户、仓位、成交 | cTrader 权威响应 + fresh reconcile |
| 运行态、恢复、执行审计 | PostgreSQL `runtime` |
| 不可变事实、生命周期事件、学习样本 | PostgreSQL `canonical_v2` |
| 开仓/改仓/治理风险裁决 | `RiskPolicyService` / canonical risk calculator |
| Safety | serial safety plane；旧链只在发布观察期做只读比较 |
| RuntimeConfig 变更 | typed governance mutation + committed projection |
| readiness/API/frontend | 只读 canonical snapshot，不重新计算授权事实 |
| K 线 | `data/bars_monthly/bars_YYYY_MM.duckdb` |
| 外部 PIT 数据 | `data/external_data.duckdb` |
| 经济事件 | `data/events.duckdb` |

历史 tick、L2、SQLite `data/state.db`、旧 Web Console/H5、MT5 并行执行路线均已退役，不得恢复。

## 3. 当前主线

1. 继续收集 P1 的 post-repair 新成交、重启 replay 和 `open -> protection -> close -> deal sync -> review -> sample` 完整生命周期证据；S7.6 验收 = 缺陷批部署后新开仓位样本直接 integrity=full / train_weight=1.0（参照 284485647 于 2026-08-21 重算转正）。若修复后干净样本产率仍持续偏低，需与用户显式重新校准证据门槛（审计 D5 已定性为策略决策），不得默默等待。
2. 继续观察 Safety shadow，满足既有 24 小时空仓或完整 lifecycle 门槛后，才可单独讨论 Safety 发布门。
3. 对 `legacy-debt-register.md` 中仍处于 `migrating`、`quarantined` 或 `regressed` 的路径逐条收集退出证据，同批删除旧 authority、旧重算、旧字段回退或无意义 wrapper；所有过渡态/双记录模式须登记退役条件与期限，不允许无限期双轨。
4. 不扩展新的 V16 调度层，不新增 Brain、PosteriorService、FactorCardV2、表、线程、调度器或平行生产 writer；治理底盘已领先策略内容，工程精力优先投向 alpha 研究与真实闭环数据积累，暂停新增基础设施。
5. 按前端重构文档继续完成真实接口和个人本机桌面验收；公网浏览器静态入口已退出并验证根路径 404，
   服务器只提供 API/WSS，本机认证和基本使用已确认通过，仍需完成 WS/缓存隔离、离线恢复、工作区排版、
   跨工作区数据流和危险动作安全验收。公开
   Windows 分发、安装器签名、GitHub Releases 和自动更新不属于本批范围。

## 4. 最小工作流

```text
读本页
  -> 查 system-source-of-truth
  -> 查 active legacy debt
  -> 按 change-impact-checklist 确认调用链和影响面
  -> 最小修改，并同步删除被替代路径
  -> 针对性测试 + migration check + OpenAPI check
  -> 必要时受控重启
  -> 服务 / PostgreSQL / runtime_kv / 日志只读验收
  -> 更新当前状态、rollout status、acceptance matrix
```

硬规则：

- 一个事实一个计算者，一个状态一个写入者；
- 不新增风险计算器、线程、调度器、数据库表或阈值，除非现有合同无法表达且证据充分；
- `unknown/warming_up/stale/error` 保持真实语义，禁止默认零、兼容值或猜测值；
- readiness、API、Web、小程序不得复制 Safety、风险和授权计算；
- 新路径若未删除被替代路径，阶段不得标为完成；
- 不提交、不推送、不切换生产开关、不清锁，除非用户明确要求。

## 5. 文档地图

### 每次系统级修改必读

1. 本页；
2. [system-source-of-truth.md](system-source-of-truth.md)；
3. [legacy-debt-register.md](legacy-debt-register.md)；
4. [change-impact-checklist.md](change-impact-checklist.md)。

### 当前工程收口

- [planning/production-autonomy-repair-optimization-plan.md](planning/production-autonomy-repair-optimization-plan.md)：唯一活动实施计划；
- [planning/final-execution-checklist.md](planning/final-execution-checklist.md)：停机重建终极执行清单（canonical 单库 + 架构清扫 + 学习进化结构修复 + 冷启动），当前停机专项蓝本；
- [planning/architecture-audit-2026-08-18.md](planning/architecture-audit-2026-08-18.md)：全项目架构审计 + 清库重建问题点全清单；
- [phased-repair-rollout-status.md](phased-repair-rollout-status.md)：当前阶段、运行姿态和未完成证据；
- [phased-repair-acceptance-matrix.md](phased-repair-acceptance-matrix.md)：可重复验收门和发布证据。

### 前端重构（前端领域活动计划）

- [planning/frontend-refactor-plan.md](planning/frontend-refactor-plan.md)：B+C+A 产品模型、实施顺序、替代和删除清单；
- [frontend-operator-contract.md](frontend-operator-contract.md)：五个工作区、动作、权限、Fact 展示和视觉合同；
- [frontend-desktop-contract.md](frontend-desktop-contract.md)：Tauri、Windows 本地运行、认证、缓存和离线合同；
- [frontend-refactor-acceptance-matrix.md](frontend-refactor-acceptance-matrix.md)：前端、桌面、接口和删除验收门；
- [frontend-refactor-status.md](frontend-refactor-status.md)：只记录前端重构实际进度。

以上文档是前端领域的 scoped 活动计划和合同，不替代全局生产计划。当前代码已完成
首批 renderer/Tauri 实施；服务器目标为后端 API/WSS-only，不再托管公网静态入口；本人已确认个人本机认证和基本使用通过，
排版、数据流及其余运行态验收仍在收口，公开 Windows 发行和 updater 不在范围内。接口事实发生真实变化时继续同步
api-fact-contract.md。

### 领域合同，按需读取

- [api-fact-contract.md](api-fact-contract.md)：`fact.v1`、freshness、unknown 和前端展示语义；
- [learning-evidence-contract.md](learning-evidence-contract.md)：学习样本、污染、资格和权重；
- [position-supervisor-contract.md](position-supervisor-contract.md)：持仓监督器输入、候选和执行边界；
- [factor-card-schema.md](factor-card-schema.md)：因子卡片/目录展示合同；
- [parameter-template-contract.md](parameter-template-contract.md)：参数模板及 online/offline 变更边界；
- [server-backend-sop.md](server-backend-sop.md)：启动、日志、数据库、cTrader、重启和运行验收。

### 文档维护

- [documentation-governance.md](documentation-governance.md)：文档职责、更新和删除规则。

未列出的历史设计、版本计划和完成流水不作为活动文档保留；需要追溯时使用 Git 历史，不恢复为新的入口。
