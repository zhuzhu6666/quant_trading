"""scripts/test_p0_etf_factors.py — P0-ETF/CB 因子接入 multi_factor_m15 验证 (2026-06-03)

跑 baseline (3 票) + 加 1-2 个新 ETF 因子做协同过滤, 对比 PnL delta.
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
    """算单个因子整段序列."""
    f = factor_registry.get(factor_name)
    if f is None:
        return np.full(len(df), np.nan)
    return f(df)


class IndexVoter:
    """基于 df_full index 的 voter — 不依赖 bar.time, 解决 timestamp 时区差异."""
    def __init__(self, df_full: pd.DataFrame, factor_name: str,
                 direction: int = 1, threshold: float = 0.0):
        self.values = compute_factor_series(df_full, factor_name)
        self.direction = direction
        self.threshold = threshold
        # bar index → values index 直接 1:1 (paper_trader 按顺序喂 bar)
        # 因为 paper_trader 内部 df 跟 df_full 来自同一个 store.load_bars
        # 不需要 timestamp 映射, 计数即可
        self.counter = [0]  # mutable for closure
        self.n_filtered = [0]

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


def run_config(name: str, voters: list, base_kwargs: dict) -> dict:
    """跑一个 paper 配置.

    voters: IndexVoter 列表. 协同过滤规则: voter 任一反向 → 撤 baseline 信号.
    """
    store = DataStore("data/market_data.db")

    strat = strategy_registry.create("multi_factor_m15", symbol="XAUUSD+",
                                     timeframe="M15", **base_kwargs)
    trader = PaperTrader(
        strategy=strat, initial_balance=500.0, default_lots=0.01,
        max_lots=2.0, warmup_bars=500, enable_circuit=False,
    )
    trader.load_data(store, "XAUUSD+", "M15")

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
            # 严格过滤: voter 任一反向就撤
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
    print(" P0-ETF/CB 因子接入验证 — 50K M15 bar XAUUSD+")
    print("=" * 78)
    print()

    base_kwargs = dict(sl_atr=3.0, tp_atr=4.0, cooldown_bars=3)

    # 准备 df_full (M15 + 外部数据)
    store = DataStore("data/market_data.db")
    df = store.load_bars("XAUUSD+", "M15")
    loader = ExternalDataLoader("data/market_data.db")
    ext = loader.align_to_bars(df)
    df_full = df.join(ext)
    print(f"Loaded {len(df_full)} M15 bars, {df_full.index[0]} → {df_full.index[-1]}")
    print()

    # 跑 4 个 config
    results = []

    print("Running A: baseline (3 votes) ...")
    results.append(run_config("A_baseline", [], base_kwargs))
    print(f"  ret={results[-1]['ret']:+.2f}% trades={results[-1]['trades']} WR={results[-1]['wr']:.1f}% DD={results[-1]['dd']:.2f}% PF={results[-1]['pf']:.2f} [{results[-1]['sec']:.1f}s]")

    print("Running B: + slv_gld_ratio voter (IC 0.036, direction=1) ...")
    voter_b = [IndexVoter(df_full, "slv_gld_ratio", direction=1)]
    results.append(run_config("B_+slv", voter_b, base_kwargs))
    print(f"  ret={results[-1]['ret']:+.2f}% trades={results[-1]['trades']} WR={results[-1]['wr']:.1f}% DD={results[-1]['dd']:.2f}% PF={results[-1]['pf']:.2f} [{results[-1]['sec']:.1f}s]")

    print("Running C: + real_yield_pct_rank (IC -0.0096, direction=-1) ...")
    voter_c = [IndexVoter(df_full, "real_yield_pct_rank", direction=-1)]
    results.append(run_config("C_+ry", voter_c, base_kwargs))
    print(f"  ret={results[-1]['ret']:+.2f}% trades={results[-1]['trades']} WR={results[-1]['wr']:.1f}% DD={results[-1]['dd']:.2f}% PF={results[-1]['pf']:.2f} [{results[-1]['sec']:.1f}s]")

    print("Running D: + slv_gld_ratio + cb_china_3m_zscore ...")
    voter_d = [
        IndexVoter(df_full, "slv_gld_ratio", direction=1),
        IndexVoter(df_full, "cb_china_3m_zscore", direction=1),
    ]
    results.append(run_config("D_+slv+cb", voter_d, base_kwargs))
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

    # 落盘报告
    out_path = Path("data/charts/p0_etf_factor_report.txt")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("P0-ETF/CB 因子接入验证 (2026-06-03)\n")
        f.write("=" * 78 + "\n\n")
        f.write("IC Top 新因子 (4 周期平均, 真实数据 50K M15 bar):\n")
        f.write("  slv_gld_ratio:          ic_mean=+0.036 (5/10/20-bar 正相关)\n")
        f.write("  real_yield_pct_rank:    ic_mean=-0.0096 (5y 百分位, 历史高位 = 黄金压制)\n")
        f.write("  cb_china_3m_zscore:     ic_mean=+0.0012\n")
        f.write("  dxy_corr_20 (旧):       ic_mean=-0.058 (基线最强)\n\n")
        f.write("机制: baseline 2 票 + voter 协同过滤\n")
        f.write("      baseline 通过 + voter 任一反向 → 撤信号\n")
        f.write("      (保守: 多 voter 协同, 多数派通过)\n\n")
        f.write(f"{'Config':<20s} {'ret%':>8s} {'trades':>7s} {'WR%':>6s} {'DD%':>6s} {'PF':>5s} {'sec':>6s}\n")
        f.write("-" * 78 + "\n")
        for r in results:
            f.write(f"{r['name']:<20s} {r['ret']:>+8.2f} {r['trades']:>7d} {r['wr']:>6.1f} {r['dd']:>6.2f} {r['pf']:>5.2f} {r['sec']:>6.1f}\n")
        f.write("\n")
        f.write("结论: 跟 baseline 对比, 哪些 voter 改善了 PnL/WR/DD\n")
    print()
    print(f"  → {out_path}")


if __name__ == "__main__":
    main()
