# Quant Trading Web Console — Design Spec

**Date:** 2026-06-07
**Author:** Claude (brainstorming session)
**Status:** Approved by user
**Target:** Replace all terminal CLI operations with a Web-based console

---

## 0. Background & Goals

### 0.1 Current state
- Phase 1-5 + tuning fully complete (2026-06-06)
- ~9000+ lines of core Python (alpha/strategy/execution/risk/data)
- 35+ CLI scripts in `scripts/` + `main.py --mode {backtest|paper|live|dashboard}`
- Existing `monitor/dashboard.py` is 154 lines of minimal FastAPI + WebSocket monitor (read-only, no actions)

### 0.2 Goal
**Fully replace terminal operations with a Web console** — every CLI command, every config edit, every report view, every long-running task (backtest/discover/sync/tuning/A-B) must be accessible from a browser. Existing Python code is **reused via in-process import**, not rewritten.

### 0.3 Non-goals (explicitly out of scope for v1)
- Multi-tenant / SaaS deployment
- Native mobile apps (responsive web only)
- Brokers beyond MT5 / cTrader
- L2/L3 data feeds
- T3 governance (Bonferroni/CSCV)
- Historical K-line backfill UI (use `scripts/fetch_mt5_data.py` one-shot)
- Backtest parameter save/compare (reports on disk are enough)
- WebSocket cluster / Redis pub-sub (single-process sufficient)
- PWA / offline mode (responsive web is enough)

---

## 1. Architecture Overview

### 1.1 Directory layout

```
quant_trading/
├── (existing alpha/ strategy/ execution/ risk/ data/ db/ core/ factors/ live/ modules/ memory/ tests/ logs/ — unchanged)
│
├── backend/                         ★ NEW: FastAPI business layer
│   ├── __init__.py
│   ├── app.py                       # FastAPI app factory + lifespan + CORS + static mount
│   ├── main.py                      # uvicorn entry (python -m backend)
│   ├── deps.py                      # DI: shared DataStore / EventBus / StateContainer singletons
│   │
│   ├── api/                         # REST routes
│   │   ├── __init__.py              # unified router registration
│   │   ├── health.py                # GET /api/health
│   │   ├── market.py                # GET /api/market/bars
│   │   ├── backtest.py              # POST /api/backtest/run, GET /api/backtest/:id
│   │   ├── paper.py                 # POST /api/paper/start|stop, GET /api/paper/status, POST /api/paper/emergency-stop
│   │   ├── live.py                  # POST /api/live/start|stop|emergency-close
│   │   ├── factors.py               # GET /api/factors, GET /api/factors/:name
│   │   ├── factor_health.py         # POST /api/factor-health/run, GET /api/factor-health/latest
│   │   ├── discover.py              # POST /api/discover, GET /api/discover/:id
│   │   ├── sync.py                  # GET /api/sync/status, POST /api/sync/once|daemon/start|daemon/stop
│   │   ├── calibrator.py            # GET/POST /api/calibrator
│   │   ├── tuning.py                # POST /api/tuning/run, GET /api/tuning/:id
│   │   ├── reports.py               # GET /api/reports, GET /api/reports/:name
│   │   ├── config.py                # GET/PUT /api/config
│   │   ├── shadow.py                # GET /api/shadow, POST /api/shadow/promote|demote
│   │   ├── ab_test.py               # POST /api/ab/run
│   │   └── auth.py                  # RBAC hook stub (v1: noop, v2: JWT)
│   │
│   ├── ws/                          # WebSocket
│   │   ├── __init__.py
│   │   ├── endpoints.py             # /ws/state, /ws/alerts, /ws/jobs/:id, /ws/logs
│   │   └── manager.py               # ConnectionManager with rooms (job_id / alert_type)
│   │
│   ├── services/                    # Business wrappers — turn 35+ scripts/ into importable services
│   │   ├── __init__.py
│   │   ├── backtest_service.py
│   │   ├── paper_service.py
│   │   ├── live_service.py
│   │   ├── factor_health_service.py
│   │   ├── discover_service.py
│   │   ├── sync_service.py
│   │   ├── calibrator_service.py
│   │   ├── tuning_service.py
│   │   ├── shadow_service.py
│   │   ├── ab_service.py
│   │   └── report_service.py
│   │
│   ├── jobs/                        # Long-task management
│   │   ├── __init__.py
│   │   ├── manager.py               # JobManager: in-process queue + state
│   │   ├── runner.py                # asyncio.create_task wrapper, supports cancel
│   │   ├── state.py                 # in-memory dict (key=job_id → JobState)
│   │   └── progress.py              # ProgressCB type + callback injection contract
│   │
│   └── core/                        # Backend-specific core utilities
│       ├── __init__.py
│       ├── paths.py                 # project root / data dir / log dir
│       ├── settings.py              # pydantic Settings wrapping config/settings.yaml
│       └── logging.py               # loguru configuration (compatible with existing logging)
│
├── frontend/                        ★ NEW: Next.js 14
│   ├── package.json
│   ├── next.config.mjs              # dev rewrites /api/* → :8000; prod static output to backend/static
│   ├── tailwind.config.ts           # Bloomberg theme: bg=#0d1117, accent=#58a6ff, up=#3fb950, down=#f85149
│   ├── tsconfig.json                # strict mode
│   ├── components.json              # shadcn/ui config
│   │
│   ├── app/                         # App Router
│   │   ├── layout.tsx               # sidebar + topbar + WebSocket provider
│   │   ├── page.tsx                 # /  Overview
│   │   ├── loading.tsx
│   │   ├── globals.css
│   │   ├── (terminal)/              # Business route group
│   │   │   ├── market/page.tsx
│   │   │   ├── backtest/page.tsx
│   │   │   ├── paper/page.tsx
│   │   │   ├── live/page.tsx
│   │   │   ├── factors/page.tsx
│   │   │   ├── factors/[name]/page.tsx
│   │   │   ├── discover/page.tsx
│   │   │   ├── sync/page.tsx
│   │   │   ├── tuning/page.tsx
│   │   │   ├── calibrator/page.tsx
│   │   │   ├── shadow/page.tsx
│   │   │   ├── ab/page.tsx
│   │   │   ├── reports/page.tsx
│   │   │   ├── reports/[name]/page.tsx
│   │   │   ├── config/page.tsx
│   │   │   └── jobs/page.tsx
│   │   └── (auth)/                  # Reserved for future
│   │       └── login/page.tsx
│   │
│   ├── components/
│   │   ├── ui/                      # shadcn/ui generated
│   │   ├── layout/
│   │   │   ├── sidebar.tsx
│   │   │   ├── topbar.tsx
│   │   │   └── ws-provider.tsx
│   │   ├── charts/
│   │   │   ├── candlestick.tsx      # TradingView lightweight-charts
│   │   │   ├── equity-curve.tsx
│   │   │   ├── heatmap.tsx
│   │   │   ├── factor-health-radar.tsx
│   │   │   └── drawdown.tsx
│   │   ├── tables/
│   │   │   ├── factor-table.tsx
│   │   │   ├── trade-table.tsx
│   │   │   ├── job-table.tsx
│   │   │   └── shadow-table.tsx
│   │   ├── forms/
│   │   │   ├── backtest-form.tsx
│   │   │   ├── paper-form.tsx
│   │   │   ├── discover-form.tsx
│   │   │   ├── tuning-form.tsx
│   │   │   ├── config-editor.tsx
│   │   │   └── ab-form.tsx
│   │   └── feedback/
│   │       ├── job-progress.tsx
│   │       ├── alert-toast.tsx
│   │       └── confirm-dialog.tsx
│   │
│   ├── lib/
│   │   ├── api.ts                   # fetch wrapper + zod validation
│   │   ├── ws.ts                    # WS client + reconnect + room subscription
│   │   ├── store.ts                 # zustand: state snapshot / position / jobs
│   │   ├── format.ts                # number/percentage/time format
│   │   ├── i18n.ts                  # UI text dictionary (zh-CN default)
│   │   └── types.ts                 # shared TS types (generated from openapi.json)
│   │
│   └── public/
│       └── favicon.svg
│
├── start.bat                        ★ NEW: one-shot launch (backend :8000 + frontend :3000)
├── start.sh                         # Unix version
├── start-prod.bat                   # production: next build + uvicorn static serve
├── stop.bat / stop.sh               # graceful shutdown
├── README_WEB.md                    ★ NEW: web UI user doc
│
├── (scripts/ refactored: CLI entry preserved + importable service function added)
│
├── tests/                           ★ NEW: full-stack tests
│   ├── test_backend_api.py
│   ├── test_backend_jobs.py
│   ├── test_backend_services.py
│   ├── test_scripts_refactor.py     # CRITICAL: guard 35+ scripts still work as CLI
│   ├── test_frontend_components.test.ts
│   └── e2e/
│       └── critical_paths.spec.ts   # Playwright
│
└── docs/
    ├── superpowers/
    │   └── specs/
    │       └── 2026-06-07-quant-web-console-design.md   ★ THIS FILE
    └── (existing docs unchanged)
```

### 1.2 Key boundaries

| Boundary | Type | Communication | Notes |
|---|---|---|---|
| Frontend ↔ Backend | HTTP/JSON + WS | REST `/api/*` + `/ws/*` | **Single boundary** — frontend never touches db/sqlite files directly |
| Backend ↔ existing alpha/strategy/execution/... | in-process Python import | direct import + service function call | Zero network overhead, reuse 9000+ lines |
| Backend ↔ SQLite db | in-process | sqlite3 / DataStore existing API | db file stays at `data/market_data.db`, untouched |
| Backend ↔ MT5/cTrader terminal | out-of-process IPC | subprocess (MT5) + .env (cTrader) | reuse `execution/mt5_bridge.py` / `ctrader_bridge.py` |
| Backend ↔ long tasks (backtest/discover/sync) | in-process | `JobManager` + asyncio task | progress pushed via `/ws/jobs/:id` |
| Start script ↔ frontend+backend | same machine | `start.bat` uses `start` + `cmd /c` to run both | production: `next build` static + uvicorn mount |

### 1.3 scripts/ refactor strategy

**Goal**: every `scripts/xxx.py` supports both `python scripts/xxx.py --flag` (CLI) and `from backend.services.xxx_service import run_xxx()` (service).

**Refactor pattern**:
```python
# scripts/discover_factors.py (refactored)
def run_discovery(n_candidates, top_k, forward_periods, auto_register,
                  progress_cb=None) -> DiscoveryResult:
    """Core logic — importable by backend"""
    # ... existing logic
    if progress_cb: progress_cb(step="eval", pct=50, msg=f"evaluated {n}/{total}")
    return result

def main():
    """CLI entry — argparse preserved"""
    parser = argparse.ArgumentParser()
    # ... existing args
    args = parser.parse_args()
    result = run_discovery(args.n_candidates, args.top_k, args.forward_periods, args.auto_register)
    # ... print

if __name__ == "__main__":
    main()
```

**Risk**: a few scripts have side effects in `if __name__ == "__main__":` blocks (e.g., `sys.exit()`). Audit all 35+ scripts, add guards per script.

---

## 2. Core Component Contracts

### 2.1 Jobs long-task system

```python
# backend/jobs/state.py
@dataclass
class JobState:
    id: str                          # uuid4 hex
    kind: str                        # "backtest" | "discover" | "factor_health" | "sync" | "tuning" | "ab" | "shadow_promote"
    status: Literal["queued","running","done","error","cancelled"]
    progress_pct: float              # 0.0 ~ 100.0
    current_step: str                # "loading bars" | "evaluating 234/1000" | ...
    started_at: datetime
    finished_at: datetime | None
    result: dict | None              # populated on done: PnL / report path / new factor list
    error: str | None
    log_tail: list[str]              # last 50 log lines for frontend to pull

# backend/jobs/manager.py
class JobManager:
    def submit(self, kind: str, params: dict, fn: Callable[[ProgressCB], Any]) -> JobState
    def get(self, job_id: str) -> JobState
    def list(self, kind: str | None = None, status: str | None = None) -> list[JobState]
    def cancel(self, job_id: str) -> bool
    def stream(self, job_id: str) -> AsyncIterator[JobState]   # for SSE/WS push

ProgressCB = Callable[[str, float, str], None]
# step: "loading" | "eval" | "register" | ...
# pct:  0.0 ~ 100.0
# msg:  "evaluated 234/1000 candidates"
```

**Constraints**:
- **In-process queue** (`asyncio.Queue` + `asyncio.create_task`); v1 does NOT use Redis/Celery.
- **No persistence** — process restart clears in-memory state. v1 accepts this limit. (Reserved: `data/charts/jobs.jsonl` append-only for v2.)
- **Cancellation**: `asyncio.CancelledError` propagated to service function. Numba/pandas long loops **must** periodically `await asyncio.sleep(0)` to yield control — otherwise cancel is blocked.
- **WebSocket rooms**: `/ws/jobs/:id` subscribes to a single job's progress; `/ws/state` broadcasts global snapshot (1s).

### 2.2 WebSocket endpoints

| Path | Direction | Frequency | Payload |
|---|---|---|---|
| `GET /ws/state` | server→client | 1s tick | `{equity, balance, pnl, position, daily, risk, factor_health_summary}` |
| `GET /ws/alerts` | server→client | event-triggered | `{level, source, msg, ts}` |
| `GET /ws/jobs/:id` | server→client | on state change | `{job_id, status, progress_pct, current_step, log_tail}` |
| `GET /ws/logs` | server→client | loguru tail (INFO+) | `{ts, level, source, msg}` |

**Frontend `lib/ws.ts`**:
- **One physical WebSocket connection** + client subscribes to multiple "channels" (state/alerts/logs). Channels are rooms in backend `ConnectionManager`.
- **Reconnect**: exponential backoff (1s/2s/4s/8s, max 30s); auto-resubscribe on reconnect.
- **Disconnect indicator**: topbar shows "⚠ Realtime disconnected, reconnecting..." (v1 dashboard.py lacks this; v1 of new web must include it).

### 2.3 Core service contracts

| Service | Entry function | Key params | Returns / side effects | Depends on |
|---|---|---|---|---|
| `BacktestService` | `run(args, progress_cb) -> JobResult` | symbol, tf, sl/tp/cd scan range, risk_pct, kelly | calls `main.run_backtest()` core loop, writes `data/charts/backtest_*.txt` | `core.event_bus`, `data.store.DataStore` |
| `PaperService` | `start(config) / stop() / status() / emergency_stop()` | enable_router/scheduler/calibrator/event_filter/shadow_factors/retrain (8 flags) | singleton `PaperTrader` background task; reads `core.state` | `execution.paper_trader.PaperTrader` |
| `LiveService` | `start(config) / stop() / emergency_close(symbol?)` | broker (mt5/ctrader), risk overrides | calls `mt5_bridge.fetch_history` / `close_all_positions`; balance=0 blocks v1 | `execution.mt5_bridge`, `execution.ctrader_bridge` |
| `FactorHealthService` | `run(bar_csv?, progress_cb) -> HealthReport` | threshold, dimension weights | calls `alpha/factor_health.py`; writes `data/charts/factor_health_report.{txt,json}` | `alpha.factor_health`, `data.store` |
| `DiscoverService` | `run(n_candidates, top_k, forward_periods, auto_register, engine, progress_cb) -> DiscoveryResult` | engine="gp"\|"random", pop, gen, DSL expression preview | calls `alpha/factor_search_gp.py` or `alpha/factor_search.py`; new factors to `alpha/registry.jsonl` | `alpha.factor_search_gp`, `alpha.factor_discovery` |
| `SyncService` | `run_once(timeframes, type) / start_daemon(interval_s) / stop_daemon() / status()` | timeframes, full/incremental | calls `data/live_sync/orchestrator.py`; writes db + `live_sync_status.json` | `data.live_sync.orchestrator` |
| `TuningService` | `run(risk_pct_grid, cb_pct_grid, progress_cb)` | grid parameters | calls `scripts/tune_risk_params.py` core loop; writes `data/charts/tune_*.txt` | `scripts.tune_risk_params` (refactored) |
| `CalibratorService` | `load() / save(buckets) / status()` | path | read/write `data/charts/calibrator_bucket.json` | `alpha.probability_calibrator` |
| `ShadowService` | `list() / promote(name) / demote(name)` | shadow factor name | calls `alpha/persistent_registry.py` | `alpha.persistent_registry` |
| `ABService` | `run(config, progress_cb) -> ABResult` | A/B path selection | calls `scripts/p1_e_ab_test.py` core; writes `data/charts/p1_e_ab_report.txt` | `scripts.p1_e_ab_test` (refactored) |
| `ReportService` | `list() / read(name) / image(name)` | report name | lists `data/charts/`; reads .txt/.json/.png | filesystem only |

### 2.4 Key data contracts (TypeScript)

```typescript
// frontend/lib/types.ts
export interface StateSnapshot {
  equity: number;
  balance: number;
  pnl_today: number;
  position: { dir: "LONG"|"SHORT"|"FLAT"; entry: number; size: number; unrealized: number };
  daily: { trades: number; win: number; loss: number; pnl: number; drawdown_pct: number };
  risk: { circuit_breaker: boolean; consecutive_loss: number; max_daily_loss_pct: number };
  factor_health_summary: { healthy: number; watch: number; decaying: number };
  server_time: string;  // ISO
}

export interface FactorHealthEntry {
  name: string;
  status: "HEALTHY"|"WATCH"|"DECAYING";
  score: number;
  abs_ic: number;
  stability: number;
  decay: number;
  regime_consistency: number;
  independence: number;
}

export interface DiscoveryResult {
  candidates_total: number;
  valid: number;
  promoted: number;
  top_factors: Array<{ name: string; expr: string; ic: number; cv_score: number }>;
  report_path: string;
  job_id: string;
}
```

### 2.5 Error handling strategy

| Error type | HTTP status | Frontend handling |
|---|---|---|
| Param validation (zod/pydantic) | 422 | form red border + error details |
| Business exception (already running / balance=0 / MT5 disconnected) | 400 + `error_code` | toast + action suggestion |
| Internal exception (code bug) | 500 + `error_id` (uuid) | toast + error_id logged |
| Long task failure | `job.status="error"` | Jobs page red + retry button |
| WebSocket disconnect | — | topbar yellow + 5s reconnect |
| Emergency operation secondary confirm | client-side dialog | emergency close / paper stop requires 2nd confirm |

**Key principle**: backend never silently swallows errors. All service functions raise; router unified try/except converts to HTTP. Frontend doesn't trust HTTP 200 alone — checks `data.ok` secondarily.

---

## 3. Frontend Page Structure & Key Interactions

### 3.1 Global layout

```
┌─────────────────────────────────────────────────────────────────────┐
│ Topbar (h=56)                                                       │
│  ⚡ Quant  │  XAUUSD+ 4529.12 ↑0.8%  │  Equity 1593.85  │  ● live  │
│  ←  asset info on left           real-time PnL/Equity in center    status badge on right
├──────────┬──────────────────────────────────────────────────────────┤
│ Sidebar  │  Main Content (max-w=1600, mx-auto, p-6)                 │
│ (w=240)  │                                                          │
│ 🏠 总览  │                                                          │
│ 📊 K线   │                                                          │
│ ▶ 回测   │                                                          │
│ ▶ 模拟盘 │                                                          │
│ ▶ 实盘   │                                                          │
│ 🧪 因子  │                                                          │
│ 🔍 发现  │                                                          │
│ 🔄 同步  │                                                          │
│ 🎛 调参  │                                                          │
│ 📐 校准  │                                                          │
│ 👁 影子  │                                                          │
│ ⚖ A/B   │                                                          │
│ 📑 报告  │                                                          │
│ ⚙ 配置  │                                                          │
│ 📋 任务  │                                                          │
└──────────┴──────────────────────────────────────────────────────────┘
   dark bg=#0d1117
   active item blue highlight + 3px left bar
```

**Overview `/`**: 6 key cards (Equity curve 24h / current position / today PnL / risk status / factor health summary / recent alerts) + 1 K-line thumbnail. Click any card → detail page.

### 3.2 Key page interactions

#### 3.2.1 Paper `/paper`

- **State card**: [已停止 / 运行中 PID 1234 / 启动于 14:32:01]
- **Buttons**: [▶ 启动]  [⏹ 停止]  [⏮ 紧急停止]
- **Config collapsible**: 8 checkboxes (MAB 路由 / 调度器 / 评分器 / 校准 / 因子监控 / 告警 / 重训 / 事件过滤) + symbol/tf dropdowns + risk overrides
- **Equity Curve** (lightweight-charts area, 24h default) + time-range buttons (1h/4h/1d/1w/all)
- **Position card** (or "FLAT — no position")
- **Daily stats** row

- **2nd confirm**: `⏮ 紧急停止` requires typing "emergency" + 5s countdown
- **Pre-fill**: defaults from last `main.py --mode paper ...` invocation
- **Realtime**: `/ws/state` 1s tick; number tween 200ms ease-out

#### 3.2.2 Factor health `/factors`

- **Header**: [▶ 重新评估]  [导出报告]  threshold [0.04]  bar_count [50000]
- **Last run**: timestamp + duration
- **Stats row**: ●2 HEALTHY  ●45 WATCH  ●18 DECAYING   共 65 因子
- **Sortable + filterable table** with 5-dim scores
- **Row click → expand**: factor detail (IC time series + regime consistency radar + 5-dim breakdown)
- **Re-evaluate** uses Jobs system (5-30s); toast on completion

#### 3.2.3 Discover `/discover`

- **Engine radio**: ( ) GP 推荐   ( ) Random
- **GP params**: 种群 [100]  世代 [20]  crossover [0.7]  mutate [0.2]
- **Candidates** [1000]  top-k [50]  forward_periods [1, 5, 20]
- **Auto-register as shadow** checkbox
- **DSL preview**: monaco/codemirror with live IC eval mini-chart
- **Run button** + "View history (8 runs)" link
- **Live progress** during run: bar + "evaluating 234/1000" + [Cancel]
- **Top-10** running list as they emerge

#### 3.2.4 Market `/market`

- **TradingView Lightweight Charts** (candlestick)
- **Timeframe buttons**: M5/M15/M30/H1/H4/D1
- **Indicator overlays**: MA(20/50/200) / BB(20,2) / MACD / RSI / 成交量
- **Performance**: 50K+ bar via LWC virtualization; first paint 500 bars, scroll-load
- **Right drawer** (collapsible): 实时盘口 (placeholder for v1, MT5 not connected)

#### 3.2.5 Jobs `/jobs`

- **Filter**: type [all▼]  status [all▼]
- **Table**: ID / type / status / progress% / current step / started_at
- **Action**: [Cancel] button only on running rows
- **5s auto-refresh** (polling — jobs list in memory, no WS)
- **Row click → detail**: log tail (last 50 lines) + result/error + retry button

### 3.3 Reuse vs rebuild principle

| Module | Reuse existing | Build new |
|---|---|---|
| K-line renderer | — | TradingView LWC wrapper (new) |
| Factor IC time series / Equity | — | LWC area (new) |
| 5-dim radar | — | ECharts (new) |
| Heatmap | — | ECharts (new) |
| Tables | — | shadcn Table + TanStack Table (new) |
| Forms | — | react-hook-form + zod (new) |
| Emergency confirm dialog | — | shadcn Dialog + 5s countdown (new) |
| Business logic | 100% reuse (backend import) | — |
| K-line data source | — | `GET /api/market/bars?from=&to=` (new); backend calls existing `DataStore.load_bars()` |

### 3.4 Mobile responsive

- v1: desktop-first (`lg:` breakpoint and above)
- Overview page: shrink to `md:` (cards stack to 1 column)
- Paper / factor pages: not optimized for mobile (mobile primarily uses Telegram alerts + PWA bookmark, no hard requirement)
- **Reserved**: shadcn/ui responsive components used; future mobile nav doesn't require rewrite

### 3.5 i18n

- v1: Chinese-first (user's primary language), all UI text goes through `frontend/lib/i18n.ts` dictionary:
```ts
const dict = {
  "总览": "Overview",
  "Equity": "Equity",
  "持仓": "Position",
  // ...
};
// current zh-CN = full Chinese
// future en-US additions don't need component changes
```

### 3.6 Bloomberg dark theme

- Background `#0d1117` (GitHub dark) / card `#161b22` / border `#30363d`
- Primary text `#c9d1d9` / muted `#8b949e`
- Accent blue `#58a6ff` / up green `#3fb950` / down red `#f85149` / warning yellow `#d2991d`
- Font: `ui-sans-serif, system-ui` (system-first) + numbers use `font-feature-settings: "tnum"` (tabular nums, PnL doesn't jitter)
- Table density: `text-sm` (13px), tight row height
- No decorative shadows; border-based separation

---

## 4. Data Flow & Key API Behavior

### 4.1 End-to-end flow (start paper example)

```
[User] opens /paper
   ↓
[Frontend] reads store → paper.status = "stopped"
   ↓
[User] toggles enable_router=true, clicks [▶ 启动]
   ↓
[Frontend] POST /api/paper/start
        body: { symbol, tf, enable_router, ..., risk_pct: 1.0 }
   ↓
[Backend] PaperService.start(config)
   ↓
[Backend] instantiates PaperTrader (or mab_paper_runner)
   ↓
[Backend] asyncio.create_task(paper_main_loop(state))
   ↓
[Backend] returns { status: "started", pid, started_at }
   ↓
[Frontend] 200 → state card switches to "running" + start animation
   ↓
[WS]  /ws/state 1s tick snapshot { equity, position, ... }
   ↓
[Frontend] Zustand store updates → all subscribed components re-render
   ↓
[Event] PaperTrader trade close → EventBus emits trade_close
   ↓
[Backend] EventBus subscriber → ConnectionManager.broadcast("alerts", alert)
   ↓
[WS]  /ws/alerts pushes { level, source: "big_trade", msg: "Closed LONG +$125" }
   ↓
[Frontend] AlertToast top-right 5s
```

### 4.2 Key API endpoint contracts (11 groups)

#### 4.2.1 Health check
```
GET /api/health
→ 200 { status: "ok", db: "connected", mt5: "unknown"|"connected"|"disconnected", server_time }
```

#### 4.2.2 Market data
```
GET /api/market/bars?symbol=XAUUSD%2B&timeframe=M15&from=1749000000&to=1749100000
→ 200 { bars: [{ t, o, h, l, c, v, spread }, ...], total: 50000, range: { from, to } }
→ 422 { error: "invalid_timeframe", details: "timeframe must be one of M5,M15,M30,H1,H4,D1" }
```

#### 4.2.3 Paper
```
POST /api/paper/start
  body: {
    symbol, timeframe,
    use_router, use_scheduler, use_calibrator,
    use_meta_monitor, use_factor_monitor, use_alerter,
    use_retrain, retrain_every_n, use_event_filter,
    risk_per_trade_pct, max_daily_loss_pct, single_risk_usd,
    include_shadow_factors, shadow_top_k
  }
→ 200 { status: "started", started_at }
→ 400 { error: "already_running", current_pid }
→ 422 { error: "invalid_config" }

GET /api/paper/status
→ 200 { status: "running"|"stopped"|"starting"|"stopping", started_at, pid, config, last_error? }

POST /api/paper/stop
  body: { close_positions: false }
→ 200 { status: "stopped", closed_trades: 0 }

POST /api/paper/emergency-stop
  body: {}
→ 200 { status: "stopped", emergency: true, closed_positions: N }
# requires 2nd confirm; backend re-validates (defense in depth)
```

#### 4.2.4 Factor health
```
POST /api/factor-health/run
  body: { threshold: 0.04, bar_count: 50000, sync_run: false }
  # sync_run=true blocks; false returns job_id
→ 200 { job_id }                    # async
→ 200 { report: { healthy, watch, decaying, factors: [...] } }  # sync
→ 202 { job_id, status_url: "/api/jobs/{id}" }

GET /api/factor-health/latest
→ 200 { report: {...}, generated_at, report_path }
→ 404 { error: "no_report_yet" }
```

#### 4.2.5 Discover
```
POST /api/discover
  body: { engine, n_candidates, top_k, forward_periods, auto_register, gp_pop?, gp_gen? }
→ 202 { job_id }

GET /api/discover/{job_id}
→ 200 { job: { status, progress_pct, current_step, result?, error? } }
```

#### 4.2.6 Sync
```
GET /api/sync/status
→ 200 {
    per_tf: { M5: { last_sync_utc, total_bars, latest_bar_utc, status }, ... },
    daemon_running, daemon_pid
  }

POST /api/sync/once
  body: { timeframes, type: "incremental"|"full" }
→ 200 { inserted: { M15: 12, H1: 2, D1: 0 }, skipped: {...} }
→ 500 { error: "mt5_unavailable", reason: "IPC pipe timeout -10005" }   # T16 known block

POST /api/sync/daemon/start
  body: { interval_seconds, timeframes }
→ 200 { daemon_pid, started_at }
POST /api/sync/daemon/stop
→ 200 { stopped: true, last_run: {...} }
```

#### 4.2.7 Tuning
```
POST /api/tuning/run
  body: { risk_pct_grid, cb_pct_grid, n_bars }
→ 202 { job_id }
GET /api/tuning/{job_id}
→ 200 { job: { ..., result?: { best: { risk_pct, cb_pct, pnl, sharpe, trades, dd } } } }
```

#### 4.2.8 Calibrator
```
GET  /api/calibrator
  → 200 { path, exists, buckets?, platt?, last_modified }
POST /api/calibrator/save
  body: { buckets }   # or { source: "regenerate" }
POST /api/calibrator/load
  body: { path? }
  → 200 { buckets, platt }
```

#### 4.2.9 Config
```
GET /api/config
  → 200 { yaml: "<settings.yaml text>", parsed: { ... } }
PUT /api/config
  body: { yaml: "..." }
  → 200 { ok: true, changes: ["risk.max_daily_loss_pct: 5.0 → 10.0"] }
  → 422 { error: "yaml_parse_error", line, column, msg }
# PUT only writes file; needs paper restart to take effect (in response)
```

#### 4.2.10 Shadow
```
GET  /api/shadow
  → 200 { shadows: [{ name, expr, ic, cv_score, created_at, status }] }
POST /api/shadow/promote  body: { name }  → 200 { ok, new_status: "active" }
POST /api/shadow/demote   body: { name }  → 200 { ok }
```

#### 4.2.11 Reports
```
GET /api/reports?kind=txt|json|png|all
  → 200 { reports: [{ name, path, size, kind, modified_at }] }
GET /api/reports/{name}
  → 200 { kind: "txt"|"json"|"png", content?: "<text>", data_url?: "data:image/png;base64,..." }
  → 404 { error: "not_found" }
# png returns base64 data_url; txt < 1MB inline (truncate + flag if larger)
```

### 4.3 Key design constraints

#### 4.3.1 Long-task progress push

```
[Service.run(progress_cb)]
   |
   |-- progress_cb("loading_bars", 10, "loading 50000 bars from db")
   |   ↓
   |  JobManager receives callback
   |   ↓
   |  ConnectionManager.broadcast(f"jobs:{job_id}", progress_event)
   |   ↓
   |  /ws/jobs/{job_id} pushes { job_id, progress_pct, current_step, ts }
   |
   |-- 1 iteration of compute loop
   |-- periodic (every N iterations or 0.5s) → progress_cb("eval", pct, msg)
   |   ↑ KEY: long loops must periodically `await asyncio.sleep(0)` to yield control (else cancel blocked)
   |
[Service.run] complete → write JobState.result
   ↓
[JobManager] pushes "done" event + result
```

- Frontend `JobProgress` component subscribes to `ws://host/ws/jobs/{job_id}`
- Close dialog doesn't cancel subscription; continue receiving until "done" / "error" written to toast
- Multiple parallel jobs: each job gets its own WS connection (lightweight; backend isolates by job_id room)

#### 4.3.2 WebSocket reconnect + state recovery

- Backend pushes latest snapshot immediately on connect (state machine)
- Frontend exponential backoff (1s/2s/4s/8s, max 30s)
- Topbar yellow "⚠ Realtime disconnected (reconnecting...)" during outage
- Realtime cards show `--` gray; HTTP `/api/paper/status` polling as fallback
- Reconnect: topbar green "● live"; card numbers tween 200ms slide-in

#### 4.3.3 Emergency operation 2nd confirm (backend defense)

- Client: dialog asks for "emergency" text + 5s countdown
- Server: `POST /api/paper/emergency-stop` requires `X-Confirm: emergency` header in v1. Without it, returns 403. This is v1's defense-in-depth (single-user local; token system comes in v2 with auth).

#### 4.3.4 Large data / report streaming

- K-line: 50K bar ≈ 5MB JSON, gzipped ~500KB, direct 200 OK
- Report .png: base64 inline, single image < 5MB
- Report .txt: single file < 1MB full; >1MB truncate + "see file directly" notice
- Report directory: `data/charts/` 30+ files, list endpoint returns metadata only

#### 4.3.5 CORS / Auth (production)

- Dev: `next dev` `next.config.mjs` `rewrites` proxy `/api/*` → `localhost:8000`, same-origin, no CORS
- Prod: `next build` outputs to `backend/static/`; FastAPI `StaticFiles` mount at `/` + `/{any:path}` SPA fallback
- Auth (reserved): `backend/api/auth.py` provides `get_current_user()` dependency stub; v1 fixed local password, v2 add JWT/OAuth
- Multi-user (reserved): all `/api/*` use `Depends(get_current_user)`; v1 returns hardcoded "zhu"; v2 add user_id isolation

### 4.4 Performance baseline (targets)

| Operation | Target | Notes |
|---|---|---|
| Overview TTI | < 1.5s | static assets + 1 `/api/state` (cached) + 1 `/api/factor-health/latest` |
| K-line 50K render | < 500ms | LWC virtualization + first paint 500 |
| WS state 1s frame | < 50ms backend | pure in-memory read + serialize |
| Backtest progress latency | < 1s | progress_cb → WS push |
| GP discover 100×10 | 17s (same as CLI) | reuse `factor_search_gp`, no speedup |
| Backend cold start | < 3s | uvicorn + 0 db warmup |

---

## 5. Testing Strategy

### 5.1 Test pyramid

```
                ┌─────────────┐
                │   E2E       │  1 suite (Playwright critical paths)
                │   (slow/fragile)  │
            ┌───┴─────────────┴───┐
            │   Integration        │  FastAPI TestClient
            │   (medium)           │  + real db + real services
        ┌───┴─────────────────────┴───┐
        │   Unit                       │  service wrappers + JobManager
        │   (fast/many)                │  + progress_cb injection contract
    ┌───┴─────────────────────────────┴───┐
    │   Static / type                     │  TypeScript strict + mypy
    │   (foundation)                      │  + pydantic boundary validation
    └─────────────────────────────────────┘
```

### 5.2 Backend tests (`tests/`)

#### 5.2.1 Unit (fast, ms)

| Module | Test | Tool |
|---|---|---|
| `backend/jobs/state.py` | JobState state machine | pytest |
| `backend/jobs/manager.py` | submit/cancel/query; 100 concurrent jobs isolated; cancel long loop exits | pytest + asyncio |
| `backend/services/*.py` | each service importable + bare-callable (progress_cb=None) | pytest |
| `backend/api/*.py` | router param validation (zod/pydantic 422) | pytest + TestClient |
| `backend/core/paths.py` | project root resolves correctly from subdir | pytest |

**Critical pattern**: `progress_cb` injection contract
```python
def test_discover_service_emits_progress():
    events = []
    cb = lambda step, pct, msg: events.append((step, pct, msg))
    discover_service.run(n_candidates=10, ..., progress_cb=cb)
    assert len(events) >= 3
    assert all(0 <= p <= 100 for _, p, _ in events)
    assert events[-1][0] == "done"
```

#### 5.2.2 Integration (medium, 1-10s)

| Test | Coverage | Tool |
|---|---|---|
| `test_api_market.py` | GET /api/market/bars 200, data structure, range | TestClient + real db |
| `test_api_paper.py` | start → running → stop → stopped | TestClient + real paper |
| `test_api_factor_health.py` | 1 run, verify healthy/watch/decaying distribution | TestClient + 50K bar |
| `test_api_sync.py` | mock MT5, verify once mode inserted | TestClient + mock |
| `test_api_jobs.py` | submit backtest, poll until done, verify result | TestClient + real backtest |
| `test_ws_state.py` | mock client connects /ws/state, receives 1s snapshot | websockets + TestClient |
| `test_scripts_refactor.py` | **CRITICAL**: 35+ scripts refactored, both CLI + import work | subprocess + importlib |

#### 5.2.3 Performance / smoke

| Test | Target |
|---|---|
| `test_perf_market.py` | 50K bar response < 200ms (loc) |
| `test_perf_ws.py` | WS state frame < 50ms |
| `test_smoke.py` | `start.bat` launches, all pages GET 200 (curl/Playwright) |

### 5.3 Frontend tests

| Test | Tool | Coverage |
|---|---|---|
| Component unit | Vitest + React Testing Library | Button/Card/Table/Form render + interact |
| Utility | Vitest | format.ts (number/time/percentage) |
| WS client | Vitest | reconnect, subscribe/unsubscribe |
| Type guards | `tsc --noEmit` | TypeScript strict |

#### 5.3.1 E2E (Playwright, 1 suite)

Only **critical paths** (avoid test bloat):
1. **Start → stop paper**: open /paper, start, 3s wait, see "running", stop, see "stopped"
2. **Factor health report**: open /factors, re-evaluate, 30s wait, see "2 HEALTHY 45 WATCH 18 DECAYING"
3. **Config edit**: open /config, change max_daily_loss_pct 5→10, save, see toast "config saved"
4. **Emergency stop 2nd confirm**: open /paper (running), click emergency stop, see dialog requiring "emergency"

E2E uses TestClient backend + headless Chromium, total < 2 min.

### 5.4 Definition of Done

- ✅ All 35+ scripts refactored to dual-mode (CLI + service); `test_scripts_refactor.py` 100% pass
- ✅ Backend unit coverage ≥ 70% (service + jobs modules)
- ✅ All 11 services have TestClient tests
- ✅ All 4 WS endpoints tested
- ✅ Frontend `tsc --noEmit` 0 errors, strict mode
- ✅ 4 critical E2E paths pass
- ✅ `start.bat` one-shot launch, browser shows /page within 30s

---

## 6. Deployment & Launch

### 6.1 Launch scripts

#### `start.bat` (Windows primary)
```bat
@echo off
setlocal
set PROJECT_ROOT=%~dp0
set PYTHON=C:\Users\zhu\AppData\Local\Programs\Python\Python312\python.exe

echo === Starting backend FastAPI (port 8000) ===
start "Quant Backend" /min cmd /c "cd /d %PROJECT_ROOT% && %PYTHON% -m backend"
timeout /t 3 /nobreak >nul

echo === Starting frontend Next.js (port 3000) ===
cd /d %PROJECT_ROOT%frontend
call npm run dev

echo === Frontend exited, backend window continues ===
endlocal
```

#### `start.sh` (Unix backup)
```bash
#!/usr/bin/env bash
set -e
PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_ROOT"
python3.12 -m backend &
BACKEND_PID=$!
echo "Backend PID: $BACKEND_PID"
sleep 3
cd frontend
npm run dev
trap "kill $BACKEND_PID 2>/dev/null" EXIT
```

#### `stop.bat`
```bat
@echo off
taskkill /FI "WINDOWTITLE eq Quant Backend*" /T /F
```

### 6.2 Launch modes

| Mode | Command | Use |
|---|---|---|
| **Dev** (default) | `start.bat` | backend debug + Next.js HMR |
| **Prod** | `start-prod.bat` | `next build` static + uvicorn static serve |
| **Backend only** | `python -m backend --port 8000` | API only (paired with remote frontend) |
| **Frontend only** | `cd frontend && npm run dev` | UI only (develop against remote API) |

### 6.3 Ports & process management

- Backend: `0.0.0.0:8000`
- Frontend dev: `0.0.0.0:3000`
- Prod: `0.0.0.0:8000` (FastAPI mounts `frontend/out/` static)
- Processes: backend single-process single-instance; frontend dev = Next.js Node.js subprocess
- Conflict detection: pre-start port check (`lsof` / Windows `netstat`); occupied → error exit
- Logs:
  - Backend: `logs/backend.log` (loguru)
  - Frontend: `frontend/.next/trace` (Next.js)
  - Existing `logs/quant.log` preserved (not redirected)

### 6.4 Environment variables

```env
# .env (new, gitignore)
QUANT_PROJECT_ROOT=C:\Users\zhu\quant_trading
QUANT_LOG_LEVEL=INFO
QUANT_API_PORT=8000
QUANT_WEB_PORT=3000
QUANT_DB_PATH=data/market_data.db
# existing cTrader/MT5 configs stay in .env
```

Backend uses `pydantic-settings` for `.env`; v1 defaults work without `.env`.

### 6.5 Persistence vs ephemeral

| Data | Location | Persistent | Backup |
|---|---|---|---|
| SQLite db | `data/market_data.db` | ✅ existing | existing (not web's job) |
| Factor reports | `data/charts/*.txt\|*.json` | ✅ existing | existing |
| Report images | `data/charts/*.png` | ✅ existing | existing |
| Calibrator | `data/charts/calibrator_bucket.json` | ✅ existing | existing |
| Shadow factor log | `data/charts/shadow_factors.jsonl` | ✅ existing | existing |
| **Jobs state** | memory | ❌ restart-cleared | accepted (reserved jsonl) |
| **WS rooms** | memory | ❌ restart-cleared | accepted |
| **Frontend user prefs** (theme/col width/time range) | localStorage | browser-local | accepted |

### 6.6 Dependencies

#### Backend (add to `requirements.txt`)
```
fastapi>=0.110
uvicorn[standard]>=0.27
pydantic>=2.6
pydantic-settings>=2.2
python-multipart>=0.0.9
websockets>=12.0          # tests
httpx>=0.27               # TestClient
```
(other alpha/execution/data deps unchanged)

#### Frontend (`frontend/package.json` new)
```
next: 14.2.x
react: 18.3.x
react-dom: 18.3.x
typescript: 5.4.x
tailwindcss: 3.4.x
shadcn/ui (CLI generated)
zustand: 4.5.x
react-hook-form: 7.51.x
zod: 3.23.x
@tanstack/react-table: 8.x
lightweight-charts: 4.2.x     # TradingView
echarts: 5.5.x
echarts-for-react: 3.0.x
@radix-ui/* (shadcn deps)
lucide-react (icons)
@hookform/resolvers: 3.x
date-fns: 3.x
clsx + tailwind-merge: standard
vitest: 1.x
@testing-library/react: 15.x
@playwright/test: 1.44.x
```

### 6.7 First-time install (`README_WEB.md` new)

```bash
# 1. Install backend deps
pip install -r requirements.txt

# 2. Install frontend deps (first time)
cd frontend
npm install
cd ..

# 3. Launch (dev mode)
start.bat
# Browser auto-opens http://localhost:3000
```

---

## 7. Migration / Risk / Future

### 7.1 Migration path (zero-downtime switchover)

| Phase | Status | Notes |
|---|---|---|
| **Phase 0**: existing CLI paths unchanged | ✅ | all `python main.py ...` keep working |
| **Phase 1**: backend skeleton + 1 service (backtest) + 1 WS (/ws/state) | 1 week | verify import boundary + WS comms |
| **Phase 2**: 5 core services + overview page | 2 weeks | user can run backtest / view status |
| **Phase 3**: all 11 services + 14 pages | 4-6 weeks | full feature |
| **Phase 4**: scripts fully refactored + E2E + perf | 2 weeks | old CLI can be deprecated (kept as fallback) |
| **Phase 5**: productionize (auth/single-port/multi-user scaffold) | 1-2 weeks | future extension |

**Key**: phases 0-3 run new web UI alongside old CLI; user can pick either, **either side breaking doesn't affect the other**.

### 7.2 Risk & mitigation

| Risk | Impact | Prob | Mitigation |
|---|---|---|---|
| **scripts refactor breaks CLI behavior** | 35+ scripts fail, quant main flow broken | Med | `test_scripts_refactor.py` 100% CLI entry coverage; before/after diff compares PnL/output |
| **Numba/pandas loops don't yield, cancel fails** | user clicks cancel, job hangs, backend restart needed | High | service functions must periodically `await asyncio.sleep(0)`; unit test verifies cancel response < 1s |
| **WebSocket disconnect → state misalign** | user sees wrong PnL | Med | push latest snapshot on reconnect; frontend zustand uses latest to override, refuses "unknown" interpolation |
| **MT5 block (T16 known issue)** | sync always reports IPC timeout | Certain | UI gives clear error ("MT5 package version incompatible, please sync manually") + fallback command documented |
| **Backend single-process in-memory jobs restart-lost** | long task can't resume | Med | accepted (v1 limit); reserved jsonl persistence; v2 → Redis |
| **K-line 50K bar first paint slow** | bad UX | Low | LWC virtualization; first paint 500 + scroll load; future Redis cache |
| **K-line multi-indicator overlay perf** | browser jank | Med | only 2-3 indicator overlays; disable anti-aliasing; reserved WebGL |
| **shadcn/ui components insufficient** | need to write custom | Low | shadcn = Radix + Tailwind, highly extensible; v1 only imports necessary |

### 7.3 Known non-goals (explicit v1 no-builds)

- ❌ Multi-tenant / SaaS deployment — v1 single-user local
- ❌ Native mobile app — responsive web only
- ❌ Multi-broker aggregation — MT5/cTrader only
- ❌ L2/L3 data feeds — broker doesn't support
- ❌ T3 governance (Bonferroni/CSCV) — institutional flow, no need
- ❌ Historical K-line backfill UI — `scripts/fetch_mt5_data.py` one-shot
- ❌ Backtest param save/compare — reports on disk enough
- ❌ WS cross-node/Redis pubsub — single-process sufficient
- ❌ PWA / offline — responsive web enough

### 7.4 Future extension points (reserved, not v1)

- **Auth**: `backend/api/auth.py` stub reserved, add JWT later
- **Multi-user**: `Depends(get_current_user)` dependency laid down, add user_id isolation later
- **Production deploy**: `start-prod.bat` + Nginx reverse proxy, `next build` output ready
- **Remote access**: intranet penetration (frp/cpolar) or WireGuard; config in `.env`
- **Jobs persistence**: `data/charts/jobs.jsonl` append-only, UI adds history view
- **WebSocket clustering**: `ConnectionManager` abstract reserved, swap to Redis pub/sub
- **PWA**: responsive + manifest add a few lines, offline-capable
- **Telegram integration**: existing bot, UI add "send to Telegram" button hits bot API
- **K-line replay**: reuse `execution/match_replay.py` Brownian bridge, add timeline control

### 7.5 Doc relationships

| File | Relation |
|---|---|
| `README.md` | **Keep** as main entry, add line "Web UI launch: `start.bat`" |
| `README_WEB.md` | **New**, web UI detailed user doc |
| `PROJECT_MAP.md` | **Append section** "Web Console" indexing backend/frontend dirs |
| `ROADMAP.md` | **Append section** "P4 Web UI" task list (11 services + 14 pages + 35 scripts refactor) |
| `TODO.md` | **No new file**, merge into ROADMAP P4 section |
| `docs/superpowers/specs/2026-06-07-quant-web-console-design.md` | **New** (this file) |

---

## Appendix A: Open Questions (none — all resolved during brainstorming)

All design decisions resolved during 2026-06-07 brainstorming:
- Frontend scope: Full-function console (covers all CLI)
- Tech stack: Next.js 14 + shadcn/ui
- Realtime: WS + long-task progress push
- Users: Future multi-user (reserve architecture)
- Design: Bloomberg terminal style
- Launch: One-shot command
- Charts: TradingView Lightweight Charts
- Architecture: Monolith single-repo two-tier (backend + frontend)
- WS: 4 endpoints, single connection multi-channel
- Jobs: in-process queue, no v1 persistence
- Test: scripts refactor guard test is critical
- Migration: 6-phase, 11 weeks, parallel with old CLI
- Non-goals: explicit v1 no-builds listed
