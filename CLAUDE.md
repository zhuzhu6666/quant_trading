# Quant Trading System — CLAUDE.md

## Project Identity

A production-grade algorithmic trading system focused on gold (XAUUSD) futures, with automated factor discovery, genetic programming-based alpha mining, multi-broker execution (cTrader + MT5), and a full lifecycle management regime (discover → shadow → promote → monitor → decay → retire).

## Codebase Map

| Layer | Path | Purpose |
|-------|------|---------|
| Alpha mining | `alpha/` | Factor DSL, GP/random search, IC tracking, health scoring |
| Backend | `backend/` | FastAPI app, REST/WS, sync, shadow, backtest runners |
| Data | `data/` | Market data store, bar builder, tick receiver, live sync |
| DB | `db/` | SQLite schema & store |
| Execution | `execution/` | OMS, paper engine, cTrader/MT5 bridges, routing, slippage |
| Factors | `factors/` | Concrete factor implementations (aroon, cci, mfi, williams_r) |
| Frontend | `frontend-v2/` | React/TypeScript UI (Vite + Tailwind) |
| Live | `live/` | Executor, factor monitor, meta-learner monitor |
| Monitor | `monitor/` | Dashboard, metrics, alerting, evolution story |
| Risk | `risk/` | Circuit breaker, position sizing, regime detection |
| Scripts | `scripts/` | Cron jobs, daemons, factor mining, backfills |
| Config | `config/` | Runtime settings |

## Conventions

- **Factor lifecycle**: DISCOVERED → SHADOW → ACTIVE → WATCH → DECAYING → DEAD
- **Factor health scoring**: 5-dimension (mean_abs_ic 40%, ic_stability 20%, regime_consistency 20%, decay_rate 10%, independence 10%)
- **IC evaluation**: Always check `ICTracker.rolling_ic()` before claiming a factor works
- **GP config**: pop=50, gen=30 for daily auto-discover; use `--dry-run` before registering
- **Data timeframe**: M15 primary for factor work, H1/D1 for regime detection
- **Symbol**: Default XAUUSD+ throughout

## Testing

- `pytest tests/ -v` for unit tests
- `pytest tests/ -v -k <pattern>` for targeted tests
- Test files mirror source structure: `tests/alpha/`, `tests/execution/`, etc.

## AI Behavior Rules

1. **Before editing a file, read it** — never assume content from memory
2. **Verify before claiming complete** — run the relevant test or command, show evidence
3. **When debugging, use systematic-debugging skill** — never trial-and-error
4. **Check git status before proposing changes** — don't clobber uncommitted work
5. **Respect alpha/factor_dsl.py AST conventions** — don't invent new expression formats
6. **Use RegistryAdapter for factor registration** — never write directly to SQLite
7. **Memory-first**: Save non-obvious project insights to `memory/` directory
8. **Before touching execution/ctrader_bridge.py or execution/mt5_bridge.py**, check if the bridge is connected (can block the threadpool)
9. **Backend runs on FastAPI** — blocking calls go in `run_in_executor` or background tasks
10. **Database is SQLite** — avoid writes in hot paths, use LIMIT on market data queries

## Self-Evolution

This project uses Continuous Learning v2 (CLv2) for instinct-based learning:
- **Hooks** observe tool usage and create observation records
- **Instincts** are extracted atomic behaviors with confidence scores
- **Evolve** clusters instincts into skills/commands/agents

To evolve: `python3 ~/.claude/skills/ecc/continuous-learning-v2/scripts/instinct-cli.py evolve --generate`
