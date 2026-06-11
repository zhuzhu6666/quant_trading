# Python 量化交易框架

XAUUSD 黄金 M15 量化交易系统 — 自动化因子发现、GP 因子挖掘、cTrader 实盘执行、Web 总控台。

**最后更新**: 2026-06-11 | **HEAD**: daa7f54 | **分支**: main

---

## 状态速览

| 维度 | 状态 |
|---|---|
| **因子库** | 39 builtin + 26 GP DSL auto → 2 HEALTHY / 45 WATCH / 18 DECAYING |
| **策略** | multi_factor_m15 (主) + 6 辅助策略, MAB Thompson sampling 路由 |
| **回测** | 50K bar M15, risk=1% CB=15% → **+59.17% / Sharpe 0.936 / DD 15.9%** |
| **cTrader** | ✅ 开平仓 + SL/TP server 端全流通过 (demo 47276606, 1000 JPY) |
| **自进化** | ✅ 编排器: GP→OOS→Canary→WeightPolicy→Retire 全闭环 |
| **Web UI** | ✅ Vite + React 19, 5 面板, 43 REST + 1 WS |
| **MT5 数据** | ⏸ IPC pipe 不兼容, 50K bar 离线已够 |

---

## 快速启动

```bash
# 一键启动 (后 :8000 + 前 :5173 + 自动开浏览器)
cd C:\Users\zhu\quant_trading
python start-all.py

# 回测
python main.py --mode backtest --timeframe M15

# 模拟盘 (MAB 全栈)
python main.py --mode paper --timeframe M15 --use-router --use-scheduler \
  --use-calibrator --use-meta-monitor --use-factor-monitor \
  --use-alerter --use-retrain --retrain-every-n 300 --use-event-filter

# cTrader PoC
python scripts/ctrader_poc.py

# cTrader 全流测试 (demo 真下单)
python scripts/test_ctrader_full_flow.py
```

---

## 核心架构

```
MT5 (数据源) → SQLite bars → alpha/ 因子计算 → strategies/ 信号 → cTrader (执行)
                                        ↕
                              backend/runtime/ 自进化编排器
                              (GP→OOS→Canary→WeightPolicy→Retire)
                                        ↕
                              frontend-v2/ Web 总控台 (Vite+React19)
```

### 双 broker 分工
- **MT5** — 仅数据源 (拉 K 线填 SQLite, 不交易)
- **cTrader** — 唯一执行通道 (Pepperstone demo, Open API)

---

## 文档索引

| 文档 | 内容 |
|---|---|
| `PROJECT_MAP.md` | 完整目录结构 + 关键路径速查 |
| `docs/planning/ROADMAP.md` | 路线图 |
| `docs/planning/self-evolution-system.md` | 自进化设计 |
| `docs/CTRADER_INTEGRATION.md` | cTrader 接入 + 已知坑 |
| `docs/startup.md` | 启动指南 |
| `docs/user-guide/README.md` | Web Console 用户手册 |
| `docs/frontend-architecture.md` | 前端架构 |
| `docs/design/product-spec.md` | 产品定位 |
| `docs/design/ui-design-tokens.md` | UI 设计 token |
| `docs/REALTIME_PAPER_DESIGN.md` | 实盘设计 draft |
| `CLAUDE.md` | AI 行为规则 |

---

## 安装

```bash
# Python 3.11+ (推荐 3.12)
pip install -r requirements.txt

# 前端 (首次)
cd frontend-v2 && npm install && cd ..

# cTrader 凭证
# 编辑 .env: CTRADER_CLIENT_ID / SECRET / ACCESS_TOKEN / ACCOUNT_ID
```

---

## 关键 PnL 记录

| 配置 | PnL | Trades | Sharpe | DD |
|---|---|---|---|---|
| 无风控 baseline | +407.51% | 738 | 1.807 | 39.77% |
| MAB T1-T13 全栈 | +120.75% | 639 | 0.894 | 64% |
| **调参最优 (risk=1% CB=15%)** | **+59.17%** | 354 | **0.936** | **15.9%** |
| 调参前 (risk=2% CB=10%) | -10.28% | 13 | -0.864 | 11.3% |
