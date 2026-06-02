"""scripts/test_latency.py — LatencySimulator 验证

两个 Case:
  a. std=20 (默认): 均值 ~50ms, p95 < 100ms (lognormal 右尾)
  b. std=100:       均值 ~50ms, p95 显著更大 (离散度大)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution.latency import LatencySimulator


def print_stats(label: str, stats: dict):
    print(f"\n  {label}")
    print(f"    n      = {stats['n']}")
    print(f"    mean   = {stats['mean_ms']:>8.3f} ms")
    print(f"    std    = {stats['std_ms']:>8.3f} ms")
    print(f"    min    = {stats['min_ms']:>8.3f} ms")
    print(f"    max    = {stats['max_ms']:>8.3f} ms")
    print(f"    p50    = {stats['p50_ms']:>8.3f} ms")
    print(f"    p95    = {stats['p95_ms']:>8.3f} ms")
    print(f"    p99    = {stats['p99_ms']:>8.3f} ms")


def main():
    print("=" * 72)
    print("  LatencySimulator — LogNormal 延迟模拟验证")
    print("=" * 72)

    # ── Case A: mean=50, std=20 (默认) ────────────────
    print("\n" + "-" * 72)
    print("  Case A: mean_ms=50, std_ms=20 (默认配置)")
    print("-" * 72)

    sim_a = LatencySimulator()
    sim_a.sample_batch(1000)
    stats_a = sim_a.stats()
    print_stats("Case A stats", stats_a)

    # 验证: mean 接近 50ms, p95 < 100ms
    mean_ok = 40 <= stats_a["mean_ms"] <= 60
    p95_ok = stats_a["p95_ms"] < 100
    print(f"\n    验证 mean∈[40,60]: {'✓' if mean_ok else '✗'}"
          f"  → {stats_a['mean_ms']:.2f} ms")
    print(f"    验证 p95 < 100ms:  {'✓' if p95_ok else '✗'}"
          f"  → {stats_a['p95_ms']:.2f} ms")
    print(f"    → {'✅ ALL PASS' if (mean_ok and p95_ok) else '❌ FAIL'}")

    # ── Case B: mean=50, std=100 ──────────────────────
    print("\n" + "-" * 72)
    print("  Case B: mean_ms=50, std_ms=100 (高离散度)")
    print("-" * 72)

    sim_b = LatencySimulator(config={"mean_ms": 50, "std_ms": 100, "seed": 99})
    sim_b.sample_batch(1000)
    stats_b = sim_b.stats()
    print_stats("Case B stats", stats_b)

    # 验证: mean ~50 (skew 会拉高), p95 明显 > case_a
    mean_b_ok = 40 <= stats_b["mean_ms"] <= 80
    p95_bigger = stats_b["p95_ms"] > stats_a["p95_ms"] * 1.5
    print(f"\n    验证 mean∈[40,80]:  {'✓' if mean_b_ok else '✗'}"
          f"  → {stats_b['mean_ms']:.2f} ms")
    print(f"    验证 p95 > case_a×1.5: {'✓' if p95_bigger else '✗'}"
          f"  → {stats_b['p95_ms']:.2f} > {stats_a['p95_ms']:.2f}×1.5")
    print(f"    → {'✅ ALL PASS' if (mean_b_ok and p95_bigger) else '❌ FAIL'}")

    # ── 对比总结 ──────────────────────────────────────
    print("\n" + "=" * 72)
    print("  对比总结")
    print("=" * 72)
    print(f"               {'Case A (std=20)':>20s}  {'Case B (std=100)':>20s}")
    print(f"  {'mean':>12s}  {stats_a['mean_ms']:>10.3f} ms  {stats_b['mean_ms']:>10.3f} ms")
    print(f"  {'std':>12s}  {stats_a['std_ms']:>10.3f} ms  {stats_b['std_ms']:>10.3f} ms")
    print(f"  {'p50':>12s}  {stats_a['p50_ms']:>10.3f} ms  {stats_b['p50_ms']:>10.3f} ms")
    print(f"  {'p95':>12s}  {stats_a['p95_ms']:>10.3f} ms  {stats_b['p95_ms']:>10.3f} ms")
    print(f"  {'p99':>12s}  {stats_a['p99_ms']:>10.3f} ms  {stats_b['p99_ms']:>10.3f} ms")

    print("\n" + "=" * 72)
    print("  ✅ LatencySimulator 验证完成")
    print("=" * 72)


if __name__ == "__main__":
    main()
