"""
P2 SL/TP 事件日 spread 注入 (M1.2, 2026-06-03)

FOMC/NFP/CPI 事件日 broker spread 实际 1-3 USD (XAUUSD+, 100-300 points).
我们 db 里只有 10% bar 有真实 spread (broker 限 5000 历史), 其余 fallback 0.13 USD.

方法:
  1. 从 db events 表拿 FOMC/NFP/CPI 日期
  2. 临时把当天 + 后 N 小时的 bar spread 注入到 100-300 points
  3. 跑 P2 paper mode 一次 (新逻辑)
  4. 跟老 spread 跑出来的 PnL 对比

输出: data/charts/m1_event_spread_report.txt
"""
import sys
import os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import logging
import sqlite3
import shutil
import time as _time
import numpy as np
import pandas as pd

logging.basicConfig(level=logging.WARNING)
log = logging.getLogger("event_spread")
log.setLevel(logging.INFO)

DB_PATH = "data/market_data.db"
DB_BACKUP = "data/market_data.db.pre_event_spread"
REPORT = Path("data/charts/m1_event_spread_report.txt")

# 事件日 spread 注入: 当天 (UTC) + 后 4 小时, spread 200 points = 2.00 USD
EVENT_SPREAD_POINTS = 200
EVENT_HOURS_AFTER = 24  # FOMC/NFP/CPI 12:30 UTC 公布, 24h 窗口覆盖事件 + 后续波动
EVENT_TYPES = ("FOMC", "NFP", "CPI")


def get_event_dates(conn) -> list[tuple[int, str]]:
    """拿 FOMC/NFP/CPI 日期 → (epoch seconds 当天 00:00 UTC, type)."""
    # 只取 db 范围内的事件
    r = conn.execute(
        "SELECT MIN(time), MAX(time) FROM bars WHERE symbol='XAUUSD+' AND timeframe='M15'"
    ).fetchone()
    db_min_date = pd.to_datetime(r[0], unit="s").strftime("%Y-%m-%d")
    db_max_date = pd.to_datetime(r[1], unit="s").strftime("%Y-%m-%d")

    placeholders = ",".join("?" * len(EVENT_TYPES))
    q = (f"SELECT date, type FROM events "
         f"WHERE type IN ({placeholders}) AND date >= ? AND date <= ? "
         f"ORDER BY date")
    rows = conn.execute(q, EVENT_TYPES + (db_min_date, db_max_date)).fetchall()
    out = []
    for d, t in rows:
        ts = int(pd.Timestamp(d).timestamp())
        out.append((ts, t))
    return out


def inject_event_spreads(conn, events: list[tuple[int, str]], points: int, hours_after: int):
    """把所有 event 当天 + 后 N 小时的 bar spread 临时设成 points.
    events: [(ts_00_utc, type), ...]
    返回 (n_injected, by_type).
    """
    n_total = 0
    by_type = {}
    for ev_ts, ev_type in events:
        start = ev_ts
        end = ev_ts + hours_after * 3600
        cur = conn.execute(
            "UPDATE bars SET spread=? WHERE symbol='XAUUSD+' AND timeframe='M15' AND time >= ? AND time < ?",
            (points, start, end),
        )
        n_total += cur.rowcount
        by_type[ev_type] = by_type.get(ev_type, 0) + cur.rowcount
    conn.commit()
    return n_total, by_type


def restore_db():
    """还原 db 到事件注入前的状态.

    BUG-14 (audit 2026-06-04): 失败时 caller 应当 raise SystemExit,
    不能让注入的事件 spread 留在 live db 里污染后续 run。
    """
    if not Path(DB_BACKUP).exists():
        raise FileNotFoundError(
            f"DB backup {DB_BACKUP} 不存在, 无法还原, "
            f"已注入的事件 spread 会留在 live db 里!"
        )
    shutil.copy(DB_BACKUP, DB_PATH)
    # 验证还原成功
    if not Path(DB_PATH).exists():
        raise IOError(f"Restore failed: {DB_PATH} 不存在 after copy")
    return True


def backup_db():
    if not Path(DB_BACKUP).exists():
        shutil.copy(DB_PATH, DB_BACKUP)
        # 验证 backup 成功 (BUG-14: 防止 shutil.copy 静默失败)
        assert Path(DB_BACKUP).exists(), (
            f"BUG-14: backup {DB_BACKUP} 创建后不存在, shutil.copy 静默失败"
        )


def run_paper_and_summarize(enable_circuit: bool = False) -> dict:
    """跑一次 main.py paper 模式, 返回解析后的 summary."""
    import subprocess
    py_exe = r"C:\Users\zhu\AppData\Local\Programs\Python\Python312\python.exe"
    cmd = f'"{py_exe}" main.py --mode paper --symbol XAUUSD+ --timeframe M15'
    t0 = _time.time()
    proc = subprocess.run(cmd, cwd=".", capture_output=True, text=True,
                          timeout=600, shell=True)
    elapsed = _time.time() - t0
    log.info(f"paper run exit={proc.returncode} elapsed={elapsed:.1f}s "
             f"out={len(proc.stdout)} chars err={len(proc.stderr)} chars")
    return {
        "elapsed": elapsed,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "returncode": proc.returncode,
    }


def parse_paper_summary(text: str) -> dict:
    import re
    out = {"ret_pct": None, "n_trades": None, "sharpe": None, "dd_pct": None,
           "pf": None, "wr_pct": None, "balance": None}
    patterns = {
        "ret_pct":   r"Net PnL[^\n]*\(\+?([\-\d\.]+)\s*%\)",
        "n_trades":  r"Trades\s*:\s*(\d+)",
        "dd_pct":    r"Max Drawdown\s*:\s*([\d\.]+)\s*%",
        "sharpe":    r"Sharpe \(ann\.\)\s*:\s*([\-\d\.]+)",
        "pf":        r"PF=([\d\.]+)",
        "wr_pct":    r"WR=([\d\.]+)\s*%",
        "balance":   r"Final\s*:\s*\$?([\-\d\.]+)",
    }
    for k, p in patterns.items():
        m = re.search(p, text)
        if m:
            try:
                out[k] = float(m.group(1))
            except ValueError:
                pass
    return out


def main():
    print("=" * 60)
    print("M1.2: P2 SL/TP 事件日 spread 注入 (2026-06-03)")
    print("=" * 60)

    conn = sqlite3.connect(DB_PATH)
    events = get_event_dates(conn)
    print(f"\n事件数 (db 范围内): {len(events)} (类型: {EVENT_TYPES})")
    if events:
        from datetime import datetime
        print(f"  范围: {datetime.utcfromtimestamp(min(e[0] for e in events))} → "
              f"{datetime.utcfromtimestamp(max(e[0] for e in events))}")

    # 备份
    backup_db()
    print(f"\nDB 备份: {DB_BACKUP}")

    # 1) 跑 baseline (spread 是 broker 真实 + 老 bar fallback 0.13)
    print("\n[1/2] 跑 baseline (db 原状, spread 真实 + 0.13 fallback)...")
    base = run_paper_and_summarize()
    base_sum = parse_paper_summary(base["stdout"] + base["stderr"])
    print(f"  ret={base_sum['ret_pct']}% trades={base_sum['n_trades']} "
          f"sharpe={base_sum['sharpe']} dd={base_sum['dd_pct']}%")

    # 2) 注入事件日 spread
    n_inj, by_type = inject_event_spreads(conn, events, EVENT_SPREAD_POINTS, EVENT_HOURS_AFTER)
    print(f"\n[注入] 事件日 spread={EVENT_SPREAD_POINTS} points ({EVENT_SPREAD_POINTS*0.01:.2f} USD), "
          f"覆盖后 {EVENT_HOURS_AFTER}h, 共 {n_inj} bar (by_type: {by_type})")

    # 3) 跑注入后
    print("\n[2/2] 跑注入后 (事件日 spread=2.00 USD)...")
    inj = run_paper_and_summarize()
    inj_sum = parse_paper_summary(inj["stdout"] + inj["stderr"])
    print(f"  ret={inj_sum['ret_pct']}% trades={inj_sum['n_trades']} "
          f"sharpe={inj_sum['sharpe']} dd={inj_sum['dd_pct']}%")

    # 4) 还原
    conn.close()
    print(f"\n[还原] DB 还原到 {DB_PATH}")
    # BUG-14 (audit 2026-06-04): restore_db 现在 raise on 失败, 不再静默
    try:
        restore_db()
        print("  ✅ DB 还原成功")
    except (FileNotFoundError, IOError) as e:
        print(f"  ❌ Restore failed: {e}")
        # abort: 不写 report (会误导), 退出码 1
        raise SystemExit(1) from e

    # 5) 写报告
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("M1.2: P2 SL/TP 事件日 spread 注入 (2026-06-03)\n")
        f.write("=" * 70 + "\n\n")
        f.write(f"事件类型: {', '.join(EVENT_TYPES)}\n")
        f.write(f"事件数 (db 范围内): {len(events)}\n")
        f.write(f"注入 spread: {EVENT_SPREAD_POINTS} points = {EVENT_SPREAD_POINTS*0.01:.2f} USD/oz\n")
        f.write(f"持续时间: 事件日 + 后 {EVENT_HOURS_AFTER} 小时\n")
        f.write(f"覆盖 bar: {n_inj} (M15) by_type: {by_type}\n\n")
        f.write(f"{'Metric':<14s} {'Baseline':>14s} {'Event+Spread':>14s} {'Δ':>14s}\n")
        f.write("-" * 60 + "\n")
        for k in ["ret_pct", "n_trades", "sharpe", "dd_pct", "pf", "wr_pct", "balance"]:
            v1 = base_sum.get(k)
            v2 = inj_sum.get(k)
            if v1 is None and v2 is None:
                continue
            d = (v2 - v1) if (v1 is not None and v2 is not None) else None
            v1s = f"{v1:.2f}" if v1 is not None else "—"
            v2s = f"{v2:.2f}" if v2 is not None else "—"
            ds = f"{d:+.2f}" if d is not None else "—"
            f.write(f"{k:<14s} {v1s:>14s} {v2s:>14s} {ds:>14s}\n")
        f.write("\n")
        f.write("=" * 70 + "\n")
        f.write("结论 (2026-06-03)\n")
        f.write("=" * 70 + "\n")
        if base_sum.get("ret_pct") and inj_sum.get("ret_pct"):
            ret_drop = base_sum["ret_pct"] - inj_sum["ret_pct"]
            f.write(f"- 事件日 spread 2.00 USD (vs baseline 0.13 USD) → PnL 变化 {ret_drop:+.2f}%\n")
            f.write(f"  (注入后 spread 加大, 假设交易成本上升, 因此 PnL 应下降)\n")
            f.write(f"- baseline 事件日是 24h 内 spread 100-300 points = 1-3 USD\n")
            f.write(f"- 真实影响: 事件日每笔 trade 多付 {EVENT_SPREAD_POINTS*0.01/2:.2f} USD 滑点\n")
            f.write(f"- 如果 {len(events)} 个事件日各跳过/调整, 避免 {-ret_drop:.2f}% PnL 损失\n")
        else:
            f.write("- PnL 解析失败, 看 raw output\n")
        f.write("\n")
        f.write(f"DB 状态: 已还原到注入前 ({'OK' if restore_ok else '⚠️ 没还原'})\n")
        f.write(f"  原始 backup: {DB_BACKUP}\n")
        f.write(f"  跑命令: python scripts/m1_event_spread.py (可重跑)\n")

    print(f"\n报告: {REPORT}")


if __name__ == "__main__":
    main()
