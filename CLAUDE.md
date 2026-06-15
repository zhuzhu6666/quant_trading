# Quant Trading System — CLAUDE.md

## Project Identity

A production-grade algorithmic trading system focused on gold (XAUUSD) futures, with a **Factor Takeover v4** closed-loop architecture:
因子计算 (StreamingFactorEngine) → 三域归一 (SignalNormalizer) → 两层组合 (PortfolioCompositor) → 执行闸门 (ExecutionGate) → 归因 (AttributionEngine) → 权重自适应 (AdaptiveWeightEngine)
取代了旧的 multi_factor_m15 投票策略。cTrader demo 为唯一执行通道，MT5 仅作数据源。

## Codebase Map (Factor Takeover v4)

| Layer | Path | Purpose |
|-------|------|---------|
| **Factor Engine** | `alpha/streaming_factor_engine.py` | 流式因子计算，deque buffer 200，每 bar 增量计算 39+ 因子 |
| **Signal Normalizer** | `alpha/signal_normalizer.py` | 三域归一：zscore_tanh / rank_mapping / discrete → [-1, +1] |
| **Portfolio Compositor** | `alpha/portfolio_compositor.py` | Tactical/Macro 两层加权组合，tags_breakdown |
| **Execution Gate** | `alpha/execution_gate.py` | 开仓闸门：信号强度/MACD反向/冷却/NFP事件过滤 |
| **Attribution Engine** | `alpha/attribution_engine.py` | 实盘归因：线性 MC + Gram-Schmidt 正交，NW-HAC Sharpe |
| **Adaptive Weight** | `alpha/adaptive_weight_engine.py` | 权重自适应：exp(k×score)，锚点回归，DSR/健康分退役，复活 |
| **GP Classifier** | `alpha/gp_classifier.py` | AST 表达式 → 类型标签（量价/动量/均值回归/波动率/非线性） |
| Alpha mining | `alpha/search/` | GP/Random search, MAP-Elites, BlendSearch SLSQP |
| Backend | `backend/` | FastAPI app, REST/WS, scheduler, live loop |
| Data | `data/` | Market data store (MT5 pull), bar builder, live sync |
| Execution | `execution/` | cTrader bridge, OMS, slippage, market impact |
| Frontend | `frontend-v2/` | React/TypeScript UI (Vite + Tailwind) |
| Risk | `risk/` | Circuit breaker, regime detection |
| Config | `config/runtime_config.py` | 热更新配置：factor_signal_config, factor_portfolio_weights, awe_* |

## Architecture Flow

```
每根 M15 bar → StreamingFactorEngine.append_bar(bar)
    → SignalNormalizer.normalize(values)           # 39 因子 → [-1, +1]
    → PortfolioCompositor.compose(signals)          # Tactical 70% + Macro 30%
    → ExecutionGate.filter(composite, ...)          # 信号/MACD/冷却/事件
    → market_buy/sell (到 cTrader demo)
    ↓ (平仓时)
    AttributionEngine.record_close(...)             # 线性 MC / Gram-Schmidt
    ↓ (每 30 分钟 / 50 笔交易)
    AdaptiveWeightEngine.adapt(...)                 # NW-HAC Sharpe → exp(k×score)
                                                    # DSR + 健康分三重门控退役
                                                    # 多样性约束 ≤ 40%/类型
```

## Conventions

- **No legacy strategy**: `multi_factor_m15` 已删除，全部由因子管道驱动
- **cTrader 唯一执行通道**: `ctrader_send_orders=True` 默认发单到 demo
- **Data source**: MT5 定时拉 K 线填充 DataStore，cTrader 不做数据请求
- **Factor lifecycle**: DISCOVERED → SHADOW → ACTIVE（通过 evolution_orchestrator）
- **Factor health**: 5-dimension (mean_abs_ic 40%, ic_stability 20%, regime_consistency 20%, decay_rate 10%, independence 10%)
- **Scheduler**: 9 jobs (evolution_hourly, canary_fast, retire_hourly, sync_health, data_pull, awe_adapt, ml_retrain, feature_eng, ml_drift_check)
- **Default symbol**: XAUUSD+, timeframe M5
- **Default weights**: 设计文档手拍值, 由 AWE 实盘自适应调优

## Key Config (RuntimeConfig)

- `factor_signal_config` — 每个因子的归一化模式/window/tags
- `factor_portfolio_weights` — 每个因子的初始权重
- `factor_tactical_alpha=0.7` — 战术层权重
- `factor_signal_threshold=0.4` — 开仓信号阈值
- `awe_sensitivity=0.5` / `awe_anchor_pull=0.15` — 自适应参数
- `factor_dry_run=False` — cTrader demo 默认真发单

## Testing

- `pytest tests/alpha/ -v` for Factor Takeover v4 module tests (315+ alpha tests, 497 total)
- `pytest tests/alpha/ -v -k <pattern>` for targeted tests
- Test files mirror source structure: `tests/alpha/`, `tests/execution/`, etc.
- Key test files: `test_streaming_factor_engine.py`, `test_signal_normalizer.py`, `test_portfolio_compositor.py`, `test_execution_gate.py`, `test_attribution_engine.py`, `test_adaptive_weight_engine.py`, `test_gp_classifier.py`

## Audit

- `PROJECT_AUDIT_v10.md` — 2026-06-14 全代码库审计 (296 files, 55,578 lines)
- Alpha 核心管道无 P0 bug，问题集中在执行层和后端鉴权
- 当前已修复: P0-1~P0-6, P1-1~P1-2 (JWT_SECRET 环境变量化, get_current_user 抛 401, BaseBrokerBridge 统一接口, _cleanup_stale_jobs TTL)
- 未修复: `TODO.md` 查看

## Startup

```bash
python start-all.py                    # 开发模式 (后端 :8000 + 前端 :5173)
python start-all.py --refresh-data     # 启动前刷新外部数据 (COT/Events/ETF)
```

## AI Behavior Rules

1. **Before editing a file, read it** — never assume content from memory
2. **Verify before claiming complete** — run the relevant test or command, show evidence
3. **When debugging, use systematic-debugging skill** — never trial-and-error
4. **Check git status before proposing changes** — don't clobber uncommitted work
5. **All new features must integrate into the factor pipeline** — no standalone scripts
6. **Respect alpha/factor_dsl.py AST conventions** — don't invent new expression formats
7. **Use RegistryAdapter for factor registration** — never write directly to SQLite
8. **Memory-first**: Save non-obvious project insights to `memory/` directory
9. **Before touching cTrader bridge**, check if connected (can block threadpool)
10. **Backend runs on FastAPI** — blocking calls go in `run_in_executor` or background tasks
11. **Database is SQLite** — avoid writes in hot paths, use LIMIT on market data queries
12. **cTrader = execution only, MT5 = data source only** — never conflate the two
