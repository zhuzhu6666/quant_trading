"""
tests/test_p10_footgun6_order_retry_sleep.py — Batch execution 修复

引自 framework_audit_20260604.md FOOTGUN-6:
execution/order_retry.py:78 算 backoff 但不 sleep, 实盘接入时
退化为"立即重试"风暴。

修复: 加 time.sleep(delay / 1000), 但加 sleep_backoff config 让
paper test 可关掉不浪费测试时间。
"""
import time
from unittest.mock import patch

from execution.order_retry import OrderRejectionSimulator


def test_backoff_sleeps_when_enabled():
    """FOOTGUN-6: 修复后 try_open_with_retry 真的 sleep"""
    sim = OrderRejectionSimulator({
        "base_reject_rate": 1.0,  # 100% reject, 确保走 backoff 分支
        "max_retries": 3,
        "backoff_base_ms": 50,
        "sleep_backoff": True,
    })

    sleep_durations = []
    with patch("execution.order_retry._time.sleep",
               side_effect=lambda d: sleep_durations.append(d)):
        sim.try_open_with_retry(lambda: (True, "filled"))

    # 应当 sleep 2 次 (attempt 0, 1 — 不 sleep 最后一个)
    assert len(sleep_durations) == 2, (
        f"FOOTGUN-6 复发: sleep {len(sleep_durations)} 次, 应为 2"
    )
    # 第一次 backoff ~50ms = 0.05s
    assert 0.04 < sleep_durations[0] < 0.1, (
        f"第一次 backoff {sleep_durations[0]}s 不在 50ms 范围"
    )


def test_backoff_skips_sleep_when_disabled():
    """FOOTGUN-6 修复保留 opt-out: paper test 传 sleep_backoff=False 不 sleep"""
    sim = OrderRejectionSimulator({
        "base_reject_rate": 1.0,
        "max_retries": 3,
        "backoff_base_ms": 50,
        "sleep_backoff": False,
    })

    sleep_called = []
    with patch("execution.order_retry._time.sleep",
               side_effect=lambda d: sleep_called.append(d)):
        sim.try_open_with_retry(lambda: (True, "filled"))

    # 关闭时不应 sleep
    assert len(sleep_called) == 0, (
        f"sleep_backoff=False 时仍 sleep {len(sleep_called)} 次"
    )
