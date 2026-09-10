# 项目总览与当前状态

> Status: canonical
> Last verified: 2026-09-10（文档收敛批：状态页去流水、旧债登记按 scope 删已完成条目、入口对齐实际文件）
> Scope: 新对话、实施、排障和发布的唯一文档入口。

读完本页即可知道项目当前处于什么阶段、系统怎样运行、哪些事情禁止做。只有准备修改某个领域时，才继续读对应合同。

## 1. 当前结论

> 本页只放稳定结论和指针。服务状态、PostgreSQL、`runtime_kv`、日志和 broker 的逐时事实一律现查（见 §4）。

- **运行姿态**：三服务（`quant-backend` / `quant-learning-worker` / `quant-job-worker`）active。发布开关、release gate 顺序和 `supervisor -> governance -> pg_job_queue` 推进规则见 [system-source-of-truth.md](system-source-of-truth.md) §2。
- **运行态与事实域**：运行态统一 PostgreSQL `runtime`，不可变事实与学习样本统一 `canonical_v2`；migration ledger 当前 v34（`0034_governance_mutation_intent_overlay_hash`）。旧 `state_v1` / `public` / `legacy_mapping` 和 SQLite `data/state.db` 运行路径已退役，磁盘上的 0 字节残留不是可用入口。
- **执行单轨**：`governed_execute -> RiskPolicy -> cTrader -> lifecycle -> fresh reconcile`。readiness 阻断只能来自实时 market/session、Safety、incident 或 broker 事实，不代表迁移回退或兼容路径仍在。
- **闭环证据**：`open → protection → close → deal sync → review → sample` 持续产出。样本计数、门槛和未满足证据只在 [phased-repair-rollout-status.md](phased-repair-rollout-status.md) 与 [legacy-debt-register.md](legacy-debt-register.md) 维护，本页不复制。
- **结构收敛**：db_helpers 公共层 + S2/S3 域清扫 + A1–A6 / B1–B5 结构修复已完成；D1–D13 缺陷批、减法批、meta shadow 批等历史流水由 Git 历史追溯。
- **Git 与部署**：发布流程从 `main` 创建临时发布分支。最近一次提交与最近一次生产重启的先后关系用 `git log -1` 对比 `systemctl show -p ActiveEnterTimestamp` 现查，不引用本页快照。
- 前端（`miniprogram_v2` / `web_frontend`）在 Windows 本地维护；服务器是后端-only sparse checkout，只提供 API 与 `/ws/state`。

每次回答“现在能否交易/发布”前，都必须重新查询服务、PostgreSQL、`runtime_kv`、日志和 broker。

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

权力边界、唯一计算者/写入者和数据源的权威定义在 [system-source-of-truth.md](system-source-of-truth.md) §1.1（三层生产权力）与 §6（数据事实源）；本页不复制该表。

历史 tick、L2、SQLite `data/state.db`、旧 Web Console/H5、MT5 并行执行路线均已退役，不得恢复。

## 3. 当前主线

1. 继续收集 `open → protection → close → deal sync → review → sample` 完整生命周期证据；S7.6 终验标准已达成并持续。当前计数、skip/rejected 双轨（full/1.0 与 full/0.35）和 supervisor trace 分级只在 [phased-repair-rollout-status.md](phased-repair-rollout-status.md) 现查。
2. 监督治理闭环：需要 `≥10 笔 matured` 监督样本与 `tighten` 真实执行覆盖，模板治理闭环才会自动进有界 Demo；此前 `position_supervisor_selection.v1` 保持无候选/`off` 是安全基线，开启不需要人工改模式开关。门槛与关闭项见 [legacy-debt-register.md](legacy-debt-register.md)。
3. 对 `legacy-debt-register.md` 中仍处于 `active`、`migrating` 或 `monitoring` 的路径逐条收集退出证据，同批删除旧 authority、旧重算、旧字段回退或无意义 wrapper；所有过渡态/双记录模式须登记退役条件与期限，不允许无限期双轨。
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
- [phased-repair-rollout-status.md](phased-repair-rollout-status.md)：当前阶段、运行姿态和未完成证据；
- [phased-repair-acceptance-matrix.md](phased-repair-acceptance-matrix.md)：可重复验收门和发布证据。

> 历史专项文档（`final-execution-checklist` / `architecture-audit` / `audit-defects` / `handoff-2026-08-18-rebuild` / `supervisor-confirmation-chain-fix-plan`）已于 S5 清库重建与 D 批修复后归档 Git 历史，不再作为活动入口。

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
