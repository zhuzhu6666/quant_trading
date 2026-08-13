# 项目总览与当前状态

> Status: canonical
> Last verified: 2026-08-13
> Scope: 新对话、实施、排障和发布的唯一文档入口。

读完本页即可知道项目当前处于什么阶段、系统怎样运行、哪些事情禁止做。只有准备修改某个领域时，才继续读对应合同。

## 1. 当前结论

- 当前分支为 `main`；本次前端实施批次修改 `web_frontend/`、市场 bars 最小事实合同、
  对应 OpenAPI snapshot 和 scoped 文档，保留工作区已有用户改动。
- P0 已完成。
- P1 的代码与历史污染修复已完成，但仍保持 `runtime acceptance`：必须继续用真实 broker deal、重启回放和完整持仓生命周期证明修复后的链路。
- P2 canonical risk、P3 writer/identity 收敛和 P4 V16 因果调度已完成；P5 是持续删除旧路径的工程纪律，P6 Demo 观察/毕业仍受真实证据阻塞。
- 2026-08-10 只读运行核对：backend、learning worker active；`/api/health` 为 `db=connected`、`ctrader=connected`；readiness 当前报告 live execution、live alpha、autonomous mutation 和 release 均 ready。
- live loop 当前 running、accepting new risk，account/positions reconcile fresh，当前空仓；`risk_metrics_snapshot.v2` 为 known，使用 500 个闭合 M5 样本，空仓零敞口是计算结果，不是缺失兜底。
- Safety v2 仍为 `shadow/observing`，尚未满足 24 小时连续空仓或完整 broker lifecycle 的切换条件；Generation、Execution Outcome、Governance 和 PG Job Queue 的静态发布开关保持原值。
- learning worker capability 为 `ready/complete/available`；readiness 和 capability 只表示当前事实可用，不授权绕过 V16、Candidate Review、RiskPolicy 或 Coordinator，也不等同于切换静态发布开关。
- 2026-08-13 前端重构首批 renderer/Tauri 代码已落在工作树；React 19、五个工作区、
  Fact/唯一 WS、Safety rail 和研究缓存均已有本地证据。桌面端目标是本人本地使用，
  服务器只保留后端 API/WSS，不部署公网浏览器静态站点；
  Windows 安装包签名、GitHub Releases manifest、公开 updater 和 Windows runner 不再
  是本批完成条件。远程 `market.bars.v1` Fact 合同及 factor-card 只读性能收敛已完成
  本机/公网认证 smoke。

每次回答“现在能否交易/发布”前，都必须重新查询服务、PostgreSQL、`runtime_kv`、日志和 broker；本页不保存逐时运行流水。

## 2. 当前生产结构

```text
cTrader spot/account/positions/execution
  -> serial live loop
     -> closed-bar factors and signal
     -> canonical RiskPolicy / RiskGovernor
     -> broker execution intent and reconcile
     -> position protection / emergency reduction
  -> PostgreSQL state_v1
     -> canonical runtime snapshots
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
| 运行态、恢复、学习审计 | PostgreSQL `state_v1` |
| 开仓/改仓/治理风险裁决 | `RiskPolicyService` / canonical risk calculator |
| Safety | serial safety plane；旧链只在发布观察期做只读比较 |
| RuntimeConfig 变更 | typed governance mutation + committed projection |
| readiness/API/frontend | 只读 canonical snapshot，不重新计算授权事实 |
| K 线 | `data/bars_monthly/bars_YYYY_MM.duckdb` |
| 外部 PIT 数据 | `data/external_data.duckdb` |
| 经济事件 | `data/events.duckdb` |

历史 tick、L2、SQLite `data/state.db`、旧 Web Console/H5、MT5 并行执行路线均已退役，不得恢复。

## 3. 当前主线

1. 继续收集 P1 的 post-repair 新成交、重启 replay 和 `open -> protection -> close -> deal sync -> review -> sample` 完整生命周期证据。
2. 继续观察 Safety shadow，满足既有 24 小时空仓或完整 lifecycle 门槛后，才可单独讨论 Safety 发布门。
3. 对 `legacy-debt-register.md` 中仍处于 `migrating`、`quarantined` 或 `regressed` 的路径逐条收集退出证据，同批删除旧 authority、旧重算、旧字段回退或无意义 wrapper。
4. 不扩展新的 V16 调度层，不新增 Brain、PosteriorService、FactorCardV2、表、线程、调度器或平行生产 writer。
5. 按前端重构文档继续完成真实接口和个人本机桌面验收；公网浏览器静态入口已退出，
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
