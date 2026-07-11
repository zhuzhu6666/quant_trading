# Development Workflow

> Status: active
> Last verified: 2026-07-06
> Scope: local frontend (Web console + mini-program status surface) + Linux server backend workflow.

本文用来固化当前项目的唯一推荐协作方式，目标只有一个：
避免 Windows 本地副本和 Linux 服务器运行代码长期分叉。

## 1. 核心原则

1. 本地 Windows 负责前端：Web 操作台和微信小程序轻量状态面。
2. Linux 服务器是后端、策略、执行、配置、日志的唯一真实工作区。
3. 实盘相关代码不再采用“本地修改后再手工同步服务器”的方式。
4. 后端问题以服务器复现、服务器修改、服务器验证为准。
5. 小程序问题以本地微信开发者工具验证为准；Web 问题以本地浏览器和 Playwright 验证为准。
6. 系统级改动先查文档事实源、旧债登记和影响面清单，再动代码。

## 2. 角色边界

### 本地 Windows

本地只负责这些内容：

- `miniprogram_v2`
- `web_frontend`
- 前端交互、展示、登录态跳转
- 小程序页面验证
- Web 页面验证
- 文档整理

本地默认不再承担这些工作：

- `backend`
- `execution`
- `alpha`
- `risk`
- `monitor`
- `.env` / systemd / cTrader / 数据库 / 实盘日志

### Linux 服务器

服务器负责这些内容：

- 后端接口
- 交易循环
- 风控逻辑
- cTrader 连接与执行
- 数据库、日志、环境变量、systemd
- 所有实盘相关验证

### Git

Git 仍然保留，但用途要更明确：

- 前端改动：本地 Windows 的 `main` 提交
- 后端改动：Linux 服务器的 `main` 提交
- 不再要求“后端先在本地改，再推服务器”

### 当前分支和工作区

本地 Windows 当前约定：

```text
branch: main
sparse checkout:
  miniprogram_v2/**
  web_frontend/**
  docs/**
  AGENTS.md
  README.md
  .gitignore
```

服务器当前约定：

```text
branch: main
workspace: /home/ubuntu/quant_trading
scope: backend / execution / alpha / risk / monitor / scripts / data / logs / systemd
```

注意：

- 本地 `main` 通过 sparse checkout 只作为小程序和文档工作区使用。
- 服务器 `main` 是后端和运行态真实工作区。
- 文档/协作规则统一推送到 `main`。
- 本地旧后端残留已归档到 `C:\Users\zhu\quant_trading_local_archive_20260629_191212`，不再作为工作区使用。

## 3. 目录约定

### 本地主要目录

```text
miniprogram_v2/
web_frontend/
docs/
AGENTS.md
README.md
.gitignore
```

### 服务器主要目录

```text
backend/
execution/
alpha/
risk/
monitor/
config/
data/
logs/
scripts/
tests/
```

### 服务器运行数据

服务器数据不进入 GitHub。当前关键数据结构：

```text
data/bars_monthly/bars_YYYY_MM.duckdb     # cTrader K 线月库
data/bars.duckdb                          # 指向当前月份 K 线月库的兼容 symlink
data/external_data.duckdb                 # 外部研究数据主库(COT/ETF/宏观), 按 release_at 做 point-in-time 对齐
data/ctrader_data.duckdb                  # 旧 K 线冷备/兼容库，不再作为 live K 线主写入入口
data/events.duckdb                        # 经济事件日历，供风控事件缩放读取
data/external_raw/                        # 外部数据原始响应快照(FRED/events 等)，不入 Git
data/cot/                                 # CFTC COT 原始 zip 缓存，不入 Git
data/sec_gld/                             # SEC GLD filing 缓存，不入 Git
data/archive/                             # 已归档的旧运行库，例如 legacy decision_log.db
```

因子数据统一入口是 `data.factor_frame.FactorFrameBuilder`。live、factor health、evolution 不应各自手写 external/events join；新增外部因子时先落到 `data/external_data.duckdb` / `data/events.duckdb` 的 PIT 标准列，再由 builder 暴露给因子函数。

运行时状态与学习审计主库已迁移到本机 PostgreSQL 的 `state_v1` schema。`data/state.db` 已删除，不再保留本地 SQLite state 冷备，也不再作为 live 主写入口。服务器本地 `.env` 使用 `QUANT_STATE_BACKEND=postgres` 与 `QUANT_STATE_PG_DSN` 控制主库连接；历史 PostgreSQL audit 双写表保留为迁移留痕，不再替代主状态 schema。详见 [state-postgres-store.md](state-postgres-store.md)。

当前 systemd 约定：

```text
quant-backend.service             # 后端主服务
quant-learning-worker.service     # 学习、进化、因子自治治理、特征工程和盘外训练 worker
caddy.service                     # 当前公网 TLS / 反代入口
```

当前公网入口：

```text
https://www.zhuzhu666.icu -> caddy.service -> 127.0.0.1:8000
```

当前服务器主入口是 Caddy。排查公网 502 / TLS / WebSocket 时优先看 `caddy.service` 和 `/etc/caddy/Caddyfile`。


历史独立采集方案已清理：


### 历史 tick 采集退役状态

2026-07-11 起，Dukascopy/cTrader 历史 tick 拉取、月库、原始缓存、timer、retention 和健康检查均已删除。live 继续使用 cTrader `ProtoOASpotEvent` 实时报价；两者不可混同。

## 4. 标准工作流

### 小程序开发流程

```text
本地修改 miniprogram_v2
  -> 微信开发者工具验证
  -> 如接口异常，再去服务器排查后端
  -> 前端确认稳定后提交
```

### Web 前端开发流程

```text
本地修改 web_frontend
  -> 浏览器 / Playwright 验证
  -> 如接口异常，再去服务器排查后端或 Caddy
  -> 前端确认稳定后提交
```

### 后端开发流程

```text
SSH 到服务器
  -> 查 docs/system-source-of-truth.md
  -> 查 docs/legacy-debt-register.md
  -> 按 docs/change-impact-checklist.md 扫影响面
  -> 在服务器上改代码
  -> 在服务器上跑最小验证
  -> 看日志
  -> 必要时重启服务
  -> 再做接口验证
  -> 如事实源/契约/旧债变化，同步更新文档
  -> 确认后提交
```

### 实盘问题排查流程

```text
先看服务器日志
  -> 再看接口状态
  -> 再看数据库 / 配置 / 运行态
  -> 最后才改代码
```

## 5. 服务器日常操作规范

### 登录服务器

当前服务器信息：

- Host: `124.221.7.195`
- User: `ubuntu`
- 项目目录: `/home/ubuntu/quant_trading`

### 常用命令

```bash
cd /home/ubuntu/quant_trading
git status --short
git rev-parse HEAD
systemctl status quant-backend.service --no-pager
journalctl -u quant-backend.service -n 100 --no-pager
curl http://127.0.0.1:8000/api/health
```

### 服务重启

```bash
sudo systemctl restart quant-backend.service
systemctl status quant-backend.service --no-pager -n 30
journalctl -u quant-backend.service --since "2 min ago" --no-pager
```

### 最小验证

每次后端改动后，至少验证：

```bash
curl http://127.0.0.1:8000/api/health
```

如果改动涉及登录或交易页，再额外验证：

```bash
read -rsp "Password: " QUANT_LOGIN_PASSWORD; echo
curl -X POST http://127.0.0.1:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"zhu\",\"password\":\"${QUANT_LOGIN_PASSWORD}\"}"
unset QUANT_LOGIN_PASSWORD
```

如涉及实盘接口，再继续验证：

- `/api/live/account`
- `/api/live/positions`
- `/api/live/strategy-status`
- `/api/live/loop-status`
- `/api/risk/summary`

## 6. Codex CLI 使用规则

服务器已经安装并登录 `codex`，当前用法如下：

```bash
codex
codex --version
codex login status
```

当前约定：

- 本地 Codex：负责前端、小程序、文档、总控协作
- 服务器 Codex：负责后端、实盘、日志、配置、热修

服务器 Codex 适合：

- 服务器才复现的问题
- 交易循环、风控、cTrader 排查
- systemd / `.env` / 数据库 / 权限问题
- 小范围后端改动和验证

服务器 Codex 不适合：

- 改小程序页面
- 本地 UI 联调
- 长期在服务器堆积未整理改动

## 7. 提交规则

### 前端提交

前端改动在本地 Windows 的 `main` 提交。

示例：

```bash
git add miniprogram_v2
git commit -m "fix mini program trading status rendering"
git push origin main
```

### 后端提交

后端改动优先在服务器 `main` 提交。

示例：

```bash
cd /home/ubuntu/quant_trading
git add backend execution alpha risk monitor config tests
git commit -m "fix live loop factor restore path"
git push
```

### 文档/规则同步

如果修改 `AGENTS.md` 或 `docs/development-workflow.md` 这类双方都依赖的规则文档，统一推送到 `main`。

示例：

```bash
git push origin main
```

服务器随后执行：

```bash
cd /home/ubuntu/quant_trading
git pull --ff-only
```

## 8. 明确禁止的做法

以下做法从现在开始默认禁止：

- 在本地修改后端代码，再手工 `scp` 整批覆盖服务器
- 不确认服务器当前代码状态，就直接从本地覆盖
- 后端同时在本地和服务器两边并行修改
- 服务器保留长期未提交热修
- 看到异常先猜代码，再看日志

补充说明：

- 紧急线上止血时，允许做“小文件、单问题、可回滚”的临时同步，但必须满足：
  - 先记录为什么没直接在服务器工作区改
  - 只同步本次热修涉及的单个或少量文件
  - 同步后立刻补文档、补验证结果、补后续服务器工作区收口动作
- 这种临时同步只能算例外，不能重新退回“本地改后端、服务器只接收覆盖”的旧模式

## 9. 每次后端改动后的检查清单

```text
[ ] 改动发生在服务器
[ ] 已查看 git diff
[ ] 已做最小接口验证
[ ] 已查看最新 journalctl
[ ] 如涉及交易循环，已验证 start / stop 或相关状态接口
[ ] 服务状态正常
[ ] 改动已提交或明确记录原因

如果这次改动牵涉：

- cTrader 连接
- `execution/ctrader_bridge.py`
- `backend/services/live_service.py`
- `/api/learning/*`
- `DuckDB` / `SQLite` 路径

则额外检查：

```text
[ ] 已确认 CPU 没有持续爬升
[ ] 已确认 TCP 连接数没有持续异常增长
[ ] 已确认 /api/live/status 最终回到 connected / ready
[ ] 已确认没有把当前不需要的 L2 depth 默认挂回 live 主链
```
```

## 10. 一句话版本

当前项目从现在开始按这条规则执行：

```text
本地做前端（小程序 + Web），服务器做后端和运行态。
```
