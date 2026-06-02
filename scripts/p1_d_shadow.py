"""
scripts/p1_d_shadow.py
======================

P1-D 影子交易 — production + shadow 两路 router 并行 paper, 对比 PnL.

简化设计 (P1-D v1):
  - 不调 classify_regime (太慢, 每次 O(200))
  - 用固定 regime='RANGING' (M15 黄金最常见)
  - 两个 router 唯一区别: seed
  - 同策略实例 + 共享 baseline → 跑出 seed 探索差异对 PnL 的影响
  - 最近 5000 bar 跑回放

后续 v2 可以加: 不同 baseline / 不同策略 / 不同 urgency
"""
import logging
import sys
import time as _time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import strategies  # noqa: F401
from strategy.registry import strategy_registry
from strategy.mab_router import MABRouter
from data.store import DataStore
from execution.paper_engine import PaperExecutionEngine

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("p1_d_shadow")
logger.setLevel(logging.WARNING)


M15_STRATEGIES = ["multi_factor_m15", "trend_following", "mean_reversion", "breakout"]
BASELINE = {
    "multi_factor_m15": (376, 362),
    "trend_following":  (18, 30),
    "mean_reversion":   (457, 786),
    "breakout":         (530, 792),
}


@dataclass
class ShadowStats:
    n_bars: int = 0
    n_trades: int = 0
    n_wins: int = 0
    n_losses: int = 0
    shadow_pnl: float = 0.0
    n_diverged: int = 0
    n_agreed: int = 0
    prod_choices: dict = field(default_factory=dict)
    shadow_choices: dict = field(default_factory=dict)


def run_one(router, strats, engine, bars_recent, regime="RANGING") -> dict:
    """跑一路 router + engine, 返回 stats"""
    n = len(bars_recent)
    last_trade_total = 0
    closes = bars_recent["close"].values
    ema50 = pd.Series(closes).ewm(span=50, min_periods=50).mean().values
    ema200 = pd.Series(closes).ewm(span=200, min_periods=200).mean().values
    # ATR 简化
    highs = bars_recent["high"].values
    lows = bars_recent["low"].values
    tr = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(highs[i] - lows[i],
                    abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
    atr_arr = pd.Series(tr).ewm(span=14, min_periods=14).mean().values

    # 给 strats 喂 ATR
    def _atr_source(_bar):
        return float(atr_arr[max(0, n-1)]) if n > 0 else 0.0

    engine.atr_source = _atr_source

    choices = {s: 0 for s in M15_STRATEGIES}
    for s in strats.values():
        s.on_init()

    n_trades = 0
    n_wins = 0
    n_losses = 0
    pnl_total = 0.0
    n_diverged = 0
    n_agreed = 0
    last_prod_dir = 0
    last_shadow_dir = 0

    # 简单 main loop: 每 bar router.select + on_bar + engine.on_bar
    for i, (ts, bar) in enumerate(bars_recent.iterrows()):
        chosen = router.select(regime)
        choices[chosen] = choices.get(chosen, 0) + 1
        bar_dict = bar.to_dict()
        bar_dict["time"] = ts
        sig = strats[chosen].on_bar(bar_dict) if chosen else None
        trade = engine.on_bar(bar_dict, sig)
        if trade is not None:
            n_trades += 1
            if trade.pnl > 0:
                n_wins += 1
            else:
                n_losses += 1
            pnl_total += trade.pnl
            # router update (P1-D 简化: 假设平仓, 反馈)
            router.update(chosen, regime, win=trade.pnl > 0)

    return {
        "n_trades": n_trades,
        "n_wins": n_wins,
        "n_losses": n_losses,
        "pnl": pnl_total,
        "choices": choices,
        "final_balance": getattr(engine, "balance", 500.0),
    }


def main():
    print("=" * 78)
    print("  P1-D 影子交易 v1 — production + shadow router 并行 (简化版)")
    print("=" * 78)

    # 1) 加载数据
    store = DataStore("data/market_data.db")
    bars = store.load_bars("XAUUSD+", "M15")
    if bars.empty:
        print("  ⚠ 无 M15 bar")
        return 1
    print(f"  Loaded {len(bars)} bars")

    # 截取最近 5000 bar
    bars_recent = bars.tail(5000)
    print(f"  截取最近 5000 bar: {bars_recent.index[0]} → {bars_recent.index[-1]}")

    # 2) 策略实例 (共用一组)
    common_kwargs = dict(
        sl_atr=3.0, tp_atr=4.0, cooldown_bars=3,
        enable_nfp_skip=True, nfp_skip_days=1,
        enable_dual_event_skip=True,
        enable_gvz_gate=True, gvz_drop_pct=-2.0,
    )
    strats = {n: strategy_registry.create(n, symbol="XAUUSD+", timeframe="M15",
                                            **common_kwargs)
              for n in M15_STRATEGIES}

    # 3) Production router + engine (seed=42)
    print("\n  [Production] router seed=42 ...")
    prod_router = MABRouter(M15_STRATEGIES, baseline=BASELINE, seed=42)
    prod_strats = {n: strategy_registry.create(n, symbol="XAUUSD+", timeframe="M15",
                                                **common_kwargs)
                    for n in M15_STRATEGIES}
    prod_engine = PaperExecutionEngine(initial_balance=500.0)
    t0 = _time.time()
    prod_stats = run_one(prod_router, prod_strats, prod_engine, bars_recent)
    print(f"  done in {_time.time()-t0:.1f}s")

    # 4) Shadow router + engine (seed=123)
    print("\n  [Shadow]     router seed=123 ...")
    shadow_router = MABRouter(M15_STRATEGIES, baseline=BASELINE, seed=123)
    shadow_strats = {n: strategy_registry.create(n, symbol="XAUUSD+", timeframe="M15",
                                                  **common_kwargs)
                      for n in M15_STRATEGIES}
    shadow_engine = PaperExecutionEngine(initial_balance=500.0)
    t0 = _time.time()
    shadow_stats = run_one(shadow_router, shadow_strats, shadow_engine, bars_recent)
    print(f"  done in {_time.time()-t0:.1f}s")

    # 5) 报告
    print("\n" + "=" * 78)
    print("  P1-D 影子交易 v1 报告")
    print("=" * 78)
    print(f"  Bar 范围: {len(bars_recent)} (M15, RANGING regime)")
    print()
    print(f"  {'指标':<22s}  {'Production':>15s}  {'Shadow':>15s}  {'差':>10s}")
    print("-" * 70)
    print(f"  {'n_trades':<22s}  {prod_stats['n_trades']:>15d}  {shadow_stats['n_trades']:>15d}  "
          f"{shadow_stats['n_trades'] - prod_stats['n_trades']:>+10d}")
    print(f"  {'n_wins':<22s}  {prod_stats['n_wins']:>15d}  {shadow_stats['n_wins']:>15d}  "
          f"{shadow_stats['n_wins'] - prod_stats['n_wins']:>+10d}")
    print(f"  {'n_losses':<22s}  {prod_stats['n_losses']:>15d}  {shadow_stats['n_losses']:>15d}  "
          f"{shadow_stats['n_losses'] - prod_stats['n_losses']:>+10d}")
    print(f"  {'WR':<22s}  {prod_stats['n_wins']/max(prod_stats['n_trades'],1):>14.1%}  "
          f"{shadow_stats['n_wins']/max(shadow_stats['n_trades'],1):>14.1%}")
    print(f"  {'PnL':<22s}  {prod_stats['pnl']:>+15.2f}  {shadow_stats['pnl']:>+15.2f}  "
          f"{shadow_stats['pnl'] - prod_stats['pnl']:>+10.2f}")
    print(f"  {'final_balance':<22s}  {prod_stats['final_balance']:>15.2f}  "
          f"{shadow_stats['final_balance']:>15.2f}")
    print()
    print(f"  策略选择对比:")
    print(f"  {'策略':<22s}  {'Production':>15s}  {'Shadow':>15s}  {'差':>10s}")
    print("-" * 70)
    for s in M15_STRATEGIES:
        pc = prod_stats["choices"].get(s, 0)
        sc = shadow_stats["choices"].get(s, 0)
        print(f"  {s:<22s}  {pc:>15d}  {sc:>15d}  {sc - pc:>+10d}")

    # 6) 落盘
    out_path = PROJECT_ROOT / "data" / "charts" / "p1_d_shadow_report.txt"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("P1-D 影子交易 v1 报告\n\n")
        f.write(f"Bar 范围: {len(bars_recent)} (M15, RANGING regime)\n")
        f.write(f"Production: router seed=42, baseline=BASELINE\n")
        f.write(f"Shadow    : router seed=123, baseline=BASELINE\n\n")
        f.write(f"n_trades: prod={prod_stats['n_trades']}  shadow={shadow_stats['n_trades']}\n")
        f.write(f"WR:       prod={prod_stats['n_wins']/max(prod_stats['n_trades'],1):.1%}  "
                f"shadow={shadow_stats['n_wins']/max(shadow_stats['n_trades'],1):.1%}\n")
        f.write(f"PnL:      prod={prod_stats['pnl']:+.2f}  shadow={shadow_stats['pnl']:+.2f}  "
                f"diff={shadow_stats['pnl']-prod_stats['pnl']:+.2f}\n")
        f.write(f"\n策略选择:\n")
        for s in M15_STRATEGIES:
            f.write(f"  {s}: prod={prod_stats['choices'].get(s,0)}  "
                    f"shadow={shadow_stats['choices'].get(s,0)}\n")
    print(f"\n→ 落盘: {out_path}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    import sys as _s
    _s.exit(main())
