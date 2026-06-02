#!/usr/bin/env python
"""backfill_strategy_perf.py — run PaperTrader once and persist
per-bar state (direction, hold_bars, unrealized PnL, cum PnL, regime)
into ``strategy_perf`` for later regime-conditional analysis.

What it does
============
1. Boot a ``multi_factor_m15`` strategy on XAUUSD+ / M15, no circuit
   breaker (matches ``main.py`` R5 baseline config).
2. Replay the loaded bars through PaperTrader's existing engine, but
   snapshot engine state *after* every bar (engine exposes balance,
   equity, position, unrealized_pnl).
3. For each bar, run ``RegimeDetector.detect()`` with a small rolling
   history of recent M15 bars.  Regime DB lookups are routed through
   a single in-memory SQLite snapshot (no per-bar disk I/O).
4. Write one ``strategy_perf`` row per bar via
   ``AnalyticsStore.insert_strategy_perf``.
5. Report: row count, position distribution, cum PnL final.

Usage
-----
    python scripts/backfill_strategy_perf.py \\
        --symbol XAUUSD+ --timeframe M15 \\
        --strategy multi_factor_m15

No flags required — defaults match ``main.py`` paper-mode baseline.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time as _time
from datetime import datetime
from pathlib import Path

# ── make project importable when run as a script ───────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from data.store import DataStore  # noqa: E402
from strategy.registry import strategy_registry  # noqa: E402
import strategies  # noqa: E402,F401  — triggers @register decorators
from execution.paper_trader import PaperTrader  # noqa: E402
from risk.regime import RegimeDetector  # noqa: E402
import risk.regime as _regime_mod  # noqa: E402
from db.store import AnalyticsStore  # noqa: E402

logger = logging.getLogger("backfill_strategy_perf")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)


# ──────────────────────────────────────────────────────────────────
# 1.  Pre-load regime DB inputs into a single in-memory connection
# ──────────────────────────────────────────────────────────────────

def _build_inmemory_regime_db(src_path: str) -> sqlite3.Connection:
    """Copy the three tables regime.detect() needs into :memory:.

    The detector opens a connection per ``detect()`` call.  If we let
    it open ``data/market_data.db`` 50,000 times we waste tens of
    seconds on cold disk reads.  Pre-loading into a single in-memory
    connection (then monkey-patching ``_open``) means every detect()
    call is a pure in-process query.
    """
    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row
    src = sqlite3.connect(src_path)
    cur = src.cursor()

    for tbl in ("macro_daily", "events"):
        # Read schema, create same in :memory:, copy rows.
        rows = cur.execute(
            f"SELECT sql FROM sqlite_master WHERE name=?", (tbl,)
        ).fetchone()
        if rows and rows[0]:
            mem.execute(rows[0])
        data = cur.execute(f"SELECT * FROM {tbl}").fetchall()
        cols = [d[0] for d in cur.description]
        placeholders = ",".join("?" * len(cols))
        mem.executemany(
            f"INSERT INTO {tbl} VALUES ({placeholders})",
            [tuple(r) for r in data],
        )

    # DXY correlation queries `candles` for D1 XAUUSD closes.  The
    # regime detector only needs symbol_id=1 AND timeframe='D1'.
    candles_sql = cur.execute(
        "SELECT sql FROM sqlite_master WHERE name='candles'"
    ).fetchone()
    if candles_sql and candles_sql[0]:
        mem.execute(candles_sql[0])
    d1_rows = cur.execute(
        "SELECT * FROM candles WHERE symbol_id=1 AND timeframe='D1'"
    ).fetchall()
    d1_cols = [d[0] for d in cur.description]
    ph = ",".join("?" * len(d1_cols))
    mem.executemany(
        f"INSERT INTO candles VALUES ({ph})",
        [tuple(r) for r in d1_rows],
    )

    src.close()
    return mem


def _install_inmemory_regime_db(mem_conn: sqlite3.Connection) -> None:
    """Monkey-patch risk.regime._open to return the cached connection.

    The detector runs in a single thread during backfill, so reusing
    one connection is safe.  We do *not* close it on exit; Python
    reclaims it at process teardown.
    """
    def _open(_db_path: str) -> sqlite3.Connection:
        return mem_conn
    _regime_mod._open = _open


# ──────────────────────────────────────────────────────────────────
# 2.  Per-bar snapshot helper
# ──────────────────────────────────────────────────────────────────

def _snapshot(engine, strategy_name: str, run_id: int,
              bar: dict, regime_str: str, hold_bars: int,
              cum_pnl: float) -> dict:
    """Build one strategy_perf row from current engine state."""
    pos = engine.position
    if pos and pos.direction != 0:
        direction = int(pos.direction)
        position_open = 1
        unrealized = float(pos.unrealized_pnl or 0.0)
        meta = {
            "open_price": float(pos.entry_price),
            "sl_price": float(pos.sl_price),
            "tp_price": float(pos.tp_price),
            "volume": float(pos.volume),
            "current_price": float(pos.current_price or bar["close"]),
        }
    else:
        direction = 0
        position_open = 0
        unrealized = 0.0
        meta = {}

    return {
        "run_id": run_id,
        "bar_ts": float(bar["time"]),
        "bar_date": datetime.utcfromtimestamp(bar["time"]).strftime("%Y-%m-%d"),
        "strategy": strategy_name,
        "regime": regime_str,
        "direction": direction,
        "hold_bars": int(hold_bars),
        "unrealized_pnl": unrealized,
        "cum_pnl": cum_pnl,
        "position_open": position_open,
        "meta": meta,
    }


# ──────────────────────────────────────────────────────────────────
# 3.  Main backfill driver
# ──────────────────────────────────────────────────────────────────

def run_backfill(symbol: str, timeframe: str, strategy_name: str,
                 initial_balance: float, db_path: str,
                 analytics_path: str, bar_limit: int | None = None) -> dict:
    """Replay bars, snapshot state, persist to strategy_perf."""

    # ── A. Create + init store + assign run_id ──
    store = AnalyticsStore(analytics_path)
    last_run = store.last_run_id()
    run_id = last_run + 1
    print(f"→ Run ID: {run_id}  (previous max={last_run})")
    print(f"→ Analytics DB: {analytics_path}")

    # ── B. Pre-load regime DB into memory ──
    t0 = _time.time()
    mem_conn = _build_inmemory_regime_db(db_path)
    _install_inmemory_regime_db(mem_conn)
    logger.info("In-memory regime DB ready (%.0f ms)",
                (_time.time() - t0) * 1000)

    detector = RegimeDetector()
    regime_lookup_count = 0
    regime_first_t = _time.time()

    # ── C. Build strategy + PaperTrader (no circuit) ──
    strategy = strategy_registry.create(
        strategy_name, symbol=symbol, timeframe=timeframe,
    )
    strategy.on_init()

    trader = PaperTrader(
        strategy=strategy,
        initial_balance=initial_balance,
        enable_circuit=False,        # baseline, no risk gates
        warmup_bars=500,
    )
    trader.load_data(DataStore(db_path), symbol, timeframe)
    n_bars = len(trader._bars)
    if bar_limit is not None and bar_limit > 0:
        trader._bars = trader._bars[-bar_limit:]
        n_bars = len(trader._bars)
        print(f"→ bar_limit={bar_limit} → using last {n_bars} bars")
    print(f"→ Loaded {n_bars} {timeframe} bars for {symbol}")

    # ── D. Manual bar loop with per-bar snapshot ──
    trader.strategy.on_init()
    trader.engine.balance = trader.engine.initial_balance
    trader.engine.equity = trader.engine.initial_balance
    trader.engine.position = None
    trader._equity_curve.clear()
    trader._last_reset_date = None

    # 重新同步全局 state（PaperEngine 会改它）
    from core.state import state, DailyStats  # noqa: WPS433
    state.balance = initial_balance
    state.equity = initial_balance
    state.position = type(state.position)()  # blank Position
    state.daily = DailyStats()
    state.daily.peak_equity = initial_balance

    snapshot_rows: list[dict] = []
    history: list[dict] = []   # 滚动窗口给 regime detect 用
    # regime 需要 ~max(EMA_SLOW=200, 2*ADX=28)+1 = 201 根
    HISTORY_WINDOW = 250

    # 这些是给 daily reset 用的本地变量（避免改 trader 内部）
    last_reset_date: object | None = None

    for i, bar in enumerate(trader._bars):
        # 每日重置（仅在没持仓时清 daily.peak_equity，保留 balance/position）
        bar_date = datetime.utcfromtimestamp(bar["time"]).date()
        if last_reset_date is None:
            last_reset_date = bar_date
        elif bar_date != last_reset_date:
            peak = state.daily.peak_equity
            state.daily = DailyStats(date=bar_date, peak_equity=peak)
            last_reset_date = bar_date

        # 1. 策略生成信号（warmup 内不交易）
        signal = None
        if i >= trader.warmup_bars:
            signal = trader.strategy.on_bar(bar)

        # 2. 撮合
        trader.engine.on_bar(bar, signal)

        # 3. Regime: vectorized indicators, in-memory DB
        #    只在历史够长时算（< 201 根 detector 内部会全返 False）
        flags = detector.detect(bar, history, date_str=None,
                                db_path=db_path)
        regime_lookup_count += 1
        # 拼接非空标签
        active_labels = [k for k, v in flags.items() if v]
        regime_str = "|".join(active_labels)

        # 4. 计算 hold_bars
        pos = trader.engine.position
        if pos and pos.direction != 0 and pos.entry_time:
            elapsed = bar["time"] - pos.entry_time.timestamp()
            hold_bars = max(1, int(elapsed // (15 * 60)) + 1)
        else:
            hold_bars = 0

        # 5. cum PnL
        cum_pnl = trader.engine.balance - trader.engine.initial_balance

        # 6. 快照
        snapshot_rows.append(_snapshot(
            trader.engine, strategy_name, run_id,
            bar, regime_str, hold_bars, cum_pnl,
        ))

        # 7. 更新 history（限长）
        history.append(bar)
        if len(history) > HISTORY_WINDOW:
            history = history[-HISTORY_WINDOW:]

        # 8. 记录 equity（保持与原 PaperTrader 行为一致）
        trader._equity_curve.append((bar["time"], trader.engine.equity))

    regime_elapsed = _time.time() - regime_first_t
    print(f"→ Regime detect() called {regime_lookup_count}× "
          f"in {regime_elapsed:.1f}s "
          f"({regime_elapsed / max(1, regime_lookup_count) * 1000:.2f}ms/call)")

    # ── E. Bulk insert ──
    t1 = _time.time()
    n_written = store.insert_strategy_perf(snapshot_rows)
    write_elapsed = _time.time() - t1
    print(f"→ Wrote {n_written} rows to strategy_perf in {write_elapsed:.2f}s")

    # ── F. 报告 ──
    direction_dist = store.direction_distribution(run_id)
    final_row = snapshot_rows[-1]

    print()
    print("=" * 72)
    print(f"  BACKFILL REPORT — run_id={run_id}  {strategy_name}")
    print("=" * 72)
    print(f"  Symbol         : {symbol}  ({timeframe})")
    print(f"  Period         : "
          f"{snapshot_rows[0]['bar_date']} → {snapshot_rows[-1]['bar_date']}")
    print(f"  Bars processed : {len(snapshot_rows)}")
    print(f"  Rows written   : {n_written}")
    print(f"  Final cum PnL  : ${final_row['cum_pnl']:+.2f}")
    print(f"  Final position : "
          f"{'LONG' if final_row['direction']==1 else 'SHORT' if final_row['direction']==-1 else 'FLAT'}")
    print(f"  Direction dist : {direction_dist}")
    print("=" * 72)

    return {
        "run_id": run_id,
        "n_written": n_written,
        "final_cum_pnl": final_row["cum_pnl"],
        "direction_dist": direction_dist,
        "regime_calls": regime_lookup_count,
        "regime_elapsed_s": regime_elapsed,
        "first_3": snapshot_rows[:3],
        "last_3": snapshot_rows[-3:],
    }


# ──────────────────────────────────────────────────────────────────
# 4.  CLI
# ──────────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(
        description="Backfill strategy_perf from a single PaperTrader run."
    )
    p.add_argument("--symbol", default="XAUUSD+")
    p.add_argument("--timeframe", default="M15")
    p.add_argument("--strategy", default="multi_factor_m15")
    p.add_argument("--balance", type=float, default=500.0)
    p.add_argument("--db", default="data/market_data.db",
                   help="Source market DB (read-only).")
    p.add_argument("--analytics-db", default="data/analytics.db",
                   help="Destination analytics DB (strategy_perf).")
    p.add_argument("--limit", type=int, default=None,
                   help="Use only the last N bars (for smoke tests).")
    args = p.parse_args()

    result = run_backfill(
        symbol=args.symbol,
        timeframe=args.timeframe,
        strategy_name=args.strategy,
        initial_balance=args.balance,
        db_path=args.db,
        analytics_path=args.analytics_db,
        bar_limit=args.limit,
    )
    print(f"\nresult.run_id = {result['run_id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
