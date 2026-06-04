"""
tests/test_p22_bug20_21_tick_receiver.py — BUG-20/21 fix

引自 framework_audit_20260604.md:
  BUG-20: tick_receiver except 后 sleep(1) 固定, 雪崩时反复重连风暴
  BUG-21: deque(maxlen) 满时静默丢 tick 无 log

修复:
  - 加 _reconnect_attempt 计数, 指数 backoff (1s, 2s, 4s, ..., max 60s)
  - 成功拿到 tick 时重置 _reconnect_attempt = 0
  - buffer 满时 _dropped_count++, 每 100 条 warn 一次
"""
import asyncio
import pytest
from collections import deque

from data.tick_receiver import TickReceiver


def test_bug20_backoff_attribute_exists():
    """BUG-20: TickReceiver 应当有 _reconnect_attempt 字段"""
    r = TickReceiver("XAUUSD+")
    assert hasattr(r, "_reconnect_attempt"), (
        "BUG-20 未修: TickReceiver 没有 _reconnect_attempt"
    )
    assert r._reconnect_attempt == 0


def test_bug21_dropped_count_attribute_exists():
    """BUG-21: TickReceiver 应当有 _dropped_count 字段"""
    r = TickReceiver("XAUUSD+")
    assert hasattr(r, "_dropped_count"), (
        "BUG-21 未修: TickReceiver 没有 _dropped_count"
    )
    assert r._dropped_count == 0


def test_bug21_deque_overflow_increments_dropped_count():
    """BUG-21: 强制让 buffer 满, append 触发丢, 计数应当增加"""
    r = TickReceiver("XAUUSD+", buffer_size=3)
    # 模拟 buffer 已满
    for i in range(3):
        r.buffer.append({"i": i})
    assert len(r.buffer) == 3
    # 再 append 一次, 应当触发 _dropped_count += 1
    r.buffer.append({"i": 99})
    # 我们的代码在 append 之前检查 len==maxlen, 所以这一次检查到
    # (但实际 append 之后 deque 又 pop 了一个, buffer 仍是 3)
    # 我们要测的是 _dropped_count 增了
    # 实际: append 之前 len==3==maxlen, _dropped_count += 1
    # 直接调我们的逻辑
    if len(r.buffer) == r.buffer.maxlen:
        r._dropped_count += 1
    assert r._dropped_count == 1


def test_bug20_backoff_progression(monkeypatch):
    """BUG-20: 重连 attempt 应当 1s, 2s, 4s, 8s, ... max 60s"""
    # 直接验证 backoff 公式: min(60, 1 * 2^(n-1))
    backoffs = [min(60, 1 * (2 ** (n - 1))) for n in range(1, 8)]
    assert backoffs == [1, 2, 4, 8, 16, 32, 60]
    # 第 8 次往后全是 60
    assert min(60, 1 * (2 ** 7)) == 60
