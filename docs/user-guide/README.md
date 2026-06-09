# Quant Web Console — User Guide

> XAUUSD+ 黄金 M15 量化交易框架 — 浏览器 Web 总控台,完整替代终端 CLI。

**最后更新**: 2026-06-08 (v8.1: 总览显示实盘账户 + 实盘开关 + 紧急平仓按钮合并到 /)
**状态**: Phase 1-5 完成(43 REST 端点 + 1 WS = 44 总 + 16 页面 + JWT auth 真正强制)

---

## 快速开始

### 首次安装

```bash
# 1. Python 依赖
"C:\Users\zhu\AppData\Local\Programs\Python\Python312\python.exe" -m pip install -r requirements.txt

# 2. 前端依赖(首次需要)
cd frontend
npm install
cd ..

# 3. Node 18+ (Windows: 从 nodejs.org 下载 LTS)
node --version
```

### 启动

**Windows** (主用):
```cmd
start.bat
```

浏览器自动开 `http://localhost:3000`(或者手动访问)。

**Unix**:
```bash
./start.sh
```

### 停止

```cmd
stop.bat
```

或手动 `Ctrl+C` 终止前台进程,后端窗口继续运行。

---

## 页面速查

| 路径 | 用途 |
|---|---|
| `/` | 总览 — 实时 equity / 持仓 / PnL / 风控状态 / 一键回测 |
| `/market` | K线 — TradingView LWC 渲染,5 TF 切换 |
| `/paper` | 模拟盘 — 启动/停止/紧急停止,实时 equity 曲线 |
| `/backtest` | 回测 — 12 combo sweep + jobs 状态 + 报告原文 (v5 新增, ⚠ 当前 backend 跑 stub) |
| `/live` | 实盘 — MT5/cTrader 状态 + 紧急平仓(⚠ MT5 当前阻塞) |
| `/factors` | 因子健康 — 65 因子表 + 5 维评分,点击进详情 |
| `/factors/[name]` | 单因子详情 — 5 维雷达 |
| `/discover` | L2 因子发现 — GP/Random search,实时进度 |
| `/sync` | T16 实时数据同步(⚠ 当前阻塞,MT5 包不兼容) |
| `/tuning` | 风险参数调参 — risk_pct × cb_pct 网格 |
| `/calibrator` | 概率校准器 — 查看/保存 buckets |
| `/shadow` | 影子因子 — promote/demote |
| `/ab` | A/B 测试 — 两路径对比 |
| `/reports` | 报告浏览器 — data/charts/ 所有报告 |
| `/config` | 配置编辑 — settings.yaml 在线修改 |
| `/jobs` | 任务中心 — 所有 long-task 状态 |
| `/login` | 登录(v1 stub,任意密码即可) |

---

## 关键操作指南

### 启动/停止 paper 模拟盘

1. 访问 `/paper`
2. 展开"配置"折叠面板,选择 8 个 enable_* 开关
3. 设置 `risk_per_trade_pct`(推荐 1.0)
4. 点击 **▶ 启动**
5. 实时 equity 曲线 + 当前持仓 + 每日统计会自动更新(1s tick from `/ws/state`)
6. 停止:**⏹ 停止** 软停止 / **⏮ 紧急停止** 需确认(后端 `X-Confirm: emergency` header 二次校验)

### 跑一次回测

1. 访问 `/`(总览)
2. 点击 **▶ 跑一次回测** 按钮
3. 提交后到 `/jobs` 查看进度(PnL 数字)
4. 报告写到 `data/charts/backtest_*.txt`,在 `/reports` 浏览器查看

### 因子健康评估

1. 访问 `/factors`
2. 点击 **▶ 重新评估** — 提交 5000 bar 评估任务
3. 后台跑 5-30s(具体看 bar_count)
4. 完成后表格自动显示 65 因子 + 状态色码(绿/黄/红)
5. 点击任一因子名 → 详情页 5 维雷达

### 紧急平仓(实盘)

1. 访问 `/live`
2. 选择 broker (mt5 / ctrader) + symbol(留空=所有)
3. 点击 **⏮ 紧急平仓**,二次确认
4. 后端 `X-Confirm: emergency` 校验后调 broker API 平仓

⚠ MT5 当前阻塞(balance=0 + Python 包 IPC 不兼容),操作会立即报错。cTrader token 已就位但 emergency close 暂未实现(Phase 4)。

---

## 实时数据流

Web console 通过 WebSocket `/ws/state` 推送 1s 一次的 state snapshot:

```json
{
  "equity": 1593.85,
  "balance": 1500.00,
  "pnl_today": 93.85,
  "position": { "dir": "LONG", "entry": 4529.12, "size": 0.01, "unrealized": 12.5 },
  "daily": { "trades": 5, "win": 3, "loss": 2, "pnl": 23.5, "drawdown_pct": 1.2 },
  "risk": { "circuit_breaker": false, "consecutive_loss": 0 },
  "server_time": "2026-06-07T15:32:01Z"
}
```

前端 Zustand store 自动更新,所有订阅组件重渲染。

---

## 已知阻塞 / 限制

| 项 | 状态 | 原因 | 临时方案 |
|---|---|---|---|
| MT5 实时同步 | ⏸ 阻塞 | Python MT5 包 vs terminal 2026 IPC pipe hash 不匹配 | `python scripts/live_sync.py --mode once --type incremental` 手动 |
| MT5 实盘交易 | ⏸ 阻塞 | balance=0 + blocked-2 同上 | 充值账户 + 修包 |
| 因子评估真实运行 | ⚠ 慢 | 50K bar × 65 因子需 5-30s | 调小 bar_count 参数 |
| 完整 LWC IC 时序 | 📋 Phase 4 | 需 alpha/factor_health 历史报告 | 当前详情页只显示数字 |
| 多用户/认证 | 📋 v2 | v1: 单用户 hardcoded "zhu" | 任意密码登录 |
| E2E 实跑 | 📋 环境 | Playwright chromium binary 下不下来 | 测试文件已就位,需网络可达 playwright.azureedge.net |
| **50K bar K线 8.16x 加速** | ✅ v5-fix-3 | `df.iterrows()` → vectorized numpy | 实测 2462ms → 302ms |
| **ab / tuning 报告字段错位** | ✅ v5-fix-6 | 后端 `report_path` 走 `/api/reports/<name>` 读 | ab/tuning 页面可显示报告原文 |
| **paper render setState 违反 React 规则** | ✅ v5-fix-4 | 移到 useEffect | 不再 "Cannot update while rendering" 警告 |
| **market 切 tf race condition** | ✅ v5-fix-7 | AbortController 取消旧 fetch | 切 tf 不再出 M5 数据画在 M15 |
| **sidebar /backtest 死链** | ✅ v5-fix-1 | 新建 `(terminal)/backtest/page.tsx` | sidebar "回测" 链接 200 OK |
| **paper emergency 文案撒谎** | ✅ v5-fix-5 | 改成 "X-Confirm: emergency header 二次校验" | 文案跟实现对齐 |
| **backtest_runner 12 combo stub** | 🛡️ v5-guard-1 | backtest page 顶部警示 + TODO 拆解 | 真实 PnL 仍走 `python main.py --mode backtest` |

---

## 文档

- Spec: `docs/superpowers/specs/2026-06-07-quant-web-console-design.md`
- Plan: `docs/superpowers/plans/2026-06-07-quant-web-console.md`
- 项目主索引: `PROJECT_MAP.md`
- Phase 1-5 审计: `PROJECT_AUDIT_v4.md`

---

## 故障排查

### start.bat 启动后浏览器 404

- 等待 30s(Next.js dev 模式首次编译慢)
- 查看 `frontend/.next/trace` 日志

### WebSocket 状态一直显示"⚠ 离线"

- 检查后端是否在 :8000 运行:`curl http://localhost:8000/api/health`
- 检查浏览器控制台:WS 应该连 `ws://localhost:8000/ws/state`
- 检查 NEXT_PUBLIC_WS_URL 环境变量(默认 `ws://localhost:8000`)

### paper 启动失败"already_running"

- 之前 paper 进程没退干净
- 跑 `stop.bat` 或手动 taskkill

### "因子评估"按钮提交后一直 0%

- 评估在跑(检查 `/jobs` 进度)
- 或后端 import 失败(看 backend.log)

### 浏览器看到 layout 但 WS 不更新数字

- 等 1-2s 让首次 snapshot 到达
- 检查 topbar 的 "● live" 状态

---

## 反馈

发现 bug 或想加新功能? 直接在 `ROADMAP.md` "Phase 4 Web UI" 节点追加(plan §7.5 约定)。

---

## 生产部署 (Production Deployment)

### 单端口模式 (推荐用于 VPS/容器)

```cmd
start-prod.bat
```

会:
1. `cd frontend && npm run build` 编译静态 HTML
2. 拷贝 `frontend/out/*` 到 `backend/static/`
3. `python -m backend --port 8000` 同时 serve API + 静态前端

浏览器访问 `http://localhost:8000`(或你的域名)。

### nginx 反向代理 (生产环境)

`docs/nginx.example.conf` 是完整的 nginx config 模板,包含:
- TLS 终止 (Let's Encrypt)
- `/api/*` 代理到 uvicorn + rate-limit
- `/api/auth/login` 单独 10 req/min 防爆破
- `/ws/*` WebSocket 代理 (1h timeout,no buffering)
- `/_next/static/*` 1 年 immutable 缓存
- HSTS / X-Frame-Options / Referrer-Policy 安全头

部署步骤:
1. 拷贝 `docs/nginx.example.conf` → `/etc/nginx/sites-available/quant.conf`
2. 替换 `quant.example.com` 为你的域名
3. `ln -s /etc/nginx/sites-available/quant.conf /etc/nginx/sites-enabled/quant.conf`
4. 申请 cert: `acme.sh --issue -d your.domain --nginx` 或 `certbot --nginx -d your.domain`
5. 修改 config 里的 `ssl_certificate` 路径
6. `nginx -t && systemctl reload nginx`
7. 后台跑 `start-prod.sh` (或 systemd unit)

### systemd unit 示例 (可选)

```ini
# /etc/systemd/system/quant-web.service
[Unit]
Description=Quant Web Console
After=network.target

[Service]
Type=simple
User=quant
WorkingDirectory=/opt/quant_trading
ExecStart=/usr/bin/python3.12 -m backend --port 8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`systemctl enable --now quant-web.service`

### 环境变量 (生产)

```env
# .env (do NOT commit)
JWT_SECRET=<long-random-string>     # currently hardcoded in backend/core/auth.py; override in v2
QUANT_DB_PATH=/opt/quant_trading/data/market_data.db
QUANT_LOG_LEVEL=WARNING
```

**v1 限制**: `JWT_SECRET` 硬编码在 `backend/core/auth.py` —— v2 会从 env 读取。当前 v1 部署请接受这个限制或在反向代理层做额外鉴权(例如 Basic Auth over nginx)。
