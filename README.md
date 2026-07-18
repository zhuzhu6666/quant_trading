# Quant Trading System

> Status: active
> Last verified: 2026-07-06
> Scope: top-level project entry points and current operating shape.

XAUUSD+ 量化交易系统。当前主线是 Factor Takeover v4 + 因子自治治理 V3: 因子引擎、执行闸门、cTrader demo、持仓监督、归因复盘、规则驱动学习、模型数据管道、Factor Catalog、自治治理编排、RuntimeConfig overlay 和统一进化账本已经合并为一条可审计、可回放、可回滚的闭环。

## Current Entry Points

- Backend API: `./.venv/bin/python -m backend`
- Frontend: `web_frontend` is the full operator console; `miniprogram_v2` is the lightweight mobile status surface
- Current docs: [docs/README.md](docs/README.md)
- Workspace rules: [AGENTS.md](AGENTS.md)
- Current work board: [TODO.md](TODO.md)
- Architecture: [docs/architecture.md](docs/architecture.md)
- Current runtime map: [docs/system-operation-map.md](docs/system-operation-map.md)
- Startup guide: [docs/startup.md](docs/startup.md)

## Current Workflow

当前默认工作流已经收紧为：

- 本地 Windows 负责前端：`web_frontend` 和轻量状态小程序 `miniprogram_v2`
- Linux 服务器负责后端、策略、执行、日志和实盘验证

具体规则见 [AGENTS.md](AGENTS.md) 和 [docs/development-workflow.md](docs/development-workflow.md)。

## Architecture

For the code-verified current runtime order, read [docs/system-operation-map.md](docs/system-operation-map.md) first. The short map is:

```text
Market data
  -> FactorFrameBuilder (bars + PIT external/events)
  -> StreamingFactorEngine
  -> SignalNormalizer
  -> PortfolioCompositor
  -> ExecutionGate / risk gate
  -> cTrader demo execution
  -> PositionSupervisor / RiskPolicyService
  -> DecisionLedger / lifecycle events
  -> Trade review / attribution recovery / experience memory
  -> supervisor trace / counterfactual / policy suggestion
  -> Factor Catalog / FactorGovernanceOrchestrator
  -> runtime_config_overlay / evolution_decision / runtime_config_snapshot
  -> learning dataset / model pipeline / shadow model audits
```

The old browser Web Console and MT5-era documents are no longer maintained. The current maintained frontend is `miniprogram_v2` plus the new `web_frontend` browser console. Web now carries the heavier operator views, including model capability, learning health, risk, trading and PnL pages; the mini-program should remain a lightweight status surface.

Factor data now has a single internal source of truth: `data.factor_frame.FactorFrameBuilder`. Live calculation, factor health, and evolution should all consume the same point-in-time factor frame instead of rebuilding external joins separately.

Runtime state and learning audit state use PostgreSQL `state_v1` as the source of truth. Legacy SQLite state files are cold backup / migration inputs only and must not be treated as live state.

## Quick Start

```bash
./.venv/bin/python -m pip install --require-hashes -r requirements.txt
./.venv/bin/python -m backend
```

Optional CLI flows:

```bash
python main.py --mode backtest --timeframe M15
python main.py --mode paper --timeframe M15 --use-router
```

## Tests

```bash
python -m pytest tests/test_autonomous_learning.py tests/test_position_supervisor_governance.py tests/test_supervisor_counterfactual.py tests/test_live_service_lifecycle.py tests/risk/test_policy_service.py tests/test_runtime_config.py -q
python -m pytest tests/test_open_quality_lightgbm.py tests/test_position_quality_lightgbm.py tests/test_factor_governance_lightgbm.py tests/test_meta_model_lightgbm.py -q
python scripts/phase_a_health_check.py
python scripts/phase_c_supervisor_check.py --limit 30
```

Full `tests` can be slower on Windows; prefer targeted suites for learning/live changes.

## Current Autonomous Interfaces

- `GET /api/learning/evolution/runs`
- `GET /api/learning/evolution/runs/{run_id}`
- `POST /api/learning/position-supervisor/traces/backfill`
- `POST /api/learning/position-supervisor/traces/materialize-labels`
- `POST /api/learning/autonomous/run`
- `GET /api/learning/dataset/quality-health`
- `POST /api/learning/model/open-quality-lightgbm/train`
- `POST /api/learning/model/open-quality-lightgbm/shadow-run`
- `GET /api/learning/model/open-quality-lightgbm/audits`
- `POST /api/learning/model/position-quality-lightgbm/train`
- `POST /api/learning/model/position-quality-lightgbm/shadow-run`
- `POST /api/learning/model/factor-governance-lightgbm/train`
- `POST /api/learning/model/factor-governance-lightgbm/shadow-run`
- `POST /api/learning/model/meta-lightgbm/train`
- `POST /api/learning/model/meta-lightgbm/shadow-run`
- `GET /api/learning/model/permissions/audits`

Current model policy:

- Mathematical models are shadow/advisory by default and cannot place orders, close positions or change hard risk limits.
- `open_quality_lightgbm` scores open timing quality from matured open outcome samples and writes shadow audit rows only.
- `position_quality_lightgbm`, `factor_governance_lightgbm` and `meta_model_lightgbm` all use time-ordered holdout metrics.
- LLM advisory is explanation/governance assistance only; it is not an execution model.
