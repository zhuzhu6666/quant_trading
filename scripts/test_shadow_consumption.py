"""
Test T15.5 shadow factor consumption - A/B verification
========================================================

Verifies that --include-shadow-factors actually wires DSL-discovered factors
into the strategy vote pipeline.

A (baseline):    multi_factor_m15, shadow OFF
B (shadow on):   multi_factor_m15, shadow ON (top-3 from lifecycle_log)

Compares: total_trades / return / sharpe / DD / win_rate / profit_factor.
The actual behavior change in B (vs A) confirms wiring works.

Bug history (2026-06-03):
- Root cause: strategy_registry.create() runs cls(...) before assigning instance.params,
  so eager load in __init__ never saw include_shadow_factors=True.
- Fix: lazy load in on_bar via _shadow_loaded flag (avoids registry ordering issue).
- Result: B PnL != A PnL, shadow factors actually vote.

Usage:
    python scripts/test_shadow_consumption.py
"""
import sys
import os
from pathlib import Path

ROOT = r'C:\Users\zhu\quant_trading'
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

import logging
import time
logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
)
logging.getLogger('strategies.multi_factor_m15').setLevel(logging.INFO)
logging.getLogger('execution.paper_engine').setLevel(logging.WARNING)

# T15.5: restore DSL factors into alpha.factor_registry before any strategy import
try:
    from alpha.persistent_registry import restore_from_log
    n = restore_from_log(verbose=False)
    print(f'[T15.5] restored {n} shadow/discovered factors from lifecycle log')
except Exception as e:
    print(f'[T15.5] restore failed (continuing without): {e}')

from data.store import DataStore
from strategy.registry import strategy_registry
import strategies  # noqa
from execution.paper_trader import PaperTrader


def run_once(label: str, include_shadow: bool, shadow_top_k: int, n_bars: int = 5000):
    print()
    print('=' * 72)
    print(f'  {label}: include_shadow={include_shadow}  top_k={shadow_top_k}  n_bars={n_bars}')
    print('=' * 72)

    store = DataStore('data/market_data.db')
    df = store.load_bars('XAUUSD+', 'M15')
    if df.empty or len(df) < n_bars:
        print(f'  ! not enough data: {len(df)} bars')
        return None

    # Limit to last n_bars via start time
    start_ts = df.index[-n_bars].timestamp()
    start_str = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(start_ts))
    print(f'  Start: {start_str} (UTC)')

    strategy = strategy_registry.create(
        'multi_factor_m15',
        symbol='XAUUSD+',
        timeframe='M15',
        sl_atr=3.0, tp_atr=4.0, cooldown_bars=3,
        enable_nfp_skip=True, nfp_skip_days=1,
        enable_dual_event_skip=True,
        enable_gvz_gate=True, gvz_drop_pct=-2.0,
        include_shadow_factors=include_shadow,
        shadow_top_k=shadow_top_k,
    )
    # Lazy load: _load_shadow_factors() 在第一次 on_bar 时才调 (绕过 registry 的 kwargs 时序问题)
    print(f'  Shadow factors (post-init): {len(strategy._shadow_factors)}  (lazy load, 会在首个 on_bar 触发)')
    print(f'  Shadow loaded flag: {strategy._shadow_loaded}')

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
    elapsed = time.time() - t0

    trader.print_report(report)
    print(f'  Runtime: {elapsed:.2f}s')

    # Count shadow factor active count from log? Strategy._last_shadow_active
    # gets reset every on_bar. We can re-create the strategy and call on_bar
    # on a few bars to verify shadow_votes returns nonzero. Or check via meta.
    # Simpler: re-instantiate strategy, drive one bar to verify it works.
    print(f'  Strategy._shadow_factors count after run: {len(strategy._shadow_factors)}')

    return {
        'label': label,
        'include_shadow': include_shadow,
        'report': report,
    }


def main():
    t_total = time.time()
    res_a = run_once('A baseline', include_shadow=False, shadow_top_k=0, n_bars=5000)
    res_b = run_once('B shadow ON', include_shadow=True, shadow_top_k=3, n_bars=5000)

    print()
    print('=' * 72)
    print('  A/B SUMMARY (last 5000 M15 bars)')
    print('=' * 72)
    if res_a and res_b:
        ra = res_a['report']
        rb = res_b['report']
        print(f"  {'metric':<22} {'A (no shadow)':>18} {'B (shadow on)':>18} {'delta':>10}")
        print('  ' + '-' * 70)
        # PaperReport 实际字段: total_trades / total_return_pct / sharpe / max_drawdown_pct / win_rate / profit_factor / final_balance
        for attr in ['total_trades', 'total_return_pct', 'sharpe',
                     'max_drawdown_pct', 'win_rate', 'profit_factor',
                     'final_balance']:
            va = getattr(ra, attr, 0)
            vb = getattr(rb, attr, 0)
            delta = vb - va
            print(f'  {attr:<22} {va:>18.4f} {vb:>18.4f} {delta:>+10.4f}')
    print()
    print(f'  Total runtime: {time.time() - t_total:.2f}s')


if __name__ == '__main__':
    main()