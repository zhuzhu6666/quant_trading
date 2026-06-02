"""
scripts/p1_e_ab_test.py
========================

P1-E A/B 测试: 3 组 baseline 对比 PnL (production 风格 paper)

设计:
  - 复用 P1-D 的 run_one (router + engine), 只换 baseline
  - A: 原始 baseline (multi_factor 376/362 等, 现有)
  - B: 反 baseline (mean_reversion 排前 1, 看 PnL 是否翻转)
  - C: 均匀 baseline (全部 (1,1), 强制 Thompson 探索)
  - 同一 5000 bar / 同 seed (避免随机性干扰)

跟 P1-D 区别:
  - P1-D 测 "seed 探索性"
  - P1-E 测 "baseline design 是否合理"

结论指导:
  - A vs C 差 → baseline 主导严重, 探索不足 (跟 P1-D 一致)
  - B vs A 差 → 切换策略是否合理 (验证 baseline 反向的 PnL)
  - A vs C 接近 → 探索性已经够, baseline 是稳定锚点
"""
import logging
import sys
import time as _time
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
logger = logging.getLogger("p1_e_ab")
logger.setLevel(logging.WARNING)


M15_STRATEGIES = ["multi_factor_m15", "trend_following", "mean_reversion", "breakout"]

# 三组 baseline
BASELINE_A_ORIG = {
    "multi_factor_m15": (376, 362),
    "trend_following":  (18, 30),
    "mean_reversion":   (457, 786),
    "breakout":         (530, 792),
}
# B: 反 baseline (mean_reversion 跟 breakout 排前, multi_factor 排末)
BASELINE_B_INV = {
    "multi_factor_m15": (362, 376),    # 反过来
    "trend_following":  (30, 18),
    "mean_reversion":   (786, 457),    # 反过来
    "breakout":         (792, 530),
}
# C: 均匀 baseline (全部 (1,1), Beta(2,2) → 50% 期望, 强制探索)
BASELINE_C_UNIFORM = {s: (1, 1) for s in M15_STRATEGIES}


def run_one(router, strats, engine, bars_recent, regime="RANGING") -> dict:
    """跟 p1_d_shadow 一样, 跑一路 router + engine"""
    n = len(bars_recent)
    closes = bars_recent["close"].values
    highs = bars_recent["high"].values
    lows = bars_recent["low"].values
    tr = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(highs[i] - lows[i],
                    abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
    atr_arr = pd.Series(tr).ewm(span=14, min_periods=14).mean().values

    def _atr_source(_bar):
        return float(atr_arr[-1]) if n > 0 else 0.0

    engine.atr_source = _atr_source

    choices = {s: 0 for s in M15_STRATEGIES}
    for s in strats.values():
        s.on_init()

    n_trades = 0
    n_wins = 0
    n_losses = 0
    pnl_total = 0.0

    for i, (ts, bar) in enumerate(bars_recent.iterrows()):
        chosen = router.select(regime)
        choices[chosen] = choices.get(chosen, 0) + 1
        # 转 dict + time 字段 (策略需要 int epoch, 不是 pd.Timestamp)
        bar_dict = bar.to_dict()
        try:
            bar_dict["time"] = int(ts.timestamp())
        except (AttributeError, TypeError):
            bar_dict["time"] = 0
        sig = strats[chosen].on_bar(bar_dict) if chosen else None
        trade = engine.on_bar(bar_dict, sig)
        if trade is not None:
            n_trades += 1
            if trade.pnl > 0:
                n_wins += 1
            else:
                n_losses += 1
            pnl_total += trade.pnl
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
    print("  P1-E A/B 测试 — 3 组 baseline 对比 (M15 paper, 5000 bar)")
    print("=" * 78)

    # 1) 加载
    store = DataStore("data/market_data.db")
    bars = store.load_bars("XAUUSD+", "M15")
    if bars.empty:
        print("  ⚠ 无 M15 bar")
        return 1
    bars_recent = bars.tail(5000)
    print(f"  Loaded {len(bars)} bars, 截取最近 5000: {bars_recent.index[0]} → {bars_recent.index[-1]}")

    common_kwargs = dict(
        sl_atr=3.0, tp_atr=4.0, cooldown_bars=3,
        enable_nfp_skip=True, nfp_skip_days=1,
        enable_dual_event_skip=True,
        enable_gvz_gate=True, gvz_drop_pct=-2.0,
    )

    configs = [
        ("A: 原始 baseline (multi_factor 排前)", BASELINE_A_ORIG),
        ("B: 反 baseline (mean_reversion 排前)", BASELINE_B_INV),
        ("C: 均匀 baseline (强制探索)", BASELINE_C_UNIFORM),
    ]

    results = []
    for label, baseline in configs:
        print(f"\n  [{label}]")
        print(f"  baseline: {baseline}")
        # 固定 seed=42 隔离随机性
        router = MABRouter(M15_STRATEGIES, baseline=baseline, seed=42)
        strats = {n: strategy_registry.create(n, symbol="XAUUSD+", timeframe="M15",
                                                **common_kwargs)
                  for n in M15_STRATEGIES}
        engine = PaperExecutionEngine(initial_balance=500.0)
        t0 = _time.time()
        stats = run_one(router, strats, engine, bars_recent)
        stats["label"] = label
        stats["baseline"] = baseline
        stats["elapsed_s"] = round(_time.time() - t0, 1)
        results.append(stats)
        print(f"  done in {stats['elapsed_s']}s  PnL={stats['pnl']:+.2f}  WR="
              f"{stats['n_wins']/max(stats['n_trades'],1):.1%}")

    # 2) 对比报告
    print("\n" + "=" * 78)
    print("  P1-E A/B 测试报告")
    print("=" * 78)
    print(f"  Bar 范围: 5000 (M15, RANGING regime, seed=42 固定)")
    print()
    print(f"  {'配置':<40s}  {'n_trades':>10s}  {'WR':>7s}  {'PnL':>10s}  {'final':>10s}")
    print("-" * 85)
    for r in results:
        wr = r["n_wins"] / max(r["n_trades"], 1)
        print(f"  {r['label']:<40s}  {r['n_trades']:>10d}  {wr:>6.1%}  "
              f"{r['pnl']:>+10.2f}  {r['final_balance']:>10.2f}")

    # 3) 策略选择对比
    print()
    print(f"  {'策略':<22s}  " + "  ".join(f"{lbl[:18]:>18s}" for lbl, _ in configs))
    print("-" * 90)
    for s in M15_STRATEGIES:
        row = f"  {s:<22s}  "
        for r in results:
            c = r["choices"].get(s, 0)
            row += f"  {c:>18d}"
        print(row)

    # 4) 关键诊断
    print()
    print("=" * 78)
    print("  关键诊断")
    print("=" * 78)
    a = results[0]
    b = results[1]
    c = results[2]
    print(f"  A vs C  PnL 差: {a['pnl'] - c['pnl']:+.2f}  "
          f"(baseline 主导程度, >50 表示 A 严重吃 baseline)")
    print(f"  B vs A  PnL 差: {b['pnl'] - a['pnl']:+.2f}  "
          f"(反 baseline 是否合理, >0 表明原 baseline 选择错)")
    print(f"  C vs A  PnL 差: {c['pnl'] - a['pnl']:+.2f}  "
          f"(强制探索是否更好)")
    # 解读
    print()
    if a["pnl"] > c["pnl"] + 50:
        print("  诊断: baseline 主导严重, 当前 baseline 让 router 选优策略")
        print("  → 探索不足 (跟 P1-D 一致)")
    elif abs(a["pnl"] - c["pnl"]) < 30:
        print("  诊断: 探索性已经够, baseline 是稳定锚点")
    else:
        print(f"  诊断: c 比 a 好 {c['pnl'] - a['pnl']:+.2f}, 探索更优")

    if b["pnl"] > a["pnl"]:
        print("  ⚠ 反 baseline 比原 baseline 好, 原 baseline 选错策略")
    else:
        print("  ✓ 原 baseline 选对方向, 反 baseline 没超过")

    # 5) 落盘
    out_path = PROJECT_ROOT / "data" / "charts" / "p1_e_ab_report.txt"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("P1-E A/B 测试报告\n\n")
        f.write(f"Bar 范围: 5000 (M15, RANGING regime, seed=42 固定)\n\n")
        for r in results:
            wr = r["n_wins"] / max(r["n_trades"], 1)
            f.write(f"{r['label']}\n")
            f.write(f"  baseline: {r['baseline']}\n")
            f.write(f"  n_trades: {r['n_trades']}  WR: {wr:.1%}  PnL: {r['pnl']:+.2f}  "
                    f"final: {r['final_balance']:.2f}\n")
            f.write(f"  choices: {r['choices']}\n\n")
        f.write(f"== 关键诊断 ==\n")
        f.write(f"A vs C  PnL 差: {a['pnl'] - c['pnl']:+.2f}\n")
        f.write(f"B vs A  PnL 差: {b['pnl'] - a['pnl']:+.2f}\n")
    print(f"\n→ 落盘: {out_path}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    import sys as _s
    _s.exit(main())
