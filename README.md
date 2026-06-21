# Quant Trading System

量化交易框架 — XAUUSD 黄金交易，数据→因子→策略→风控→执行全链路闭环。

## 架构

```
main.py (CLI 入口)
├── cli/backtest.py   — 回测模式, backtrader 参数扫描
├── cli/paper.py      — 模拟盘, 历史 bar 回放 + 模拟撮合
├── cli/live.py       — 实盘, cTrader Open API (开发中)
│
├── core/             — EventBus / State / Clock / AppContext
├── data/             — DuckDB 存储 + cTrader 实时拉取
├── alpha/            — 因子引擎 (22+ 因子 + GP 发现 + 健康评估)
├── strategy/         — MAB Thompson Sampling 路由 + 仓位管理
├── execution/        — OMS 状态机 + 撮合 + 算法执行 (TWAP/VWAP/POV/IS)
├── risk/             — 四道防线 (前置检查→熔断→VaR→Kelly)
├── backend/          — FastAPI REST API + WebSocket
├── monitor/          — metrics / alerter / structured logging
├── config/           — YAML + RuntimeConfig 热更新
├── tests/            — 38 smoke tests
└── scripts/          — 离线分析脚本
```

## 快速开始

```bash
pip install -r requirements.txt
python main.py --mode backtest --timeframe M15
python main.py --mode paper --timeframe M15 --use-router
python -m backend          # 启动 API (端口 8000)
```

## 测试

```bash
pip install pytest
python -m pytest tests/ -v
```

## 配置

核心: `config/settings.yaml`。凭证通过 `.env` (已在 .gitignore 排除)。

## 前端

Web 面板已移除，通过微信小程序接入后端 API。
