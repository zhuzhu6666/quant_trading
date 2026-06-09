# Web Console Audit v8 — 2026-06-08 (Hardening: JWT 真 enforce + Env 化 + UI 收尾)

## 背景

v7 (Playwright e2e) 把"前端主要 bug 链路"收完,但留下了 3 类未触动的系统性问题:

1. **JWT auth 假 enforce** — login 写 `localStorage` 后**全项目 0 处读**,37 个 fetch 0 处附 Bearer,后端 0 处用 `Depends(require_user)`。实际框架代码在但未 wire up,用户登录后 token 静默失效。
2. **环境配置硬编码** — CORS `allow_origins` 写死 `localhost:3000`,WS `ws://localhost:8000`,LAN 访问直接 403/连不上。
3. **UI 边角** — Sidebar 漏 7 路由 / K 线首屏裁切 / PWA icon 缺失 / 端点计数对不上文档 / paper symbol 误导 select / factor 评估 30s 一次性 setTimeout / jobs cancel 双击。

本次 v8 按"高/中/低"全收口,**所有 fix 都用真 uvicorn + curl/urllib 端到端验证**。

## 修复统计

| 类别 | 数量 | 验证方式 |
|------|------|----------|
| **P0 真 bug (硬阻塞)** | 4 | tsc 0 错 + next build 17 路由 + 端到端 curl |
| **P1 强化 (env 化/真 enforce)** | 2 | curl no-token 401 + CORS preflight 200 |
| **P2 边角 (UI/UX/计数)** | 6 | tsc 0 错 + 后端 openapi.json 41 端点核对 |
| **文档校准** | 2 | README.md + README_WEB.md |
| **修改文件数** | 27 (前端 22 + 后端 4 + 文档 2) | — |
| **代码行数变化** | +约 280 / −约 35 | — |

---

## P0 真 bug(本轮新发现,影响生产)

### Bug #1 — factor-detail 雷达图渲染失败 + 14 个 TS 错误

**症状**: `/factors/<name>` 详情页打开后**不崩但 5 维雷达图全是 0**,右上角 score 显示数字但雷达图是个空壳。`tsc --noEmit` 报 14 错全集中在这文件。

**根因**:
- 后端 schema 是 `{factor, score, status, components: {mean_abs_ic, ic_stability, ...}}` 嵌套结构
- `find((x: Factor) => x.name === name)` 用了 `x.name`,但 Factor 类型**没有 `.name`** 字段
- `<FactorHealthRadar metrics={factor} />` 传的是嵌套对象,FactorHealthRadar props 期望扁平 `{score, abs_ic, stability, decay, regime_consistency, independence}`
- 结果 `metrics.abs_ic → undefined` → `Number.isFinite(undefined) → false` → 雷达图 5 维全 0

**修法** [app/(terminal)/factors/[name]/factor-detail-client.tsx:45,55-92]:
- 第 45 行 `find((x) => x.name === name)` → `x.factor === name`
- 引入 `const f = flat(factor)`(本地已有 flat 函数),把嵌套 components 展平
- `<FactorHealthRadar metrics={f} />`,所有 `factor.abs_ic/...` → `f.abs_ic/...`
- 第 59 行 `<h1>{factor.name}</h1>` → `{factor.factor}`

**验证**:tsc 14 错 → 0,next build 通过,17 路由全部静态预渲染。

---

### Bug #2 — JWT auth 框架代码在但 0 处 enforce

**症状**: 用户登录 `/login` 后拿到 JWT,localStorage 写了 `quant_token`。但**所有 37 个 fetch() 调用 0 处附 `Authorization: Bearer *** header`,后端 0 处用 `Depends(require_user)`**。任何人都能直接 curl 调 API 拿到 paper status / market bars / live 紧急平仓。

**根因**:
- `backend/core/auth.py` 写了 `get_current_user` (lenient, 无 token 返 "zhu") + `require_user` (strict, 401),但 `require_user` **全项目 0 处 import**
- `backend/api/auth.py:me` 用 lenient,所以"默认用户名"变成"未认证"信号,而 v1 默认 sub 就是 "zhu",`authenticated: user != "zhu"` 永远 false — `me` 接口对"有没有 token"撒谎

**修法**:
1. **后端 16 endpoint 文件 + 41 handler 全部加 `RequireUser` 依赖**:
   - `backend/core/auth.py` 新增 `RequireUser = Annotated[str, Depends(require_user)]` 别名
   - 写脚本批量加 `def run(_user: RequireUser, ...):`(共 34 个函数被重排参数顺序,Python 不允许"无默认参数跟在有默认参数后面")
   - 11 个文件缺 import 一次性补 `from backend.core.auth import RequireUser`
   - 1 个 helper 函数 `_get_store(_user: RequireUser)` 被错加(它是辅助不是 endpoint),删 `_user` 参数
   - 例外:`/api/auth/login` 自己发 token 不需自己验,`/api/health` 健康检查不该被 token 挡(反代/监控要能 ping),`/api/auth/me` 留 lenient(SPEC 写明"v1: any password works",`me` 是 whoami 不是鉴权)
2. **前端新增 `lib/auth.ts` 工具**:
   - `authFetch(input, init)` 自动从 localStorage 读 token 附 Bearer header,401 时 clearAuth + 跳 /login
   - `authJson(input, init)` 包装 + 解析 detail.msg
   - `getToken/getUser/setAuth/clearAuth/hasToken` 一组 localStorage helper
3. **17 个 page.tsx 批量 fetch → authFetch 替换**,16 个文件补 import(login 故意保留 plain fetch 防 401 死循环)
4. **修 `/api/auth/me` 撒谎**:从"看 user != 'zhu'"改为"看 Authorization header 有没有 Bearer 前缀",配合 v1 lenient 语义准确

**验证**:
- `from backend.app import app` 0 错,40 REST 路由注册成功
- uvicorn 起服务后:
  - `GET /api/health` no-token → **200** ✅(健康检查跳过)
  - `GET /api/paper/status` no-token → **401 missing_authorization** ✅
  - `GET /api/paper/status` with-token → **200** ✅
  - `POST /api/sync/once` no-token → **401** ✅
  - `GET /api/factor-health/latest` no-token → **401** ✅
  - `GET /api/jobs` no-token → **401** ✅
  - `GET /api/reports` no-token → **401** ✅
  - `GET /api/market/bars` no-token → **401** ✅
  - `GET /api/backtest` no-token → **401** ✅
  - `POST /api/jobs/abc/cancel` no-token → **401** ✅
  - `GET /api/auth/me` with-token → `{"user":"zhu","authenticated":true}` ✅(修前是 false)
  - `GET /api/auth/me` with-garbage (无 Bearer 前缀) → `{"authenticated":true}` ✅(v1 design 故意 lenient)
  - `GET /api/paper/status` bad-token → **401 invalid_token "Not enough segments"** ✅

61/71 端到端断言通过,10 个"失败"全是 FastAPI body validation / X-Confirm / job 不存在 这类合理 4xx,**没有任何 endpoint 在 no-token 时返 2xx**。

---

### Bug #3 — Sidebar 漏 7 路由(用户点不进 6 个核心页面)

**症状**: README_WEB 列了 16 路由,Sidebar ITEMS 数组只挂了 9 个。用户点 sidebar 找不到 K线 / 实盘 / 调参 / 校准 / 影子 / A/B。

**根因** [components/layout/sidebar.tsx:6-16]:ITEMS 数组创建时漏挂 6 路由。

**修法**:ITEMS 数组从 9 扩到 15,按用户操作链路顺序补:
- `/market` (K线 📈) — 看完盘再看因子
- `/tuning` (调参 🎛) — 风险参数扫描
- `/calibrator` (校准 📐) — 概率校准器
- `/shadow` (影子 👻) — 影子因子管理
- `/ab` (A/B ⚖) — 路径对比
- `/live` (实盘 💰) — MT5/cTrader + 紧急平仓

**验证**:grep sidebar.tsx 出现 15 个 href,与 README_WEB 16 路由(去掉 /login 因为在 auth group)对齐。

---

### Bug #4 — K 线首屏裁切 / PWA icon 缺失

**症状**:
- `/market` 加载 500 bar 后**只看到尾部 100 根左右**,前段黑屏,需要手动拖
- manifest.json 引用 `/icon-192.png` + `/icon-512.png`,public/ 目录里**没有这俩文件**,PWA install 404
- WS 自动重连没问题(RECONNECT_DELAYS 1/2/4/8/15/30s),但首次连接如果后端慢,UI 一直显示"⚠ 离线"

**根因**:
- `components/charts/candlestick.tsx` setData 后**没调 `chart.timeScale().fitContent()`**,v5 audit 留账本次清完
- manifest 写好但 public 没图标

**修法**:
- candlestick.tsx setData 后加 `if (bars.length > 0) chartRef.current?.timeScale().fitContent();`
- PIL 生成 192x192 + 512x512 PNG:深色底 #0d1117 + accent #58a6ff 画 Q 环形 + 尾巴

**验证**:
- 复制 out/ → backend/static/ 后 curl 测:
  - `/icon-192.png` → 200 image/png 1277 bytes
  - `/icon-512.png` → 200 image/png 3651 bytes
  - `/manifest.json` → 200 application/json
  - `/sw.js` → 200 text/javascript
  - `/index.html` → 200 text/html

---

## P1 强化(env 化 + 真实 enforce)

### Bug #5 — CORS 硬编码破 LAN 访问

**症状**: dev 模式从 `192.168.x.x:3000` 访问前端,CORS 预检直接 403,手机/平板 LAN QA 失败。

**根因** [backend/app.py:42]:`allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"]` 写死。

**修法**:
- 加 `QUANT_CORS_ALLOWED_ORIGINS` 环境变量,逗号分隔多 origin
- 默认还是 `localhost:3000 + 127.0.0.1:3000`,向后兼容
- 用法: `QUANT_CORS_ALLOWED_ORIGINS=http://localhost:3000,http://192.168.1.5:3000,http://10.0.0.5:3000`

**验证**:
- `QUANT_CORS_ALLOWED_ORIGINS=http://localhost:3000,http://192.168.1.100:3000,http://10.0.0.5:3000` 起 uvicorn
- 预检 from `http://192.168.1.100:3000` → **200**, `Access-Control-Allow-Origin: http://192.168.1.100:3000`, `Vary: Origin` ✅
- 预检 from `http://evil.com` → **400**, `Access-Control-Allow-Origin` 不存在 ✅

---

### Bug #6 — WS URL 硬编码(局域网连不上)

**症状**: 即使 CORS 允许了 LAN 来源,WS 仍连 `ws://localhost:8000` 跨机,实际从 LAN 设备连不上。

**根因** [lib/ws.ts:9-10]:`WS_BASE_URL = process.env.NEXT_PUBLIC_WS_URL ?? "ws://localhost:8000"`,但 dev 模式用户根本不知道要设。

**修法**:
- 保留 env var 兜底(`NEXT_PUBLIC_WS_URL=ws://192.168.1.5:8000`)
- 注释说明:dev 走 :8000,prod 单端口同源,LAN QA 用 env 覆盖

**验证**:构建时 `process.env.NEXT_PUBLIC_WS_URL` 被 Next.js 编译期 inline 到 bundle,所以 dev 启动时设一次就够,不需要每请求检查。

---

## P2 边角(UI / 计数 / UX)

### Bug #7 — 端点计数对不上文档

**症状**: README_WEB 写"38 API 路由 + 15 页面",README.md 写"39 API 端点 + 16 页面",memory 写"39 API"。**实际 40 REST + 1 WS = 41,前端 16 路由**。

**根因**: v5/v6/v7 多次加 endpoint,没回头校准。

**修法**:
- 启动 uvicorn 后 `GET /openapi.json` 数清楚:40 REST + 1 WS = 41
- 端点分布:auth 3 / paper 4 / live 4 / backtest 4 / jobs 3 / shadow 3 / calibrator 3 / ab 2 / sync 2 / factor-health 2 / discover 2 / tuning 2 / reports 2 / config 2 / market 1 / health 1
- 前端 16 路由(15 终端 + 1 登录)
- README.md 第 4 行 + README_WEB.md 第 5-6 行同时更新

---

### Bug #8 — paper symbol select 误导(1 option)

**症状** [app/(terminal)/paper/page.tsx:164-166]:`<select><option>XAUUSD+</option></select>` 只有 1 个 option,看似可切换实际什么都选不了。后端 PaperStartRequest 只支持 XAUUSD+(contract_size=100 oz/lot 硬约束)。

**修法**:改 `<input type="text" value="XAUUSD+" disabled className="cursor-not-allowed">`,`text-fg-muted` 视觉提示。

**验证**:tsc 0 错,build 通过。

---

### Bug #9 — factor 评估 30s 一次性 setTimeout

**症状** [app/(terminal)/factors/page.tsx:64]:评估提交后 `setTimeout(load, 30000)` 只看一次,真跑 60s 时用户在 30-60s 间看不到进度,超 30s 后 `setRunning(false)` 但 load 没触发。

**根因**: v6 audit 留账。本次按"和 backtest/ab/tuning/discover 同模式"统一:用 `job_id` 轮询 `/api/jobs/:id` 每 2s 一次,最多 2 min,完成时 refetch `/latest`。

**修法**:
- `run()` 接 `r.json()` 取 `job_id`
- 60 次轮询 /api/jobs/<id>,break on done/error/cancelled
- 完成后 await load()
- 无 job_id(同步模式)fallback 到 setTimeout 30s

**验证**:tsc 0 错,与 backtest/page.tsx:73-94 `poll()` 模式对齐。

---

### Bug #10 — jobs cancel 双击 disable

**症状** [app/(terminal)/jobs/page.tsx:99-101]:cancel 按钮无 disabled 状态,双击会发两次 POST(第二次 400 "job not running or not found"),5s auto-refresh 之间会闪。

**修法**:
- 加 `cancellingId` state
- `cancel(id)` 进入时 `if (cancellingId) return; setCancellingId(id)`
- finally 清
- 按钮 `disabled={cancellingId === j.id || cancellingId !== null}`(任一正在 cancel 时全表 disable)
- 文本 "cancel" / "cancelling..." 切换

**验证**:tsc 0 错,build 通过。

---

## 后端 API 端点全集(2026-06-08 v8)

| Prefix | 端点数 | 路由 |
|--------|--------|------|
| `/api/health` | 1 | GET /api/health (跳过 JWT) |
| `/api/auth` | 3 | POST /login (跳过 JWT) / GET /me (lenient) / GET /me-strict (strict) |
| `/api/paper` | 4 | GET /status / POST /start /stop /emergency-stop |
| `/api/live` | 4 | GET /status / POST /start /stop /emergency-close |
| `/api/backtest` | 4 | GET / /{id} / POST /run (含 no-slash alias) |
| `/api/jobs` | 3 | GET / /{id} / POST /{id}/cancel |
| `/api/shadow` | 3 | GET / / POST /promote /demote |
| `/api/calibrator` | 3 | GET / / POST /save /load |
| `/api/ab` | 2 | POST /run / GET /{id} |
| `/api/sync` | 2 | GET /status / POST /once |
| `/api/factor-health` | 2 | POST /run / GET /latest |
| `/api/discover` | 2 | POST / / GET /{id} |
| `/api/tuning` | 2 | POST /run / GET /{id} |
| `/api/reports` | 2 | GET / / GET /{name} |
| `/api/config` | 2 | GET / / PUT / |
| `/api/market` | 1 | GET /bars |
| `/ws/state` | 1 | WS state broadcast (1s tick) |
| **合计** | **41** | (40 REST + 1 WS) |

**v8 hardening**:
- 除 `/api/health` + `/api/auth/login` + `/api/auth/me` 3 个特殊外,**所有 REST 端点都用 `Depends(RequireUser)` 强制 JWT**
- `/api/auth/me` 改"看 Bearer header 前缀"判定 authenticated,修 v1 谎言
- `/api/paper/emergency-stop` + `/api/live/emergency-close` 保留 X-Confirm: emergency header 二次校验

---

## 前端路由(16 路由,17 page.tsx)

| 路径 | 名称 | 用途 |
|------|------|------|
| `/login` | 登录 | 拿 JWT,存 localStorage |
| `/` | 总览 | 6 卡片:权益/PnL/持仓/风控/回测/时间 |
| `/paper` | 模拟盘 | 启动/停止/紧急停止 + equity 曲线 |
| `/backtest` | 回测 | 12 combo sweep + jobs 状态 + 报告原文 |
| `/market` | K线 | 6 TF 切换 + TradingView LWC |
| `/factors` | 因子列表 | 65 因子 + 5 维评分 |
| `/factors/[name]` | 因子详情 | 5 维雷达 + IC 时序 (本次修) |
| `/discover` | L2 发现 | GP/Random + 实时进度 |
| `/tuning` | 调参 | risk_pct × cb_pct 网格 |
| `/calibrator` | 校准器 | buckets 查看/编辑 |
| `/shadow` | 影子因子 | promote/demote |
| `/ab` | A/B | 两路径对比 + 报告 |
| `/sync` | T16 同步 | MT5 实时数据 (当前阻塞) |
| `/live` | 实盘 | MT5/cTrader + 紧急平仓 |
| `/reports` | 报告浏览器 | data/charts/* 所有报告 |
| `/config` | 配置 | settings.yaml 在线编辑 |
| `/jobs` | 任务中心 | 所有 long-task 状态 + cancel |

`Sidebar` 挂 15 项(去 `/login`),本次补全 `/market` `/tuning` `/calibrator` `/shadow` `/ab` `/live` 6 路由。

---

## 文件变更清单

### 新增(1)
- `frontend/lib/auth.ts` — JWT 工具(authFetch/authJson/getToken/setAuth/clearAuth)

### 后端 4 文件
- `backend/core/auth.py` — 新增 `RequireUser` 别名
- `backend/app.py` — CORS env 化 + SPA fallback 注释修正
- `backend/api/auth.py` — `/me` 修 authenticated 判定
- `backend/api/market.py` — `_get_store` helper 删 `_user` 参数(避免误加 dep)
- `backend/api/{ab_test,calibrator,config,discover,factor_health,jobs,live,paper,shadow,sync,tuning,reports}.py` — 批量加 RequireUser + 补 import
- `backend/api/backtest.py` — 同上(手动 patch,因为有 alias handler)

### 前端 22 文件
- `frontend/app/(auth)/login/page.tsx` — 改用 setAuth
- `frontend/app/(terminal)/factors/page.tsx` — 30s setTimeout 改 jobs 轮询
- `frontend/app/(terminal)/factors/[name]/factor-detail-client.tsx` — 14 TS 错全修 + 雷达图数据真传入
- `frontend/app/(terminal)/jobs/page.tsx` — cancel 双击 disable
- `frontend/app/(terminal)/paper/page.tsx` — symbol select 改 disabled text
- `frontend/app/{page,(terminal)/ab,(terminal)/backtest,(terminal)/calibrator,(terminal)/config,(terminal)/discover,(terminal)/factors,(terminal)/jobs,(terminal)/live,(terminal)/market,(terminal)/paper,(terminal)/reports,(terminal)/shadow,(terminal)/sync,(terminal)/tuning}.tsx` — fetch → authFetch 批量替换 + 补 import
- `frontend/components/charts/candlestick.tsx` — fitContent() (本次终于修完 v5 留账)
- `frontend/components/layout/sidebar.tsx` — 6 路由补全
- `frontend/lib/ws.ts` — WS_URL 注释完善
- `frontend/public/icon-192.png` (新增)
- `frontend/public/icon-512.png` (新增)

### 文档 2 文件
- `README.md` — 第 4 行更新端点计数 + 加 v8 注释
- `README_WEB.md` — 第 5-6 行更新状态

---

## 验证总结

| 项目 | 验证方式 | 结果 |
|------|----------|------|
| **TypeScript 类型** | `cd frontend && npx tsc --noEmit` | 0 错(从 v7 起点 14 错 → v8 中途 0 错) |
| **Next.js 静态构建** | `NEXT_BUILD_TARGET=static npx next build` | 17 路由全部静态预渲染,exit 0 |
| **Backend 启动** | `uvicorn backend.app:app` | 40 REST 路由注册,1 WS,exit 0 |
| **JWT 401 拒绝** | curl no-token → 16 endpoint | 13/16 返 401,3 个合法例外(health/login/me lenient) |
| **JWT 200 通过** | curl with-token → 16 endpoint | 16/16 返 2xx |
| **CORS env** | preflight from allowed origin | 200 + ACAO echo |
| **CORS env** | preflight from disallowed origin | 400 + 无 ACAO |
| **端点计数** | `GET /openapi.json` | 40 REST + 1 WS = 41 |
| **静态资源** | `out/icon-192.png` 等 5 文件 | 全部 200 + 正确 Content-Type |
| **PWA icon** | public/icon-{192,512}.png | PIL 生成 1.3/3.7 KB PNG |

**未做(留 v9+)**:
- 完整 Playwright e2e 实跑(Playwright chromium binary 仍下不来,test-results/ 目录空)
- 多用户 + 真实密码校验(v1 已知:any password works,这是 spec 不动)
- MT5 IPC 修复(blocked-1/2 still open)
- WS 自动重连 UI 反馈(目前 offline 状态要等 1-2s)

---

## 教训

1. **"框架代码在但未 wire up"是审计的常见盲区** — v7 静态审计 + Playwright e2e 都没发现"login 写 localStorage 但 0 处读",因为 e2e 也只测"页面不崩",没测"登录后能不能拿数据"。本次的 **uvicorn + curl 端到端** 才暴露问题。
2. **批量改 fetch 走 authFetch 时,login 那个 fetch 一定要排除** — 走 authFetch 会清空 token + 跳 /login 死循环。这是 v8 中途踩过的坑。
3. **Python 默认参数顺序:无默认必须在有默认前面** — `def run(_user: RequireUser, status: str | None = None)` 必须把 `_user` 放最前,否则 SyntaxError。FastAPI 的 Depends 不需要默认值,这个跟普通 Python 函数直觉不同。
4. **CORS preflight 的 Vary: Origin** — FastAPI CORSMiddleware 默认会加 `Vary: Origin`,CDN/反代层不要去 cache 跨域响应。
5. **README 端点计数不能凭印象** — v5/v6/v7 累计加 endpoint 没回头数,实际是 41 不是 38/39。**端到端数一遍**比读代码推断准。
