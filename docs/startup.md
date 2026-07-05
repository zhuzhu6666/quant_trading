# Quant Trading — 启动指南

> 最后更新: 2026-07-01
> 当前形态: FastAPI API-only 后端 + 微信小程序 V2；新 Web 操作台进入规划阶段。旧 Vite/Web Console 构建产物不再维护。

> 2026-06-26 工作流更新:
> - 本地 Windows 默认负责前端：`miniprogram_v2`，以及建立后的 `web_frontend`
> - 后端、策略、执行、日志排查默认都在 Linux 服务器完成
> - 本文保留本地启动命令，主要用于临时调试，不再作为实盘后端的主工作流

---

## 当前开发启动

Linux 服务器后端必须使用仓库内独立虚拟环境，避免污染 Codex/Hermes 等共享运行环境:

```bash
cd /home/ubuntu/quant_trading
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python -m backend
```

### cTrader / protobuf 依赖边界

当前执行通道使用 `ctrader-open-api==0.9.2`，该 SDK 在 metadata 中固定依赖 `protobuf==3.20.1`。Python 3.12 下这个 protobuf 版本会在测试中触发第三方 `utcfromtimestamp()` 弃用提示，项目通过 `pytest.ini` 精准过滤 `google.protobuf.internal.well_known_types` 里的这条 warning；不要为了消除 warning 单独升级 protobuf。

只有在同时验证下面链路后，才允许升级 `ctrader-open-api` 或 protobuf：

- cTrader connect / auth probe
- protobuf message parse / enum payload
- close / reduce / amend_position_sltp 执行链路

本地 Windows 临时调试时，在仓库根目录启动后端:

```bash
cd C:\Users\zhu\quant_trading
.\.venv\Scripts\python.exe -m backend
```

后端启动前必须配置认证环境变量；缺失时服务会 fail closed:

```env
QUANT_JWT_SECRET=至少 32 字节随机字符串
QUANT_AUTH_USER=登录用户名
QUANT_PASSWORD_HASH=登录密码的 SHA256 十六进制摘要
```

默认监听:

- HTTP API: `http://localhost:8000`
- WebSocket: `ws://localhost:8000/ws/state`

等价命令:

```bash
./.venv/bin/uvicorn backend.app:app --host 0.0.0.0 --port 8000
```

需要热重载时:

```bash
./.venv/bin/python -m backend --reload
```

---

## 小程序前端

当前已维护的移动端前端是微信小程序 V2:

```text
C:\Users\zhu\quant_trading\miniprogram_v2
```

用微信开发者工具直接打开该目录。小程序依赖当前 FastAPI 后端提供:

- `/api/live/*`
- `/api/v4/*`
- `/api/factor-health/latest`
- `/api/control/*`
- `/api/system/db-health`
- `/api/learning/*`

更多说明见 `miniprogram_v2/README.md`。

---

## Web 前端

Web 前端目标是接替小程序的完整操作台能力，目录约定为：

```text
web_frontend/
```

已有源码目录，推荐本地开发与生产调试命令如下：

```bash
cd web_frontend
npm install
npm run dev
```

生产入口由服务器 Caddy 承接：

```text
https://www.zhuzhu666.icu -> Caddy -> FastAPI / Web static
```

当前服务器状态：

- Caddy 监听公网 `:80` / `:443`
- FastAPI 后端只监听 `127.0.0.1:8000`
- `/api/*` 与 `/ws/state` 由 Caddy 反代到 FastAPI

规划见 [web-frontend-upgrade-plan.md](web-frontend-upgrade-plan.md)。

---

## CLI 模式

```bash
# 回测
python main.py --mode backtest --timeframe M15

# 模拟盘
python main.py --mode paper --timeframe M15 --use-router

# 因子健康评估
python main.py --mode paper --timeframe M15 --factor-health-report

# 因子发现：默认只生成报告，不自动注册
python scripts/discover_factors.py --n-candidates 1000 --top-k 50

# 明确完成 review / 多 forward / 风控门槛后，才允许显式注册 shadow 因子
python scripts/discover_factors.py --n-candidates 1000 --top-k 50 --auto-register
```

---

## cTrader 操作

```bash
# Token 验证
python scripts/validate_ctrader_token.py

# 回填历史成交到 PostgreSQL state_v1.ctrader_deals
python scripts/backfill_ctrader_deals.py --days 30
```

OAuth 脚本是否存在可能随本地凭证流调整；执行前先用 `rg ctrader_oauth scripts` 确认当前入口。

---

## 外部数据刷新

```bash
# 查看各数据源时效
python scripts/refresh_external_data.py --status

# 自动刷新所有过期数据
python scripts/refresh_external_data.py --once

# 强制刷新某个源
python scripts/refresh_external_data.py --source cot --force
```

---

## 数据库体检

2026-06-26 起，数据库层默认先体检再排障。

统一规则:

- `PostgreSQL state_v1` 用于运行时状态；`SQLite` 仅保留 `experiments.db` 和显式临时/迁移源库
- `DuckDB` 只用于行情、外部研究数据、tick、L2、归因、事件库
- 业务代码必须走 `backend/core/db.py` 的统一连接入口

启动前或排障时优先执行:

```bash
# 标准修复 + 体检
python scripts/db_doctor.py --repair
```

这个命令会检查:

- 库文件是否能被正确引擎打开
- 关键表和关键字段是否齐全
- 历史 schema 漂移是否需要自动修复

如果这里不通过，不要先怀疑策略逻辑，先修数据库契约。

---

## 环境要求

- Python 3.11+，推荐 3.12
- cTrader 凭证写入 `.env`
- 微信开发者工具用于 `miniprogram_v2`
- Node.js 用于新 `web_frontend` 开发和浏览器测试；不要用它维护旧 Web Console 构建产物

---

## 常见问题

### 端口冲突

后端默认使用 `:8000`。Windows 上可手动查看并结束占用进程:

```powershell
netstat -ano | findstr :8000
taskkill /F /PID <PID>
```

### WebSocket 离线

先确认后端健康:

```bash
curl http://localhost:8000/api/health
```

再检查小程序配置中的 API base URL 是否指向当前后端。

### cTrader Token 过期

先运行:

```bash
python scripts/validate_ctrader_token.py
```

如果失败，再按当前 `.env` 和凭证流刷新 token。
