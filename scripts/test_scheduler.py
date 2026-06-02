"""scripts/test_scheduler.py — 验证 SelfLearningScheduler 的自学习调权逻辑

模拟 200 笔交易 (4 策略轮询, 每策略 5 笔/窗口),
每窗口按各策略真实胜率指派确切的胜/负 (确定性的),
从而干净验证调度器的权重调整逻辑。

验证:
- multi_factor  (WR=0.55) 权重保持 1.0 (持续高胜率)
- mean_reversion (WR=0.20) / breakout (WR=0.30) 权重被降到 ~0
- 评估次数 == 200/check_interval == 10 次
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from strategy.mab_router import MABRouter, REGIMES
from strategy.scheduler import SelfLearningScheduler


# ── 策略真实胜率 ──────────────────────────────────────
STRATEGIES = ["multi_factor", "ma_cross", "mean_reversion", "breakout"]
TRUE_WR = {
    "multi_factor": 0.55,    # 持续高胜率
    "ma_cross": 0.45,        # 边界线
    "mean_reversion": 0.20,  # 持续低胜率
    "breakout": 0.30,        # 持续低胜率
}

N_TRADES = 200
CHECK_INTERVAL = 20
N_STRATS = len(STRATEGIES)
EXPECTED_EVALS = N_TRADES // CHECK_INTERVAL  # 10


def _win_for_trade(trade_idx: int, strategy: str) -> bool:
    """
    确定性指派胜/负, 使每策略在每 20 笔窗口内恰好命中其真实胜率.

    Round-robin: 每个窗口 4×5=20 笔.
    每个策略在每个窗口内获得 TRADES_PER_STRAT 笔交易,
    其中固定的笔数为赢 (见下表), 余下为输.

    Strategy         | True WR | Wins/5 | Window WR | 行为
    -----------------|---------|--------|-----------|-----------
    multi_factor     |  0.55   |   3    |   0.60    | 每次都恢复
    ma_cross         |  0.45   | 2/3    | 0.40/0.60 | 交替惩罚/恢复
    mean_reversion   |  0.20   |   1    |   0.20    | 每次都降权
    breakout         |  0.30   | 2/1    | 0.40/0.20 | 每次都降权

    注: 5 笔 / 策略粒度下 WR 只有 0.20, 0.40, 0.60, 0.80, 1.00 几种离散值.
    ma_cross 交替 2→3 赢使期望接近 0.45.
    """
    window_id = trade_idx // CHECK_INTERVAL  # 0..9

    # trade_in_window 是一个策略在本窗口内的第几笔 (0..4)
    # 由于 round-robin, 每 4 笔回到同一策略
    trade_in_window = (trade_idx // N_STRATS) % 5

    if strategy == "multi_factor":
        return trade_in_window < 3               # 3 wins → WR=0.60
    if strategy == "ma_cross":
        wins_this_window = 2 if window_id % 2 == 0 else 3  # alternates
        return trade_in_window < wins_this_window
    if strategy == "mean_reversion":
        return trade_in_window < 1               # 1 win → WR=0.20
    # breakout
    wins_this_window = 2 if window_id % 2 == 0 else 1       # alternates
    return trade_in_window < wins_this_window


def main():
    # 用固定 regime 避免不必要的变化
    regime = "RANGING"

    # ── 初始化 ────────────────────────────────────────
    router = MABRouter(STRATEGIES)
    scheduler = SelfLearningScheduler(
        router,
        check_interval=CHECK_INTERVAL,
        underperformer_threshold=0.45,
        recovery_threshold=0.55,
    )

    # ── 模拟 200 笔交易 (每笔都是确定性 outcome) ─────
    for i in range(N_TRADES):
        strategy = STRATEGIES[i % N_STRATS]
        win = _win_for_trade(i, strategy)
        pnl = 10.0 if win else -10.0   # 简化为固定值
        scheduler.on_trade_close(strategy, regime, win, pnl)

    # ── 报告输出 ──────────────────────────────────────
    print("=" * 70)
    print("  SelfLearningScheduler 测试报告")
    print(f"  模拟交易: {N_TRADES} 笔, 检查间隔: {CHECK_INTERVAL}, "
          f"预期评估: {EXPECTED_EVALS} 次")
    print("=" * 70)

    # 1. 每窗口实际 WR (验证确定性指派正确)
    print("\n1. 每窗口每策略实际胜率 (验证确定性指派)")
    print(f"   {'Window':<8} {'mf':>5} {'mc':>5} {'mr':>5} {'bo':>5}")
    for w in range(EXPECTED_EVALS):
        wins = {s: 0 for s in STRATEGIES}
        cnt = {s: 0 for s in STRATEGIES}
        base = w * CHECK_INTERVAL
        for j in range(CHECK_INTERVAL):
            s = STRATEGIES[(base + j) % N_STRATS]
            cnt[s] += 1
            if _win_for_trade(base + j, s):
                wins[s] += 1
        wr_str = " ".join(
            f"{wins[s]}/{cnt[s]}={wins[s]/cnt[s]:.2f}" for s in STRATEGIES
        )
        print(f"   [{w:<4d}]   {wr_str}")

    # 2. 最终权重
    print("\n2. 策略最终权重")
    print(f"   {'Strategy':<20s} {'True WR':>10} {'Final Weight':>15}")
    print(f"   {'-'*45}")
    for s in STRATEGIES:
        w = scheduler.weights.get(s, 0.0)
        print(f"   {s:<20s} {TRUE_WR[s]:>10.2f} {w:>15.4f}")

    # 3. 评估次数
    print(f"\n3. 评估执行: {scheduler.eval_count} 次 "
          f"(期望 {EXPECTED_EVALS}) "
          f"{'✓' if scheduler.eval_count == EXPECTED_EVALS else '✗'}")

    # 4. 调权事件摘要 (只列有变化的)
    print(f"\n4. 调权事件 (有变化的)")
    events = scheduler.get_events()
    changed = [e for e in events if e["changed"]]
    for e in changed:
        print(f"   [{e['strategy']:<20s}] "
              f"{e['old_weight']:.4f} → {e['new_weight']:.4f}  "
              f"WR={e['recent_wr']:.2f}  {e['reason']}")
    print(f"   总事件: {len(events)}  (调整: {len(changed)}, "
          f"无变化: {len(events)-len(changed)})")

    # 5. 调权统计汇总
    print(f"\n5. 调权统计汇总")
    print(scheduler.stats().to_string(index=False))

    # 6. 验证断言
    print(f"\n{'=' * 70}")
    print("  验证结果")
    print("=" * 70)

    mf_w = scheduler.weights.get("multi_factor", 0.0)
    mr_w = scheduler.weights.get("mean_reversion", 0.0)
    bo_w = scheduler.weights.get("breakout", 0.0)
    mc_w = scheduler.weights.get("ma_cross", 0.0)

    checks = [
        ("评估次数 == 10",
         scheduler.eval_count == EXPECTED_EVALS,
         EXPECTED_EVALS, scheduler.eval_count),
        ("multi_factor 权重 == 1.0 (持续高胜率 → 每次恢复, cap=1.0)",
         0.999 <= mf_w <= 1.001, 1.0, round(mf_w, 4)),
        ("mean_reversion 权重 ≈ 0 (持续低胜率 → 每次降权)",
         mr_w <= 0.01, "≈0", round(mr_w, 6)),
        ("breakout 权重 ≈ 0 (持续低胜率 → 每次降权)",
         bo_w <= 0.01, "≈0", round(bo_w, 6)),
    ]

    all_pass = True
    for label, ok, expected, actual in checks:
        mark = "✓" if ok else "✗"
        if not ok:
            all_pass = False
        print(f"  {mark}  {label}: 期望={expected}, 实际={actual}")

    print()
    if all_pass:
        print("  ✅ 全部验证通过!")
    else:
        print("  ❌ 部分验证未通过")
    print("=" * 70)


if __name__ == "__main__":
    main()
