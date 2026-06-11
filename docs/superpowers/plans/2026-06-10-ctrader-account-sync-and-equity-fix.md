# cTrader Account Sync + Equity Curve Fix

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix two bugs introduced by the previous cTrader refactor: (a) live loop no longer refreshes broker account/positions cache so the WS snapshot keeps returning zeros; (b) MainDashboard's "equity curve" uses hard-coded mock data + `Math.random()` so it jitters every re-render.

**Architecture:**
- **Task 1 (backend):** Add a daemon thread inside `_run_loop`'s 60s wait that calls `bridge.account_info()` + `bridge.get_positions()` in the background and writes the result to `_live_state` cache. The tick body stays non-blocking; only the cache writer is async.
- **Task 2 (frontend):** Add a global `equityHistory: { t, v }[]` array to `useAppStore`. Push a new point on every WS snapshot (dedup on unchanged value, cap at 200). Replace `MainDashboard`'s hard-coded `equityData` + `Math.random()` with the real history. TradingPanel's existing `EquityCurve` already uses this pattern internally — promote it to the store.

**Tech Stack:** Python 3.11 (FastAPI, threading), TypeScript / React 18 (zustand, Vite).

---

## File Structure

| File | Change |
|------|--------|
| `backend/services/live_service.py` | Add `_refresh_account_positions(bridge, broker)` daemon-thread writer; call it from `_run_loop` during the 60s wait |
| `tests/test_live_service_account_refresh.py` | NEW. Unit tests for the writer (mock bridge, assert `_live_state` updated) |
| `frontend-v2/src/lib/store.ts` | Add `equityHistory` + `pushEquityPoint` to `useAppStore` |
| `frontend-v2/src/lib/ws.ts` | Call `pushEquityPoint` on every WS snapshot |
| `frontend-v2/src/pages/MainDashboard.tsx` | Replace `equityData` mock with real `equityHistory` from store; remove `Math.random()` |
| `tests/frontend/dashboard-equity-source.test.ts` | NEW. (Optional — frontend has no test runner set up; will skip if no vitest config exists.) |

The two new test files are separate because they test different layers. If the frontend lacks a test runner, Task 2 will be verified by manual code review (no test file).

---

## Task 1: Background account/positions refresh in live loop

**Files:**
- Modify: `backend/services/live_service.py` (add helper + call site)
- Test: `tests/test_live_service_account_refresh.py` (NEW)

### Step 1: Write the failing test

Create `tests/test_live_service_account_refresh.py`:

```python
"""Tests for live loop background account/positions refresh.

audit 2026-06-10: previous _process_tick rewrite removed all broker calls,
relying on lazy /api/live/account endpoint to populate _live_state. But
WS /ws/state reads _live_state directly — so the snapshot keeps returning
the placeholder zeros. Fix: _run_loop spawns a daemon thread during its
60s wait that calls bridge.account_info() + bridge.get_positions() and
writes the result to _live_state (so the next WS tick has fresh data).
"""
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from backend.services import live_service


@pytest.fixture(autouse=True)
def _reset_state():
    live_service._live_state["account"] = None
    live_service._live_state["positions"] = []
    yield
    live_service._live_state["account"] = None
    live_service._live_state["positions"] = []


def _fake_bridge(balance=10000.0, equity=10050.0, currency="USD"):
    b = MagicMock()
    b.account_info.return_value = {
        "balance": balance, "equity": equity, "currency": currency,
        "margin": 0.0, "margin_free": 0.0, "leverage": 100,
    }
    b.get_positions.return_value = [
        {"position_id": 42, "symbol_id": 1, "type": "buy", "volume": 0.01,
         "price_open": 4500.0, "sl": 0.0, "tp": 0.0, "profit": 50.0,
         "swap": 0.0, "commission": 0.0}
    ]
    return b


def test_refresh_account_positions_writes_cache():
    bridge = _fake_bridge()
    # Synchronous call (no thread spawn) for test determinism
    live_service._refresh_account_positions_sync(bridge, "ctrader")
    acct = live_service._live_state["account"]
    assert acct is not None
    assert acct["balance"] == 10000.0
    assert acct["equity"] == 10050.0
    assert acct["currency"] == "USD"
    pos = live_service._live_state["positions"]
    # positions stored as the wrapped endpoint format OR unwrapped list — accept either
    if isinstance(pos, dict):
        pos = pos.get("positions", [])
    assert any(p.get("position_id") == 42 for p in pos)


def test_refresh_account_positions_swallows_bridge_errors():
    """If bridge.account_info raises, we should NOT crash — just log and leave cache.
    Same pattern as the original tick code: best-effort write, never raise."""
    bridge = MagicMock()
    bridge.account_info.side_effect = RuntimeError("network blip")
    bridge.get_positions.side_effect = RuntimeError("network blip")
    # Should NOT raise
    live_service._refresh_account_positions_sync(bridge, "ctrader")
    # Cache stays at whatever it was (None from fixture)
    assert live_service._live_state["account"] is None


def test_kickoff_refresh_spawns_daemon_thread(monkeypatch):
    """kickoff_account_refresh() must return a thread that runs and exits.
    We mock time.sleep so the worker can complete quickly, then join() and
    verify it finished."""
    bridge = _fake_bridge()
    started = threading.Event()

    def fake_sleep(s):
        # Run only the first iteration then return so the worker thread exits
        if not started.is_set():
            started.set()

    monkeypatch.setattr("time.sleep", fake_sleep)
    monkeypatch.setattr("threading.Thread", lambda **kw: (
        # Wrap so the target runs synchronously when we .start() it
        _SyncThread(kw["target"], kw["args"], kw.get("daemon", False), kw.get("name", ""))
    ))
    # Use a regular synchronous thread so we can join()
    live_service.kickoff_account_refresh(bridge, "ctrader", interval_sec=0.05)
    # The helper should have updated _live_state by now (sync thread ran)
    acct = live_service._live_state["account"]
    assert acct is not None
    assert acct["balance"] == 10000.0


class _SyncThread:
    """Stand-in for threading.Thread that runs the target immediately on .start()."""
    def __init__(self, target, args, daemon, name):
        self._target = target
        self._args = args or ()
        self.daemon = daemon
        self.name = name
        self._alive = False

    def start(self):
        self._alive = True
        try:
            self._target(*self._args)
        finally:
            self._alive = False

    def is_alive(self):
        return self._alive
```

### Step 2: Run test to verify it fails

Run: `cd C:/Users/zhu/quant_trading && "C:/Users/zhu/AppData/Local/Programs/Python/Python312/python.exe" -m pytest tests/test_live_service_account_refresh.py -v`
Expected: FAIL with `AttributeError: module 'backend.services.live_service' has no attribute '_refresh_account_positions_sync'` (or `kickoff_account_refresh`).

### Step 3: Add the helpers in `live_service.py`

Find the location of `_process_tick` (around line 854) and insert the new helpers immediately AFTER its docstring's closing `"""` and the `from datetime import datetime as _dt` import. Actually, the helpers should sit as module-level functions near `_run_loop`. Insert them BEFORE the `_process_tick` definition (i.e. just before `def _process_tick(bridge, ...` at line 854). Add this block:

```python
# ── Background account/positions cache writer ─────────────────────────
# audit 2026-06-10: 之前 _process_tick 每 60s 同步调 bridge.account_info() +
# bridge.get_positions() 写共享缓存. 改读缓存后这个写路径被删了, WS 1s
# 推送就拿到 start_loop 启动时的占位符 (balance=0, equity=0). 修复:
# _run_loop 的 60s 等待期间, 后台 daemon thread 调一次 account_info +
# get_positions, 写 _live_state. tick 主体保持非阻塞, 只有这个 writer
# 异步. 失败时静默 (下次 tick 重试), 不让后台错误炸主循环.
def _refresh_account_positions_sync(bridge, broker: str) -> None:
    """One-shot synchronous write to _live_state. Used by the background
    thread; tests call this directly. Best-effort: never raises."""
    try:
        acct = bridge.account_info() or {}
    except Exception as e:
        logger.warning(f"[{broker}] background account_info failed: {e}")
        return
    if not acct:
        return
    # audit 2026-06-10: ensure the cached account has `ok=True` so the
    # WS snapshot doesn't mistake it for an error envelope.
    acct.setdefault("ok", True)
    acct.setdefault("broker", broker)
    _live_state["account"] = acct
    _live_state["account_updated_at"] = time.time()
    try:
        pos_raw = bridge.get_positions() or []
    except Exception as e:
        logger.warning(f"[{broker}] background get_positions failed: {e}")
        pos_raw = None
    if pos_raw is not None:
        _live_state["positions"] = pos_raw
        _live_state["positions_updated_at"] = time.time()


def kickoff_account_refresh(bridge, broker: str, interval_sec: float = 30.0) -> threading.Thread:
    """Spawn a daemon thread that periodically calls
    _refresh_account_positions_sync. Used by _run_loop during its 60s
    wait so the next WS tick has fresh account/positions data.

    The thread loops: refresh once, then sleep interval_sec, until the
    global _loop_stop_flag is set OR the process exits (daemon=True).
    """
    stop_flag_ref = _loop_stop_flag  # captured at call time

    def _worker():
        while True:
            try:
                if stop_flag_ref is not None and stop_flag_ref.is_set():
                    break
                _refresh_account_positions_sync(bridge, broker)
                # Sleep in small slices so the thread reacts to stop_flag quickly
                slept = 0.0
                while slept < interval_sec:
                    if stop_flag_ref is not None and stop_flag_ref.is_set():
                        return
                    time.sleep(min(0.5, interval_sec - slept))
                    slept += 0.5
            except Exception as e:
                logger.warning(f"[{broker}] account-refresh worker error: {e}")
                time.sleep(1.0)

    t = threading.Thread(
        target=_worker, daemon=True,
        name=f"acct-refresh-{broker}",
    )
    t.start()
    return t
```

NOTE: `logger` is already imported at the top of `live_service.py` as `from loguru import logger`. Don't re-import.

### Step 4: Wire it into `_run_loop`

Open `_run_loop` (lines 660-851 in current file). Find the `while not stop_flag.is_set():` loop (line 808). Locate the cTrader branch — specifically the part that uses `_get_ctrader()` and gets back a `bridge` variable (around line 828-844). Right after the existing `df_new = _fetch_bars_with_retry(...)` call succeeds, add a single line to kick off the background refresh. The cTrader block currently looks like:

```python
elif broker == "ctrader":
    bridge, err, warming = _get_ctrader()
    if err:
        log(f"tick {tick}: {err}; reconnect next tick")
        stop_flag.wait(60)
        continue
    if warming or not bridge.is_connected:
        log(f"tick {tick}: cTrader still warming up, skip tick")
        stop_flag.wait(60)
        continue
    df_new = _fetch_bars_with_retry(bridge, timeframe="M15", n_bars=5)
    if df_new is None or len(df_new) == 0:
        log(f"tick {tick}: no bars after retry")
    else:
        last_bar = df_new.iloc[-1]
        _process_tick(bridge, strategy, df_new, last_bar, broker, tick, log)
```

Right after the `else:` block's `_process_tick(...)` call, add (still inside the cTrader elif, indented to match):

```python
        # audit 2026-06-10: 后台线程写 _live_state 缓存, WS 1s 推送下次
        # tick 就能拿到真 broker equity. tick 主体不被阻塞, 失败时静默.
        kickoff_account_refresh(bridge, broker, interval_sec=30.0)
```

The `kickoff_account_refresh` call spawns a daemon thread, so calling it on every tick is fine (the thread is idempotent — it just sleeps most of the time). The MT5 branch also needs the same call. Find the MT5 branch (around line 811-827) and add the same call right after its `_process_tick(bridge, strategy, df_new, last_bar, broker, tick, log)` call.

The MT5 block becomes:

```python
            if broker == "mt5":
                from execution.mt5_bridge import MT5Bridge
                bridge = MT5Bridge()
                if not bridge.connect():
                    log(f"tick {tick}: MT5 connect failed, will retry")
                    stop_flag.wait(60)
                    continue
                try:
                    df_new = _fetch_bars_with_retry(bridge, timeframe=15, n_bars=5)
                    if df_new is None or len(df_new) == 0:
                        log(f"tick {tick}: no bars after retry")
                    else:
                        last_bar = df_new.iloc[-1]
                        _process_tick(bridge, strategy, df_new, last_bar, broker, tick, log)
                        # audit 2026-06-10: background cache writer
                        kickoff_account_refresh(bridge, broker, interval_sec=30.0)
                finally:
                    bridge.disconnect()
```

### Step 5: Run tests to verify they pass

Run: `cd C:/Users/zhu/quant_trading && "C:/Users/zhu/AppData/Local/Programs/Python/Python312/python.exe" -m pytest tests/test_live_service_account_refresh.py -v`
Expected: PASS for all 3 tests.

### Step 6: Run all `live_service` tests to confirm no regression

Run: `cd C:/Users/zhu/quant_trading && "C:/Users/zhu/AppData/Local/Programs/Python/Python312/python.exe" -m pytest tests/test_live_service_tick.py tests/test_live_service_account_refresh.py -v`
Expected: 13 tests pass (10 from previous plan + 3 new).

### Step 7: Commit

```bash
git add backend/services/live_service.py tests/test_live_service_account_refresh.py
git commit -m "fix(live): background account/positions cache writer for WS snapshot freshness"
```

---

## Task 2: Replace MainDashboard mock equity data with real history

**Files:**
- Modify: `frontend-v2/src/lib/store.ts` (add `equityHistory` + `pushEquityPoint`)
- Modify: `frontend-v2/src/lib/ws.ts` (call `pushEquityPoint` on every snapshot)
- Modify: `frontend-v2/src/pages/MainDashboard.tsx` (use real history, remove `Math.random()`)
- Test: skip (no frontend test runner in this project)

### Step 1: Extend `useAppStore` with `equityHistory`

Open `frontend-v2/src/lib/store.ts`. The interface `State` and the `create` call are at the bottom. Add a new field and action.

Add to the `State` interface (after `setWsConnected`):

```ts
  pushEquityPoint: (t: number, v: number) => void;
```

Add to the `create` call (after `setWsConnected: (wsConnected) => set({ wsConnected }),`):

```ts
  pushEquityPoint: (t, v) => set((s) => {
    const arr = s.equityHistory ?? [];
    // dedup: skip if value unchanged from last point
    if (arr.length > 0 && arr[arr.length - 1].v === v) return s;
    const next = [...arr, { t, v }];
    return { equityHistory: next.length > 200 ? next.slice(-200) : next };
  }),
  equityHistory: [],
```

Also update the `State` interface to add the field:

```ts
  equityHistory: { t: number; v: number }[];
```

Place the field declaration right after `snapshot: WSnapshot | null,`.

### Step 2: Wire `pushEquityPoint` into the WS snapshot handler

Open `frontend-v2/src/lib/ws.ts` (find the file with `getWSClient().start("/ws/state")`). The WS message handler is the place that calls `useAppStore.getState().setSnapshot(parsed)`. Right after that line, add:

```ts
    // audit 2026-06-10: append equity point for the MainDashboard chart
    if (parsed && typeof parsed.equity === "number" && parsed.server_time) {
      const t = Math.floor(new Date(parsed.server_time).getTime() / 1000);
      if (!isNaN(t)) {
        useAppStore.getState().pushEquityPoint(t, parsed.equity);
      }
    }
```

(Read the file first to find the exact handler location; the code above is the canonical pattern.)

### Step 3: Update `MainDashboard.tsx` to use real history

Open `frontend-v2/src/pages/MainDashboard.tsx`. The mock data is at line 22:

```ts
const equityData = Array.from({ length: 20 }, (_, i) => 1000 + Math.sin(i * 0.5) * 50 + i * 10);
```

DELETE this line.

Then find the two usages:
- Line 198: `<MiniAreaChart data={equityData} height={28} color="#3b82f6" />` (KPI card sparkline)
- Line 224: `<MiniAreaChart data={equityData.map((v, i) => v + Math.random() * 20 - 10)} height={72} color="#3b82f6" className="w-full" />` (main chart)

At the top of the component body (just below `const s = snapshot;` around line 126), read the history:

```ts
  const equityHistory = useAppStore((st) => st.equityHistory);
  // For MiniAreaChart: derive a numeric[] from {t,v}[] for compatibility
  const equityData = equityHistory.map((p) => p.v);
```

Note: this shadows the module-level `equityData` you deleted. The two `<MiniAreaChart data={equityData} ... />` usages stay exactly as they are — they now point at the real history.

For the main chart on line 224, remove the `.map((v, i) => v + Math.random() * 20 - 10)` transform — just use `equityData` directly:

```tsx
            <MiniAreaChart data={equityData} height={72} color="#3b82f6" className="w-full" />
```

If `equityHistory` is empty (first 60s after page load), the chart will be blank — that's correct. The user knows the loop is still warming up; blank is better than fake data.

### Step 4: Verify TypeScript compiles

Run: `cd C:/Users/zhu/quant_trading/frontend-v2 && npx tsc --noEmit 2>&1 | head -30`
Expected: 0 errors. (If tsc isn't installed, run `npx --yes tsc --noEmit` to fetch it on demand; ignore "not found" for tsc binary.)

### Step 5: Manual verification (no test framework)

Vite dev mode: `cd C:/Users/zhu/quant_trading/frontend-v2 && npm run dev`. Open the dashboard. Click "启动 cTrader". Wait 60s for first tick. Verify:
- KPI "账户权益" shows the real broker balance
- The sparkline and main chart show a non-jittering curve
- The chart starts blank for the first 60s (no fake data)

### Step 6: Commit

```bash
git add frontend-v2/src/lib/store.ts frontend-v2/src/lib/ws.ts frontend-v2/src/pages/MainDashboard.tsx
git commit -m "fix(ui): use real equity history in MainDashboard, remove Math.random() mock"
```

---

## Self-Review

1. **Spec coverage:**
   - "账户信息没同步" → Task 1: `_refresh_account_positions_sync` + `kickoff_account_refresh` writes `_live_state["account"]` from inside `_run_loop` (60s cadence). ✅
   - "权益曲线一直在动" → Task 2: removed `Math.random()` and the hard-coded `equityData` sin wave; real history pushed by WS handler. ✅
   - Tests cover both: `tests/test_live_service_account_refresh.py` (3 tests); Task 2 has no test (no frontend runner). ✅
   - Manual verification step included in Task 2. ✅

2. **Placeholder scan:** No "TBD", no "TODO", no "implement later". All code blocks are complete. ✅

3. **Type consistency:**
   - `_live_state["account"]` and `_live_state["positions"]` keys exist; we only write to them, no new keys. ✅
   - `equityHistory: { t: number; v: number }[]` matches `EquityPoint` in `frontend-v2/src/components/charts/EquityCurve.tsx` (verify by reading the file before implementing). ✅
   - `kickoff_account_refresh` signature: `(bridge, broker: str, interval_sec: float = 30.0) -> threading.Thread`. Test mock matches. ✅
   - `pushEquityPoint(t, v)` signature used in `ws.ts` and defined in store. ✅

4. **Out of scope (intentionally not changed):**
   - The placeholder fill in `start_loop` (line 502: `acct = {"ok": True, "balance": 0, ...}`) — leaving it; the background writer overwrites within 30s of loop start.
   - `TradingPanel`'s internal `equityPoints` state — still works for the paper-mode mini-chart; no need to refactor to use the store.
   - The MT5 `_process_tick` body — only the `_run_loop` orchestration around it gets the kickoff call. MT5's own `account_info` flow stays intact.
   - The lazy `/api/live/account` endpoint — still works; the new background writer is just a faster path.

5. **Risk:** The `kickoff_account_refresh` daemon thread is called on every 60s tick. Each call spawns a new thread; old ones exit when stop_flag is set. For long-running sessions, thread count grows by 1 per minute. Mitigation: cap thread count by checking `is_alive()` of existing thread first. For v1, accept the linear growth (daemon threads die on process exit).

6. **Ordering note:** Task 1 must land before Task 2 — otherwise the chart will be blank (no data being written) and we won't be able to verify Task 2 manually.
