#!/usr/bin/env python3
"""
test_alerter.py — 告警系统单元测试

验证:
1. 不传 webhook 时, 4 个便捷方法都能输出到 console + log
2. level 过滤: min_level='WARNING' 时 INFO 不应写入 log
3. log_file 正确追加写入
"""

import os
import re
import sys

# ── 确保项目根目录在 sys.path ──
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.dirname(_HERE)
if _PROJECT not in sys.path:
    sys.path.insert(0, _PROJECT)

from monitor.alerter import Alerter, DEBUG, INFO, WARNING, ERROR, CRITICAL

LOG_FILE = "logs/test_alerter.log"
PASS = "✅ PASS"
FAIL = "❌ FAIL"
_total = 0
_passed = 0

# 日志中一行条目的头部格式: "YYYY-MM-DD HH:MM:SS | [LEVEL]"
_ENTRY_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \| \[\w+\]")


def _clean():
    if os.path.exists(LOG_FILE):
        os.remove(LOG_FILE)


def _entry_count(path: str) -> int:
    """统计日志中有多少条独立告警 (按 timestamp 行计数)"""
    if not os.path.exists(path):
        return 0
    with open(path, "r", encoding="utf-8") as f:
        return sum(1 for line in f if _ENTRY_RE.match(line))


def _read_all(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _check(ok: bool, label: str, detail: str = ""):
    global _total, _passed
    _total += 1
    if ok:
        _passed += 1
        print(f"  {PASS} {label}")
    else:
        print(f"  {FAIL} {label} {detail}")


# ── 测试用例 ──


def test_basic_output():
    """Test 1: 4 个便捷方法 + send 都能输出到 console + log"""
    print("\n" + "=" * 50)
    print("  Test 1: Basic Output (no webhook)")
    print("=" * 50)
    _clean()
    a = Alerter({"log_file": LOG_FILE, "min_level": "DEBUG"})

    # 5 条告警
    a.circuit_tripped("test_breaker", "模拟熔断触发", {"equity": 95000, "balance": 100000})
    a.daily_loss(3.5, 5.0, 100000)   # WARNING
    a.daily_loss(6.0, 5.0, 100000)   # ERROR
    a.trade_closed("test_strat", 250.0, "BTCUSDT")
    a.trade_closed("test_strat", -150.0, "ETHUSDT")

    cnt = _entry_count(LOG_FILE)
    _check(cnt == 5, f"log has 5 entries (got {cnt})", f"got {cnt}")


def test_level_filter():
    """Test 2: min_level='WARNING' 时 DEBUG/INFO 不应写入 log"""
    print("\n" + "=" * 50)
    print("  Test 2: Level Filtering (min_level=WARNING)")
    print("=" * 50)
    _clean()
    a = Alerter({"log_file": LOG_FILE, "min_level": "WARNING"})

    a.send(DEBUG, "D1", "debug msg")
    a.send(INFO, "I1", "info msg")
    a.send(WARNING, "W1", "warning msg")
    a.send(ERROR, "E1", "error msg")
    a.send(CRITICAL, "C1", "critical msg")

    cnt = _entry_count(LOG_FILE)
    _check(cnt == 3, f"expected 3 entries (WARNING+ERROR+CRITICAL), got {cnt}", f"got {cnt}")

    content = _read_all(LOG_FILE)
    _check("D1" not in content, "DEBUG filtered out")
    _check("I1" not in content, "INFO filtered out")
    _check("W1" in content, "WARNING present")
    _check("E1" in content, "ERROR present")
    _check("C1" in content, "CRITICAL present")


def test_level_filter_info():
    """Test 3: min_level=INFO 时 DEBUG 不应写入"""
    print("\n" + "=" * 50)
    print("  Test 3: Level Filtering (min_level=INFO)")
    print("=" * 50)
    _clean()
    a = Alerter({"log_file": LOG_FILE, "min_level": "INFO"})

    a.send(DEBUG, "D2", "debug msg")
    a.send(INFO, "I2", "info msg")
    a.send(WARNING, "W2", "warning msg")

    cnt = _entry_count(LOG_FILE)
    _check(cnt == 2, f"expected 2 entries (INFO+WARNING), got {cnt}", f"got {cnt}")

    content = _read_all(LOG_FILE)
    _check("D2" not in content, "DEBUG filtered out")
    _check("I2" in content, "INFO present")
    _check("W2" in content, "WARNING present")


def test_convenience_methods():
    """Test 4: 便捷方法生成的 log 格式包含关键字段"""
    print("\n" + "=" * 50)
    print("  Test 4: Convenience Method Format")
    print("=" * 50)
    _clean()
    a = Alerter({"log_file": LOG_FILE, "min_level": "DEBUG"})

    a.circuit_tripped("cb_vol", "Vol spike", {"atr": 2.5})
    a.daily_loss(4.2, 5.0, 95000)
    a.trade_closed("rsi_strat", 100.0, "SOLUSDT")

    content = _read_all(LOG_FILE)
    _check("Circuit Breaker" in content, "circuit_tripped title")
    _check("cb_vol" in content, "circuit_tripped includes breaker_name")
    _check("Daily Loss" in content, "daily_loss title")
    _check("4.20" in content or "4.2" in content, "daily_loss includes pct")
    _check("Trade Closed" in content, "trade_closed title")
    _check("SOLUSDT" in content, "trade_closed includes symbol")


def test_append():
    """Test 5: 追加写入而非覆盖"""
    print("\n" + "=" * 50)
    print("  Test 5: Append Mode")
    print("=" * 50)
    _clean()
    a = Alerter({"log_file": LOG_FILE, "min_level": "DEBUG"})

    a.send(INFO, "First", "line one")
    cnt1 = _entry_count(LOG_FILE)

    a.send(INFO, "Second", "line two")
    cnt2 = _entry_count(LOG_FILE)

    _check(cnt2 == cnt1 + 1, f"appended 1 entry (was {cnt1}, now {cnt2})")


def test_no_log_file_doesnt_crash():
    """Test 6: 不设 log_file 不报错"""
    print("\n" + "=" * 50)
    print("  Test 6: No log_file configured")
    print("=" * 50)
    a = Alerter({"min_level": "DEBUG"})
    try:
        a.send(INFO, "NoFile", "should not crash")
        a.circuit_tripped("br", "r", {})
        a.daily_loss(1.0, 5.0, 100)
        a.trade_closed("s", 10.0, "X")
        _check(True, "no exception raised")
    except Exception as e:
        _check(False, f"unexpected exception: {e}")


def test_self_test():
    """Test 7: Alerter.test() writes all levels to log"""
    print("\n" + "=" * 50)
    print("  Test 7: Self-Test")
    print("=" * 50)
    _clean()
    a = Alerter({"log_file": LOG_FILE, "min_level": "DEBUG"})
    a.test()
    cnt = _entry_count(LOG_FILE)
    _check(cnt == 5, f"test() wrote 5 entries (DEBUG~CRITICAL), got {cnt}", f"got {cnt}")


# ── 入口 ──


def main():
    print(f"Python: {sys.version}")
    print(f"CWD   : {os.getcwd()}")

    test_basic_output()
    test_level_filter()
    test_level_filter_info()
    test_convenience_methods()
    test_append()
    test_no_log_file_doesnt_crash()
    test_self_test()

    print("\n" + "=" * 50)
    print(f"  Results: {_passed}/{_total} passed")
    print("=" * 50)

    # ── 打印最后 5 行日志 ──
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        tail = lines[-5:]
        print(f"\nLast {len(tail)} lines of {LOG_FILE}:")
        print("-" * 50)
        for line in tail:
            print(line.rstrip())
        print("-" * 50)
    else:
        print(f"\n(no log file at {LOG_FILE})")

    return _passed == _total


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
