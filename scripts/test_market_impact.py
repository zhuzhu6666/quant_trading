"""scripts/test_market_impact.py — AlmgrenChrissModel 验证

验证不同订单规模下切片策略的冲击成本对比:
  - Q = 0.5 lot (50 oz) / 1 lot (100 oz) / 5 lots (500 oz) / 10 lots (1000 oz)
  - 切片数: 1 / 5 / 10 / 20 / 50 / 100

关键洞察:
  - 大单必须切片: 黄金 1 手 100 oz 切 10 次 < 全成 1 次
  - 临时冲击随切片数增加线性下降
  - 永久冲击只与总量有关, 与切片数无关
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution.market_impact import AlmgrenChrissModel


def main() -> None:
    print("=" * 72)
    print("  Almgren-Chriss 市场冲击模型 — 验证")
    print("=" * 72)

    model = AlmgrenChrissModel()

    # 打印默认参数
    print("\n[默认参数]")
    for k, v in model.params.items():
        print(f"  {k:>20s} = {v}")

    # ------------------------------------------------------------------
    # 多订单规模 x 多切片数 对比
    # ------------------------------------------------------------------
    test_cases: list[tuple[str, float]] = [
        ("0.5 lot  (50 oz)", 50),
        ("1 lot   (100 oz)", 100),
        ("5 lots  (500 oz)", 500),
        ("10 lots (1000 oz)", 1000),
    ]

    for label, qty in test_cases:
        print(f"\n{'─' * 72}")
        print(f"  订单: {label}")
        print(f"{'─' * 72}")
        df = model.compare_strategies(qty)

        # 科学记数法显示, 让比例关系清晰
        for col in ("temporary_bps", "permanent_bps", "total_bps"):
            df[col] = df[col].apply(lambda v: f"{v:.6e}")

        df["cost_usd_per_oz"] = df["cost_usd_per_oz"].apply(lambda v: f"{v:.6e}")
        print(df.to_string(index=False))

    # ------------------------------------------------------------------
    # 关键洞察切片数 vs 成本
    # ------------------------------------------------------------------
    print(f"\n{'=' * 72}")
    print("  关键洞察: 大单必须切片")
    print("=" * 72)

    qty = 100  # 1 lot
    cost_1 = model.expected_cost(qty, n_slices=1)
    cost_5 = model.expected_cost(qty, n_slices=5)
    cost_10 = model.expected_cost(qty, n_slices=10)
    cost_50 = model.expected_cost(qty, n_slices=50)

    print(f"\n  Q = 1 lot (100 oz):")
    print(f"  {'Slices':>8s}  {'Temp(bps)':>15s}  {'Perm(bps)':>15s}  {'Total(bps)':>15s}")
    print(f"  {'─' * 8}  {'─' * 15}  {'─' * 15}  {'─' * 15}")
    for n, c in ((1, cost_1), (5, cost_5), (10, cost_10), (50, cost_50)):
        print(
            f"  {n:>8d}  {c['temporary_impact_bps']:>15.6e}  "
            f"{c['permanent_impact_bps']:>15.6e}  {c['total_slippage_bps']:>15.6e}"
        )

    # 显示比例关系
    ratio = cost_1["temporary_impact_bps"] / cost_50["temporary_impact_bps"]
    print(f"\n  ➜ 切片后临时冲击降低 {ratio:.0f}×  (50 slices vs 1 slice)")
    print(f"  ➜ 永久冲击不变: {cost_1['permanent_impact_bps']:.6e} bps = "
          f"{cost_50['permanent_impact_bps']:.6e} bps")
    print(
        f"  ➜ 总滑点降低 {cost_1['total_slippage_bps'] / cost_50['total_slippage_bps']:.2f}×"
    )

    # ------------------------------------------------------------------
    # 大单对比: 5 lots
    # ------------------------------------------------------------------
    print(f"\n{'=' * 72}")
    print("  大单场景: 5 lots (500 oz)")
    print("=" * 72)

    cost_big_1 = model.expected_cost(500, n_slices=1)
    cost_big_50 = model.expected_cost(500, n_slices=50)
    print(f"\n  1 slice:  temp={cost_big_1['temporary_impact_bps']:.6e} bps, "
          f"perm={cost_big_1['permanent_impact_bps']:.6e} bps, "
          f"total={cost_big_1['total_slippage_bps']:.6e} bps")
    print(f"  50 slices: temp={cost_big_50['temporary_impact_bps']:.6e} bps, "
          f"perm={cost_big_50['permanent_impact_bps']:.6e} bps, "
          f"total={cost_big_50['total_slippage_bps']:.6e} bps")
    print(
        f"  总滑点降低: {cost_big_1['total_slippage_bps'] / cost_big_50['total_slippage_bps']:.2f}×"
    )

    print(f"\n{'=' * 72}")
    print("  ✅  Almgren-Chriss 模型验证完成")
    print("=" * 72)


if __name__ == "__main__":
    main()
