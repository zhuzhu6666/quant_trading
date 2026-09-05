#!/usr/bin/env python3
"""Read-only baseline comparison harness (A2, meta-loop kill criteria).

Compares the system's realized demo trades (canonical_v2 trade_review_outcome
samples) against simple, well-documented baselines on the same M5 window:
buy & hold, EMA-slope momentum, EMA cross, and RSI mean reversion. Baselines
trade 1 lot (100 oz XAUUSD) with explicit round-trip costs; the system uses
its actual variable sizing. Daily Sharpe for both sides is computed on daily
PnL over the same initial equity base so the ratio is size-neutral.

Pre-registered kill criteria (approved 2026-09-05):
    PASS  requires system_daily_sharpe > best_baseline_daily_sharpe
          AND system_expectancy_per_trade > 0 AND trades >= 30.
    FAIL  means the composite/meta loop work stops here and only this report
          ships; the live shadow deployment is cancelled.

Writes nothing to the trading databases; output goes to run_artifacts/.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import duckdb
import yaml

from backend.core.db import get_state_pg_conn

SYMBOL = "XAUUSD+"
TIMEFRAME = "M5"
LOT_OUNCES = 100.0
BASE_EQUITY = 531.0  # demo account balance at window start (system fact)


def _load_bars(window_start: int) -> list[dict[str, Any]]:
    start_dt = datetime.fromtimestamp(window_start, tz=timezone.utc)
    threshold = (start_dt.year * 100 + start_dt.month - 1)  # load one earlier month for warmup
    months = sorted(
        p for p in (PROJECT_ROOT / "data" / "bars_monthly").glob("bars_*.duckdb")
        if int(p.stem.split("_")[1]) * 100 + int(p.stem.split("_")[2]) >= threshold
    )
    rows: list[dict[str, Any]] = []
    for path in months:
        con = duckdb.connect(str(path), read_only=True)
        try:
            rows.extend(
                {
                    "time": r[0], "open": r[1], "high": r[2], "low": r[3],
                    "close": r[4], "volume": r[5], "spread": r[6],
                }
                for r in con.execute(
                    "SELECT time, open, high, low, close, volume, spread FROM bars "
                    "WHERE symbol=? AND timeframe=? AND time>=? ORDER BY time",
                    [SYMBOL, TIMEFRAME, window_start - 14 * 86400],
                ).fetchall()
            )
        finally:
            con.close()
    return rows


def _ema(values: list[float], period: int) -> list[float]:
    k = 2.0 / (period + 1.0)
    out = [math.nan] * len(values)
    if not values:
        return out
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = values[i] * k + out[i - 1] * (1.0 - k)
    return out


def _rsi(closes: list[float], period: int = 14) -> list[float]:
    out = [math.nan] * len(closes)
    gains = losses = 0.0
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gain, loss = max(change, 0.0), max(-change, 0.0)
        if i <= period:
            gains += gain / period
            losses += loss / period
            if i == period:
                out[i] = 100.0 if losses == 0 else 100.0 - 100.0 / (1.0 + gains / losses)
        else:
            gains = (gains * (period - 1) + gain) / period
            losses = (losses * (period - 1) + loss) / period
            out[i] = 100.0 if losses == 0 else 100.0 - 100.0 / (1.0 + gains / losses)
    return out


def _simulate(bars: list[dict[str, Any]], positions: list[int], cost_per_side: float) -> dict[str, Any]:
    """One-position flip simulation; fills at the next bar's open."""
    trades: list[dict[str, Any]] = []
    entry = entry_idx = 0.0
    held = 0
    for i in range(1, len(bars)):
        want = positions[i - 1]
        fill = float(bars[i]["open"])
        ts = float(bars[i]["time"])
        if want != held:
            if held != 0:
                pnl = (fill - entry) * held - 2.0 * cost_per_side
                trades.append(
                    {"entry_ts": entry_idx, "exit_ts": ts, "pnl": pnl, "side": held}
                )
            if want != 0:
                entry, entry_idx = fill, ts
            held = want
    if held != 0:
        fill = float(bars[-1]["close"])
        trades.append(
            {
                "entry_ts": entry_idx,
                "exit_ts": float(bars[-1]["time"]),
                "pnl": (fill - entry) * held - 2.0 * cost_per_side,
                "side": held,
            }
        )
    return {"trades": trades}


def _metrics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    if not trades:
        return {"n_trades": 0}
    pnls = [t["pnl"] for t in trades]
    daily: dict[str, float] = {}
    for t in trades:
        day = datetime.fromtimestamp(t["exit_ts"], tz=timezone.utc).date().isoformat()
        daily[day] = daily.get(day, 0.0) + t["pnl"]
    series = [daily[d] for d in sorted(daily)]
    mean = sum(series) / len(series)
    var = sum((x - mean) ** 2 for x in series) / max(1, len(series) - 1)
    std = math.sqrt(var)
    sharpe = (mean / std) * math.sqrt(252.0) if std > 1e-9 else 0.0
    peak = cum = 0.0
    max_dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = min(max_dd, cum - peak)
    wins = sum(1 for p in pnls if p > 0)
    return {
        "n_trades": len(trades),
        "win_rate": round(wins / len(trades), 4),
        "total_pnl": round(sum(pnls), 2),
        "expectancy_per_trade": round(sum(pnls) / len(trades), 2),
        "max_drawdown": round(max_dd, 2),
        "daily_sharpe": round(sharpe, 3),
        "n_days": len(series),
    }


def _system_trades() -> list[dict[str, Any]]:
    conn = get_state_pg_conn()
    try:
        rows = conn.execute(
            """
            SELECT event_ts, label_json
            FROM canonical_v2.training_sample_row
            WHERE sample_type='trade_review_outcome'
              AND label_status='matured' AND integrity='full'
            ORDER BY event_ts ASC
            """
        ).fetchall()
    finally:
        conn.close()
    trades = []
    for row in rows:
        label_raw = row["label_json"]
        label = label_raw if isinstance(label_raw, dict) else json.loads(label_raw or "{}")
        pnl = label.get("pnl")
        if pnl is None:
            continue
        trades.append({"exit_ts": float(row["event_ts"]), "pnl": float(pnl), "side": 0})
    return trades


def _live_spread_median() -> float:
    """Median entry spread (price units) from matured open-decision samples."""
    conn = get_state_pg_conn()
    try:
        rows = conn.execute(
            """
            SELECT features_json
            FROM canonical_v2.training_sample_row
            WHERE sample_type='shadow_open_decision'
              AND label_status='matured' AND integrity='full'
            ORDER BY event_ts DESC LIMIT 200
            """
        ).fetchall()
    finally:
        conn.close()
    spreads = []
    for row in rows:
        raw = row["features_json"]
        features = raw if isinstance(raw, dict) else json.loads(raw or "{}")
        micro = features.get("market_micro_context") or {}
        spread = micro.get("spread")
        if isinstance(spread, (int, float)) and spread > 0:
            spreads.append(float(spread))
    return sorted(spreads)[len(spreads) // 2] if spreads else 0.0


def main() -> int:
    settings = yaml.safe_load((PROJECT_ROOT / "config" / "settings.yaml").read_text())
    commission_per_lot = float((settings.get("commission") or {}).get("value") or 0.0)
    slippage = float((settings.get("execution") or {}).get("slippage_value") or 0.0)

    trades = _system_trades()
    if not trades:
        print("no matured trade_review_outcome rows; nothing to compare", file=sys.stderr)
        return 1
    window_start = min(t["exit_ts"] for t in trades)
    bars = _load_bars(int(window_start))
    if len(bars) < 500:
        print("insufficient bars loaded", file=sys.stderr)
        return 1
    closes = [float(b["close"]) for b in bars]
    spread_price = _live_spread_median()
    if spread_price <= 0:
        spreads = [float(b["spread"] or 0.0) for b in bars if b["spread"]]
        median_spread = sorted(spreads)[len(spreads) // 2] if spreads else 0.0
        spread_price = median_spread / 100.0 if median_spread > 5 else median_spread
    # Round trip: one side cost at entry + one at exit.
    cost_per_side = (spread_price / 2.0 + slippage) * LOT_OUNCES + commission_per_lot / 2.0

    ema50 = _ema(closes, 50)
    ema20 = _ema(closes, 20)
    ema100 = _ema(closes, 100)
    rsi14 = _rsi(closes, 14)
    window_mask = [b["time"] >= window_start for b in bars]

    def _positions_from(fn) -> list[int]:
        return [
            int(fn(i)) if in_window else 0
            for i, in_window in enumerate(window_mask)
        ]

    strategies: dict[str, list[int]] = {
        "buy_hold_long": [1 if w else 0 for w in window_mask],
        "ema_slope_momentum": _positions_from(
            lambda i: (
                0
                if math.isnan(ema50[i]) or math.isnan(ema50[max(0, i - 12)])
                else (1 if ema50[i] > ema50[max(0, i - 12)] else -1)
            )
        ),
        "ema_cross": _positions_from(
            lambda i: (
                0
                if math.isnan(ema20[i]) or math.isnan(ema100[i])
                else (1 if ema20[i] > ema100[i] else -1)
            )
        ),
    }
    rsi_state = 0
    rsi_positions: list[int] = []
    for i, in_window in enumerate(window_mask):
        value = rsi14[i]
        if not in_window or math.isnan(value):
            rsi_positions.append(0)
            continue
        if rsi_state == 0:
            if value < 30:
                rsi_state = 1
            elif value > 70:
                rsi_state = -1
        elif rsi_state == 1 and value >= 50:
            rsi_state = 0
        elif rsi_state == -1 and value <= 50:
            rsi_state = 0
        rsi_positions.append(rsi_state)
    strategies["rsi_meanrev"] = rsi_positions

    report: dict[str, Any] = {
        "schema_version": "baseline_comparison.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": {
            "start": datetime.fromtimestamp(window_start, tz=timezone.utc).isoformat(),
            "end": datetime.fromtimestamp(float(bars[-1]["time"]), tz=timezone.utc).isoformat(),
            "bars": len(bars),
        },
        "costs": {
            "median_spread_price": round(spread_price, 3),
            "slippage_price": slippage,
            "commission_per_lot": commission_per_lot,
            "cost_per_side_1lot": round(cost_per_side, 2),
            "lot_ounces": LOT_OUNCES,
        },
        "system_actual": _metrics(trades),
        "baselines_1lot": {},
        "kill_criteria": {
            "rule": "PASS = system_daily_sharpe > max(baseline daily_sharpe) AND system_expectancy_per_trade > 0 AND n_trades >= 30",
            "equity_base_daily_sharpe": BASE_EQUITY,
        },
    }
    for name, positions in strategies.items():
        sim = _simulate(bars, positions, cost_per_side)
        report["baselines_1lot"][name] = _metrics(sim["trades"])

    sys_m = report["system_actual"]
    base_sharpes = {
        k: v.get("daily_sharpe", 0.0)
        for k, v in report["baselines_1lot"].items()
        if v.get("n_trades")
    }
    best_base = max(base_sharpes.values()) if base_sharpes else 0.0
    passed = (
        sys_m.get("n_trades", 0) >= 30
        and sys_m.get("expectancy_per_trade", 0.0) > 0
        and sys_m.get("daily_sharpe", 0.0) > best_base
    )
    report["kill_criteria"]["best_baseline_daily_sharpe"] = best_base
    report["kill_criteria"]["verdict"] = "PASS" if passed else "FAIL"

    out_dir = PROJECT_ROOT / "run_artifacts" / "baseline_comparison"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"baseline_{stamp}.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nwritten: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
