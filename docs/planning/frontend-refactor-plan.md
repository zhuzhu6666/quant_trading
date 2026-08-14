# 前端重构实施计划

> Status: active
> Last verified: 2026-08-13
> Scope: web_frontend 原地重构为 Tauri 桌面优先的量化操作台。
> Owner: 前端工程

本文是前端重构工作流的唯一活动计划。它是全局生产计划
planning/production-autonomy-repair-optimization-plan.md 的前端领域从属计划，
不替代全局计划，也不重新定义后端、交易、风控和治理事实。

长期事实源、权力边界和接口状态分别以
system-source-of-truth.md、api-fact-contract.md、legacy-debt-register.md
和 change-impact-checklist.md 为准。

## 1. 当前基线

当前 web_frontend 是 React + Vite 的完整 Tauri 桌面 renderer，承担交易状态、PnL、
风控、运维、学习治理和因子治理展示；不作为服务器上的公网浏览器站点部署。当前代码已经具备：

- endpoint-level Fact 解码和 FactBoundary；
- 单一 /ws/state 实时快照连接；
- React Query 查询层；
- 登录、refresh、step-up 和一次性 WS ticket 的客户端接入；
- /overview、/trading、/performance/:section、/autonomy/:section
  和 /ops/:section 等旧页面路由。

现状问题不是后端事实源不足，而是前端产品模型仍按页面集合组织：

1. 交易、风险、研究和治理之间缺少连续工作流；
2. 面板布局、上下文和证据链不能作为工作区保存；
3. API 客户端仍有大量宽泛 Record 类型，关键读模型的边界不够集中；
4. 旧路由、旧页面和兼容 fallback 让同一事实存在多个展示入口；
5. 浏览器页面没有桌面端的安装、更新、凭证和受控离线语义。

重构从当前代码迁移，但不把这些问题解释为新增后端事实或新增执行器的理由。

## 2. 目标与非目标

### 2.1 目标

- 形成 B Market Workbench 工作台外壳、C Evidence Lab 研究治理工作区和
  A 全局 Safety / Readiness / Risk 安全门；
- 在现有 web_frontend 内建立 Tauri 2 桌面端；
- 用 React 19、TypeScript、Vite、TanStack Query 和 Radix 原语构成强类型、
  可停靠、可保存的终端界面；
- 首版同时提供 Trade Ops、Risk Desk、Research Lab、Governance、Ops 五个工作区；另提供只读 Workflow 运行路径页，不新增生产 authority；
- 后端继续是唯一事实源、唯一风险裁决者和唯一执行权威；
- 仅在本地缓存行情和研究材料，离线只读，不能把缓存变成授权输入；
- 支持本人 Windows 本地运行的 Tauri 壳；不把 Windows 对外发行、安装包签名、
  GitHub Releases 或自动更新作为本批交付目标；
- 直接切换新路由并删除被替代的旧路由、页面、fallback、宽泛类型和无调用方
  兼容层。

### 2.2 非目标

- 不重写 backend、execution、alpha、risk、monitor 或 PostgreSQL；
- 不新增 /api/dashboard 或第二套事实聚合服务；
- 不在 Tauri 进程内运行本地交易引擎、风控计算器、因子计算器或治理 writer；
- 不把账户、持仓、风险、控制状态、认证结果或 access token 写入研究缓存；
- 不保留新旧前端生产双轨，不为旧地址建立长期 redirect alias；
- 不用 readiness、前端状态栏或缓存重新计算 Safety、Risk sizing 或授权。

## 3. 产品模型

~~~text
Tauri Desktop Shell
├── 全局 Safety / Readiness / Risk Rail
├── Workbench Shell
│   ├── Trade Ops
│   ├── Risk Desk
│   ├── Research Lab
│   ├── Governance
│   └── Ops
├── Command Palette
└── Read-only Research Cache
~~~

三个设计来源的组合关系：

| 来源 | 在最终产品中的职责 |
|---|---|
| B Market Workbench | 交易终端的停靠、拆分、图表、订单/持仓、风险上下文和快捷操作 |
| C Evidence Lab | 回放、因子证据、决策追踪、治理候选和审计材料的连续工作区 |
| A Safety Gate | 全局实时安全边界、未知状态提示、动作可用性和危险操作确认 |

研究与操作的主链为：

~~~text
Research Lab
  -> Decision Trace
  -> Governance
  -> Risk Desk
  -> Trade Ops
~~~

这条链只传递服务器已持久化的引用、事实和提交结果；前端不在链路中推导
因果结论、风险数值或执行许可。

## 4. 路由与工作区

### 4.1 目标路由

| 路由 | 工作区 | 主要内容 |
|---|---|---|
| /login | 身份入口 | 登录、refresh session、step-up 入口 |
| /trade-ops | Trade Ops | 实时状态、行情、图表、订单/持仓和风险缩减动作 |
| /risk-desk | Risk Desk | 风险摘要、政策裁决、仓位 sizing 投影和 trade trace |
| /research | Research Lab | K 线、回放、因子目录、证据、decision trace |
| /governance | Governance | 候选、审查、mutation、release 和审计链 |
| /ops | Ops | 健康、恢复、incident、服务和桌面连接诊断 |
| /workflow | Workflow | 当前项目运行路径、组件职责和最近观测 |

路由只表达用户当前工作区，不表达事实来源。事实仍由各自 API contract 和
/ws/state 提供。

### 4.2 旧路由策略

以下路径在直接切换批次中移除注册，不建立重定向别名：

~~~text
/overview
/trading
/pnl
/risk
/learning
/models
/v15
/v16
/performance/*
/autonomy/*
/ops/*
/governance/*
~~~

其中 /ops 是目标工作区新路由，但旧的 /ops/:section 和旧页面 section 语义
必须删除；目标路由只保留明确的 Ops 工作区入口。旧地址在静态托管层返回
404/410；若仍被 SPA fallback 捕获，只显示 route_deprecated，不自动导航到新地址。

## 5. 技术选型

### 5.1 渲染层

| 层 | 选型 | 边界 |
|---|---|---|
| Desktop shell | Tauri 2 | 负责窗口、安装、更新、受控 OS 能力和安全存储 |
| Renderer | React 19 + TypeScript | 负责视图、交互和强类型读模型，不拥有业务权力 |
| Build | Vite | 同一 renderer 可用于桌面构建和必要的浏览器验收 |
| Routing | React Router | 只管理工作区 URL 和 route-deprecated，不做事实 fallback |
| Server state | TanStack Query | 管理 HTTP 读模型；不复制 WS live authority |
| UI primitives | Radix | Dialog、Popover、Tooltip、Tabs、Menu、Focus 管理等无业务原语 |
| Design system | 自建终端 tokens/components | 统一工作台、研究画布、Fact 状态、危险动作和密度 |
| Local cache | IndexedDB | 仅保存 allowlist 内的行情/研究只读快照 |

图表、表格和文本编辑器都必须通过 renderer 内的稳定适配接口接入，不能让
第三方组件直接读取原始 API payload 或拥有动作权限。

### 5.2 前端模块边界

~~~text
src/
├── app/                 路由、权限边界、全局 provider
├── shell/               Tauri/浏览器 shell、rail、palette、layout
├── design-system/       tokens、原语封装、Fact 状态组件
├── api/                 endpoint schema、client、query key、mutation result
├── live/                唯一 /ws/state 连接和快照 store
├── workspaces/
│   ├── trade-ops/
│   ├── risk-desk/
│   ├── research/
│   ├── governance/
│   └── ops/
├── cache/               IndexedDB allowlist、版本、失效、离线只读
├── auth/                access memory、refresh、step-up、session lifecycle
└── tests/               contract、behavior、architecture、desktop seam
~~~

模块之间只通过显式类型和接口通信。工作区不能直接读取另一个工作区的
React state；跨工作区只传递 URL 引用、server-issued ID、Fact 或 mutation
结果。

## 6. 前后端边界

### 6.1 后端继续拥有

- broker 账户、持仓、成交和 reconcile；
- Safety 硬事实和风险缩减；
- Readiness 的事实充分性判断；
- Risk sizing、VaR/CVaR、stress、concentration 和最终仓位；
- factor、learning、governance、release 和 incident 的生产 writer；
- access/refresh/session/step-up/WS ticket 的认证事实；
- 所有 mutation 的权限、提交、durable ID、审计和最终状态。

### 6.2 前端只拥有

- 工作区导航、面板布局和用户显示偏好；
- server-issued Fact 的展示、排序、过滤和引用；
- 用户动作的意图收集、确认、提交和结果展示；
- IndexedDB 中 allowlist 研究快照的本地生命周期；
- 桌面窗口、更新、凭证存储调用和本地诊断。

前端不得因为某个字段缺失而回退到另一个字段、默认零值或旧接口；新类型
解码必须按 endpoint contract 和显式字段读取。

### 6.3 复用的接口面

首版继续复用以下现有接口族：

~~~text
/ws/state
/api/market/bars
/api/live/*
/api/risk/*
/api/v4/*
/api/learning/*
/api/ops/replay/*
/api/ops/incident-control
/api/ops/release/*
/api/ops/brain/*
~~~

不新增巨型 dashboard 聚合接口。只在新前端真正依赖且当前 OpenAPI 类型不足
的关键端点补齐 response_model、contract、reason_code、mutation durable ID、
audit ID 和提交状态；每次真实接口变更必须同步 api-fact-contract.md、
OpenAPI snapshot、后端测试和前端 decoder。

关键接口优先级：

1. 实时：/ws/state、/api/live/status、/api/live/account、
   /api/live/positions、/api/live/session-stats、/api/live/realized-pnl-series；
2. 市场：/api/market/bars；
3. 风险：/api/risk/summary、/api/risk/policy/verdicts、
   /api/risk/trade-trace/recent；
4. 研究：/api/v4/catalog、/api/ops/replay/latest、
   /api/ops/replay/bar-decisions、相关 learning factor card/review/application；
5. 治理与运维：incident-control、release、brain governance candidate/review。

## 7. 统一类型与 Fact 语义

前端建立统一类型层，类型只描述客户端消费，不成为新的服务端事实源：

~~~text
WorkspaceId
FactViewState
ResearchSnapshot
DecisionTrace
ActionIntent
MutationResult
CacheEntry
~~~

FactViewState 只允许 known、stale、unknown、error，并带来源、观测时间、
新鲜度和 reason_code。具体 envelope、freshness 和 authoritative-empty 规则
只引用 api-fact-contract.md。

渲染要求：

- known 且业务值正常才显示绿色；
- stale 可以保留最后 known 数值，但必须同时显示 stale、观测时间和年龄；
- unknown 不显示猜测的零账户、零风险、空仓或“正常”；
- error 显示错误原因和重试动作，不抹掉失败语义；
- readiness false 是已知业务结论，不等于数据源 error；
- unknown/stale/error 禁用 start、unlock、治理放松和风险增加动作；
- stop、emergency、close、reduce、tighten 等风险缩减动作仍按服务端策略可用。

## 8. 本地缓存与离线语义

IndexedDB 只允许以下 cache namespace：

| Namespace | 可缓存内容 | 可离线动作 |
|---|---|---|
| market | K 线、行情查询结果和图表视口所需快照 | 查看、筛选、改变视口 |
| replay | 已完成回放报告和回放证据引用 | 查看、筛选、复制引用 |
| factor | 因子目录、因子卡片和证据引用 | 查看、筛选、打开详情 |
| research | learning/review/application/governance 只读材料 | 查看、筛选、打开审计引用 |

每条 CacheEntry 至少包含：

~~~text
cache_key
contract
schema_version
payload
source
observed_at
generated_at
expires_at
content_hash
~~~

禁止缓存：

~~~text
access token
refresh token
账户
持仓
风险摘要
Safety/readiness/control 状态
mutation 提交结果
认证结果
~~~

离线时只允许显示 cache/stale 标记的研究内容；不允许开仓、解锁、治理
变更、启动、恢复或任何风险增加动作。恢复在线后必须重新向服务端读取并
校验，不能把缓存升级为 known 或授权输入。

## 9. 视觉与交互方向

- 默认深色终端背景，交易工作台使用高密度网格，研究工作区使用更宽的证据画布；
- Safety rail 始终可见，不因面板滚动、全屏图表或切换工作区隐藏；
- 绿色只表示 server-known 且业务允许，蓝色表示信息/研究，黄色表示
  stale 或待确认，红色表示 error、硬阻断或危险动作；
- 统一使用等宽数字、明确的单位、来源和观测时间；
- 面板可以 dock、split、pin、collapse，并把布局保存为本地 UI preference；
- 工作区布局不得改变服务端数据权限，也不得把隐藏面板解释为事实不存在；
- Command Palette 只列出当前权限和当前 Fact 状态允许的动作；
- 危险动作必须展示影响范围、当前事实、服务端 reason_code 和审计反馈。

详细交互和 tokens 见 frontend-operator-contract.md；桌面安全和缓存见
frontend-desktop-contract.md。

## 10. 实施阶段

### 阶段 0：冻结合同

完成本文、操作台合同、桌面合同、验收矩阵和状态文档；冻结目标路由、权限、
Fact 状态、缓存 allowlist、删除清单和回滚方式。

### 阶段 1：Shell 与设计系统

在 web_frontend 内建立 src-tauri、Tauri capability、Workbench Shell、全局
Safety rail、Command Palette、布局状态和终端 tokens。先不迁移业务事实。

### 阶段 2：强类型数据层

建立 endpoint schema、query key、mutation result、唯一 live store 和
FactBoundary。关键接口先补 OpenAPI/response_model，再迁移工作区消费。

### 阶段 3：安全门和动作层

实现全局 Safety / Readiness / Risk rail、权限边界、step-up、危险动作确认、
mutation durable/audit ID 展示和审计反馈。

### 阶段 4：五个工作区与 Workflow 运行路径

按 Trade Ops、Risk Desk、Research Lab、Governance、Ops 完成所有首版工作区，并补充只读 Workflow 运行路径页，
并接通 Research → Decision Trace → Governance → Risk Desk → Trade Ops 引用链。

### 阶段 5：缓存与离线

实现 IndexedDB schema/version/content hash、过期策略、cache/stale 展示、
恢复在线重验证和动作禁用。

### 阶段 6：个人本机运行收口

验证本人使用环境中的 WebView2、Tauri 本地 dev/build、Credential Manager、
offline/re-auth、缓存 schema、窗口恢复和危险动作安全边界。Windows 对外发行、
安装包签名、GitHub Releases、公开 updater 和 Windows runner 不在本批范围；相关
配置可以保留为未启用的实验代码，但不作为完成门。

### 阶段 7：直接切换与删除

一次性启用新路由，旧地址不重定向；删除旧页面、旧 AppShell、旧 section
组合、旧 fallback、宽泛公共类型和无调用方兼容层。完成验收矩阵后更新
README、旧债、状态和必要的接口合同。

内部可以按小批提交，但生产环境不保留新旧前端双轨。

## 11. 替代与删除清单

| 新实现 | 替代对象 | 完成时必须删除 |
|---|---|---|
| Workbench Shell | src/components/AppShell.tsx 和旧导航 | 旧 AppShell、旧导航数组、旧默认 redirect |
| workspace routes | src/pages/* + WorkspacePages.tsx 旧 section 组合 | 旧 page route、旧 section redirect、旧 route alias |
| typed endpoint decoder | api/client.ts 中关键端点的 Record 返回值 | 关键端点宽泛返回类型和重复字段读取 |
| FactBoundary v2 消费 | src/lib/compat.ts 和 recursive pick fallback | 无调用方的 compat helper、旧字段 fallback、耦合测试 |
| live snapshot store | useLiveState.ts 中旧快照兼容投影 | 重复 live projection、HTTP fallback、页面级 WS |
| 自建终端设计系统 | 旧 styles/console.css 等页面专属重复样式 | 被替代 tokens、重复状态色和页面专属壳 |
| src-tauri desktop shell | 仅浏览器发布的旧运行入口 | 旧桌面假设、未使用的桌面兼容代码 |

删除以 import graph、测试和生产构建为证据；若某个旧文件中的局部组件被
迁移后仍有真实调用方，只提取该组件并删除原文件，不以兼容为由保留旧 page
和 wrapper。

## 12. 本地运行与回滚

- 个人本地构建绑定 Git commit、版本号、OpenAPI snapshot 和 renderer artifact hash；
- 本地回滚通过重新构建已知 commit 或恢复本地可用构建，不通过旧路由双轨回滚；
- 本批不发布 Windows 安装包，不配置公开签名、GitHub Releases 或自动更新；
- 任何后端接口变更先通过 API contract/OpenAPI/前端 acceptance，再更新本人本地
  renderer；
- 服务器只保留后端 API/WSS；必须确认 Caddy 不再托管 `index.html`、`dist` 或前端静态 asset，
  且服务器同步不会拉取 `web_frontend/` 和 `miniprogram_v2/`。

## 13. 完成条件

只有同时满足以下条件，前端重构才可标记 complete：

1. 六个工作区都能在本人本地 Tauri dev 或本地构建中打开并通过权限/Fact 验收；Workflow 仅验证只读事实和动画语义；
2. 本机可以启动、重启并完成 WebView2、认证、WS、缓存和窗口恢复验收；不要求
   Windows 安装器、签名、公开升级或升级回退证据；
3. /ws/state 仍是唯一实时状态来源，后端仍是唯一事实源和执行权威；
4. 离线只读行情/研究缓存，风险增加动作全部禁用；
5. unknown、stale、error 没有被伪装为正常值或零值；
6. 危险动作有服务端重新校验、step-up（如需要）、durable ID、audit ID 和
   提交状态；
7. 旧路由、旧页面、旧 fallback、旧宽泛公共类型和无调用方 wrapper 已删除；
8. OpenAPI、类型检查、构建、合同测试、本机桌面验收和旧路径扫描全部通过；
9. README、legacy debt、status、acceptance matrix 与实际证据一致。

公开 Windows 分发、GitHub Releases、安装包签名和自动更新明确不在本批完成条件内。
