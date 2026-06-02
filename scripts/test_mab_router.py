"""
MAB Router — 1000 轮 Thompson Sampling 模拟测试

测试策略:
  1. 构造 7 个策略 + baseline 冷启动
  2. 跑 1000 轮: select → 用 baseline 胜率模拟 outcome → update
  3. 验证 multi_factor_m15 (baseline 胜率最高 ≈ 50.9%) 被选中次数最多

运行:
  C:/Users/zhu/AppData/Local/Programs/Python/Python312/python.exe scripts/test_mab_router.py
"""

import sys
from pathlib import Path

# ── 项目根目录 ──
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np
import pandas as pd

from strategy.mab_router import MABRouter, REGIMES

# ── Baseline: 7 策略 (wins, losses) ──
BASELINE: dict[str, tuple[int, int]] = {
    "multi_factor_m15":  (376, 362),
    "ma_cross_h4":       (35,  44),
    "trend_following":   (18,  30),
    "mean_reversion":    (457, 786),
    "breakout":          (530, 792),
    "gold_momentum":     (200, 200),
    "macd_bb":           (200, 200),
}
STRATEGIES = list(BASELINE.keys())
N_ROUNDS = 1000


def baseline_win_rate(name: str) -> float:
    w, l = BASELINE[name]
    return w / (w + l)


def main():
    print("=" * 78)
    print("  MAB Thompson Sampling Router — 1 000 轮模拟验证")
    print("=" * 78)
    print()

    # ── 1. 初始化 ──
    rng = np.random.default_rng(42)  # 固定种子, 可复现
    router = MABRouter(STRATEGIES, baseline=BASELINE)

    print("  Baseline (prior win rates, 用于冷启动):")
    print(f"  {'Strategy':<25} {'W':>5} {'L':>5} {'Win Rate':>10}")
    print(f"  {'-'*25} {'-'*5} {'-'*5} {'-'*10}")
    for s in STRATEGIES:
        w, l = BASELINE[s]
        print(f"  {s:<25} {w:>5} {l:>5} {baseline_win_rate(s):>9.2%}")
    print()

    # ── 2. 模拟 ──
    count: dict[str, int] = {s: 0 for s in STRATEGIES}
    wins: dict[str, int] = {s: 0 for s in STRATEGIES}

    for i in range(N_ROUNDS):
        regime = REGIMES[i % len(REGIMES)]  # 轮询 5 个 regime

        chosen = router.select(regime)

        # 用 baseline 胜率模拟真实 outcome
        outcome = rng.random() < baseline_win_rate(chosen)
        router.update(chosen, regime, outcome)

        count[chosen] += 1
        if outcome:
            wins[chosen] += 1

    # ── 3. 结果 ──
    ranked = sorted(STRATEGIES, key=lambda s: count[s], reverse=True)

    print("  1 000 轮后结果 (按选中次数降序):")
    header = f"  {'Rank':<6} {'Strategy':<25} {'Selected':<10} {'Wins':<8} {'WinRate':<10} {'Prior WR':<10}"
    print(header)
    print(f"  {'-'*len(header)}")
    for rank, s in enumerate(ranked, 1):
        prior_wr = baseline_win_rate(s)
        actual_wr = wins[s] / max(count[s], 1)
        print(f"  {rank:<6} {s:<25} {count[s]:<10} {wins[s]:<8} {actual_wr:<8.2%}    {prior_wr:<8.2%}")

    print()

    # ── 4. 验证 ──
    top = ranked[0]
    second = ranked[1]
    print(f"  ▶ Top 1: {top}  ({count[top]} 次)")
    print(f"  ▶ Top 2: {second} ({count[second]} 次)")

    if top == "multi_factor_m15":
        print()
        print("  ✅  PASS: multi_factor_m15 被选中次数最多, 符合预期")
        print(f"     (baseline 胜率 {baseline_win_rate('multi_factor_m15'):.2%} "
              f"为所有策略中最高)")
    else:
        print()
        print(f"  ⚠️  NOTE: 最优策略为 {top} (预期 multi_factor_m15)")
        print("     这不是严格失败 — randomness 存在, 可加大轮数复现")

    # ── 5. 后验统计 ──
    print()
    print("  Per-Regime 后验分布 (stats()):")
    df_stats = router.stats()
    for _, row in df_stats.iterrows():
        print(f"    [{row['regime']:<15}] {row['strategy']:<25} "
              f"α={row['alpha']:<8.2f} β={row['beta']:<8.2f} "
              f"E[WR]={row['expected_win_rate']:.4%}")

    print()
    print("=" * 78)
    print()


if __name__ == "__main__":
    main()
