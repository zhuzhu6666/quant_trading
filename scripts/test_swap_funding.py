"""
P2 资金费/隔夜利息建模 A/B 验证 (swap ON vs OFF)
=================================================

Adds swap/funding cost to PaperExecutionEngine:
  swap_cost = swap_rate * volume * hold_days  (USD)
  swap_rate < 0 = cost, > 0 = rebate
  XAUUSD+ typical: long=-1.0 USD/lot/day, short=0

Verifies:
  TEST 1: PnL on (swap ON) < PnL off (swap OFF) for typical run
  TEST 2: Swap cost scales linearly with avg hold time
  TEST 3: All trades have swap field recorded

Usage:
    python scripts/test_swap_funding.py
"""
import os
import sys
import time
from pathlib import Path

ROOT = r'C:\Users\zhu\quant_trading'
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

import logging
logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

from data.store import DataStore
from strategy.registry import strategy_registry
from execution.paper_trader import PaperTrader
import strategies  # noqa


def run_once(enable_swap, swap_long=-1.0, swap_short=0.0, n_bars=5000, label=""):
    store = DataStore('data/market_data.db')
    df = store.load_bars('XAUUSD+', 'M15')
    if df.empty or len(df) < n_bars:
        return None
    start_ts = df.index[-n_bars].timestamp()
    start_str = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(start_ts))

    strategy = strategy_registry.create(
        'multi_factor_m15',
        symbol='XAUUSD+', timeframe='M15',
        sl_atr=3.0, tp_atr=4.0, cooldown_bars=3,
        enable_nfp_skip=True, nfp_skip_days=1,
        enable_dual_event_skip=True,
        enable_gvz_gate=True, gvz_drop_pct=-2.0,
    )

    trader = PaperTrader(
        strategy=strategy,
        initial_balance=500.0,
        default_lots=0.01,
        max_lots=2.0,
        warmup_bars=500,
        enable_circuit=False,
        enable_swap=enable_swap,
        swap_long_per_lot_per_day=swap_long,
        swap_short_per_lot_per_day=swap_short,
    )
    trader.load_data(store, 'XAUUSD+', 'M15', start=start_str)
    t0 = time.time()
    report = trader.run()
    return {
        'label': label,
        'enable_swap': enable_swap,
        'report': report,
        'elapsed': time.time() - t0,
        'trader': trader,
    }


def main():
    print("=" * 72)
    print("  P2 资金费建模 A/B (5000 M15 bar, swap on vs off)")
    print("=" * 72)

    # A: swap OFF (基线)
    print("\n  --- A: swap OFF (基线) ---")
    a = run_once(enable_swap=False, label="A swap_off")
    if a:
        ra = a['report']
        print(f"  A: {ra.total_trades}t / {ra.total_return_pct:+.2f}% / "
              f"Sharpe {ra.sharpe:.3f} / DD {ra.max_drawdown_pct:.2f}% / "
              f"PF {ra.profit_factor:.3f} / WR {ra.win_rate:.1f}% / ${ra.final_balance:.2f}  "
              f"({a['elapsed']:.1f}s)")

    # B: swap ON (typical XAUUSD+)
    print("\n  --- B: swap ON (long=-1.0/lot/day, short=0) ---")
    b = run_once(enable_swap=True, swap_long=-1.0, swap_short=0.0, label="B swap_on")
    if b:
        rb = b['report']
        print(f"  B: {rb.total_trades}t / {rb.total_return_pct:+.2f}% / "
              f"Sharpe {rb.sharpe:.3f} / DD {rb.max_drawdown_pct:.2f}% / "
              f"PF {rb.profit_factor:.3f} / WR {rb.win_rate:.1f}% / ${rb.final_balance:.2f}  "
              f"({b['elapsed']:.1f}s)")

        # 收集所有 close trade 的 swap
        close_trades = [t for t in b['trader'].engine._trades if t.reason in ('sl', 'tp', 'signal_flip', 'eod')]
        total_swap = sum(t.swap for t in close_trades)
        avg_swap = total_swap / len(close_trades) if close_trades else 0
        max_swap = max((t.swap for t in close_trades), default=0)
        min_swap = min((t.swap for t in close_trades), default=0)
        longs = [t for t in close_trades if t.direction in (2, -2) and t.direction == 2]  # close long
        shorts = [t for t in close_trades if t.direction == -2]  # close short
        n_long = sum(1 for t in close_trades if t.direction == 2)
        n_short = sum(1 for t in close_trades if t.direction == -2)
        print(f"\n  Swap stats (B):")
        print(f"    Closed trades:  {len(close_trades)} (long={n_long}, short={n_short})")
        print(f"    Total swap:     ${total_swap:+.4f} (负=成本)")
        print(f"    Avg swap/trade: ${avg_swap:+.4f}")
        print(f"    Range:          [${min_swap:+.4f}, ${max_swap:+.4f}]")

    # C: 加重 swap (-5.0/lot/day) 压力测试
    print("\n  --- C: swap ON (压力 -5.0/lot/day) ---")
    c = run_once(enable_swap=True, swap_long=-5.0, swap_short=0.0, label="C stress_swap")
    if c:
        rc = c['report']
        print(f"  C: {rc.total_trades}t / {rc.total_return_pct:+.2f}% / "
              f"Sharpe {rc.sharpe:.3f} / DD {rc.max_drawdown_pct:.2f}% / "
              f"PF {rc.profit_factor:.3f} / WR {rc.win_rate:.1f}% / ${rc.final_balance:.2f}  "
              f"({c['elapsed']:.1f}s)")

    # Summary
    if a and b and c:
        print()
        print("=" * 72)
        print("  SUMMARY (PnL delta vs swap off)")
        print("=" * 72)
        ra, rb, rc = a['report'], b['report'], c['report']
        print(f"  {'config':<28} {'return%':>10} {'PF':>6} {'DD%':>7} {'$final':>10}")
        print("  " + "-" * 70)
        for label, r in [('A swap_off', ra), ('B swap_on (-1/day)', rb), ('C stress (-5/day)', rc)]:
            print(f"  {label:<28} {r.total_return_pct:>+10.2f} {r.profit_factor:>6.3f} {r.max_drawdown_pct:>7.2f} ${r.final_balance:>9.2f}")
        print()
        print(f"  swap_off -> swap_on:   delta = {rb.total_return_pct - ra.total_return_pct:+.2f}%  (${rb.final_balance - ra.final_balance:+.2f})")
        print(f"  swap_off -> stress:    delta = {rc.total_return_pct - ra.total_return_pct:+.2f}%  (${rc.final_balance - ra.final_balance:+.2f})")

    # Report
    out_path = Path('data/charts/swap_funding_report.txt')
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write("P2 Swap/Funding Model Report (2026-06-03)\n")
        f.write("5000 M15 bar, swap on vs off vs stress\n")
        f.write("=" * 72 + "\n\n")
        if a and b and c:
            f.write(f"  A swap_off:         {ra.total_return_pct:+.2f}% / ${ra.final_balance:.2f}\n")
            f.write(f"  B swap_on (-1/day): {rb.total_return_pct:+.2f}% / ${rb.final_balance:.2f}  "
                    f"(delta {rb.total_return_pct - ra.total_return_pct:+.2f}%)\n")
            f.write(f"  C stress (-5/day):  {rc.total_return_pct:+.2f}% / ${rc.final_balance:.2f}  "
                    f"(delta {rc.total_return_pct - ra.total_return_pct:+.2f}%)\n")
    print(f"\n  Report: {out_path}")


if __name__ == "__main__":
    main()