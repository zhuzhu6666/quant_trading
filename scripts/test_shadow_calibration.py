"""
T15.5 shadow factor parameter calibration (sweep)
==================================================

Sweeps (top_pct, vote_weight, min_samples) to find best params on 5000 M15 bar.
Uses A (shadow off) as baseline; for each param combo, runs B (shadow on) and
compares.

The result identifies whether shadow factors CAN contribute positively with
better tuning (vs the default params that produced -24% PnL).

Output: data/charts/shadow_calibration_report.txt

Usage:
    python scripts/test_shadow_calibration.py
"""
import os
import sys
import time
from pathlib import Path
from itertools import product

ROOT = r'C:\Users\zhu\quant_trading'
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

import logging
logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

from alpha.persistent_registry import restore_from_log
from data.store import DataStore
from strategy.registry import strategy_registry
from execution.paper_trader import PaperTrader
import strategies  # noqa: F401 触发注册


def run_once(include_shadow, params, n_bars=5000, label=""):
    store = DataStore('data/market_data.db')
    df = store.load_bars('XAUUSD+', 'M15')
    if df.empty or len(df) < n_bars:
        return None
    start_ts = df.index[-n_bars].timestamp()
    start_str = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(start_ts))

    kwargs = dict(
        symbol='XAUUSD+', timeframe='M15',
        sl_atr=3.0, tp_atr=4.0, cooldown_bars=3,
        enable_nfp_skip=True, nfp_skip_days=1,
        enable_dual_event_skip=True,
        enable_gvz_gate=True, gvz_drop_pct=-2.0,
        include_shadow_factors=include_shadow,
    )
    if include_shadow:
        kwargs.update(params)

    strategy = strategy_registry.create('multi_factor_m15', **kwargs)
    trader = PaperTrader(
        strategy=strategy,
        initial_balance=500.0,
        default_lots=0.01,
        max_lots=2.0,
        warmup_bars=500,
        enable_circuit=False,
    )
    trader.load_data(store, 'XAUUSD+', 'M15', start=start_str)
    t0 = time.time()
    report = trader.run()
    return {
        'label': label,
        'params': params,
        'report': report,
        'elapsed': time.time() - t0,
    }


def main():
    print("=" * 72)
    print("  T15.5 影子因子参数扫描 (5000 M15 bar, baseline vs shadow on)")
    print("=" * 72)

    try:
        n = restore_from_log(verbose=False)
        print(f"  [T15.5] restored {n} shadow/discovered factors")
    except Exception as e:
        print(f"  [T15.5] restore failed: {e}")

    # Baseline (no shadow)
    print("\n  --- A: baseline (shadow off) ---")
    a = run_once(False, {}, n_bars=5000, label="baseline")
    if a:
        ra = a['report']
        print(f"  A: {ra.total_trades}t / {ra.total_return_pct:+.2f}% / "
              f"Sharpe {ra.sharpe:.3f} / DD {ra.max_drawdown_pct:.2f}% / "
              f"PF {ra.profit_factor:.3f} / WR {ra.win_rate:.1f}% / ${ra.final_balance:.2f}  "
              f"({a['elapsed']:.1f}s)")

    # Param grid
    top_pcts = [0.5, 0.6, 0.7, 0.8]     # 0.7 = default
    vote_weights = [0, 0.25, 0.5, 1.0]     # 0 = 真正关, 1.0 = default
    min_samples_list = [20, 50]            # 缩到 2 个 (上次扫描发现 ms 不影响)

    results = []
    combos = list(product(top_pcts, vote_weights, min_samples_list))
    print(f"\n  --- B: shadow on, sweeping {len(combos)} combos ---")
    print(f"  top_pct x vote_weight x min_samples = {len(top_pcts)}x{len(vote_weights)}x{len(min_samples_list)} = {len(combos)}")

    for tp, vw, ms in combos:
        params = {
            'shadow_top_pct': tp,
            'shadow_vote_weight': vw,
            'shadow_min_samples': ms,
            'shadow_top_k': 3,
        }
        label = f"tp={tp},vw={vw},ms={ms}"
        r = run_once(True, params, n_bars=5000, label=label)
        if r:
            results.append(r)
            rr = r['report']
            mark = " <- beats baseline" if a and rr.total_return_pct > a['report'].total_return_pct else ""
            print(f"  {label}: {rr.total_trades}t / {rr.total_return_pct:+.2f}% / "
                  f"Sharpe {rr.sharpe:.3f} / DD {rr.max_drawdown_pct:.2f}% / "
                  f"PF {rr.profit_factor:.3f} / WR {rr.win_rate:.1f}% / ${rr.final_balance:.2f}{mark}")

    # Summary
    print()
    print("=" * 72)
    print("  SUMMARY")
    print("=" * 72)
    if a:
        ra = a['report']
        print(f"  A baseline:  {ra.total_trades}t / {ra.total_return_pct:+.2f}% / "
              f"Sharpe {ra.sharpe:.3f} / DD {ra.max_drawdown_pct:.2f}% / ${ra.final_balance:.2f}")

    # Sort by return
    results.sort(key=lambda x: x['report'].total_return_pct, reverse=True)
    print(f"\n  Top 5 by return:")
    for r in results[:5]:
        rr = r['report']
        print(f"    {r['label']:>22}: {rr.total_return_pct:+.2f}% / Sharpe {rr.sharpe:.3f} / DD {rr.max_drawdown_pct:.2f}%")

    results.sort(key=lambda x: x['report'].sharpe, reverse=True)
    print(f"\n  Top 5 by Sharpe:")
    for r in results[:5]:
        rr = r['report']
        print(f"    {r['label']:>22}: Sharpe {rr.sharpe:.3f} / {rr.total_return_pct:+.2f}% / DD {rr.max_drawdown_pct:.2f}%")

    # Best by composite score (return + sharpe - dd_penalty)
    def score(r):
        rr = r['report']
        return rr.total_return_pct + rr.sharpe * 5 - rr.max_drawdown_pct * 0.1
    results.sort(key=score, reverse=True)
    print(f"\n  Top 3 by composite (return + 5*sharpe - 0.1*dd):")
    for r in results[:3]:
        rr = r['report']
        s = score(r)
        print(f"    {r['label']:>22}: score={s:+.2f} / {rr.total_return_pct:+.2f}% / "
              f"Sharpe {rr.sharpe:.3f} / DD {rr.max_drawdown_pct:.2f}%")

    # Write report
    out_path = Path('data/charts/shadow_calibration_report.txt')
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(f"T15.5 Shadow Factor Calibration Report (2026-06-03)\n")
        f.write(f"5000 M15 bar, baseline (shadow off) vs {len(combos)} shadow-on combos\n")
        f.write("=" * 72 + "\n\n")
        if a:
            ra = a['report']
            f.write(f"  A baseline:  {ra.total_trades}t / {ra.total_return_pct:+.2f}% / "
                    f"Sharpe {ra.sharpe:.3f} / DD {ra.max_drawdown_pct:.2f}% / ${ra.final_balance:.2f}\n\n")
        f.write("  All combos (sorted by return desc):\n")
        for r in sorted(results, key=lambda x: x['report'].total_return_pct, reverse=True):
            rr = r['report']
            f.write(f"    {r['label']:>22}: {rr.total_trades}t / {rr.total_return_pct:+.2f}% / "
                    f"Sharpe {rr.sharpe:.3f} / DD {rr.max_drawdown_pct:.2f}% / PF {rr.profit_factor:.3f}\n")
        f.write(f"\n  Best by composite: {results[0]['label']}\n")
    print(f"\n  Report: {out_path}")


if __name__ == "__main__":
    main()