# 项目总览与当前状态

> Status: canonical
> Last verified: 2026-07-26
> Scope: 新对话、实施、排障和发布的唯一文档入口。

读完本页即可知道项目当前处于什么阶段、系统怎样运行、哪些事情禁止做。只有准备修改某个领域时，才继续读后面的对应合同。

## 1. 当前结论

- 当前分支：`main`；本文核对时 HEAD 为 `471ea6d`，工作区有未提交的文档收敛改动。
- P0 已完成。
- P1 代码和历史污染修复已完成；仍等待新的真实 broker deal 与完整持仓生命周期运行验收。
- P2 代码和合同已完成，schema 已到 v12；当前处于运行观察期，不进入 P3。
- P3 未开始。P2 运行验收未通过前禁止推进。
- 当前保持 `no_new_risk`。不得因服务在线、市场重新开盘或文档完成而清锁。
- Safety、Generation、Execution Outcome、Governance、PG Job Queue 的静态发布开关不得随普通修复切换。
- 最近已知全量基线：`2452 passed, 9 skipped`。日常小批默认只跑针对性测试；阶段/发布验收才跑全量。

2026-07-26 运行只读核对：

- `quant-backend.service`、`quant-learning-worker.service`、`caddy.service` active；
- `quant-job-worker.service` inactive，与 PG Job Queue 静态开关默认关闭一致；
- `/api/health` 为 `db=connected`、`ctrader=connected`；
- PostgreSQL `state_v1` migration `current=minimum=latest=12`，无 mismatch；
- `risk_metrics_snapshot.v2` 与 `backend_readiness_snapshot.v1` 正常刷新；
- readiness：frontend 可读；live execution、live alpha、autonomous mutation、release 均未授权；
- live loop 运行但不接受新风险，blocker 为 `market_session_blocks_open` 与 `no_new_risk_latched`；
- broker reconcile fresh、unknown execution 为 0、当前空仓；
- risk snapshot 为 closed-bar M5 500 样本，包含 current/candidate/forward notional 合同；空仓时 VaR/CVaR 为 0 是计算结果，不是缺失值兜底；
- Safety shadow 仍只处于 observing，尚未满足完整持仓生命周期或 24 小时无仓观察门槛。

这些是带时间的运行快照，不是永久事实。每次实施或回答“现在能否交易/发布”前必须重新查询。

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
     -> Web full console / mini-program status surface

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

当前只做 P1/P2 运行验收与兼容删除：

1. 等待并核对新的真实 broker deal、开仓、保护、平仓、同步、复盘与学习生命周期；
2. 完成 Safety shadow 的完整生命周期或 24 小时无仓观察与故障矩阵；
3. 对每条仍在迁移的兼容路径收集退出证据，同批删除旧 authority、旧重算、旧字段回退或无意义 wrapper；
4. P2 全部门槛满足后，才可提出进入 P3。

实施细节见 [planning/production-autonomy-repair-optimization-plan.md](planning/production-autonomy-repair-optimization-plan.md)，实际进度见 [phased-repair-rollout-status.md](phased-repair-rollout-status.md)，门槛见 [phased-repair-acceptance-matrix.md](phased-repair-acceptance-matrix.md)。

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
  -> 更新本页状态、rollout status、acceptance matrix
```

硬规则：

- 一个事实一个计算者，一个状态一个写入者；
- 不新增 `RiskMetricsService`、线程、调度器、数据库表或阈值，除非现有合同无法表达且证据充分；
- unknown/warming_up/stale/error 保持真实语义，禁止默认零、兼容值或猜测值；
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
- [phased-repair-rollout-status.md](phased-repair-rollout-status.md)：已完成、观察中、阻塞项；
- [phased-repair-acceptance-matrix.md](phased-repair-acceptance-matrix.md)：阶段门和发布证据。

### 领域合同，按需读取

- [api-fact-contract.md](api-fact-contract.md)：`fact.v1`、freshness、unknown 和前端展示语义；
- [learning-evidence-contract.md](learning-evidence-contract.md)：学习样本、污染、资格和权重；
- [position-supervisor-contract.md](position-supervisor-contract.md)：持仓监督器输入、候选和执行边界；
- [factor-card-schema.md](factor-card-schema.md)：因子卡片/目录展示合同；
- [parameter-template-contract.md](parameter-template-contract.md)：参数模板及 online/offline 变更边界；
- [server-backend-sop.md](server-backend-sop.md)：启动、日志、数据库、cTrader、重启和运行验收。

### 文档维护

- [documentation-governance.md](documentation-governance.md)：文档职责、更新和删除规则。

未列出的历史设计、版本计划和完成流水已删除；需要追溯时使用 Git 历史，不恢复为活动文档。
