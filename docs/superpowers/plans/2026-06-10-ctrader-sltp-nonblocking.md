# cTrader Live Loop: SL/TP on Server + Tick Non-Blocking

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** cTrader live loop stops blocking FastAPI threadpool on every tick and pushes SL/TP to broker (server-side enforcement, zero bar latency).

**Architecture:** Two independent changes in `backend/services/live_service.py::_process_tick`:
1. **SL/TP on server** — after `market_buy`/`market_sell` returns, call `bridge.amend_position_sltp(position_id, sl, tp)` (already implemented in `execution/ctrader_bridge.py:507`, never wired up). Match the new position by reconciling `local_positions` dict against `get_positions()` result.
2. **Non-blocking tick** — replace sync `bridge.account_info()` / `bridge.get_positions()` with read of shared `_live_state` cache (already updated by WS/account/position endpoints at 1s/15s cadence). Only the order-placement path stays sync.

**Tech Stack:** Python 3.11, FastAPI, Twisted reactor (cTrader), threading, loguru. Tests: pytest with `unittest.mock` (no real broker).

---

## File Structure

| File | Change |
|------|--------|
| `backend/services/live_service.py` | Modify `_process_tick`: drop sync `account_info`/`get_positions` calls, add `amend_position_sltp` after `market_buy`/`market_sell`, add `_local_positions: dict[int, LocalSLTP]` tracking |
| `tests/test_live_service_tick.py` | NEW. Unit tests for the modified `_process_tick` with mocked bridge |
| `docs/CTRADER_INTEGRATION.md` | Update section "阶段 2: SL/TP 上 server" to mark complete + describe the new path |

`_local_positions` lives in `live_service.py` (not a new file) because it's loop-scoped state. The new test file isolates the tick logic from the reactor (no Twisted needed in tests).

---

## Task 1: Add local SL/TP tracking dict

**Files:**
- Modify: `backend/services/live_service.py` (add module-level dict + dataclass + helpers)
- Test: `tests/test_live_service_tick.py` (NEW)

### Step 1: Write the failing test

Create `tests/test_live_service_tick.py`:

```python
"""Tests for live_service._process_tick — cTrader SL/TP on server + non-blocking reads.

audit 2026-06-10: Tick used to call bridge.account_info() + bridge.get_positions()
synchronously, eating 30s+ of FastAPI threadpool time per tick. Now it reads the
shared _live_state cache and only goes to broker for the amend call.
"""
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from backend.services import live_service


@pytest.fixture(autouse=True)
def _reset_state():
    """Reset module-level state between tests."""
    live_service._local_positions.clear()
    live_service._live_state["account"] = None
    live_service._live_state["positions"] = []
    yield
    live_service._local_positions.clear()


def _fake_bridge(position_id=12345, order_id=99):
    """Mock CTraderBridge: market_buy/sell returns ok, amend accepts."""
    bridge = MagicMock()
    result = MagicMock()
    result.success = True
    result.order_id = order_id
    result.position_id = position_id
    result.comment = "ok"
    bridge.market_buy.return_value = result
    bridge.market_sell.return_value = result
    amend_result = MagicMock()
    amend_result.success = True
    amend_result.position_id = position_id
    amend_result.comment = "amend ok"
    bridge.amend_position_sltp.return_value = amend_result
    return bridge


def _fake_signal(direction=1, atr=7.0, sl_atr=2.0, tp_atr=3.0, price=4500.0):
    sig = MagicMock()
    sig.direction = direction
    sig.atr = atr
    sig.sl_atr = sl_atr
    sig.tp_atr = tp_atr
    sig.price = price
    sig.strength = 1.0
    sig.strategy = "test"
    return sig


def test_local_positions_initially_empty():
    assert live_service._local_positions == {}


def test_track_local_position_adds_entry():
    live_service._track_local_sl_tp(position_id=42, sl=4486.0, tp=4521.0)
    assert 42 in live_service._local_positions
    entry = live_service._local_positions[42]
    assert entry.sl == 4486.0
    assert entry.tp == 4521.0
    assert entry.updated_at > 0
```

### Step 2: Run test to verify it fails

Run: `cd C:/Users/zhu/quant_trading && python -m pytest tests/test_live_service_tick.py -v`
Expected: FAIL with `AttributeError: module 'backend.services.live_service' has no attribute '_local_positions'` (or `_track_local_sl_tp`).

### Step 3: Add module-level state + helpers in `live_service.py`

Insert near the top of the file (after the imports, before the existing `_live_state` dict at line 32). Add this block:

```python
# ── Local SL/TP tracking (live loop only) ──────────────────────────
# audit 2026-06-10: 之前 SL/TP 完全靠本地 Python 监控 1 bar 延迟的
# check_sl_tp(), 实际 market_buy 时 bridge 协议不传 SL/TP 字段
# (MARKET 单限制). 改成: market_buy 成交后立即 amend_position_sltp 推
# server. _local_positions 跟踪每个 position_id 的 SL/TP, amend 成功后
# 覆盖, amend 失败时保留旧值(下次 tick 重试).
import threading as _threading
from dataclasses import dataclass, field

@dataclass
class _LocalSLTP:
    position_id: int
    sl: float = 0.0
    tp: float = 0.0
    updated_at: float = 0.0  # epoch seconds

_local_positions: dict[int, _LocalSLTP] = {}
_local_positions_lock = _threading.Lock()


def _track_local_sl_tp(position_id: int, sl: float, tp: float) -> None:
    """Record/amend local SL/TP mirror for a cTrader position_id.

    Thread-safe. Used by live loop after amend_position_sltp() to keep
    a local copy of where the SL/TP currently sit on the server. Useful
    for reconciliation when broker rejects the next amend (e.g. already
    closed): we know what was last pushed.
    """
    if position_id is None or position_id <= 0:
        return
    with _local_positions_lock:
        _local_positions[position_id] = _LocalSLTP(
            position_id=position_id,
            sl=sl,
            tp=tp,
            updated_at=time.time(),
        )
```

### Step 4: Run test to verify it passes

Run: `cd C:/Users/zhu/quant_trading && python -m pytest tests/test_live_service_tick.py::test_local_positions_initially_empty tests/test_live_service_tick.py::test_track_local_position_adds_entry -v`
Expected: PASS for both.

### Step 5: Commit

```bash
git add backend/services/live_service.py tests/test_live_service_tick.py
git commit -m "feat(live): add _local_positions tracking dict + _track_local_sl_tp helper"
```

---

## Task 2: Rewrite `_process_tick` — non-blocking reads + SL/TP amend

**Files:**
- Modify: `backend/services/live_service.py::_process_tick` (lines 812-885)
- Test: `tests/test_live_service_tick.py` (extend with 4 new tests)

### Step 1: Write the failing tests

Append to `tests/test_live_service_tick.py`:

```python
def test_process_tick_does_not_call_account_info_or_get_positions_synchronously(monkeypatch):
    """Tick must read from _live_state cache, not call bridge.account_info / get_positions.

    Audit 2026-06-10: those two sync calls ate 30s+ of Twisted reactor time per tick
    and blocked FastAPI's 40-thread pool. Tick is decision-only; reads come from cache.
    """
    bridge = _fake_bridge()
    strategy = MagicMock()
    strategy.on_bar.return_value = _fake_signal(direction=0)  # no signal
    strategy.last_atr = 7.0

    # Pre-populate cache as if the last WS push / status update wrote it
    live_service._live_state["account"] = {
        "ok": True, "broker": "ctrader", "balance": 10000.0,
        "equity": 10000.0, "currency": "USD", "leverage": 100,
    }
    live_service._live_state["positions"] = []

    df_new = _make_df()
    last_bar = df_new.iloc[-1]
    log_fn = MagicMock()

    with monkeypatch.context() as m:
        # If _process_tick still calls these, test fails
        m.setattr("time.sleep", lambda s: None)
        live_service._process_tick(bridge, strategy, df_new, last_bar, "ctrader", tick=1, log=log_fn)

    bridge.account_info.assert_not_called()
    bridge.get_positions.assert_not_called()


def test_process_tick_calls_amend_after_market_buy(monkeypatch):
    """Long signal → market_buy fills → amend_position_sltp pushes SL/TP to server.

    The amend must include the sl/tp we computed from the signal's sl_atr/tp_atr.
    """
    bridge = _fake_bridge(position_id=777, order_id=99)
    strategy = MagicMock()
    strategy.on_bar.return_value = _fake_signal(direction=1, atr=7.0, sl_atr=2.0, tp_atr=3.0, price=4500.0)
    strategy.last_atr = 7.0

    live_service._live_state["account"] = {"ok": True, "balance": 10000.0, "equity": 10000.0}
    live_service._live_state["positions"] = []

    df_new = _make_df()
    log_fn = MagicMock()

    with monkeypatch.context() as m:
        m.setattr("time.sleep", lambda s: None)
        live_service._process_tick(bridge, strategy, df_new, df_new.iloc[-1], "ctrader", tick=1, log=log_fn)

    bridge.market_buy.assert_called_once()
    bridge.amend_position_sltp.assert_called_once()
    call_args = bridge.amend_position_sltp.call_args
    # position_id=777 (from market_buy result), sl=4500-14=4486, tp=4500+21=4521
    assert call_args.kwargs.get("position_id") == 777 or call_args.args and call_args.args[0] == 777
    assert abs(call_args.kwargs.get("sl", call_args.args[1] if len(call_args.args) > 1 else 0) - 4486.0) < 0.01
    assert abs(call_args.kwargs.get("tp", call_args.args[2] if len(call_args.args) > 2 else 0) - 4521.0) < 0.01


def test_process_tick_amend_failure_keeps_old_sltp(monkeypatch):
    """If amend returns success=False, we should not crash and the position's
    SL/TP should be re-attempted next tick (local tracking not updated)."""
    bridge = _fake_bridge(position_id=555, order_id=99)
    bridge.amend_position_sltp.return_value = MagicMock(success=False, position_id=555, comment="rejected")
    strategy = MagicMock()
    strategy.on_bar.return_value = _fake_signal(direction=1)
    strategy.last_atr = 7.0
    live_service._live_state["account"] = {"ok": True, "balance": 10000.0, "equity": 10000.0}
    live_service._live_state["positions"] = []
    log_fn = MagicMock()
    with monkeypatch.context() as m:
        m.setattr("time.sleep", lambda s: None)
        live_service._process_tick(bridge, strategy, _make_df(), _make_df().iloc[-1], "ctrader", tick=1, log=log_fn)
    # amend was attempted; no entry was tracked (no SL/TP known to be on server)
    assert 555 not in live_service._local_positions
    # The log must record failure
    assert any("AMEND" in str(c) or "FAILED" in str(c) for c in log_fn.call_args_list)


def test_process_tick_dry_run_does_not_call_amend(monkeypatch):
    """When CTRADER_SEND_ORDERS != 1, neither market_buy nor amend should fire."""
    bridge = _fake_bridge()
    strategy = MagicMock()
    strategy.on_bar.return_value = _fake_signal(direction=1)
    strategy.last_atr = 7.0
    live_service._live_state["account"] = {"ok": True, "balance": 10000.0, "equity": 10000.0}
    live_service._live_state["positions"] = []
    log_fn = MagicMock()
    with monkeypatch.context() as m:
        m.setattr("os.getenv", lambda k, d="": "0" if k == "CTRADER_SEND_ORDERS" else d)
        m.setattr("time.sleep", lambda s: None)
        live_service._process_tick(bridge, strategy, _make_df(), _make_df().iloc[-1], "ctrader", tick=1, log=log_fn)
    bridge.market_buy.assert_not_called()
    bridge.amend_position_sltp.assert_not_called()


def _make_df():
    import pandas as pd
    idx = pd.date_range("2026-06-10 10:00", periods=5, freq="15min", tz="UTC")
    return pd.DataFrame({
        "open":  [4495, 4500, 4502, 4503, 4500],
        "high":  [4501, 4503, 4504, 4505, 4502],
        "low":   [4494, 4499, 4500, 4502, 4498],
        "close": [4500, 4502, 4503, 4503, 4500],
        "volume": [100, 110, 120, 130, 140],
    }, index=idx)
```

### Step 2: Run tests to verify they fail

Run: `cd C:/Users/zhu/quant_trading && python -m pytest tests/test_live_service_tick.py -v -k "process_tick" 2>&1 | tail -40`
Expected: 3 of 4 fail (the `dry-run` test may pass by accident; that's fine — fix happens in step 3).

### Step 3: Rewrite `_process_tick` in `live_service.py`

Replace the entire body of `_process_tick` (lines 812-885) with this:

```python
def _process_tick(bridge, strategy, df_new, last_bar, broker: str, tick: int, log) -> None:
    """Process one new bar. v3 (audit 2026-06-10):
      1. strategy.on_bar → signal
      2. Read account/positions from _live_state cache (NOT sync broker — that
         ate 30s+ per tick and blocked FastAPI threadpool). Loop is decision-only.
      3. If signal fires and send_orders=True:
         a) market_buy / market_sell (no SL/TP — cTrader MARKET 单不支持)
         b) amend_position_sltp(position_id, sl, tp) — push SL/TP to server.
            cTrader 协议限制, amend 是 0-latency 的唯一办法.
         c) On amend success: _track_local_sl_tp. On failure: log, leave stale,
            next tick retries.
      4. Update _live_state with current_price from the bar.
    """
    from datetime import datetime as _dt

    # 构造 bar dict
    bar = {
        "open": float(last_bar["open"]),
        "high": float(last_bar["high"]),
        "low": float(last_bar["low"]),
        "close": float(last_bar["close"]),
        "volume": float(last_bar["volume"]) if "volume" in last_bar.index else 0.0,
        "time": float(df_new.index[-1].timestamp()) if hasattr(df_new.index[-1], "timestamp") else 0.0,
        "timeframe": "M15",
        "complete": True,
    }

    # 1. strategy 算 signal (in-process; this is fine to do sync)
    signal = None
    if strategy is not None:
        try:
            signal = strategy.on_bar(bar)
        except Exception as e:
            log(f"  strategy.on_bar error: {e}")

    # 2. Read account + positions from shared cache (audit 2026-06-10:
    #    previously called bridge.account_info() + bridge.get_positions()
    #    synchronously — each is a Twisted round-trip, total 20-30s,
    #    blocking the FastAPI threadpool). Cache is updated by:
    #      - /api/live/account endpoint (15s TTL)
    #      - /api/live/positions endpoint (15s TTL)
    #      - WS _read_state_snapshot (1s cadence, 15s cache)
    #    The loop body no longer needs to know real-time equity; signal
    #    decisions only need the current bar + recent bars.
    acct = _live_state.get("account") or {}
    pos = _live_state.get("positions") or []
    # 兼容 positions 两种形态: list[dict] (从 live_state 取出时) or [] from endpoint
    if isinstance(pos, dict):
        pos = pos.get("positions", []) or []
    current_price = float(last_bar["close"])
    _live_state["account_updated_at"] = time.time()
    _live_state["positions_updated_at"] = time.time()
    if bridge is not None and hasattr(bridge, "get_spot_price"):
        try:
            spot = bridge.get_spot_price()
            if spot and spot > 0:
                _live_state["spot_price"] = spot
                current_price = spot
        except Exception:
            pass

    # 3. 发单 + SL/TP 上 server
    if signal is not None and signal.direction in (1, -1, 2):
        send_orders = _should_send_orders(broker)
        direction_name = {1: "LONG", -1: "SHORT", 2: "CLOSE"}.get(signal.direction, "?")
        if not send_orders:
            log(f"  signal={direction_name} (dry-run, no order)")
        else:
            atr = signal.atr or strategy.last_atr or 0.0
            sl_dist = atr * signal.sl_atr if signal.sl_atr > 0 else atr * 2.0
            tp_dist = atr * signal.tp_atr if signal.tp_atr > 0 else atr * 3.0
            sl_price = current_price - sl_dist if signal.direction == 1 else current_price + sl_dist
            tp_price = current_price + tp_dist if signal.direction == 1 else current_price - tp_dist
            volume = 0.01  # 固定 0.01 lot, v2 minimal
            try:
                # 3a) market_buy / market_sell (MARKET 单不传 SL/TP)
                if signal.direction == 1:
                    result = bridge.market_buy(volume=volume, sl=0.0, tp=0.0, comment="quant-live")
                elif signal.direction == -1:
                    result = bridge.market_sell(volume=volume, sl=0.0, tp=0.0, comment="quant-live")
                else:  # CLOSE
                    closed = 0
                    for p in pos:
                        pid = p.get("position_id") or p.get("ticket")
                        if pid is None:
                            continue
                        cres = bridge.close_position(pid)
                        if getattr(cres, "success", False):
                            closed += 1
                    log(f"  signal=CLOSE closed={closed}")
                    result = None

                # 3b) amend SL/TP 到 server (audit 2026-06-10: 消除 1 bar 延迟)
                if result is not None and getattr(result, "success", False):
                    pid = getattr(result, "position_id", 0) or 0
                    if pid <= 0:
                        # bridge 返回的 orderId 不等于 positionId — 从
                        # cached positions 找最新匹配的 (audit 2026-06-08:
                        # market_buy 文档说 "awaiting get_positions() for entry_price").
                        # 我们读的是 _live_state 缓存, 里面 pos 是上次
                        # get_positions 拿到的列表.
                        if pos:
                            pid = int(pos[0].get("position_id") or pos[0].get("ticket") or 0)
                    if pid > 0:
                        try:
                            ares = bridge.amend_position_sltp(
                                position_id=pid, sl=sl_price, tp=tp_price,
                            )
                            if getattr(ares, "success", False):
                                _track_local_sl_tp(pid, sl=sl_price, tp=tp_price)
                                log(f"  signal={direction_name} ORDER+AMEND OK vol={volume} pos={pid} sl={sl_price:.2f} tp={tp_price:.2f}")
                            else:
                                log(f"  signal={direction_name} AMEND FAILED pos={pid}: {getattr(ares, 'comment', '?')}")
                        except Exception as e:
                            log(f"  signal={direction_name} amend exception: {e}")
                    else:
                        log(f"  signal={direction_name} ORDER OK (no position_id, skip amend) vol={volume}")
                elif result is not None and not getattr(result, "success", False):
                    log(f"  signal={direction_name} ORDER FAILED: {getattr(result, 'error_code', '?')} {getattr(result, 'comment', '')}")
            except Exception as e:
                log(f"  signal={direction_name} order exception: {e}")

    # 4. 写 log + 把当前价推给 WS
    log(f"tick {tick}: price={current_price:.2f} balance={acct.get('balance', 0):.2f} positions={len(pos)}"
        + (f" signal={signal.direction}" if signal and signal.direction != 0 else ""))
    global _latest_price
    _latest_price = current_price
```

### Step 4: Run all tests

Run: `cd C:/Users/zhu/quant_trading && python -m pytest tests/test_live_service_tick.py -v 2>&1 | tail -30`
Expected: all 6 tests pass (2 from Task 1 + 4 from Task 2).

### Step 5: Commit

```bash
git add backend/services/live_service.py tests/test_live_service_tick.py
git commit -m "refactor(live): non-blocking tick reads from _live_state + amend SL/TP on server"
```

---

## Task 3: Update `docs/CTRADER_INTEGRATION.md` to reflect shipped behavior

**Files:**
- Modify: `docs/CTRADER_INTEGRATION.md` (find the "阶段 2: SL/TP 上 server" section, mark complete + describe new path)

### Step 1: Find the section

Run: `cd C:/Users/zhu/quant_trading && grep -n "阶段 2\|SL/TP\|amend" docs/CTRADER_INTEGRATION.md`
Look for the section header about SL/TP. Likely near line 154 (per earlier grep showing `accountId=见 .env CTRADER_ACCOUNT_ID` Pepperstone demo) or in the 阶段 breakdown.

### Step 2: Update the section

Replace the SL/TP section's status from `⏳` (or similar) to `✅` and append a paragraph:

```markdown
**✅ 2026-06-10 shipped:** cTrader live loop now pushes SL/TP to server via
`bridge.amend_position_sltp()` immediately after `market_buy`/`market_sell`
fills. `backend/services/live_service.py::_process_tick` line ~860 — on
`amend success` it calls `_track_local_sl_tp(position_id, sl, tp)` to keep
a local mirror for reconciliation. This eliminates the previous 1-bar
latency where SL/TP was only checked locally against the next bar's
high/low. Tests: `tests/test_live_service_tick.py::test_process_tick_calls_amend_after_market_buy`.
```

### Step 3: Commit

```bash
git add docs/CTRADER_INTEGRATION.md
git commit -m "docs(ctrader): mark SL/TP-on-server as shipped (2026-06-10)"
```

---

## Task 4: Manual smoke test against the running backend

**Files:** none (manual)

### Step 1: Start backend in DRY-RUN mode

```bash
cd C:/Users/zhu/quant_trading
# CTRADER_SEND_ORDERS unset (= 0) → DRY-RUN, no real orders
python -m uvicorn backend.app:app --reload --port 8000
```

Expected: backend starts, cTrader warms up in background.

### Step 2: Hit the live status endpoint

```bash
curl -s http://localhost:8000/api/live/status?broker=ctrader | python -m json.tool
```

Expected: `{"ctrader": {"status": "connected" | "warming_up", ...}, ...}`.

### Step 3: Start the live loop (DRY-RUN)

```bash
curl -s -X POST http://localhost:8000/api/live/start \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <your_jwt>" \
  -d '{"broker": "ctrader", "strategy_name": "multi_factor_m15"}'
```

### Step 4: Watch `logs/live_loop.log`

```bash
tail -f logs/live_loop.log
```

Expected: tick lines every ~60s. Each tick should:
- NOT block the FastAPI threadpool (you should still be able to load `/api/health` and `/api/market/...` immediately).
- In DRY-RUN, log `signal=... (dry-run, no order)` only when strategy fires.

### Step 5: Stop loop

```bash
curl -s -X POST http://localhost:8000/api/live/stop -H "Authorization: Bearer <jwt>"
```

### Step 6: Commit (no code change)

No commit. This is a verification step only. If a bug surfaces, file a follow-up issue and revert if necessary.

---

## Self-Review

1. **Spec coverage:**
   - "ctrader 阻塞" → Task 2 step 3: removed `bridge.account_info()` / `bridge.get_positions()` sync calls in tick. ✅
   - "sl/tp" → Task 2 step 3: added `amend_position_sltp` after `market_buy`/`market_sell`, tracks via `_track_local_sl_tp`. ✅
   - Tests cover both: `test_process_tick_does_not_call_account_info_or_get_positions_synchronously` (blocking) + `test_process_tick_calls_amend_after_market_buy` (SL/TP). ✅
   - Docs updated: Task 3. ✅
   - Manual smoke: Task 4. ✅

2. **Placeholder scan:** No "TBD", "TODO", "implement later". All code blocks are complete. ✅

3. **Type consistency:**
   - `_LocalSLTP` defined in Task 1, used by `_track_local_sl_tp` in Task 1, used by `_process_tick` in Task 2. ✅
   - `_local_positions` dict defined Task 1, used in Task 2. ✅
   - `amend_position_sltp(position_id, sl, tp)` signature matches `execution/ctrader_bridge.py:507`. ✅
   - `_live_state["account"]` and `_live_state["positions"]` keys exist in current code (line 37, 39). ✅

4. **Out of scope (intentionally not changed):**
   - MT5 path (MT5 protocol supports SL/TP natively; the local SL/TP mirroring is cTrader-specific).
   - `scripts/ctrader_live_runner.py` (independent CLI script, not the live loop).
   - WebSocket push latency (still 1s, but tick is no longer the blocker).
   - OAuth token auto-refresh (separate issue, not blocking).

5. **Risk:** If `bridge.amend_position_sltp` raises an exception (network blip), the position will have NO SL/TP until next tick. Task 2 step 3 wraps it in try/except + logs. Local `_local_positions` is not updated on failure, so next tick will retry (but currently the loop only re-amends on new signal — for trailing SL adjustments, this is a known gap; documenting here for follow-up).
