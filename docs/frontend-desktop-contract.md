# 前端桌面合同

> Status: active
> Last verified: 2026-08-13
> Scope: Tauri 2、Windows 本地运行、认证、缓存、离线和本地诊断。

本文定义桌面壳的安全和运行边界。后端仍是唯一事实源和执行权威；本文不授权
Tauri command、renderer 或本地缓存执行交易、风控、治理或配置写入。

## 1. 目录与职责

目标目录：

~~~text
web_frontend/
├── src/                       React renderer
│   └── desktop/updater.ts     renderer 侧可选更新 seam
├── src-tauri/
│   ├── src/
│   │   ├── lib.rs             Tauri builder 和插件注册
│   │   ├── commands.rs        受控 OS command：窗口/诊断/设置
│   │   ├── secure_store.rs    Windows Credential Manager 适配
│   │   └── diagnostics.rs     本地环境诊断
│   ├── capabilities/
│   │   └── default.json       最小权限 capability
│   ├── icons/
│   └── tauri.conf.json
├── scripts/
└── docs/                      仅在确有桌面专项说明时使用
~~~

Tauri command 只允许以下类别：

- 窗口、菜单、快捷键和桌面生命周期；
- Windows WebView2、版本和安装环境诊断；
- Credential Manager 的凭证读写；
- updater 下载、签名验证、安装和失败回退；
- 本地 UI preference 和缓存清理。

个人自用模式当前只启用窗口/诊断、凭证保护、UI preference 和研究缓存边界；公开
updater 能力即使保留，也不作为当前运行 authority 或验收条件。

禁止新增：

- 本地 broker client、交易线程、交易数据库；
- 本地 RiskPolicy、Safety、Readiness、Factor 或 Governance writer；
- 本地账户/持仓/风险 authority；
- 把 renderer 的任意输入直接传给 broker 或数据库；
- 以 Tauri command 绕过 HTTP API 的权限、step-up、审计或 mutation gate。

## 2. Windows 本地运行要求

- 目标平台：本人使用的 Windows 10/11 x64；不要求面向其他用户的安装器或发布 CI；
- 运行依赖：Microsoft Edge WebView2 Runtime；
- 本机缺少 WebView2 时必须给出明确提示；安装 WebView2 的自动分发不在本批范围；
- renderer 只使用 Tauri 暴露的受控 origin，不开放任意 remote URL；
- CSP、capability 和 allowlist 采用最小权限，默认拒绝未声明 command；
- 本人使用可以采用本地 Vite/Tauri dev 或未公开分发的本地构建；不要求签名 bundle；
- 本机验收仍应覆盖高 DPI、窗口缩放、最小化恢复、多显示器和断网恢复。

## 3. API、认证和 WebSocket

### 3.1 API 基址

API base 通过构建配置或启动设置注入：

~~~text
开发：VITE_API_BASE_URL 或本地 backend
生产：受控的 https://www.zhuzhu666.icu 或项目发布配置
~~~

禁止把生产地址硬编码在业务组件中。每次请求都使用统一 client，统一处理
timeout、401、error envelope 和 request correlation。

### 3.2 时间与新鲜度显示

- `fact.v1.state`、`observed_at` 和 `generated_at` 以服务端返回为准，renderer 不得用本地时钟改写 `known/stale`；
- renderer 使用响应中的 `server_time` 或 `_fact.generated_at` 锚定服务器时钟，避免 Windows 本地时钟偏差造成年龄跳变；
- 所有桌面时间统一显示为 `YYYY-MM-DD HH:mm:ss`（Asia/Shanghai）；事实列表、徽标和顶部状态同时显示绝对观测时刻与相对年龄，统一使用“刚刚 / 秒前 / 分秒前 / 小时前”；
- `observed_at` 表示业务事实时间，`generated_at` 只表示本次响应生成时间，不能互相替代；
- WebSocket 连接状态、完整快照时间和业务事实年龄必须分开显示，不得把“已连接”解释为“数据已确认”。
- 实时桌面展示允许保留 30 秒内的最后一次真实 broker 快照；新增风险仍只读取服务端投影的
  `live.safety-freshness.v1` 20 秒安全门，renderer 不得把展示 `known` 转换成风险授权。

### 3.3 CORS 与 origin

后端必须显式允许实际桌面 origin 和配置的 API 公网 origin；生产浏览器页面不属于
当前客户端范围，不再为公网静态站点保留 CORS 合同；
禁止为迁就桌面端使用通配符 CORS。Tauri WebView2 实际 origin 以运行时验收
记录为准，通常会落在 `http://tauri.localhost` 受控 origin。当前后端通过
`QUANT_FRONTEND_CORS_ORIGINS`（逗号分隔）覆盖 origin，未配置时只允许
`https://www.zhuzhu666.icu`（API origin）、`http://tauri.localhost` 和本地 Vite 两个 origin；
OpenAPI/集成测试必须验证带凭证请求、refresh、logout 和预检行为。

### 3.4 Token 生命周期

- access token 只保存在 renderer 内存，不进入 localStorage、IndexedDB、日志、
  URL、研究 snapshot 或 Tauri command 参数持久化路径；
- refresh session 由现有 auth contract 管理；
- 桌面重新启动时从 Windows Credential Manager 读取 refresh 所需的安全材料，
  重新换取短 access token；
- step-up 只在需要时通过现有服务端 endpoint 完成，不在桌面本地验证密码；
- logout/401 family revoke 后清理内存 token、关闭 WS、取消 query，并按现有
  auth contract 进入登录页；
- 非 401 网络错误不得主动清空仍有效的会话。

凭证存储条目只允许保存服务端定义的 refresh/session 材料和必要的 server
profile；不保存账户、持仓、风险、mutation 或研究 payload。

### 3.5 WebSocket

桌面端与小程序客户端共用 `/ws/state` 合同：

1. 使用 access token 获取一次性、短时 WS ticket；
2. 只建立一个 live state 连接；
3. 每条消息是完整 live.state.v2 快照；
4. 断线或认证失败才清空实时业务值并按有界 backoff 重连；
5. 不使用 HTTP live endpoint 轮询或旧快照合并作为 fallback；
6. 恢复后以第一条完整快照重新显示；
7. 页面切换、面板切换和窗口 resize 不重建连接。

桌面离线不是 WS 的替代来源；它只改变研究缓存的可见性。

## 4. IndexedDB 缓存合同

### 4.1 Allowlist

| store | 允许内容 | 禁止内容 |
|---|---|---|
| market_snapshots | bars、行情查询条件、图表视口数据 | account、positions、spot auth state |
| replay_snapshots | 完成的 replay report、bar decision、artifact 引用 | mutation、live permission |
| factor_snapshots | factor catalog、factor card、证据引用 | runtime weight authority |
| research_snapshots | learning/review/application/governance 只读材料 | access/refresh token、控制状态 |

研究材料中如果携带账户、持仓、风险或认证字段，写入前必须按 schema 拒绝，
不能靠“当前页面不会展示”作为安全理由。

### 4.2 CacheEntry

每个 store 使用统一结构：

~~~text
{
  cache_key,
  contract,
  schema_version,
  payload,
  source,
  observed_at,
  generated_at,
  expires_at,
  content_hash
}
~~~

写入前校验 contract、schema_version、payload allowlist 和 content_hash；
读取后保留原 observed_at，不以读取时间续鲜。schema 不兼容时删除该条目并
显示 cache_invalidated，不尝试旧字段 fallback。

### 4.3 缓存生命周期

- 只在明确的 read query 成功且事实可被缓存时写入；
- error、unknown、未完成 mutation 和 request-time plan 不写入；
- stale 允许保留用于研究浏览，但必须显示 cache/stale、source 和时间；
- 用户登出时清理与 session 绑定的非公开研究缓存；公共研究材料按版本策略
  保留；
- 设置中提供“清理研究缓存”，不提供清理服务端事实的假动作；
- 缓存容量、过期和 schema migration 失败都只能影响研究查看，不影响服务端
  风险缩减入口。

## 5. 离线模式

离线定义为：无法建立可信 API/WS 连接，或所有重试均处于网络失败状态。

离线允许：

- 查看带 cache/stale 标记的行情和研究 snapshot；
- 改变图表范围、筛选和布局；
- 复制 server-issued evidence/reference ID；
- 查看本地诊断和缓存时间。

离线禁止：

- 开仓、加仓、解锁、治理放宽、runtime/config mutation；
- 启动 live loop、恢复自动化、release apply；
- 任何依赖账户、持仓、risk、Safety、readiness 或 auth result 的动作；
- 把缓存的旧 risk/readiness/control 值画成当前可执行状态。

离线回到在线后：

1. 重新认证或刷新 session；
2. 重新建立唯一 WS；
3. 重新读取当前工作区 HTTP facts；
4. 校验 cache contract/schema/content hash；
5. 只有服务端返回 known/current 和允许结果后才恢复动作。

## 6. 本地更新边界

个人自用模式默认不启用公开自动更新，不发布 Windows 安装包，不配置 GitHub
Releases、签名私钥或 updater manifest。因此以下内容不是本批验收门：公开签名、
升级成功、升级失败回退、安装/卸载和 Windows runner。

如果未来需要公开分发，再单独建立发布批次，沿用签名和回退合同；不得把当前本地
构建误当成已发布或已验证的安装包。

### 6.1 可选的本地更新流程

~~~text
检查 manifest
  -> 下载到临时位置
  -> 校验签名/hash
  -> 保留当前可启动版本
  -> 安装新版本
  -> 启动 smoke/health check
  -> 成功确认或回退当前版本
~~~

上述流程仅作为未来公开 updater 的设计参考；个人本地模式用重新构建已知 commit
回滚，不影响服务端交易、风险、治理或数据库事实。

## 7. 桌面配置与诊断

本地配置分为：

| 类型 | 存储 | 是否可进缓存 |
|---|---|---|
| server profile/API base | 受控本地设置 | 否，除非是非敏感配置 |
| refresh/session 材料 | Windows Credential Manager | 否 |
| UI layout/theme | 本地 UI preference | 否 |
| research snapshot | IndexedDB allowlist | 是 |
| account/position/risk/control | 服务端 | 否 |

诊断页可以展示版本、WebView2、API base、WS 状态、最近错误和缓存 schema，
但不得打印 token、Cookie、Authorization header、账户敏感值或完整研究 payload。

## 8. 桌面合同完成条件

桌面实现必须通过 frontend-refactor-acceptance-matrix.md 中的：

- 本地 Tauri build/启动、WebView2 存在性和窗口运行检查；
- Credential Manager 和 token 非持久化扫描；
- CORS、refresh、logout、401 family revoke 和 WS ticket；
- 单一 WS、首次完整快照、断线/重连/认证失败；
- IndexedDB allowlist、schema、stale 和离线动作禁用；
- Tauri command capability 最小权限和无本地交易 authority 扫描。

公开 Windows 安装、签名、GitHub Releases、自动更新和更新回退不属于个人自用
完成条件。
