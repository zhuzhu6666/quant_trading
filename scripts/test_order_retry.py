"""scripts/test_order_retry.py - OrderRejectionSimulator 3 case"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from execution.order_retry import OrderRejectionSimulator


def main():
    print("=" * 72)
    print("  OrderRejectionSimulator - 3 case")
    print("=" * 72)

    # Case A
    sim_a = OrderRejectionSimulator({"seed": 100})
    first_a = retry_a = fail_a = 0
    for _ in range(1000):
        ok, n = sim_a.try_open_with_retry(lambda: (True, "filled"))
        if ok and n == 0:
            first_a += 1
        elif ok:
            retry_a += 1
        else:
            fail_a += 1
    s = sim_a.summary()
    print(f"\n  Case A: normal (rate=2%), n=1000")
    print(f"    first-try:  {first_a} ({first_a/10:.1f}%)")
    print(f"    retry:      {retry_a} ({retry_a/10:.1f}%)")
    print(f"    failed:     {fail_a} ({fail_a/10:.1f}%)")
    print(f"    attempts:   {s['attempts']}  rejections: {s['rejections']}  fills: {s['fills']}")

    # Case B
    sim_b = OrderRejectionSimulator({"seed": 100})
    first_b = retry_b = fail_b = 0
    for _ in range(1000):
        ok, n = sim_b.try_open_with_retry(lambda: (True, "filled"), is_event_day=True)
        if ok and n == 0:
            first_b += 1
        elif ok:
            retry_b += 1
        else:
            fail_b += 1
    s = sim_b.summary()
    print(f"\n  Case B: event day (rate=6%), n=1000")
    print(f"    first-try:  {first_b} ({first_b/10:.1f}%)")
    print(f"    retry:      {retry_b} ({retry_b/10:.1f}%)")
    print(f"    failed:     {fail_b} ({fail_b/10:.1f}%)")
    print(f"    attempts:   {s['attempts']}  rejections: {s['rejections']}  fills: {s['fills']}")

    # Case C
    sim_c = OrderRejectionSimulator({"seed": 42, "base_reject_rate": 0.5})
    first_c = retry_c = fail_c = 0
    for _ in range(50):
        ok, n = sim_c.try_open_with_retry(lambda: (True, "filled"))
        if ok and n == 0:
            first_c += 1
        elif ok:
            retry_c += 1
        else:
            fail_c += 1
    s = sim_c.summary()
    print(f"\n  Case C: high rate (50%), n=50")
    print(f"    first-try:  {first_c} ({first_c*2:.1f}%)")
    print(f"    retry:      {retry_c} ({retry_c*2:.1f}%)")
    print(f"    failed:     {fail_c} ({fail_c*2:.1f}%)")
    print(f"    attempts:   {s['attempts']}  rejections: {s['rejections']}  fills: {s['fills']}")

    print("\n" + "=" * 72)
    print("  All cases complete")
    print("=" * 72)


if __name__ == "__main__":
    main()
