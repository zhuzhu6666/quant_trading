# Quant Trading — 启动指南

> 最后更新: 2026-06-11 (统一为 start-all.py 入口)

---

## 开发模式 (推荐)

单命令启动所有组件:

```bash
cd C:\Users\zhu\quant_trading
python start-all.py
```

自动:
1. 启动 FastAPI 后端 (`:8000`)
2. 启动 Vite 前端 dev server (`:5173`)
3. 开浏览器访问 `http://localhost:5173`
4. 前端 `/api/*` 通过 Vite proxy 到后端

停止: 在终端按 `Ctrl+C` (同时停止前后端)。

---

## 生产模式

目前 Vite 版前端暂不支持单端口部署。开发用 `start-all.py` 即可。

---

## CLI 模式 (无 Web)

```bash
# 回测
python main.py --mode backtest

# 模拟盘 (MAB 全栈)
python main.py --mode paper --timeframe M15 --use-router --use-scheduler \
  --use-calibrator --use-meta-monitor --use-factor-monitor \
  --use-alerter --use-retrain --retrain-every-n 300 --use-event-filter

# 因子健康评估
python main.py --mode paper --timeframe M15 --factor-health-report

# L2 因子发现
python scripts/discover_factors.py --n-candidates 1000 --top-k 50 --auto-register

# 实时数据同步 (MT5 需兼容包版本)
python scripts/live_sync.py --mode once --type incremental --timeframes M15,H1,D1
```

---

## cTrader 操作

```bash
# OAuth 授权 (首次 / token 过期)
python scripts/ctrader_oauth.py listen-callback

# Token 验证
python scripts/validate_ctrader_token.py

# 全流测试 (demo 真下单, 验证开→SLTP→平)
python scripts/test_ctrader_full_flow.py
```

---

## 环境要求

- Python 3.11+ (推荐 3.12)
- Node.js 18+ (前端)
- cTrader 凭证 (`.env` 配 CTRADER_CLIENT_ID / SECRET / ACCESS_TOKEN / ACCOUNT_ID)
- MT5 terminal (可选, 仅用于数据同步)

---

## 外部数据刷新

```bash
# 查看各数据源时效
python scripts/refresh_external_data.py --status

# 自动刷新所有过期数据
python scripts/refresh_external_data.py --once

# 启动 Web 时自动刷新
python start-all.py --refresh-data

# 强制刷新某个源
python scripts/refresh_external_data.py --source cot --force
```

---

## 常见问题

### 端口冲突
`start-all.py` 会自动检测 :8000 和 :5173 是否可用, 被占用时报错。手动释放:
```bash
# 找占 8000 的进程
netstat -ano | grep :8000
taskkill -F -PID <PID>
```

### cTrader Token 过期
```bash
python scripts/ctrader_oauth.py refresh
```

### WebSocket 离线
检查后端 :8000 是否在运行, 浏览器 F12 → Console 看 WS 连接错误。
