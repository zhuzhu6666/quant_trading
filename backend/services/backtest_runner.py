"""In-process backtest runner — reproduces main.py:run_backtest core logic.

Kept separate from main.py so the web console can call it directly without
subprocess overhead. The two implementations must stay in sync; if you change
one, mirror the change in the other (or refactor main.py to call this one
outright — out of scope for v1).

Spec §1.1: main.py is preserved (no functional change).
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd

from backend.core.paths import CHARTS_DIR, DATA_DIR, PROJECT_ROOT


def _load_bars(symbol: str, timeframe: str) -> pd.DataFrame:
    """Load bars from SQLite. Returns DataFrame with DatetimeIndex + OHLCV columns."""
    from data.store import DataStore

    store = DataStore(str(DATA_DIR / "market_data.db"))
    df = store.load_bars(symbol, timeframe)
    if df.empty:
        return df
    # backtrader needs a DatetimeIndex
    if "time" in df.columns:
        df.set_index("time", inplace=True)
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)

    # Keep OHLCV columns
    keep_cols = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
    df = df[keep_cols].copy()
    if "volume" in df.columns:
        df["volume"] = df["volume"].fillna(0)
    return df


def _run_single_backtrader_pass(
    df: pd.DataFrame,
    sl_atr: float,
    tp_atr: float,
    cooldown_bars: int,
    risk_pct: float | None = None,
    enable_circuit: bool = False,
    initial_balance: float = 500.0,
    progress_cb: Optional[Callable[[str, float, str], None]] = None,
) -> dict:
    """Run one backtrader pass with given params. Returns one row dict.

    NOTE: This is a stub for v1. Full backtrader optstrategy wiring (i.e. the
    _ScanStrategy class with RSI/DI/Stoch/MACD/BB/ATR signals from
    main.py:run_backtest) is intentionally deferred — see Phase 4.7+ plan.

    We return a placeholder row with a `note` field so downstream code
    (JobManager, API) can detect the stub state and so the sweep completes
    fast without spawning a subprocess. Real PnL still requires the CLI
    `python main.py --mode backtest`.
    """
    cb = progress_cb or (lambda *_: None)
    cb("running", 50, f"sl={sl_atr} tp={tp_atr} cd={cooldown_bars}: backtrader pass (stub)")

    return {
        "sl_atr": sl_atr,
        "tp_atr": tp_atr,
        "cooldown_bars": cooldown_bars,
        "trades": 0,
        "win_rate": 0.0,
        "net_pnl": 0.0,
        "total_return": 0.0,
        "sharpe": 0.0,
        "max_drawdown": 0.0,
        "total_return_test": 0.0,
        "trades_test": 0,
        "decay": 0.0,
        "note": "in-process stub; full backtrader optstrategy wiring is Phase 4.7+",
    }


def run_backtest_sweep(
    symbol: str = "XAUUSD+",
    timeframe: str = "M15",
    risk_pct: float | None = None,
    enable_circuit: bool = False,
    progress_cb: Optional[Callable[[str, float, str], None]] = None,
) -> dict:
    """Run the 12-combo backtest sweep in-process.

    Returns dict with shape:
        {
            "rows": [row_dict, ...],       # one per (sl, tp, cd) combo
            "total_runs": int,
            "elapsed_seconds": float,
            "report_path": str | None,
            "note": str,
        }
    """
    cb = progress_cb or (lambda *_: None)
    cb("loading", 5, f"loading {symbol} {timeframe}")
    df = _load_bars(symbol, timeframe)
    if df.empty:
        raise RuntimeError(f"no {timeframe} data for {symbol}; run scripts/fetch_mt5_data.py first")

    n = len(df)
    cb("loaded", 10, f"loaded {n} bars")

    cb("splitting", 12, f"n={n} (train/test split handled inside each pass)")

    param_combinations = [
        {"sl_atr": sl, "tp_atr": tp, "cooldown_bars": cd}
        for sl in [2.0, 2.5, 3.0]
        for tp in [3.0, 4.0]
        for cd in [3, 5]
    ]
    total_runs = len(param_combinations)
    cb("running", 15, f"sweeping {total_runs} param combos")

    t0 = time.time()
    rows: list[dict] = []
    for idx, params in enumerate(param_combinations, 1):
        cb("running", 15 + 70 * idx / total_runs, f"combo {idx}/{total_runs}: {params}")
        try:
            row = _run_single_backtrader_pass(
                df,
                sl_atr=params["sl_atr"],
                tp_atr=params["tp_atr"],
                cooldown_bars=params["cooldown_bars"],
                risk_pct=risk_pct,
                enable_circuit=enable_circuit,
            )
            rows.append(row)
        except Exception as e:
            cb("running", 15 + 70 * idx / total_runs, f"combo {idx} failed: {e}")
            rows.append(
                {
                    "sl_atr": params["sl_atr"],
                    "tp_atr": params["tp_atr"],
                    "cooldown_bars": params["cooldown_bars"],
                    "error": str(e),
                }
            )

    elapsed = time.time() - t0
    cb("writing", 92, "writing report")

    # Write report (same column shape as main.py:run_backtest output)
    CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CHARTS_DIR / f"backtest_{time.strftime('%Y%m%d_%H%M%S')}.txt"
    lines: list[str] = [
        f"# Backtest Report ({symbol} {timeframe})",
        f"# risk_pct: {risk_pct}",
        f"# enable_circuit: {enable_circuit}",
        f"# n_bars: {n}",
        f"# combos: {total_runs}, elapsed: {elapsed:.2f}s",
        f"# NOTE: in-process stub — real PnL requires `python main.py --mode backtest`",
        "",
        "# sl_atr | tp_atr | cooldown_bars | trades | win_rate | net_pnl | "
        "total_return | sharpe | max_drawdown | total_return_test | trades_test | decay",
    ]
    for r in rows:
        if "error" in r:
            lines.append(
                f"  {r['sl_atr']} | {r['tp_atr']} | {r['cooldown_bars']} | ERROR: {r['error']}"
            )
        else:
            lines.append(
                f"  {r['sl_atr']} | {r['tp_atr']} | {r['cooldown_bars']} | "
                f"{r['trades']} | {r['win_rate']:.2f} | {r['net_pnl']:.2f} | "
                f"{r['total_return']:.2f} | {r['sharpe']:.2f} | {r['max_drawdown']:.2f} | "
                f"{r['total_return_test']:.2f} | {r['trades_test']} | {r['decay']:.2f}"
            )
    out_path.write_text("\n".join(lines), encoding="utf-8")

    cb("done", 100, f"report: {out_path}")
    return {
        "rows": rows,
        "total_runs": total_runs,
        "elapsed_seconds": elapsed,
        "report_path": str(out_path),
        "note": "in-process sweep; full backtrader optstrategy wiring pending (see _run_single_backtrader_pass)",
    }


def find_latest_backtest_report() -> Optional[Path]:
    """Find most recent backtest_*.txt in data/charts/."""
    if not CHARTS_DIR.exists():
        return None
    candidates = sorted(
        CHARTS_DIR.glob("backtest_*.txt"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    return candidates[0] if candidates else None
