# 前端重构当前状态

> Status: active rollout status
> Last verified: 2026-08-29 (本地 renderer/Fact/recovery 修复与 npm test/typecheck/lint/build 已验证；Tauri runtime acceptance 未重验; sparseCheckout 08-14 已收口)
> Scope: 只记录前端重构实际进度，不重复产品和架构合同。

## 1. 当前阶段

阶段：D1 renderer 与桌面壳首批实施完成；本人已确认个人本机认证通过且可以正常使用，
服务器 backend-only 收口也已完成；剩余工作集中在完整生命周期验收和少量桌面兼容性。

`web_frontend/` 工作树中的 renderer 已切换到 React 19 + TypeScript + Vite 的六工作区
路由和 Tauri 2 壳；服务器仍是唯一事实源、风险裁决者和执行权威。2026-08-13 曾将
本地通过检查的 static artifact 曾部署到 Caddy 根目录并完成公网 smoke；2026-08-14 已
撤下浏览器静态入口，Caddy 只保留 API/WSS 反代。认证 HTTP、`/ws/state` 本机/公网和
`market.bars.v1` 合同 smoke 仍是历史验证证据，当前根路径已验证为 404、`/api/health`
为 200。
Windows 本机已启动发行 executable，确认 WebView2 登录壳和可访问性树可用；本人已确认正常
登录和基本使用路径通过。本轮已完成工作区主要排版、后端字段解析和 freshness 语义收口；
缓存、断网恢复、mutation/step-up 全路径仍未作为完整验收通过。Windows 对外发行、签名、GitHub Releases
manifest 和公开 updater 已明确移出本批完成条件。

## 2. 本批已完成

- 建立最小 Tauri 2 工程、可选的本地 Windows NSIS 构建、最小 capability、
  WebView2/桌面诊断、UI preference、研究缓存边界、Windows Credential Manager
  refresh material bridge；公开发行/updater 配置不作为个人自用交付内容；
- 升级 React 19，保留 TypeScript/Vite，引入 Radix Dialog/Popover/Tabs/Tooltip 原语；
- 建立 Workbench Shell、全局 Safety/Readiness/Risk rail、Command Palette、六个工作区
  导航和可保存的 sidebar/context dock/pin/tab/split/collapse 布局；
- 建立 `WorkspaceId`、`FactViewState`、`ResearchSnapshot`、`DecisionTrace`、
  `ActionIntent`、`MutationResult`、`CacheEntry` 及 endpoint-specific decoder；
- 将 `/ws/state` 收敛为单例完整 `live.state.v2` 快照来源，使用 ticket、认证失败清空、
  有界 30 秒 backoff，删除 HTTP live fallback、页面级 live 轮询和旧快照合并；常规
  read-model 查询保留各自刷新间隔，隐藏窗口可暂停且在回到前台或网络重连时自动重验证；
- 实现 Trade Ops、Risk Desk、Research Lab、Governance、Ops 五个工作区及只读 Workflow 一体化架构拓扑页；拓扑把实时执行主干、市场与外部数据、智能学习反馈、治理后验、服务运维和 API/客户端消费放进同一张图，展示既有 authority 与传输方向，点击节点显示输入、输出、事实来源和观测状态；Research
  接入 bars、replay、bar-decisions/PIT trace、factor、learning 和 IndexedDB 只读缓存；
- 实现 known/stale/unknown/error 展示语义、unknown 零值防护、服务端 action ticket、
  step-up、mutation/audit/commit 结果投影和离线风险增加动作禁用；风险缩减入口仍交给
  服务端复核，不被普通研究缓存过期误禁用；
- 删除旧页面、旧 `AppShell`、旧导航/route alias、`src/lib/compat.ts`、旧 domain/query
  wrapper、页面级旧 live hook 和旧页面绑定样式；
- 对 `/api/market/bars` 做最小合同补充：`market.bars.v1` `_fact`、response model、
  OpenAPI snapshot、后端断言和前端 decoder 已同步，并已部署远程；重启后本机/公网
  认证 GET 均返回 `_fact`；行情 freshness 按 `1800s` 展示窗口计算，只有超过真实行情
  窗口才显示 `stale`，不再把 20 秒安全心跳阈值误用于 K 线。
- 对 `/api/learning/factor-cards` 做最小只读性能收敛：优先复用最新持久 factor catalog
  snapshot，snapshot 缺失时保留原 live build 回退；远程完整 `tests/test_factor_cards_api.py`
  为 `44 passed`，认证 `limit=25` 请求从约 54 秒降至约 9.9 秒。
- 远程合同/认证/WS/风险读模型定向批次为 `206 passed`；隔离的 Safety/Risk/Governance
  gate 批次为 `162 passed`。其中新增 `market` freshness 分类同步了
  `tests/test_fact_envelope.py`，没有改变 Safety、Risk sizing 或治理 authority。
- 2026-08-13 曾将 `frontend-20260813-043606-0fb3fba38f26` 部署到远程 Caddy 静态根；
  新 `index.html`、主 JS/CSS 和当时五个新工作区入口均返回 `200`，无 HTTP redirect；Workflow 为后续本地只读工作区，不属于该历史公网静态发布；
  旧 dist 已归档到仓库外 rollback 目录。随后 `backend-api-20260813-044620-market-fact`
  以远程 pre-change backup、targeted pytest `2 passed`、OpenAPI 生成检查和重启后
  `health=known` 验证完成；因子卡性能收敛后当前后端 PID 为 `1714609`，当时远程工作树
  曾保留 7 个未提交合同/性能改动；`system.health.v2=known`、DB/cTrader connected，
  Caddy 与 backend service 均 active，OpenAPI snapshot check 通过。

### 2026-08-13 Windows 个人自用桌面 QA 与数据流修正

- 通过已登录的 Tauri release executable 逐页检查交易运营、风险台、研究实验室、治理中心和运维中心；仅使用导航、等待数据和滚动检查，没有触发交易、治理提交、发布或事故收紧动作。
- 修正 `/ws/state` 快照的前端投影：账户、仓位、循环、session 和 spot 现在从父级 `live.state.v2.components` 读取对应事实，同时复用业务值；账户、仓位、循环和现货中间价已在客户端显示为服务端确认值，空仓显示为 `0 条已确认记录 / 当前无已确认持仓`。
- 修正因子目录字段映射：`factor_id`、`lifecycle_status`、`health_status`、`reason_excluded` 和 `catalog_ts` 不再被通用 `id/title/status` 缺失误判为“未命名记录 / 未知”；实际运行态已看到 `adx`、`atr_ratio`、`bb_width` 等 ID 以及 `QUARANTINED` 和原因码。
- 移除跨 contract 的事实回退：行情、风险、策略裁决、执行追踪、治理四类记录和运维各接口现在分别保留自己的 `fact.v1`；请求失败显示 `error` 与读取失败提示，不再借用风险或 readiness 事实伪装成已确认。
- 运行态复核：最终 release build 在 `Quant Trading Workbench` 窗口中启动，登录会话保留，WS 显示已连接，交易页显示 `180 根 K 线`；研究页顶部/中部/底部面板均可滚动到且未发现上下覆盖。Tauri 窗口截图 API 在本机返回 `SetIsBorderRequired failed (0x80004002)`，因此本次像素级截图未宣称通过，布局检查依据窗口无障碍树和实际滚动结构。
- 根据个人使用反馈，删除交易运营页底部重复的“动作 / 证据边界”折叠面板及其专属布局规则；动作票据本身保留，服务端权限和审计合同不变。
- 根据本次反馈，研究页“行情画布”原先按高度填充的彩色柱条已替换为共享 OHLC K 线组件；现在显示开盘/收盘实体、最高/最低影线、价格网格、时间轴和悬停开高低收，不改变 `market.bars.v1` 数据来源或服务端事实权威。
- 本批只改 renderer/文档/测试，不改变 backend authority、`/ws/state`、风险计算或 mutation 权限；Windows 仍仅作为个人自用桌面验证，不纳入公开发行条件。

### 2026-08-13 数据流、解析与 freshness 收口

- 原因已定位：多个后端响应使用嵌套 envelope（例如 `governance_candidates.items`、
  `readiness_dimensions`、`incident_control`、`replay.report`），同时真实字段使用
  `allowed`、`review_id`、`outcome_label`、`summary_text` 等；旧 decoder 只读取根级
  `items` 和通用字段，因此页面会出现空列表、`unknown`、`reason_unknown` 和重复的
  “未知/过期”占位。已按各 endpoint 的真实 contract 修正 decoder 和类型投影。
- freshness 已分层：普通展示 live/state/account/positions/loop 为 `30s`，健康探针为
  `75s`，行情 K 线为 `1800s`（M15 与后端数据周期一致）；新增独立
  `live.safety-freshness.v1`，安全硬门仍为 `15s` 并继续 fail-closed。WS “已连接”不再
  直接等同于业务数据“已确认”，两者分别显示。
- `/ws/state` 继续是唯一 live authority；安全 blocker、循环是否允许新增风险和各组件
  `Fact` 均由服务端投影，前端只解码和展示，不重新计算 Safety、Readiness 或 Risk sizing。
- 删除研究页无数据价值的“证据边界/研究边界”重复模块和治理页重复的“变更时间线”，
  将 replay、learning、policy verdict、execution trace、governance、readiness 和
  incident 的真实记录改为可读列表；空数据只显示明确的“无记录”，不再生成伪造的未知值。
- 后端定向回归通过：`47 passed`（fact/API/market/live 合同）和 `14 passed`（live loop
  与同步）；前端 `npm test`、`npm run typecheck`、`npm run build` 均通过。服务重启后
  `system.health.v2=known`、DB/cTrader connected；重启期间出现过一次真实 cTrader
  warmup disconnect，随后恢复，不是前端凭空制造的断线。
- 已重新生成并启动个人自用 release executable：
  `web_frontend/src-tauri/target/release/quant-workbench.exe`。未配置 Tauri 签名私钥，
  updater signing 步骤未执行；Windows 安装包签名不属于本项目个人自用验收范围。

### 2026-08-13 治理页空队列、历史证据与长字段修正

- 核对远程真实数据：候选表当前没有可审候选，现存记录均已进入
  `superseded/applied/rejected` 终态；候选审查仍有 `bridge_ready` 记录，提案登记也有
  只读记录。因此“候选为空”是后端业务结果，不是前端丢数据。
- 治理只读接口现在把成功完成的查询时间作为 read-model Fact 的观测时间；空候选会显示
  `known + 当前没有可审候选`，失败或未确认仍显示对应 `error/unknown`，不再把空列表误报
  为 `freshness_expired`。
- 发布证据的最新持久化记录确实是 `release_8a603cda328947b0`，记录时间为
  `2026-07-10 21:00:00`，状态为 `cancelled`；页面现在明确标注为历史发布记录，查询
  时间不会覆盖或伪造发布时间。
- 治理长 ID、状态、时间、动作和目标已拆行并允许任意换行，修复窄列中字体相互覆盖；
  发布证据面板改为整行布局，长字段不再挤压上方列。

### 2026-08-14 交易运营盈亏曲线

- 交易运营页原先的 `/api/market/bars` K 线面板已替换为 `/api/live/realized-pnl-series` 盈亏折线图；研究页仍保留 K 线，不混淆两个使用场景。
- 前端新增 `live.realized-pnl.v2` 专用 decoder，消费后端返回的平仓成交、单笔盈亏和累计盈亏；不使用行情价格推算收益，也不在前端生成交易结果。
- `全部`范围显示账户权益，权益 = `500.00 + 服务端全历史累计已实现盈亏`；`today / 24h / 7d / 30d` 只显示所选范围内的累计已实现盈亏，原始 `500.00 USD` 不重复注入周期曲线。该基线只属于图表展示，不修改 broker 账户余额、风险基准或运行时资金配置；未平仓浮动盈亏不纳入本图。
- `today / 24h / 7d / 30d / all` 五个范围均消费服务端返回点；`all` 显示
  `500.00 + 服务端全历史累计已实现盈亏`，周期范围只显示选定范围的累计盈亏，不重复注入
  `500.00`。`known/stale` 保留服务端历史点，`unknown/error` 不显示猜测曲线；只有 `all`
  范围在已确认但暂无平仓记录时显示 500.00 基线和明确的空记录说明，周期范围显示明确的
  无记录状态且不显示 500.00 基线。
- 本批不新增后端接口、不合并 API、不改变 Fact freshness 或安全心跳；只替换 Trade Ops renderer 的数据源和图表组件。

### 2026-08-29 本地前端修复批次

- Trade Ops 已将实际渲染路径收敛到共享 `src/design-system/PnlChart.tsx`；`all` 使用显示专用
  500.00 权益基线，周期范围以 0 为累计盈亏显示起点，7 日、30 日和全部范围的时间轴来自
  服务端点时间并显示日期+时间。
- SafetyRail 将 WebSocket 传输状态与 `live.state.v2` Fact 状态分开显示；通道连接本身不再
  被当作业务同步完成，业务事实仍按 `known/stale/unknown/error` 展示观测年龄。
- HTTP 查询策略为隐藏/最小化窗口允许暂停后台 interval，窗口重新激活或网络恢复时由 React
  Query 自动 revalidate；不新增前端计时器、第二轮询调度器或 live HTTP fallback。
- `src/api/workbench.ts` 单体已由八个 endpoint-specific domain modules 替代；这些 domain 文件
  不是删除项。Tauri 最小化恢复、高 DPI、断网重连等真实桌面运行验收仍保持未验证，不能由源码测试代替。

### 2026-08-14 服务器 backend-only 收口

- 本地与服务器版本先做了无损对比：服务器原有 37 个未提交文件与服务器快照仅有行尾差异；
  3 个合同/执行文件冲突保留已验证的本地合同版本，其余服务器独有后端改动通过普通 merge
  合入 `fd0aad52594493879fbdd33453c0fee0d19809e9`，未使用 force、reset --hard 或覆盖式拉取。
- 服务器 `main` 已与 `origin/main` 同为 `fd0aad5`；Git 使用 backend-only sparse checkout
  和 `blob:none` partial fetch，实际工作树不再物化 `web_frontend/` 或 `miniprogram_v2/`。
- Caddy 已改为只反代 `/api/*`、`/ws/*` 到 `127.0.0.1:8000`；公网根路径和旧静态 asset 均返回
  404，公网 `/api/health` 返回 200。服务器已删除前端 dist、node_modules、空小程序目录和旧
  frontend release archive，保留 `data`、`logs`、`.venv` 和后端运行目录。
- 服务器定向后端测试 `136 passed`，重启后新 PID `4158196` active，本机/公网 health 和
  `openapi.json` 均可访问；未执行交易、治理提交或发布动作。

## 3. 已通过的本地检查

在 `E:\quant_trading\web_frontend`：

- `npm install`：通过；
- `npm test`：通过，包含 smoke、架构删除扫描、Fact/auth、Fact 行为、WS 行为和六工作区
  产品合同测试；
- `npm run typecheck`：通过；
- `npm run build`：通过；
- `cargo check --manifest-path src-tauri/Cargo.toml`：通过；
- `npm run tauri -- build`：本地可生成 Windows NSIS 安装包和 release executable；签名不在
  个人自用范围；
- `npm exec tauri -- build --config src-tauri/tauri.release.conf.json --ci`：本机可生成
  executable/NSIS，未注入签名私钥时在签名步骤停止；这不构成本批阻塞；
- Windows 本机发行 executable smoke：通过，`Quant Trading Workbench` 窗口启动，
  WebView2 Runtime `151.0.4129.78` 存在，登录页控件和文档树可访问；未自动输入凭证。
- `.github/workflows/tauri-release.yml` 保留为未启用的未来公开发行配置；个人自用不配置
  GitHub Secret、不生成 Releases manifest，也不把该 workflow 纳入验收。
- 修改后的后端 Python 文件：使用 bundled Python AST 解析通过；OpenAPI snapshot 可由
  Node JSON 解析。

本机没有可运行的 FastAPI 后端 Python 环境，Windows 也没有 WSL；因此本批没有把后端
全量 pytest 写成已通过。Linux/server 已完成本合同的 targeted pytest、OpenAPI
生成/diff、认证私有 GET、`ws://`/`wss://` 完整 `live.state.v2` 首帧 smoke，且本批
新增数据流/状态语义回归已通过。仍需补完整生命周期的并发 401、refresh/logout、step-up、
mutation gate、缓存和断网恢复验收。

## 4. 当前阻塞项和未验证假设

- 公开 Windows 安装包、签名、GitHub Releases、updater 成功/失败回退和 Windows runner
  明确不在个人自用范围，不阻塞本批；本地回滚采用已知 commit 重建；
- 本人已确认认证通过且可以正常使用；本批已逐项定位并修复用户反馈的解析失败、重复空壳
  模块、15 秒展示误过期、健康探针过期和 WS 连接/业务数据混淆。剩余未验证范围是完整
  生命周期（WS 首帧/重连、缓存回退、mutation 回读）以及窗口缩放、高 DPI 和可访问性。
- 尚未完成本人机器上的 WebView2 缺失提示、多显示器、高 DPI、最小化恢复、Credential
  Manager、离线恢复、缓存 schema 和无障碍运行态验收；
- 未在 Linux/server 用真实接口和隔离 fixture 完成全量 fact freshness、完整 mutation
  矩阵和全量 pytest；本批已完成认证 bars/live/risk/readiness/WS smoke，以及
  `206 passed` 的合同/认证/WS/风险读模型批次和 `162 passed` 的 Safety/Risk/Governance
  gate 批次；并发 401/refresh、step-up 已由相关定向 fixture 覆盖，真实本机生命周期和
  mutation 仍未调用；
- 未完成浏览器/Tauri 无障碍焦点、Command Palette 权限投影、IndexedDB 临时数据库、
  缓存 hash/schema 失配、离线恢复重新认证/重新读取 Fact 的运行态验收；
- 正常登录路径已通过，但 WS 认证关闭路径当前只标记 `auth-failed` 并清空 live snapshot，
  尚需验证是否应同步清理会话并回到登录页；Tauri `clear_research_cache` 当前只是窄确认
  命令，实际 renderer IndexedDB 清理仍未接通；
- 旧浏览器静态入口和 hash asset 的历史发布目录已随服务器 backend-only 收口清理；
  以 API/WSS smoke 和根路径不再提供 SPA 为准，不再把浏览器旧地址废弃页作为生产客户端
  验收项。

## 5. 本批删除和替代

替代对象：旧浏览器操作台的 `AppShell`、旧 page route、旧 section fallback、旧宽泛
compat helper、旧页面绑定 CSS，以及页面级 live/HTTP fallback。

已删除路径包括：

- `src/pages/OverviewPage.tsx`、`TradingPage.tsx`、`PnlPage.tsx`、`RiskPage.tsx`、
  `LearningPage.tsx`、`ModelsPage.tsx`、`EvidencePage.tsx`、`V16BrainPage.tsx`、
  `WorkspacePages.tsx`；
- `src/components/AppShell.tsx`、旧 Action/Card/Dashboard/FactBoundary/Json/Query/Status
  组件；
- `src/api/workbench.ts` 单体、旧 query/risk snapshot wrapper、`src/lib/compat.ts`、旧格式化和
  display helper；当前 `src/api/domains/*` 为八个保留的 endpoint-specific domain modules；
- `src/styles/autonomy.css`、`console.css`、`domains.css`、`surface.css` 和仍绑定旧页面
  的 `accessibility.css`；
- 旧 route alias 没有迁入新 `App.tsx`，未定义路径显示废弃状态而不自动跳转。

唯一生产 authority 仍是后端 endpoint/Fact、`/ws/state` 和服务端 mutation；Tauri 仅做
桌面、诊断、凭证保护和 UI preference，不连接 broker、不写 PostgreSQL、不计算
Safety/Readiness/Risk、不执行治理。公开更新能力即使保留，也不属于个人自用 authority。

## 6. 回滚证据

历史公网静态发布的 artifact 证据为：远程发布标识为
`frontend-20260813-043606-0fb3fba38f26`，旧 dist 保存在
`/home/ubuntu/.local/share/quant_trading/frontend-releases/frontend-20260813-043606-0fb3fba38f26/rollback/`。
后端合同发布的 pre-change backup 保存在
`/home/ubuntu/.local/share/quant_trading/backend-releases/backend-api-20260813-044620-market-fact/pre-change/`。
因子卡性能发布的 pre-change backup 保存在
`/home/ubuntu/.local/share/quant_trading/backend-releases/backend-api-20260813-045921-factor-card-snapshot/pre-change/`。
个人本机桌面回滚采用已知 commit 重新构建；不要求 signed installer、`.exe.sig` 或
updater 失败回退测试。不恢复长期旧路由别名或双轨生产入口。

## 7. 下一批

1. 在本人机器复现并记录排版问题，覆盖 dock/split、窗口缩放、高 DPI、长值和无障碍焦点；
2. 沿“认证 -> WS 首帧/重连 -> endpoint Fact -> 缓存/离线回退 -> mutation 结果回读”
   跟踪数据流，补齐可定位的请求、时间和状态证据；
3. 修正或明确 WS 认证失效、缓存清理/回退和 mutation gate 的边界，再回填 acceptance
   matrix；
4. 在 Linux/server 继续运行后端全量 pytest、并发 401/refresh、step-up、mutation gate
   和真实 fact freshness fixture，并完成本人机器的本地运行态验收。

## 8. 状态更新格式

~~~text
Batch:
Canonical authority:
Replacement:
Deleted paths:
Targeted verification:
OpenAPI/typecheck/build:
Desktop/runtime verification:
Remaining compatibility:
Rollback evidence:
Next batch:
~~~
