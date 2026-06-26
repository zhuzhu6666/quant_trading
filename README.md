# Quant Trading System

XAUUSD+ 量化交易系统。当前主线是 Factor Takeover v4: 因子引擎、执行闸门、cTrader demo、决策账本、规则驱动学习和模型数据管道已经合并为一条可审计闭环。

## Current Entry Points

- Backend API: `python -m backend`
- Frontend: open `miniprogram_v2` in WeChat DevTools
- Current docs: [docs/README.md](docs/README.md)
- Workspace rules: [AGENTS.md](C:/Users/zhu/quant_trading/AGENTS.md)
- Current status and TODO: [TODO.md](TODO.md)
- Architecture: [docs/architecture.md](docs/architecture.md)
- Startup guide: [docs/startup.md](docs/startup.md)

## Current Workflow

当前默认工作流已经收紧为：

- 本地 Windows 只负责 `miniprogram_v2`
- Linux 服务器负责后端、策略、执行、日志和实盘验证

具体规则见 [AGENTS.md](C:/Users/zhu/quant_trading/AGENTS.md) 和 [docs/development-workflow.md](C:/Users/zhu/quant_trading/docs/development-workflow.md)。

## Architecture

```text
Market data
  -> StreamingFactorEngine
  -> SignalNormalizer
  -> PortfolioCompositor
  -> ExecutionGate / risk gate
  -> cTrader demo execution
  -> DecisionLedger / lifecycle events
  -> Trade review / experience memory
  -> learning dataset / model pipeline
```

The browser Web Console and MT5-era documents are no longer maintained. The current frontend is `miniprogram_v2`.

## Quick Start

```bash
pip install -r requirements.txt
python -m backend
```

Optional CLI flows:

```bash
python main.py --mode backtest --timeframe M15
python main.py --mode paper --timeframe M15 --use-router
```

## Tests

```bash
python -m pytest tests\research\test_rule_learning_pipeline.py tests\research\test_model_registry.py -q
python -m pytest tests\research tests\alpha\test_portfolio_compositor.py tests\test_live_service_lifecycle.py tests\test_evolution_closure_fixes.py tests\deployment\test_deployment.py tests\test_backend_jobs_manager.py tests\test_backend_jobs_state.py -q
```

Full `tests` can be slower on Windows; prefer targeted suites for learning/live changes.
