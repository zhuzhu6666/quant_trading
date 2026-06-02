"""
scripts/p1_c_sync_live_bars.py
===============================

P1-C: 从 MT5 拉真实 XAUUSD+ M15 bar, 跟 data/market_data.db 对齐
  1. 拉 broker 真实历史 (5000-50000 bar)
  2. 跟本地 db 对比 (起点/终点/数量)
  3. 找差异: db 缺哪些 bar?
  4. (可选) 补 db / 报告差异

输出: 控制台 + data/charts/p1_c_sync_report.txt

无 broker 时 (mt5.initialize 失败) 友好退出.
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def main():
    import time
    import MetaTrader5 as mt5
    import pandas as pd
    import sqlite3
    from datetime import datetime

    # 1) 尝试连接 MT5
    if not mt5.initialize():
        print(f"  ⚠ mt5.initialize() 失败: {mt5.last_error()}")
        print("  P1-C 需要 MT5 客户端已登录, 跳过")
        return 1
    print("=" * 78)
    print("  P1-C: 拉真实 XAUUSD+ M15 bar vs 本地 data/market_data.db")
    print("=" * 78)

    # 2) 账户状态 (顺便检查)
    acc = mt5.account_info()
    print(f"\n账户: login={acc.login} server={acc.server} balance={acc.balance} "
          f"leverage={acc.leverage}")

    # 3) 拉 broker 5000 根 M15 (≈ 2.5 月, 够增量更新)
    n_pull = 5000
    rates = mt5.copy_rates_from_pos("XAUUSD+", mt5.TIMEFRAME_M15, 0, n_pull)
    if rates is None or len(rates) == 0:
        print(f"  ⚠ 拉取失败: {mt5.last_error()}")
        mt5.shutdown()
        return 1
    broker_bars = len(rates)
    broker_first = datetime.fromtimestamp(rates[0]["time"], tz=__import__("datetime").timezone.utc)
    broker_last = datetime.fromtimestamp(rates[-1]["time"], tz=__import__("datetime").timezone.utc)
    broker_last_close = rates[-1]["close"]
    print(f"\nbroker 数据: {broker_bars} 根 M15 bar")
    print(f"  范围: {broker_first} → {broker_last} (UTC)")
    print(f"  最新 close: {broker_last_close:.2f}")

    # 4) 跟本地 db 对比 (注意: db.time 是 TEXT ISO 格式)
    db_path = PROJECT_ROOT / "data" / "market_data.db"
    con = sqlite3.connect(str(db_path))
    cur = con.cursor()
    cur.execute("""
        SELECT time, open, high, low, close, volume
        FROM bars
        WHERE symbol='XAUUSD+' AND timeframe='M15'
        ORDER BY time DESC
        LIMIT 20
    """)
    db_recent = cur.fetchall()
    cur.execute("""
        SELECT COUNT(*), MIN(time), MAX(time)
        FROM bars
        WHERE symbol='XAUUSD+' AND timeframe='M15'
    """)
    db_count, db_first_str, db_last_str = cur.fetchone()
    # time 列是 TEXT, 直接 parse
    db_first = datetime.fromisoformat(db_first_str)
    db_last = datetime.fromisoformat(db_last_str)
    print(f"\n本地 db: {db_count} 根 M15 bar")
    print(f"  范围: {db_first} → {db_last} (UTC)")
    print(f"  最近 5 根:")
    for r in db_recent[:5]:
        ts = datetime.fromisoformat(r[0])
        print(f"    {ts}  C={r[4]:.2f}  V={r[5]}")

    # 5) 找差异
    print("\n" + "=" * 78)
    print("  对比")
    print("=" * 78)
    print(f"  broker 最新: {broker_last}")
    print(f"  db 最新    : {db_last}")
    # 统一用 naive UTC (db ISO 字符串无 tz, broker 加了 UTC)
    broker_last_naive = broker_last.replace(tzinfo=None)
    db_last_naive = db_last.replace(tzinfo=None) if db_last.tzinfo else db_last
    db_ahead = (broker_last_naive - db_last_naive).total_seconds() / 60
    print(f"  时间差: {db_ahead:.0f} 分钟 ({'db 更早, 有 {db_ahead/15:.0f} bar 缺口' if db_ahead > 0 else 'db 较新'})")

    if db_ahead > 15:
        # 缺新 bar
        broker_set = {int(r["time"]) for r in rates}
        # db 现有 time >= db_last (字符串 ISO, 字典序 == 时间序)
        cur.execute("""
            SELECT time FROM bars
            WHERE symbol='XAUUSD+' AND timeframe='M15' AND time > ?
        """, (db_last_str,))
        db_newer_set = set()
        for r in cur.fetchall():
            try:
                # ISO 'YYYY-MM-DD HH:MM:SS' → epoch
                t = int(datetime.fromisoformat(r[0]).timestamp())
                db_newer_set.add(t)
            except Exception:
                pass
        missing = sorted(broker_set - db_newer_set)
        # 过滤: 缺的是 M15 间隔
        missing_m15 = [t for t in missing if (t - int(broker_first.timestamp())) % 900 == 0]
        print(f"  缺失 bar 数: {len(missing)} (broker 总 {broker_bars} - db 已有)")
        if missing_m15:
            print(f"  M15 间隔对齐后: {len(missing_m15)} 缺失")
            print(f"  最早缺失: {datetime.fromtimestamp(missing_m15[0], tz=__import__('datetime').timezone.utc)}")
            print(f"  最近缺失: {datetime.fromtimestamp(missing_m15[-1], tz=__import__('datetime').timezone.utc)}")
        # 是否补 db?
        # 保守: 只 report, 不补 (避免破坏现有 50K bar baseline)
        print(f"\n  → 不自动补 db, 只报告. 手动补: 后续 P1-C v2 加 .")
    elif db_ahead < -15:
        # db 比 broker 新 (不可能, 但 sanity check)
        print(f"  ⚠ db 比 broker 还新, 异常")
    else:
        print(f"  ✓ db 跟 broker 对齐, 缺口 < 15 分钟")

    # 6) 价格 sanity check (db 最新 close vs broker 最新 close)
    if db_recent:
        db_last_close = db_recent[0][4]
        diff = broker_last_close - db_last_close
        diff_pct = diff / db_last_close * 100
        print(f"\n  价格对比:")
        print(f"    db 最新 close: {db_last_close:.2f}")
        print(f"    broker 最新:    {broker_last_close:.2f}")
        print(f"    差: {diff:+.2f} ({diff_pct:+.2f}%)")
        if abs(diff_pct) > 1.0:
            print(f"    ⚠ 价格差 > 1%, broker/db 不同源或 backfill 异常")

    # 7) 落盘报告
    out_path = PROJECT_ROOT / "data" / "charts" / "p1_c_sync_report.txt"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"P1-C MT5 vs DB 同步报告\n")
        f.write(f"生成时间: {datetime.utcnow()} UTC\n\n")
        f.write(f"== broker (MT5) ==\n")
        f.write(f"  account: {acc.login} / {acc.server}\n")
        f.write(f"  balance: {acc.balance} {acc.currency}\n")
        f.write(f"  leverage: {acc.leverage}\n")
        f.write(f"  bars pulled: {broker_bars} (XAUUSD+ M15)\n")
        f.write(f"  range: {broker_first} → {broker_last}\n")
        f.write(f"  latest close: {broker_last_close:.2f}\n\n")
        f.write(f"== local db (data/market_data.db) ==\n")
        f.write(f"  bars: {db_count} (XAUUSD+ M15)\n")
        f.write(f"  range: {db_first} → {db_last}\n")
        if db_recent:
            f.write(f"  latest close: {db_recent[0][4]:.2f}\n\n")
        f.write(f"== diff ==\n")
        f.write(f"  time gap: {db_ahead:.0f} min ({'broker newer' if db_ahead > 0 else 'db newer'})\n")
        f.write(f"  price diff: {diff:+.2f} ({diff_pct:+.2f}%)\n")

    print(f"\n→ 落盘: {out_path}")
    print("=" * 78)

    mt5.shutdown()
    return 0


if __name__ == "__main__":
    import sys as _s
    _s.exit(main())
