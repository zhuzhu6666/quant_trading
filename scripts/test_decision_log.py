#!/usr/bin/env python3
"""
test_decision_log.py — 决策审计轨迹单元测试 (Task 16 / P9)

验证:
1. DecisionLogStore 创建 + 建表
2. log() 单条插入
3. log_batch() 批量插入 100 条, 覆盖 5 种 decision_type
4. log_batch() 性能: 10 条 < 0.1s
5. query(run_id=1) 正确返回 100 条
6. run_summary() 输出摘要统计
7. data/decision_log.db 存在且行数正确

约束:
- 新 DB data/decision_log.db, 不动 analytics.db
- 不动 paper_trader / main / 已有策略
"""

import json
import os
import sys
import time

# ── 确保项目根目录在 sys.path ──
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.dirname(_HERE)
if _PROJECT not in sys.path:
    sys.path.insert(0, _PROJECT)

from db.store import DecisionLogStore

DB_PATH = "data/decision_log.db"
PASS = "✅ PASS"
FAIL = "❌ FAIL"
_total = 0
_passed = 0


def _clean():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)


def _check(ok: bool, label: str, detail: str = ""):
    global _total, _passed
    _total += 1
    if ok:
        _passed += 1
        print(f"  {PASS} {label}")
    else:
        print(f"  {FAIL} {label} {detail}")


def _make_record(run_id: int, idx: int) -> dict:
    """生成一条模拟决策日志记录。"""
    # 轮转 5 种 decision_type
    types = ["signal", "risk_check", "open", "close", "circuit_trip", "router_select"]
    decision_type = types[idx % len(types)]

    ts = 1700000000.0 + idx * 3600.0
    bar_date = "2025-11-14"

    # 不同 type 构造不同决策值
    if decision_type == "signal":
        direction = 1 if idx % 3 != 0 else -1
        decision = "pass" if idx % 5 != 1 else "block"
        strategy = f"strategy_{idx % 4 + 1}"
        regime = "TRENDING_UP" if idx % 2 == 0 else "RANGING"
        factor_scores = json.dumps({"rsi": 30.0 + idx, "adx": 25.0 + idx * 0.5})
        confidence = round(0.5 + (idx % 50) / 100.0, 2)
        meta = json.dumps({"atr": 0.005 + idx * 0.0001})
    elif decision_type == "risk_check":
        direction = 0
        decision = "pass" if idx % 4 != 1 else "block"
        meta = json.dumps({"max_dd": 5.0, "current_dd": 1.0 + (idx % 10) * 0.5})
        factor_scores = None
        confidence = None
        strategy = ""
        regime = ""
    elif decision_type == "open":
        direction = 1 if idx % 2 == 0 else -1
        decision = "execute"
        meta = json.dumps({"price": 45000.0 + idx * 10, "lots": 0.1, "sl": 44900.0, "tp": 45200.0})
        factor_scores = None
        confidence = round(0.6 + (idx % 30) / 100.0, 2)
        strategy = f"strategy_{idx % 3 + 1}"
        regime = "TRENDING_UP" if idx % 2 == 0 else "HIGH_VOL"
    elif decision_type == "close":
        direction = 0
        decision = "execute"
        pnl = 50.0 * (1 if idx % 3 != 0 else -1)
        meta = json.dumps({"pnl": pnl, "close_price": 45100.0 + idx * 5, "hold_bars": idx % 20 + 1})
        factor_scores = None
        confidence = None
        strategy = f"strategy_{idx % 3 + 1}"
        regime = ""
    elif decision_type == "circuit_trip":
        direction = 0
        decision = "block"
        breaker_reasons = ["max_daily_loss", "consecutive_losses", "volatility", "slippage"]
        meta = json.dumps({"breaker": breaker_reasons[idx % len(breaker_reasons)], "reason": f"模拟{breaker_reasons[idx % len(breaker_reasons)]}"})
        factor_scores = None
        confidence = None
        strategy = ""
        regime = ""
    else:  # router_select
        direction = 0
        decision = "execute"
        strategies = ["strategy_1", "strategy_2", "strategy_3", "mab_router"]
        selected = strategies[idx % len(strategies)]
        meta = json.dumps({"selected": selected, "scores": {"s1": 0.8, "s2": 0.6}})
        factor_scores = None
        confidence = round(0.5 + (idx % 40) / 100.0, 2)
        strategy = "router"
        regime = ""

    return {
        "run_id": run_id,
        "ts": ts,
        "bar_date": bar_date,
        "decision_type": decision_type,
        "strategy": strategy,
        "regime": regime,
        "direction": direction,
        "confidence": confidence,
        "factor_scores": factor_scores,
        "decision": decision,
        "meta": meta,
    }


# ── 测试用例 ──


def test_init():
    """Test 1: 初始化 -> DB 文件存在 + 表已建"""
    print("\n" + "=" * 55)
    print("  Test 1: Store Initialization")
    print("=" * 55)
    _clean()
    store = DecisionLogStore(DB_PATH)
    _check(os.path.exists(DB_PATH), f"DB file created at {DB_PATH}")

    # 验证表存在
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    conn.close()
    table_names = [r[0] for r in tables]
    _check("decision_log" in table_names, "decision_log table exists", f"tables: {table_names}")


def test_single_insert():
    """Test 2: 单条插入 -> log() 返回 log_id"""
    print("\n" + "=" * 55)
    print("  Test 2: Single Insert")
    print("=" * 55)
    _clean()
    store = DecisionLogStore(DB_PATH)

    log_id = store.log(
        run_id=1,
        ts=1700000000.0,
        bar_date="2025-11-14",
        decision_type="signal",
        strategy="strategy_1",
        regime="TRENDING_UP",
        direction=1,
        confidence=0.85,
        factor_scores=json.dumps({"rsi": 65.0, "adx": 30.0}),
        decision="pass",
        meta=json.dumps({"atr": 0.0035}),
    )
    _check(isinstance(log_id, int) and log_id >= 1,
           f"log() returned log_id={log_id}", f"got {log_id} type={type(log_id)}")


def test_batch_insert():
    """Test 3: 批量插入 100 条 (覆盖 6 种 decision_type)"""
    print("\n" + "=" * 55)
    print("  Test 3: Batch Insert (100 records)")
    print("=" * 55)
    _clean()
    store = DecisionLogStore(DB_PATH)

    records = [_make_record(1, i) for i in range(100)]
    n = store.log_batch(records)
    _check(n == 100, f"log_batch returned {n}", f"got {n}")

    # 查询验证行数
    count = store.query(run_id=1, limit=9999)
    _check(len(count) == 100, f"query returns {len(count)} rows", f"got {len(count)}")

    # 验证每种 decision_type 都有记录
    types_in_db = count["decision_type"].unique()
    expected = ["signal", "risk_check", "open", "close", "circuit_trip", "router_select"]
    for t in expected:
        ok = t in types_in_db
        label = f"  decision_type '{t}' present"
        _check(ok, label, f"found {sorted(types_in_db)}")
        if not ok:
            break


def test_batch_performance():
    """Test 4: 批量插入性能 (10 条 < 0.1s)"""
    print("\n" + "=" * 55)
    print("  Test 4: Batch Performance")
    print("=" * 55)
    _clean()
    store = DecisionLogStore(DB_PATH)

    records = [_make_record(2, i) for i in range(10)]
    start = time.perf_counter()
    store.log_batch(records)
    elapsed = time.perf_counter() - start

    _check(elapsed < 0.1,
           f"10 records in {elapsed*1000:.1f}ms (< 100ms)", f"took {elapsed*1000:.1f}ms")


def test_query():
    """Test 5: query() 按 type 和 strategy 过滤"""
    print("\n" + "=" * 55)
    print("  Test 5: Query Filtering")
    print("=" * 55)
    _clean()
    store = DecisionLogStore(DB_PATH)

    records = [_make_record(1, i) for i in range(100)]
    store.log_batch(records)

    # 按 type 过滤
    df = store.query(run_id=1, decision_type="signal")
    _check(len(df) > 0, f"query(type=signal) -> {len(df)} rows", f"got {len(df)}")

    # 按 strategy 过滤
    df2 = store.query(strategy="strategy_1")
    _check(len(df2) > 0, f"query(strategy=strategy_1) -> {len(df2)} rows", f"got {len(df2)}")

    # 组合过滤
    df3 = store.query(run_id=1, decision_type="open")
    _check(len(df3) > 0, f"query(run_id=1, type=open) -> {len(df3)} rows", f"got {len(df3)}")

    # limit 生效
    df4 = store.query(run_id=1, limit=10)
    _check(len(df4) <= 10, f"query(limit=10) -> {len(df4)} rows", f"got {len(df4)}")


def test_run_summary():
    """Test 6: run_summary() 返回统计摘要"""
    print("\n" + "=" * 55)
    print("  Test 6: Run Summary")
    print("=" * 55)
    _clean()
    store = DecisionLogStore(DB_PATH)

    records = [_make_record(1, i) for i in range(100)]
    store.log_batch(records)

    summary = store.run_summary(1)

    _check(summary["run_id"] == 1, "run_id in summary", f"got {summary['run_id']}")
    _check(summary["total_logs"] == 100, f"total_logs = {summary['total_logs']}", f"got {summary['total_logs']}")
    _check("type_counts" in summary, "type_counts present")
    _check("decision_distribution" in summary, "decision_distribution present")
    _check(isinstance(summary["block_rate"], float), f"block_rate = {summary['block_rate']}", f"got {type(summary['block_rate'])}")
    _check("router_selection" in summary, "router_selection present")

    print(f"\n  ── run_summary output ──")
    print(f"    Total logs    : {summary['total_logs']}")
    print(f"    Type counts   : {dict(sorted(summary['type_counts'].items()))}")
    print(f"    Decision dist : {dict(sorted(summary['decision_distribution'].items()))}")
    print(f"    Block rate    : {summary['block_rate']:.2%}")
    print(f"    Router sel    : {summary['router_selection']}")
    print(f"  ────────────────────")


def test_file_integrity():
    """Test 7: DB 文件完整性 — 100 行"""
    print("\n" + "=" * 55)
    print("  Test 7: DB File Integrity")
    print("=" * 55)

    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT COUNT(*) AS c FROM decision_log").fetchone()
    conn.close()

    n = int(row[0])
    _check(n == 100, f"decision_log has {n} rows (expected 100)", f"got {n}")


# ── 入口 ──


def main():
    print(f"Python    : {sys.version}")
    print(f"CWD       : {os.getcwd()}")
    print(f"DB path   : {os.path.join(os.getcwd(), DB_PATH)}")

    test_init()
    test_single_insert()
    test_batch_insert()
    test_batch_performance()
    test_query()
    test_run_summary()
    test_file_integrity()

    print("\n" + "=" * 55)
    print(f"  Results: {_passed}/{_total} passed")
    print("=" * 55)

    return _passed == _total


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
