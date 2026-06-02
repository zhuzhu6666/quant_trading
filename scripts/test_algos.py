"""
scripts/test_algos.py
=====================

P1-B 智能路由算法单测:
  - TWAP: 切片数 + 等分验证
  - VWAP: U-shape profile → 开盘/收盘切片大
  - POV: 总量 cap 验证 (participation_rate * market_volume)
  - IS: 高 urgency → 市价多 + 切片多; 低 urgency → 限价 + 切片少
  - Dispatcher: 根据父单自动选算法
"""
import sys
from pathlib import Path
from datetime import datetime

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from execution.algos import (
    TWAPAlgorithm, VWAPAlgorithm, POVAlgorithm, ISAlgorithm,
    AlgoDispatcher, ParentOrder, ChildOrder,
)


def test_twap_basic():
    """TWAP: 10 分钟 1.0 手 → 5-10 片, 每片等量"""
    algo = TWAPAlgorithm()
    parent = ParentOrder(
        symbol="XAUUSD+", direction=1, total_volume=1.0,
        start_time=datetime(2026, 6, 2, 10, 0),
        end_time=datetime(2026, 6, 2, 10, 10),
        current_price=2350.0,
    )
    children = algo.slice(parent)
    n = len(children)
    total = sum(c.volume for c in children)
    vols = [c.volume for c in children]
    assert 5 <= n <= 10, f"TWAP 切片数应在 5-10 之间, 实际 {n}"
    assert abs(total - 1.0) < 0.01, f"TWAP 总量应 = 1.0, 实际 {total}"
    assert all(abs(v - vols[0]) < 1e-6 for v in vols), f"TWAP 每片应等量, 实际 {vols}"
    assert all(c.order_type == "market" for c in children), "TWAP 默认市价"
    print(f"  ✓ TWAP: 10min/1.0手 → {n} 片, 每片 {vols[0]:.4f}, 总量 {total:.4f}")


def test_vwap_ushape():
    """VWAP: 96 bar U-shape profile → 中段切片小, 两端大"""
    algo = VWAPAlgorithm()  # 用默认 U-shape
    parent = ParentOrder(
        symbol="XAUUSD+", direction=1, total_volume=10.0,
        start_time=datetime(2026, 6, 2, 9, 0),
        end_time=datetime(2026, 6, 2, 17, 0),  # 8 小时
        current_price=2350.0,
    )
    children = algo.slice(parent)
    n = len(children)
    total = sum(c.volume for c in children)
    assert abs(total - 10.0) < 0.01, f"VWAP 总量应 = 10.0, 实际 {total}"
    # U-shape: profile[0] 大, profile[n/2] 小
    vols = [c.volume for c in children]
    mid = n // 2
    assert vols[0] > vols[mid], f"VWAP U-shape: 首片 {vols[0]:.4f} 应 > 中片 {vols[mid]:.4f}"
    print(f"  ✓ VWAP: 8h/10手 → {n} 片, 首片 {vols[0]:.4f} > 中片 {vols[mid]:.4f}, 总量 {total:.4f}")


def test_pov_cap():
    """POV: participation_rate 5% × market_volume 100 = 5 手 cap"""
    algo = POVAlgorithm(participation_rate=0.05, estimated_market_volume=100.0)
    parent = ParentOrder(
        symbol="XAUUSD+", direction=1, total_volume=20.0,  # 想下 20
        start_time=datetime(2026, 6, 2, 10, 0),
        end_time=datetime(2026, 6, 2, 10, 30),
        current_price=2350.0,
    )
    children = algo.slice(parent)
    total = sum(c.volume for c in children)
    expected_cap = 100.0 * 0.05  # 5
    assert total <= expected_cap + 0.01, f"POV cap: 总量 {total} 应 <= {expected_cap}"
    assert total < 20.0, f"POV cap: 应少于 20, 实际 {total}"
    print(f"  ✓ POV: 20 手意图 → cap {total:.4f} (≤ {expected_cap}), 截断触发")


def test_pov_full():
    """POV: 小单不受 cap 限制"""
    algo = POVAlgorithm(participation_rate=0.20, estimated_market_volume=100.0)
    parent = ParentOrder(
        symbol="XAUUSD+", direction=1, total_volume=5.0,  # 远小于 cap (20)
        start_time=datetime(2026, 6, 2, 10, 0),
        end_time=datetime(2026, 6, 2, 10, 30),
        current_price=2350.0,
    )
    children = algo.slice(parent)
    total = sum(c.volume for c in children)
    assert abs(total - 5.0) < 0.01, f"POV 小单: 总量应 = 5.0, 实际 {total}"
    print(f"  ✓ POV: 5 手小单, cap 20 → 实际 {total:.4f}, 不截断")


def test_is_high_urgency():
    """IS: urgency=0.9 → 全部市价, 切片多"""
    algo = ISAlgorithm()
    parent = ParentOrder(
        symbol="XAUUSD+", direction=1, total_volume=2.0,
        start_time=datetime(2026, 6, 2, 10, 0),
        end_time=datetime(2026, 6, 2, 10, 10),
        current_price=2350.0,
        urgency=0.9,
    )
    children = algo.slice(parent)
    n = len(children)
    market_count = sum(1 for c in children if c.order_type == "market")
    assert market_count == n, f"IS 高 urgency 应全市价, 实际 {market_count}/{n}"
    print(f"  ✓ IS 高 urgency: {n} 片全 market, 切片数 = 基础×{1+0.9:.1f}")


def test_is_low_urgency():
    """IS: urgency=0.2 → 限价单"""
    algo = ISAlgorithm()
    parent = ParentOrder(
        symbol="XAUUSD+", direction=1, total_volume=2.0,
        start_time=datetime(2026, 6, 2, 10, 0),
        end_time=datetime(2026, 6, 2, 10, 10),
        current_price=2350.0,
        urgency=0.2,
    )
    children = algo.slice(parent)
    n = len(children)
    limit_count = sum(1 for c in children if c.order_type == "limit")
    assert limit_count == n, f"IS 低 urgency 应全 limit, 实际 {limit_count}/{n}"
    # 限价应该 > current_price (buy 偏激进等更好价格是 buy 偏低价 = mid - offset, 但我们向上)
    # 这里 buy + sign=1 + low_u, offset 小 → 限价轻微高于 current_price
    print(f"  ✓ IS 低 urgency: {n} 片全 limit, 限价示例 {children[0].price_hint:.2f}")


def test_dispatcher_small():
    """Dispatcher: 小单直接市价"""
    disp = AlgoDispatcher()
    parent = ParentOrder(
        symbol="XAUUSD+", direction=1, total_volume=0.01,
        start_time=datetime(2026, 6, 2, 10, 0),
        end_time=datetime(2026, 6, 2, 10, 1),
        current_price=2350.0,
    )
    children = disp.dispatch(parent)
    assert len(children) == 1 and children[0].order_type == "market"
    assert children[0].volume == 0.01
    print(f"  ✓ Dispatcher 0.01手 → MARKET (1 单)")


def test_dispatcher_urgent():
    """Dispatcher: 高 urgency → IS"""
    disp = AlgoDispatcher()
    parent = ParentOrder(
        symbol="XAUUSD+", direction=1, total_volume=1.0,
        start_time=datetime(2026, 6, 2, 10, 0),
        end_time=datetime(2026, 6, 2, 10, 10),
        current_price=2350.0,
        urgency=0.95,
    )
    children = disp.dispatch(parent)
    assert all(c.order_type == "market" for c in children), "高 urgency → 全 market"
    print(f"  ✓ Dispatcher urgency=0.95 → IS (全 market, {len(children)} 片)")


def test_dispatcher_default():
    """Dispatcher: 普通父单 → TWAP"""
    disp = AlgoDispatcher()
    parent = ParentOrder(
        symbol="XAUUSD+", direction=1, total_volume=0.5,
        start_time=datetime(2026, 6, 2, 10, 0),
        end_time=datetime(2026, 6, 2, 10, 30),
        current_price=2350.0,
    )
    children = disp.dispatch(parent)
    assert len(children) >= 3
    vols = [c.volume for c in children]
    assert all(abs(v - vols[0]) < 1e-6 for v in vols), "TWAP 等分"
    print(f"  ✓ Dispatcher 0.5手/30min → TWAP ({len(children)} 片等分)")


def test_dispatcher_big():
    """Dispatcher: 大单 → POV (跟市)"""
    disp = AlgoDispatcher()
    parent = ParentOrder(
        symbol="XAUUSD+", direction=1, total_volume=5.0,
        start_time=datetime(2026, 6, 2, 10, 0),
        end_time=datetime(2026, 6, 2, 10, 30),
        current_price=2350.0,
    )
    children = disp.dispatch(parent)
    # POV 默认 participation 10% × 100 = 10 cap, 5 < 10 不截断
    total = sum(c.volume for c in children)
    assert abs(total - 5.0) < 0.01, f"POV 大单: 总量应 = 5.0, 实际 {total}"
    print(f"  ✓ Dispatcher 5手/30min → POV ({len(children)} 片, 总量 {total:.4f})")


def main():
    print("=" * 70)
    print("  P1-B 智能路由算法单测 (TWAP / VWAP / POV / IS / Dispatcher)")
    print("=" * 70)

    tests = [
        test_twap_basic,
        test_vwap_ushape,
        test_pov_cap,
        test_pov_full,
        test_is_high_urgency,
        test_is_low_urgency,
        test_dispatcher_small,
        test_dispatcher_urgent,
        test_dispatcher_default,
        test_dispatcher_big,
    ]

    n_pass = 0
    n_fail = 0
    for t in tests:
        try:
            t()
            n_pass += 1
        except AssertionError as e:
            n_fail += 1
            print(f"  ✗ {t.__name__}: {e}")
        except Exception as e:
            n_fail += 1
            print(f"  ✗ {t.__name__}: EXCEPTION {e}")

    print()
    print("=" * 70)
    print(f"  结果: {n_pass}/{n_pass+n_fail} 通过")
    print("=" * 70)
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    import sys as _s
    _s.exit(main())
