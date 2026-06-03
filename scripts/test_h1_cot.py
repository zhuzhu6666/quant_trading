"""scripts/test_h1_cot.py — H1 baseline + COT 协同过滤 (2026-06-03)

H1 数据 3 年, COT 16 年历史, 让 COT z-score 真正有意义.
测试 3 个 config:
  A: H1 baseline (3 votes)
  B: H1 + cot_pm_net voter (商业对冲者净持仓, IC -0.047)
  C: H1 + cot_mm_net voter (投机者净持仓, IC +0.036)
  D: H1 + cot_pm_net + cot_mm_net 协同
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import time as _time
import numpy as np
import pandas as pd
import strategies
from strategy.registry import strategy_registry
from data.store import DataStore
from data.external_loader import ExternalDataLoader
from execution.paper_trader import PaperTrader
from alpha.registry import factor_registry


def compute_factor_series(df: pd.DataFrame, factor_name: str) -> np.ndarray:
    f = factor_registry.get(factor_name)
    if f is None:
        return np.full(len(df), np.nan)
    return f(df)


class IndexVoter:
    def __init__(self, df_full: pd.DataFrame, factor_name: str,
                 direction: int = 1, threshold: float = 0.0):
        self.values = compute_factor_series(df_full, factor_name)
        self.direction = direction
        self.threshold = threshold
        self.counter = [0]

    def vote(self, bar, current_close):
        idx = self.counter[0]
        self.counter[0] += 1
        if idx >= len(self.values):
            return 0, 0
        v = self.values[idx]
        if np.isnan(v) or abs(v) < self.threshold:
            return 0, 0
        if self.direction == 1:
            if v > 0:
                return 1, 0
            else:
                return 0, 1
        else:
            if v < 0:
                return 1, 0
            else:
                return 0, 1


def run_config(name: str, voters: list, base_kwargs: dict, timeframe: str = "H1") -> dict:
    store = DataStore("data/market_data.db")
    strat = strategy_registry.create("multi_factor_m15", symbol="XAUUSD+",
                                     timeframe=timeframe, **base_kwargs)
    trader = PaperTrader(
        strategy=strat, initial_balance=500.0, default_lots=0.01,
        max_lots=2.0, warmup_bars=200, enable_circuit=False,  # H1 需 warmup 更少
    )
    trader.load_data(store, "XAUUSD+", timeframe)

    if not voters:
        t0 = _time.time()
        report = trader.run()
        dt = _time.time() - t0
    else:
        original_on_bar = strat.on_bar

        def patched_on_bar(bar):
            sig = original_on_bar(bar)
            if sig is None:
                return None
            v_long, v_short = 0, 0
            for vt in voters:
                l, s = vt.vote(bar, bar.get('close'))
                v_long += l
                v_short += s
            if sig.direction == 1 and v_short > 0:
                return None
            if sig.direction == -1 and v_long > 0:
                return None
            return sig
        strat.on_bar = patched_on_bar

        t0 = _time.time()
        report = trader.run()
        dt = _time.time() - t0

    return {
        "name": name,
        "ret": report.total_return_pct,
        "trades": report.total_trades,
        "wr": report.win_rate,
        "dd": report.max_drawdown_pct,
        "pf": report.profit_factor,
        "sec": dt,
    }


def main():
    print("=" * 78)
    print(" H1 + COT 16 年历史接入验证 — 18K H1 bar XAUUSD+")
    print("=" * 78)
    print()

    base_kwargs = dict(sl_atr=3.0, tp_atr=4.0, cooldown_bars=3)

    store = DataStore("data/market_data.db")
    df = store.load_bars("XAUUSD+", "H1")
    loader = ExternalDataLoader("data/market_data.db")
    ext = loader.align_to_bars(df)
    df_full = df.join(ext)
    print(f"Loaded {len(df_full)} H1 bars, {df_full.index[0]} → {df_full.index[-1]}")
    print(f"COT history: 856 weeks (2010-01 → 2026-05, 16.4 years)")
    print()

    results = []

    print("Running A: H1 baseline (3 votes) ...")
    results.append(run_config("A_baseline", [], base_kwargs))
    print(f"  ret={results[-1]['ret']:+.2f}% trades={results[-1]['trades']} WR={results[-1]['wr']:.1f}% DD={results[-1]['dd']:.2f}% PF={results[-1]['pf']:.2f} [{results[-1]['sec']:.1f}s]")

    print("Running B: H1 + cot_pm_net voter (IC=-0.047) ...")
    voter_b = [IndexVoter(df_full, "cot_pm_net", direction=-1)]
    results.append(run_config("B_+pm", voter_b, base_kwargs))
    print(f"  ret={results[-1]['ret']:+.2f}% trades={results[-1]['trades']} WR={results[-1]['wr']:.1f}% DD={results[-1]['dd']:.2f}% PF={results[-1]['pf']:.2f} [{results[-1]['sec']:.1f}s]")

    print("Running C: H1 + cot_mm_net voter (IC=+0.036) ...")
    voter_c = [IndexVoter(df_full, "cot_mm_net", direction=1)]
    results.append(run_config("C_+mm", voter_c, base_kwargs))
    print(f"  ret={results[-1]['ret']:+.2f}% trades={results[-1]['trades']} WR={results[-1]['wr']:.1f}% DD={results[-1]['dd']:.2f}% PF={results[-1]['pf']:.2f} [{results[-1]['sec']:.1f}s]")

    print("Running D: H1 + cot_pm + cot_mm 协同 ...")
    voter_d = [
        IndexVoter(df_full, "cot_pm_net", direction=-1),
        IndexVoter(df_full, "cot_mm_net", direction=1),
    ]
    results.append(run_config("D_+pm+mm", voter_d, base_kwargs))
    print(f"  ret={results[-1]['ret']:+.2f}% trades={results[-1]['trades']} WR={results[-1]['wr']:.1f}% DD={results[-1]['dd']:.2f}% PF={results[-1]['pf']:.2f} [{results[-1]['sec']:.1f}s]")

    print()
    print("=" * 78)
    print(" Summary")
    print("=" * 78)
    print(f"{'Config':<20s} {'ret%':>8s} {'trades':>7s} {'WR%':>6s} {'DD%':>6s} {'PF':>5s} {'sec':>6s}")
    print("-" * 78)
    a_ret = results[0]['ret']
    for r in results:
        delta = r['ret'] - a_ret if r is not results[0] else 0.0
        marker = f" ({delta:+.1f}pp)" if r is not results[0] else ""
        print(f"{r['name']:<20s} {r['ret']:>+8.2f} {r['trades']:>7d} {r['wr']:>6.1f} {r['dd']:>6.2f} {r['pf']:>5.2f} {r['sec']:>6.1f}{marker}")

    out_path = Path("data/charts/h1_cot_report.txt")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("H1 + COT 16 年历史接入验证 (2026-06-03)\n")
        f.write("=" * 78 + "\n\n")
        f.write("数据: 18,050 H1 bar XAUUSD+ (2023-05-15 → 2026-06-02, 3.1 年)\n")
        f.write("COT: 856 weeks (2010-01-05 → 2026-05-26, 16.4 年历史)\n\n")
        f.write("IC Top H1 因子 (3 年 H1 数据, 让 COT z-score 真正有意义):\n")
        f.write("  slv_gld_ratio:           ic_mean=+0.102\n")
        f.write("  macd_hist:               ic_mean=+0.069\n")
        f.write("  cot_pm_net:              ic_mean=-0.047  (top 3!)\n")
        f.write("  cot_mm_net:              ic_mean=+0.036\n")
        f.write("  cot_mm_net_pct_oi:       ic_mean=+0.023\n\n")
        f.write("M15 vs H1 COT 因子 IC 对比:\n")
        f.write("  cot_pm_net:        M15 -0.022 → H1 -0.047  (2.1× 提升)\n")
        f.write("  cot_mm_net:        M15 +0.020 → H1 +0.036  (1.8× 提升)\n")
        f.write("  cot_mm_net_pct_oi: M15 +0.011 → H1 +0.023  (2.1× 提升)\n")
        f.write("  cb_total_chg_3m:   M15 -0.004 → H1 +0.025  (6.3× 提升)\n\n")
        f.write(f"{'Config':<20s} {'ret%':>8s} {'trades':>7s} {'WR%':>6s} {'DD%':>6s} {'PF':>5s} {'sec':>6s}\n")
        f.write("-" * 78 + "\n")
        for r in results:
            f.write(f"{r['name']:<20s} {r['ret']:>+8.2f} {r['trades']:>7d} {r['wr']:>6.1f} {r['dd']:>6.2f} {r['pf']:>5.2f} {r['sec']:>6.1f}\n")
    print()
    print(f"  → {out_path}")


if __name__ == "__main__":
    main()
