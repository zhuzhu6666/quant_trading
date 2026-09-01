# 前端重构验收矩阵

> Status: active acceptance matrix
> Snapshot: 2026-08-29 (local renderer/Fact/recovery repair; server API/WSS-only, Windows本地; runtime未重验)
> Scope: 前端 renderer、个人本地 Tauri 桌面、接口合同和迁移删除门。

本文只记录可重复的前端验收门和证据要求。生产客户端是本地 Tauri 桌面端与小程序；
服务器只提供 API/WSS，不部署公网浏览器静态站点。产品边界见
frontend-operator-contract.md，桌面边界见 frontend-desktop-contract.md，
后端事实见 system-source-of-truth.md 和 api-fact-contract.md，实际进度见
frontend-refactor-status.md。

本矩阵采用“个人自用”验收配置：本机 renderer、Tauri 启动、认证、WS、Fact、缓存、
权限和危险动作仍是必验；Windows 对外发行、安装包签名、GitHub Releases、公开
updater、安装/卸载和 Windows runner 标记为 N/A，不阻塞本批完成。

本轮本人已确认正常认证和基本使用路径通过；这只更新正常使用路径，不等价于并发 401、
refresh/logout、WS 认证失效、缓存恢复或 mutation 回读全部通过。当前剩余重点是工作区
排版和数据流，具体验收仍按下表逐项收口。

## 1. 验收规则

每一批必须记录：

| 维度 | 必须提供 |
|---|---|
| Problem fact | 当前代码、接口、日志或失败测试证明的问题 |
| Canonical authority | 该事实的唯一 producer/writer 和前端消费端点 |
| Replacement | 新实现替代的页面、类型、fallback、route 或 wrapper |
| Deletion | 本批删除的旧路径、旧 import、旧测试和旧文档引用 |
| Targeted tests | 直接覆盖本批 seam 的测试 |
| Contract | OpenAPI、Fact schema、mutation result 或桌面合同证据 |
| Build | TypeScript、production build、Tauri bundle（适用时） |
| Runtime | 浏览器/Tauri 的 API、WS、认证和断网行为 |
| Unknown semantics | unknown/stale/error 未被零值或绿色替代 |
| Rollback | commit、artifact、installer/update 回退证据 |

以下任一存在，批次不能标记 complete：

- 同一事实被前端重新计算或被缓存授权；
- 新 route 已通过但旧 route、redirect alias 或旧 page 仍可进入生产；
- 前端使用 recursive fallback 发现未声明字段；
- mutation 只有按钮成功，没有 durable ID、audit ID 或提交状态；
- 只有单元测试，没有必要的桌面/运行态证据；
- unknown/stale/error 被转成 known、safe、零账户、零风险或空仓。

## 2. 自动化与测试服务边界

自动化测试不得执行真实交易、真实治理变更或真实发布。统一使用：

- 固定 JSON/TypeScript fixture；
- 本地测试服务或 mock HTTP server；
- 固定的完整 WS snapshot、断线、认证失败事件；
- 模拟 mutation response，包含 durable ID、audit ID 和 committed/pending/
  rejected/unknown 状态；
- 独立的 temporary IndexedDB；
- 本人本机 Tauri 验收环境；Windows runner 只在未来公开发行时才需要。

任何测试若需要真实 broker、生产 PostgreSQL、真实 token 或线上 GitHub
Release，必须改为隔离测试服务或移出本矩阵，不能把真实副作用带入 CI。

## 3. 基础 renderer 门

| ID | 验收门 | 证据/命令 | 阻断条件 | 状态 |
|---|---|---|---|---|
| FE-001 | TypeScript 检查 | web_frontend npm run typecheck | 类型错误、关键接口仍使用未声明宽泛 payload | 通过（本地） |
| FE-002 | 生产构建 | web_frontend npm run build | build 失败、旧 route/import 被打入产物 | 通过（本地） |
| FE-003 | 前端测试 | web_frontend npm test 及新增 contract/behavior tests | Fact、auth、WS、动作或删除扫描失败 | 通过（本地） |
| FE-004 | 生产依赖扫描 | package lock、bundle 和 source graph | 引入本地交易引擎、第二 server-state authority 或未批准远程脚本 | 部分通过（静态） |
| FE-005 | 路由扫描 | route manifest、静态构建和应用导航 | 旧 route alias、默认 redirect 或 section fallback 仍存在 | 部分通过（源码） |

## 4. API、Fact 和数据层门

| ID | 验收门 | 证据/命令 | 阻断条件 | 状态 |
|---|---|---|---|---|
| FE-101 | OpenAPI snapshot | 后端现有 OpenAPI snapshot job + diff | 新使用端点无稳定 response_model 或 snapshot 未同步 | 通过（Linux remote 生成/check；本地同步；定向批次 206 passed） |
| FE-102 | endpoint decoder | 关键端点 schema/decoder 测试 | 用 Record 或 recursive lookup 读取关键业务事实 | 部分通过（本地 decoder + 认证 bars/live/risk/readiness smoke） |
| FE-103 | Fact 状态 | known/stale/unknown/error/authoritative-empty fixture | 状态被顶层 ok、默认值或请求时间伪装 | 部分通过（本地 fixture + 远程 stale/unknown smoke） |
| FE-104 | zero-value 防护 | account、positions、risk、PnL unknown fixture | unknown 变成零账户、零风险、空仓或绿色 | 部分通过（源码/测试） |
| FE-105 | mutation result | allow、rejected、pending、committed、error fixture | 没有 durable ID、audit ID、reason_code 或 commit 状态 | 部分通过（decoder/静态） |
| FE-106 | 不新增 dashboard | endpoint graph 和 backend route review | 新增 /api/dashboard 或前端专用第二事实聚合 | 通过（源码） |

关键接口至少覆盖：

~~~text
/ws/state
/api/market/bars
/api/live/status
/api/live/account
/api/live/positions
/api/live/session-stats
/api/live/realized-pnl-series
/api/risk/summary
/api/risk/policy/verdicts
/api/risk/trade-trace/recent
/api/v4/catalog
/api/learning/*
/api/ops/replay/*
/api/ops/incident-control
/api/ops/release/*
/api/ops/brain/*
~~~

接口合同真实变化时，必须同时更新 api-fact-contract.md、
system-source-of-truth.md、legacy-debt-register.md、OpenAPI snapshot 和
对应后端/前端测试；本批仅创建文档时不修改 api-fact-contract.md。

## 5. WebSocket 与认证门

| ID | 验收门 | 证据/命令 | 阻断条件 | 状态 |
|---|---|---|---|---|
| FE-201 | 首次快照 | 固定完整 live.state.v2 fixture | 页面先渲染猜测值或不完整快照 | 部分通过（fixture） |
| FE-202 | 单连接 | route/panel/window resize instrumentation | 页面切换或面板切换创建第二条 /ws/state | 部分通过（源码） |
| FE-203 | 断线重连 | close、network error、bounded backoff fixture | HTTP polling、旧快照合并或无限重试 | 部分通过（fixture） |
| FE-204 | 认证失败 | 401/WS auth failure fixture | 不清理 live state、继续使用旧 session 或多次 refresh | 部分通过（fixture；正常认证已通过，WS auth-close/session invalidation 未单独验收） |
| FE-205 | refresh/logout | 并发 401、logout、family revoke fixture | token 落入 localStorage、IndexedDB、URL、日志或缓存 | 部分通过（静态；正常登录/使用已通过，并发 401、refresh/logout/family revoke 未单独完成） |
| FE-206 | step-up | 最近 step-up 过期/通过/拒绝 fixture | 前端自行判断密码、绕过服务端权限或放宽动作 | 部分通过（远程定向认证/gate fixture；真实 mutation 未调用） |

## 6. 五个工作区和交互门

| ID | 验收门 | 证据/命令 | 阻断条件 | 状态 |
|---|---|---|---|---|
| FE-301 | Trade Ops | live fixture、bars fixture、action mock | 账户/持仓/risk 由前端重算或 action 无服务端复核 | 部分通过（源码 + 远程合同批次 206 passed；action mock 未完整运行） |
| FE-302 | Risk Desk | risk summary、verdict、trace fixture | VaR/CVaR/stress/concentration/volume 在前端计算 | 部分通过（源码 + 远程 Safety/Risk 批次 162 passed） |
| FE-303 | Research Lab | bars、replay、factor、learning fixture | 研究结论直接变成 runtime、risk 或执行授权 | 部分通过（源码） |
| FE-304 | Governance | candidate/review/mutation fixture | 前端直接 approve/apply 或绕过 Coordinator/V16/Policy | 部分通过（源码 + 远程 Governance 批次 162 passed；真实 mutation 未调用） |
| FE-305 | Ops | readiness、health、incident、recovery fixture | request-time 聚合被画成当前 known 或收紧被阻断 | 部分通过（源码） |
| FE-306 | 工作区链路 | reference ID navigation test | Research → Decision Trace → Governance → Risk Desk → Trade Ops 丢失引用或不重读权威端点 | 未验证 |
| FE-307 | 权限投影 | role/scope fixture | UI 权限与服务端返回不一致，或客户端自己授权 | 未验证 |

## 7. Layout、视觉和交互门

| ID | 验收门 | 证据/命令 | 阻断条件 | 状态 |
|---|---|---|---|---|
| FE-401 | 全局 Safety rail | route switch、scroll、fullscreen test | rail 被隐藏、状态来源不明或重复计算 blocker | 部分通过（源码） |
| FE-402 | Command Palette | command registry fixture | 隐藏未知状态、列出无权限动作或无 reason_code | 部分通过（源码） |
| FE-403 | dock/split/pin | layout save/restore test | 布局保存 token、权限、风险事实或服务端状态 | 部分通过（源码） |
| FE-404 | layout migration | layout_version old/new fixture | 旧面板 ID 使应用崩溃或静默改变事实 | 部分通过（decoder） |
| FE-405 | visual tokens | token snapshot/accessibility test | 状态只靠颜色、危险动作与普通动作无法区分 | 部分通过（静态） |
| FE-406 | keyboard/accessibility | keyboard-only、focus trap、high contrast | 无法用键盘完成导航/确认或焦点逃逸 | 未验证 |

## 8. 缓存与离线门

| ID | 验收门 | 证据/命令 | 阻断条件 | 状态 |
|---|---|---|---|---|
| FE-501 | allowlist store | IndexedDB schema test | account、position、risk、control、auth 或 token 被写入 | 部分通过（静态） |
| FE-502 | CacheEntry | schema/content hash fixture | 缺 contract、schema_version、observed_at 或读取续鲜 | 部分通过（静态） |
| FE-503 | stale display | expired research fixture | stale 被显示为当前 known 或绿色 | 部分通过（源码） |
| FE-504 | offline read-only | network disabled desktop test | 离线开仓、解锁、治理放宽、启动或 release apply | 部分通过（源码） |
| FE-505 | online recovery | reconnect/re-auth/WS/HTTP revalidation test | 缓存直接升级为 known 或恢复动作不经服务器复核 | 未验证 |
| FE-506 | cache invalidation | schema mismatch、logout、manual clear | 旧字段 fallback、清缓存误删服务端事实或凭证泄露 | 部分通过（静态） |

## 9. Tauri 与 Windows 本地运行门

| ID | 验收门 | 证据/命令 | 阻断条件 | 状态 |
|---|---|---|---|---|
| FE-601 | Tauri 本地 build | 本机 `tauri build` 或 `tauri dev` | capability、CSP、资源或本地启动错误 | 通过（本机 executable/NSIS 可构建；不要求签名） |
| FE-602 | WebView2 | 本机存在性和首次启动检查 | 缺失时静默失败或无法明确提示 | 通过（本机存在路径 + 首次启动；缺失恢复不在个人自用范围） |
| FE-603 | installer | 不适用：个人自用不发布安装器 | 对外安装残留或安装权限问题 | N/A（个人自用不作为完成门） |
| FE-604 | credentials | Credential Manager integration/scan | token 写入 localStorage、IndexedDB、日志或普通文件 | 部分通过（代码/静态） |
| FE-605 | capability | src-tauri command/capability review | 本地 broker、数据库、交易线程或任意 remote URL | 通过（静态） |
| FE-606 | update signature | 不适用：个人自用不配置公开 updater | 私钥、manifest 或签名不匹配 | N/A（公开分发不在范围） |
| FE-607 | update success | 不适用：个人自用通过本地重建更新 | 更新后 renderer、API、WS 或缓存 schema 不可用 | N/A（公开 updater 不在范围） |
| FE-608 | update rollback | 不适用：个人自用回到已知 commit 重建 | 覆盖唯一可用本地构建 | N/A（公开 updater 不在范围） |
| FE-609 | desktop runtime | high DPI、多显示器、最小化恢复、断网 | 关键状态/危险按钮因缩放或恢复错误产生误导 | 部分通过（源码静态 recovery policy 已覆盖 focus/reconnect；发行 executable 首次启动/可访问性树通过，其余场景未验证） |

## 10. 旧路径删除和直接切换门

| ID | 验收门 | 证据/命令 | 阻断条件 | 状态 |
|---|---|---|---|---|
| FE-701 | old route gone | route manifest、static server 404/410、SPA route test | /overview、/trading、/pnl、/risk、/learning、/models、/v15、/v16 或旧 section 仍可进入 | 部分通过（源码/公网无 redirect；客户端废弃态未验） |
| FE-702 | old page gone | import graph、文件扫描 | OverviewPage、TradingPage、PnlPage、RiskPage、OpsPage 等旧 page 仍被构建引用 | 通过（源码/测试） |
| FE-703 | old fallback gone | compat/fallback scan | src/lib/compat.ts、recursive pick 或旧字段 fallback 仍服务新工作区 | 部分通过（源码） |
| FE-704 | old WS fallback gone | source scan + behavior test | HTTP live fallback、页面级 WS 或旧快照 merge 存在 | 部分通过（源码/fixture） |
| FE-705 | old docs synced | README、legacy debt、status、acceptance diff | 文档声称完成但代码/包/运行证据缺失 | 通过（本地 renderer/domain/recovery 修复与文档已同步；生产 runtime 仍明确标注为历史证据，未将未验证桌面场景写成通过） |
| FE-706 | no browser production | Caddy、服务器工作树、artifact、release config review | 服务器仍托管浏览器静态入口或保留前端工作树 | 通过（根路径/旧 asset 404；sparse 工作树无前端；Caddy 仅 API/WSS） |

## 11. 个人自用收口汇总

个人本机完成前必须收集一份可追踪的 evidence bundle：

~~~text
commit
renderer artifact hash
OpenAPI snapshot hash
typecheck/build/test output
WS/auth fixture output
cache/offline output
Tauri local build/start output
old-route/import scan
known-commit local rebuild/rollback note
~~~

只有 FE-001 至 FE-706 的适用项全部通过，且前端重构计划中的个人自用完成条件全部满足，
才可以把 frontend-refactor-status.md 的阶段改为 complete。N/A 的公开 Windows 分发项
不计入完成门。本批已登记本地代码/构建证据和明确的未验证/阻塞项；静态通过不替代
Linux、浏览器和本人 Tauri 本机运行证据。
