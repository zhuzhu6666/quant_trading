# 前端操作台产品合同

> Status: active
> Last verified: 2026-08-28 (reviewed, no semantic change from 08-13; server API/WSS-only, Windows本地 renderer 已落地)
> Scope: Tauri/React 操作台的信息架构、工作区、动作和视觉语义。

本文只定义本地 Tauri 桌面端和小程序如何组织、消费事实。Fact envelope、freshness、认证和后端
权力边界分别引用 api-fact-contract.md、system-source-of-truth.md 和
frontend-desktop-contract.md，不在此复制。

## 1. 产品原则

1. 一张屏幕先回答“现在能不能安全做事”，再展示细节。
2. 事实、证据、动作和审计结果必须分区，不把建议画成已执行。
3. Research 负责理解和引用，Risk Desk 负责读取风险事实，Trade Ops 负责提交
   动作；前端不跨域重算。
4. 所有绿色状态都必须能追溯到 known Fact 和服务器返回的业务结论。
5. 布局是用户偏好，不是生产状态；隐藏面板不会删除事实。

## 2. 工作台骨架

~~~text
┌──────────────────────────────────────────────────────────────┐
│ Safety / Readiness / Risk Rail       Command Palette  User  │
├──────────────┬───────────────────────────────┬───────────────┤
│ Workspace Nav│ Main Workbench                │ Context Dock  │
│              │ dock / split / tab panels     │ evidence      │
│              │                               │ action detail │
├──────────────┴───────────────────────────────┴───────────────┤
│ Connection / freshness / audit activity                       │
└──────────────────────────────────────────────────────────────┘
~~~

### 2.1 全局 Safety / Readiness / Risk Rail

Rail 固定在最上方，跨路由保持位置和当前连接状态。它至少显示：

- broker / account / positions / loop 的实时 Fact 状态；
- effective incident mode、local latch 和是否允许新增风险；
- readiness 的已知维度：live execution、live alpha、autonomous mutation、
  release；
- risk snapshot 的 freshness、主要 blocker 和当前仓位摘要；
- WS 连接状态、最近完整快照时间、重连次数和认证失败提示。

Rail 的动作只允许：

- 打开对应工作区和证据详情；
- 触发服务端定义的风险缩减动作；
- 进入 step-up 或确认流程；
- 重新读取服务器事实。

Rail 不拥有 start、unlock、governance 或 sizing 计算权。

### 2.2 Command Palette

Command Palette 是动作入口，不是第二个导航树。每一项命令必须包含：

~~~text
command_id
label
scope
permission
fact_requirements
risk_class
confirmation
server_endpoint
~~~

列表过滤顺序为：用户权限 → 当前工作区 → 当前 Fact 状态 → 风险等级。未知
状态不会被隐藏；它应显示为“不可用，因为 reason_code”，而不是从命令列表
静默消失。

## 3. 六个工作区合同

### 3.1 Trade Ops

读模型：

- /ws/state 的完整 live snapshot；
- /api/market/bars 的当前图表数据；
- live account、positions、session、strategy；
- /api/risk/summary 和最近 trade trace 的服务端投影。

主要面板：

~~~text
Live Status | Market Chart | Positions | Order/Action Ticket | Risk Context
~~~

允许动作：

- 查看行情、账户、持仓和实时风险；
- 在服务端允许且完成确认后提交风险增加动作；
- stop、emergency、close、reduce、tighten；
- 打开同一 position、decision、trade trace 或 evidence 的详情。

动作边界：

- 订单数量、止损、可交易性和风险预算只能由服务端计算和复核；
- 前端展示用户意图，不把输入值当作最终仓位；
- unknown/stale/error 时不允许用输入框默认值提交；
- close/reduce/tighten 不因普通研究数据过期而被前端隐藏。

空状态：

- 没有持仓：显示“当前无已确认持仓”，不显示伪造的 0 风险替代文案；
- 没有实时快照：显示 offline/unknown 和最后一次观测时间；
- 图表无数据：显示数据源和时间范围，不用空数组模拟已加载。

### 3.2 Risk Desk

读模型：

- /api/risk/summary；
- /api/risk/policy/verdicts；
- /api/risk/trade-trace/recent；
- /api/live/positions、account、session 的相关 Fact。

主要面板：

~~~text
Risk Snapshot | Policy Verdicts | Position Exposure | Stress/VaR | Trade Trace
~~~

允许动作：

- 查看 blocker、reason_code、政策裁决和真实执行结果；
- 进入 Trade Ops 进行 close/reduce/tighten；
- 提交服务端允许的 incident 收紧或风险缩减动作。

禁止动作：

- 在前端重算 VaR、CVaR、stress、concentration、Kelly 或最终 volume；
- 把 readiness false 解释为“风险为零”；
- 把 policy allow 解释为 broker 已执行。

### 3.3 Research Lab

读模型：

- /api/market/bars；
- /api/v4/catalog 和因子卡片；
- /api/ops/replay/latest、bar-decisions、bar-preview；
- /api/learning/* 的 research/evidence/read-only 投影；
- 已持久化的 Decision Trace 引用。

主要面板：

~~~text
Market Canvas | Replay Timeline | Factor Catalog | Evidence Cards | Decision Trace
~~~

允许动作：

- 查询、筛选、比较和复制证据引用；
- 运行服务端允许的 replay；
- 从一个 decision、factor、replay 或 trade 打开关联证据；
- 将 research context 传给后续治理审查。

禁止动作：

- 把回放结论直接应用到 runtime；
- 把因子排序、模型建议或 LLM advisory 显示成已批准 mutation；
- 将 Research cache 作为风险、readiness 或执行输入。

空状态：

- 没有证据：显示 evidence_missing 和需要的来源；
- 证据 stale：保留内容但明确标注观测时间和不可用于授权；
- 回放未完成：显示 durable replay ID 和提交状态，不显示成功指标。

### 3.4 Governance

读模型：

- /api/ops/brain/governance-candidates；
- /api/ops/brain/governance-candidate-reviews；
- /api/ops/autonomy/proposals；
- /api/ops/release/*；
- factor lifecycle、application、effect 和 audit 的现有只读端点。

主要面板：

~~~text
Candidate Queue | Evidence Coverage | Review/Conflict | Mutation Timeline | Release
~~~

允许动作：

- 查看候选、证据缺口、冲突、source reliability 和 review 状态；
- 触发服务端定义的 review、replay 或只读 evidence refresh；
- 在权限、step-up、V16/Policy/Coordinator 服务器门全部满足时提交治理动作；
- 查看 mutation durable ID、audit ID、commit 状态和 rollback 引用。

禁止动作：

- 前端批准/应用 candidate；
- 前端绕过 Candidate Review、RiskPolicy、V16CommandGate 或 Coordinator；
- 用“按钮成功”替代 committed mutation 和回读事实。

### 3.5 Ops

读模型：

- /api/health、/api/ops/backend-readiness；
- /api/ops/recovery、alerts、incident-control；
- system、sync、cTrader token 和 external data status；
- release、incident playbook 和运行证据端点。

主要面板：

~~~text
Service Health | Readiness | Incident Control | Recovery | Logs | Release Evidence
~~~

允许动作：

- 查看运行健康、恢复、日志和发布证据；
- 执行服务器允许的 incident 收紧、stop、emergency 和只读诊断；
- 触发服务端定义的 recovery/playbook/release 操作；
- 任何风险增加或权限放宽必须进入 step-up 和审计流程。

空状态与失败：

- 服务未注册显示 unknown/not_registered；
- API 请求失败显示 error，不把最后一次成功读数标记为当前 known；
- incident mode 的 effective 值必须包含 local latch 的收紧效果。

### 3.6 Workflow

读模型：

- `/ws/state` 的完整 `live.state.v2` 快照；
- `/api/ops/backend-readiness` 的只读维度投影；
- `/api/learning/*`、治理候选/审查/提案、应用和后验效果的 endpoint-specific 只读事实；
- 架构图中涉及的 K 线月库、外部 PIT 数据、事件库、systemd/日志/recovery 只作为已确认的代码/数据边界展示。

主要面板：

~~~text
Integrated Architecture Topology | Selected Architecture Node
~~~

允许动作：

- 在同一张拓扑图中查看实时执行主干、市场/外部数据、智能学习反馈、治理后验、服务运维和客户端消费之间的职责与传输关系；
- 点击架构节点查看输入、输出、事实来源、最近观测时间和 reason_code；
- 查看 cTrader、serial live loop、因子/信号、Safety/RiskPolicy、执行对账、PostgreSQL `runtime`/`canonical_v2` 和桌面消费之间的职责关系；
- 点击节点查看来源、最近观测时间、reason_code 和当前只读状态；
- 查看学习证据、治理 Coordinator 和 committed projection 如何回流到下一轮 live loop。

限制：

- 工作流页只显示服务端事实和 readiness 投影，不新增 dashboard 聚合接口；
- 架构拓扑是服务端 authority、现有 endpoint 和已退役/保留边界的客户端只读组合投影，不新增架构事实或 dashboard 聚合接口；
- 动效只表示当前 `/ws/state` 或页面标注的传输方向，不表示发生了订单或治理提交；
- 没有独立运行 Fact 的代码/数据边界必须标记为“架构节点”，不能冒充当前健康或已确认；
- `observed_at` 只表示最近观测，不被解释为组件实际执行时间；
- 前端不重算因子、风险、Safety、readiness 或治理授权。

## 4. 跨工作区证据链

~~~text
Research Lab
  选择 decision / factor / replay
        │ server-issued reference
        ▼
Decision Trace
  读取 source、timestamp、lineage、reason_code
        │ evidence coverage / review reference
        ▼
Governance
  review、candidate、mutation、audit
        │ committed mutation / policy reference
        ▼
Risk Desk
  重新读取 RiskPolicy、风险输入和执行状态
        │ server-validated action intent
        ▼
Trade Ops
  提交或风险缩减，并等待 durable result
~~~

每一次跨区跳转都携带引用 ID，不复制大 payload。目标工作区进入后必须重新
读取自己的权威端点；URL 中的 ID 是定位线索，不是授权凭证。

## 5. 面板布局与保存

布局状态只保存 UI preference：

~~~text
workspace_id
layout_version
panel_tree
active_tabs
split_ratios
pinned_panels
collapsed_sections
updated_at
~~~

布局保存到本地设置，不进入服务端事实库，也不进入研究缓存。恢复布局时：

- 未知 panel ID 被忽略并记录本地诊断；
- 新版本 layout_version 使用显式迁移；
- 面板加载失败不改变对应 Fact；
- 布局不能恢复 action permission、token、mutation 或风险数值。

## 6. Fact 展示规则

| Fact 状态 | 视觉 | 数据 | 动作 |
|---|---|---|---|
| known | 绿色/正常 | 显示值、来源、观测时间 | 按业务权限可用 |
| stale | 黄色/过期 | 可显示最后值和年龄 | 风险增加/放宽禁用 |
| unknown | 灰色/待确认 | 不显示伪造零值；显示原因 | 风险增加/放宽禁用 |
| error | 红色/错误 | 显示错误和重试 | 风险增加/放宽禁用 |

FactBoundary 必须按 endpoint contract 解码，不递归搜索任意 status、ok、
items、data 或旧字段。authoritative-empty 只有在合同明确允许时才显示为
known empty。

## 7. 权限、危险动作和 step-up

前端权限是服务端权限的显示投影，不是授权器。每个 mutation 的按钮必须经过：

1. 当前 session 和用户权限检查；
2. 当前工作区和动作 scope 检查；
3. 所需 Fact 状态检查；
4. 服务端返回的 action gate/reason_code 检查；
5. 高影响动作的 step-up；
6. 二次确认和影响范围展示；
7. mutation 提交后等待 durable ID、audit ID、commit/status 回读。

确认对话框必须显示：

~~~text
动作
目标对象
预期影响
当前服务端事实
阻断/允许 reason_code
是否需要 step-up
提交后状态和审计引用
~~~

默认快捷键：

| 快捷键 | 动作 |
|---|---|
| Ctrl/Cmd + K | 打开 Command Palette |
| Ctrl/Cmd + 1..6 | 切换六个工作区 |
| Ctrl/Cmd + Shift + L | 聚焦 Safety rail |
| Ctrl/Cmd + Shift + R | 重新读取当前工作区非实时事实 |
| Esc | 关闭 palette、dialog、context dock |

危险动作不绑定无确认快捷键；Emergency/stop/close/reduce/tighten 的具体
可用性仍由服务器和 risk-reduction scope 决定。

## 8. 统一视觉 Token

### 8.1 色彩语义

| Token | 用途 |
|---|---|
| surface-0/1/2 | 应用、工作区、面板背景 |
| border-subtle/strong | 分隔和焦点边界 |
| text-primary/secondary/muted | 主要、辅助、说明文字 |
| signal-positive | 仅 known 且业务正常 |
| signal-warning | stale、等待、待确认 |
| signal-danger | error、硬阻断、危险动作 |
| signal-info | 研究、引用、说明 |

颜色不是事实；组件必须同时提供文字、图标、reason_code 和时间。

### 8.2 密度与排版

- 默认间距基准 4px，面板内层级使用 4/8/12/16/24px；
- 交易数据使用等宽数字和固定单位；
- 标题、数值、来源、时间、状态分层，不用超大 KPI 取代上下文；
- 研究画布允许更宽文本列，但不降低事实来源和时间的可见性；
- 焦点状态、键盘导航和高对比度不能依赖颜色。

## 9. 产品验收边界

本合同的可重复验收见 frontend-refactor-acceptance-matrix.md。任何组件若
无法说明自己的事实来源、状态、权限、空状态和删除替代对象，不得进入生产
工作区。
