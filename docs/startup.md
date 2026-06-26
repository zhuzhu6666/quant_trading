# Quant Trading — 启动指南

> 最后更新: 2026-06-26
> 当前形态: FastAPI API-only 后端 + 微信小程序 V2。旧 Vite/Web Console 启动方式已停用。

> 2026-06-26 工作流更新:
> - 本地 Windows 默认只负责 `miniprogram_v2`
> - 后端、策略、执行、日志排查默认都在 Linux 服务器完成
> - 本文保留本地启动命令，主要用于临时调试，不再作为实盘后端的主工作流

---

## 当前开发启动

在仓库根目录启动后端:

```bash
cd C:\Users\zhu\quant_trading
python -m backend
```

默认监听:

- HTTP API: `http://localhost:8000`
- WebSocket: `ws://localhost:8000/ws/state`

等价命令:

```bash
uvicorn backend.app:app --host 0.0.0.0 --port 8000
```

需要热重载时:

```bash
python -m backend --reload
```

---

## 小程序前端

当前唯一维护的前端是微信小程序 V2:

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

## CLI 模式

```bash
# 回测
python main.py --mode backtest --timeframe M15

# 模拟盘
python main.py --mode paper --timeframe M15 --use-router

# 因子健康评估
python main.py --mode paper --timeframe M15 --factor-health-report

# L2 因子发现
python scripts/discover_factors.py --n-candidates 1000 --top-k 50 --auto-register
```

---

## cTrader 操作

```bash
# Token 验证
python scripts/validate_ctrader_token.py

# 回填历史成交到 state.db 的 ctrader_deals
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

- `SQLite` 只用于 `state.db` / `experiments.db`
- `DuckDB` 只用于行情、tick、L2、归因、事件库
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
- Node.js 只在维护旧构建产物或浏览器测试工具时需要，不是当前主启动链路

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
