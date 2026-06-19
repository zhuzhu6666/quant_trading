# PROJECT_AUDIT_v14 — Comprehensive Code Audit

**Date:** 2026-06-19
**Previous audit:** v13 (2026-06-19, 494 tests pass)
**Scope:** Full codebase — closed-loop verification, bug hunt, orphan file detection
**Tests:** 494 passed (305 alpha + 189 rest), 2 warnings, 0 failures

---

## Executive Summary

Factor Takeover v4 闭环完整。8个调度任务正确注册。所有494测试通过。发现2个P0阻断级bug、2个P1高优先级问题、20个孤儿文件。

---

## 1. Closed-Loop Verification ✅

### Factor Takeover v4 Pipeline (完整闭环)

```
live_service._run_loop (line 1347)
  ├── Warmup: local DB → broker fallback → cache fallback
  ├── Pipeline init (line 1431-1472):
  │   StreamingFactorEngine → SignalNormalizer → PortfolioCompositor → ExecutionGate
  │   AttributionEngine → ICTracker → AdaptiveWeightEngine
  │
  └── Main tick loop → _process_tick_factor_pipeline (line 1968):
      1. engine.append_bar(bar)           → factor_values
      2. normalizer.normalize(values)     → signals
      3. compositor.compose(signals, ...) → composite
      4. gate.filter(composite, ...)      → gate_result
      5. _execute: market_buy/sell via ctrader_bridge
      6. AttributionEngine.record_open()  on fill
      7. AttributionEngine.record_close() on position close detection
      8. AWE trailing stop via _update_trailing_stops()
```

**Status:** ✅ All components wired, full attribution tracking with cross-restart recovery (line 2083-2119).

### Scheduler Jobs (8 jobs, all registered)

| Job | Cron | Status |
|-----|------|--------|
| evolution_hourly | 0 * * * * | ✅ |
| data_sync | */5 * * * * | ✅ |
| dukascopy_tick | 0 * * * * | 🔴 P0-1 (path wrong) |
| awe_adapt | */30 * * * * | ✅ |
| ml_retrain | 0 5 * * 0 | ⚠️ P1-3 (fallback timing) |
| feature_eng | 0 3 * * * | ✅ |
| ml_drift_check | 0 */6 * * * | ⚠️ P1-3 (fallback timing) |
| system_health | * * * * * | ✅ |

### Test Results

```
tests/alpha/                               305 passed
tests/ (non-alpha)                          189 passed
TOTAL                                      494 passed
Warnings                                      2 (scipy constant input + twisted service_identity)
```

---

## 2. P0 — CRITICAL (blocks production)

### P0-1 🔴 dukascopy_tick scheduler job SILENTLY fails

**File:** `backend/services/live_service.py:1003`
**Problem:** The scheduler constructs path:
```python
script = Path(__file__).resolve().parent.parent.parent / "_pull_dukascopy_incremental.py"
# → C:\Users\zhu\quant_trading\_pull_dukascopy_incremental.py
```
But the actual file is at:
```
C:\Users\zhu\quant_trading\scripts\debug\_pull_dukascopy_incremental.py
```
The scheduler silently logs "script not found" every hour and does nothing.
Dukascopy tick data is NOT being pulled automatically.

**Fix:** Change line 1003 to:
```python
script = Path(__file__).resolve().parent.parent.parent / "scripts" / "debug" / "_pull_dukascopy_incremental.py"
```
Or move the script to project root (also update Dockerfile line 40).

### P0-2 🔴 scripts/daily_paper_dryrun.py crashes on import

**File:** `scripts/daily_paper_dryrun.py:57`
**Problem:** Imports from DELETED file:
```python
from strategies.multi_factor_m15 import MultiFactorM15Strategy as MultiFactorM15
```
`strategies/multi_factor_m15.py` was deleted (git: `D strategies/multi_factor_m15.py`).
This script crashes with ImportError on any invocation.

**Fix:** Either delete the script entirely (it's a paper dryrun tool superseded by the v4 pipeline), or rewrite it to use the factor pipeline.

### P0-3 🔴 scripts/ctrader_live_runner.py crashes on import

**File:** `scripts/ctrader_live_runner.py:166`
**Problem:** Imports from DELETED file:
```python
from execution.mt5_bridge import MT5Bridge, fetch_history, rates_to_dataframe
```
`execution/mt5_bridge.py` was deleted (git: `D execution/mt5_bridge.py`).
This script crashes with ImportError.

**Fix:** This script is superseded by `backend/services/live_service.py` (the `_run_loop` function). Delete the script or mark as deprecated.

---

## 3. P1 — HIGH (causes incorrect behavior)

### P1-1 ⚠️ CLAUDE.md stale — wrong job count, stale references

**File:** `CLAUDE.md`
**Issues:**
- Line 56: Claims "9 jobs" but actual scheduler has 8 jobs
- Lists `canary_fast, retire_hourly, sync_health, data_pull` which were merged
- Missing `system_health` and `dukascopy_tick` from the list
- Lines 120/452/712: References `multi_factor_m15` strategy which no longer exists
- Line 6: Mentions "MT5 已完全移除" but docs still reference it

**Fix:** Update job list to reflect actual 8 jobs. Remove multi_factor_m15 references from docs.

### P1-2 ⚠️ Cron parser incomplete for timer-mode fallback

**File:** `backend/runtime/scheduler.py:87-109`
**Problem:** `_TimerJob._parse_interval_seconds()` only handles 4 cron formats:
1. `*/N` — every N minutes
2. `* *` — every minute
3. `N *` — every N minutes
4. `0 N` — every N hours

It CANNOT parse:
- `0 */6 * * *` (every 6 hours) → falls back to 3600s (1h) → **ml_drift_check runs hourly instead of every 6h**
- `0 5 * * 0` (weekly Sunday 5am) → falls back to 3600s (1h) → **ml_retrain runs hourly instead of weekly**

Currently `apscheduler` is NOT installed (`ModuleNotFoundError`), so the Timer fallback IS active. This means two jobs run at wrong frequencies.

**Fix:** Either install `apscheduler` or extend the cron parser to handle `*/N` in the hour field and day-of-week field.

### P1-3 ⚠️ 20 orphan .py files never imported

These files exist but are never imported by any active code, tests, or scripts:

| File | Original Purpose | Recommendation |
|------|-----------------|----------------|
| `factors/aroon.py` | Old factor system | DELETE |
| `factors/cci.py` | Old factor system | DELETE |
| `factors/mfi.py` | Old factor system | DELETE |
| `factors/williams_r.py` | Old factor system | DELETE |
| `data/bar_builder.py` | Old bar builder (duplicated by tick_pipeline) | DELETE |
| `data/feed.py` | Old data feed (superseded by DataStore) | DELETE |
| `data/tick_generator.py` | tick_receiver companion (tick_receiver deleted) | DELETE |
| `modules/database.py` | Only used by scripts/gp_interpret.py, scripts/factor_mining.py | Migrate then DELETE |
| `strategy/scorer.py` | Old multi-strategy scorer (v4 uses PortfolioCompositor) | DELETE |
| `strategy/signal_bus.py` | Old signal bus (v4 uses direct pipeline) | DELETE |
| `deployment/risk_rebalancer.py` | Risk rebalancer | Review, then DELETE |
| `monitor/alert_rules.py` | Alert rules (used by system_health?) | Verify, then DELETE |
| `data/live_sync/bar_filter.py` | MT5 puller dead code | Clean or DELETE |
| `data/live_sync/db_inserter.py` | Not imported by active code | DELETE |
| `data/live_sync/quality_gate.py` | Not imported by active code | DELETE |
| `execution/analytics.py` | Planned Phase 4 (not integrated) | Keep but wire in |
| `execution/latency.py` | Planned Phase 4 (not integrated) | Keep but wire in |
| `execution/market_impact.py` | Planned Phase 4 (not integrated) | Keep but wire in |
| `execution/match_replay.py` | Not imported | DELETE |
| `execution/order_retry.py` | Not imported | DELETE |

**Total ~20 files, ~4000+ lines of dead code.**

### P1-4 ⚠️ data/live_sync/bar_filter.py — dead MT5 code

**File:** `data/live_sync/bar_filter.py:43-54`
**Problem:** `BarFilter.__init__` accepts `mt5_puller=None` parameter and stores `self._puller`. Since `MT5Puller` no longer exists, this code is dead. The `_now_epoch()` method falls back to `time.time()` when `_puller` is None (which it always is). This is a working but misleading dead branch.

---

## 4. P2 — MEDIUM (code quality, non-critical)

### P2-1 93 bare `except: pass` across codebase

18 of these are in `backend/services/live_service.py` alone. Many silently swallow exceptions that should at minimum be logged. Critical examples:
- `live_service.py:2144` — swallows position close PnL calculation errors
- `live_service.py:2208` — swallows spot price fallback errors
- `live_service.py:2461,2475` — swallows trailing stop amend errors
- `adaptive_weight_engine.py:317,330` — swallows weight update failures

### P2-2 SignalNormalizer HOUR_WEIGHTS gap

**File:** `alpha/signal_normalizer.py:26-33`
Hours 4-7 UTC are not covered by any weight range. For these 4 hours daily, no hour weight is applied (default 0.0). This may be intentional (Asian session lull) but should be documented.

### P2-3 Potential div-by-zero (8 sites, mostly guarded)

All 8 flagged sites have upstream guards or fallbacks; none are confirmed crashes. See:
- `alpha/signal_normalizer.py:89` — `len(arr)` guarded by `min_samples > 0`
- `alpha/attribution_engine.py:610` — `len(tags)` could be 0 if `tags` is empty
- `alpha/registry.py:762` — `len(window)` could be 0 if window is empty list

### P2-4 adaptive_weight_engine.py:180 — unbounded exp()

`math.exp(k * score)` where `k = sensitivity/(1 + sensitivity*abs(score))` — bounded by design but no explicit clamp. If `k * score > 709`, raises OverflowError.

### P2-5 evolution_orchestrator.py — stale comments

**File:** `backend/runtime/evolution_orchestrator.py:441,509`
References `multi_factor_m15` in comments describing the old strategy format. The actual code now pushes weights to RuntimeConfig directly (v4 path). Comments should be updated.

### P2-6 Dockerfile references wrong path

**File:** `Dockerfile:40`
```
COPY _pull_dukascopy_incremental.py ./
```
File is actually at `scripts/debug/_pull_dukascopy_incremental.py`.

### P2-7 backend/api endpoints not in __init__.py router

**File:** `backend/api/__init__.py`
Several API files appear unregistered:
- `backend/api/experiments.py` — NOT in ALL_ROUTERS
- `backend/api/logs.py` — NOT in ALL_ROUTERS
- `backend/api/ops.py` — NOT in ALL_ROUTERS

These endpoints exist but have no HTTP route — they're dead HTTP endpoints.

---

## 5. P3 — LOW (minor, informational)

### P3-1 Git state: 379 changes (188 modified, 191 untracked)

Working tree has heavy modifications. Many untracked files are data files (.duckdb, .jsonl, etc.) which is expected. The modified files include deletions of MT5-related code and additions of v4 pipeline components. Clean state recommended before next feature work.

### P3-2 tools/browser.py and tools/mcp_browser_server.py

These are development utilities — not part of the trading system. Should be in a separate dev-tools directory or deleted.

### P3-3 start-all.py

This is a standalone launcher script with 93 lines of bare except:pass. While it works for development, the error handling is fragile. Consider integrating into the backend app.py startup instead.

### P3-4 data/charts/ — 17 chart files, some from July 2025+

Several old JSON/JSONL files in `data/charts/` date back to before the v4 migration. These are historical records but could be archived.

---

## 6. Positives (What Works Well)

1. ✅ Factor Takeover v4 pipeline is fully wired end-to-end in `_process_tick_factor_pipeline`
2. ✅ Attribution tracking with cross-restart recovery (line 2083-2119)
3. ✅ Trailing stop integration with AWE composite_conviction
4. ✅ Decision audit log (DecisionLogStore) at every stage
5. ✅ Structured live trade logging (factor_trades.jsonl)
6. ✅ 8 scheduler jobs all registered with correct intervals (apscheduler mode)
7. ✅ RuntimeConfig has all attributes referenced by live_service
8. ✅ All 494 tests pass with zero failures
9. ✅ Pyramid position sizing rule (stronger signal required for add)
10. ✅ VaR gate + Kelly position sizing integration

---

## 7. Fix Priority Queue

### Immediate (this session):
1. 🔴 Fix dukascopy script path (`live_service.py:1003`)
2. 🔴 Delete or fix `scripts/daily_paper_dryrun.py`
3. 🔴 Delete or fix `scripts/ctrader_live_runner.py`

### This week:
4. ⚠️ Install apscheduler or fix cron parser (`scheduler.py:87-109`)
5. ⚠️ Update CLAUDE.md (job count, stale references)
6. ⚠️ Delete 20 orphan files

### Backlog:
7. Add logging to bare except:pass sites (93 sites)
8. Document HOUR_WEIGHTS gap or add 4-7 UTC coverage
9. Wire in execution/analytics.py, latency.py, market_impact.py
10. Update evolution_orchestrator.py comments

---

## Appendix A: Full File Count

```
Python files (excluding .git, .venv, frontend-v2, node_modules, __pycache__, tests): 196
Test files: 40
Orphan files (never imported): 50 (20 truly stale + 30 API routes/setup scripts)
Deleted but referenced: 2 files (strategies/multi_factor_m15.py, execution/mt5_bridge.py)
Untracked new files: 191 (mostly data/v4 pipeline components)
```

## Appendix B: Test Coverage Summary

```
tests/alpha/                                                 305 passed
  test_adaptive_weight_engine.py                              48
  test_attribution_engine.py                                  32
  test_attribution_stats_ext.py                                5
  test_awe_blend.py                                            8
  test_execution_gate.py                                      32
  test_factor_health_c2.py                                     6
  test_gp_classifier.py                                       40
  test_pipeline_e2e.py                                         4
  test_portfolio_compositor.py                                13
  test_signal_normalizer.py                                   26
  test_streaming_factor_engine.py                             10
  evaluation/test_attribution.py                              20
  evaluation/test_bootstrap_ci.py                             30
  evaluation/test_causal_check.py                             13
  evaluation/test_evaluation_context.py                       12
  evaluation/test_purged_walkforward.py                        6
  search/test_blend_search.py, test_elite_archive.py, etc.    12

tests/ (rest)                                                189 passed
  test_backend_*                                               8 files
  test_ctrader_live_runner.py, test_data_quality_gate.py, etc.
  test_live_service_*, test_loop_host_*, test_metrics_*
  test_runtime_config.py, test_scripts_refactor.py
  test_structured_log.py, test_sync_health.py
```
