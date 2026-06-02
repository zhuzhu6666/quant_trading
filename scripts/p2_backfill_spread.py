"""
P2: backfill spread 字段到 bars 表
- MT5 broker 通常只给最近 5000 bar 历史 (XAUUSD+ 限制)
- 每个 timeframe 拉 5000 bar → 按 time 对齐写回 db
- 老 bar (broker 没返回的) 保留 spread=0, 运行时 fallback 到 0.13 USD (13 points * 0.01)

2026-06-03
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import logging
import sqlite3
import time as _time
from data.store import DataStore
from data.live_sync.mt5_puller import MT5Puller

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("p2_backfill_spread")

SYMBOL = "XAUUSD+"
TIMEFRAMES = ["M5", "M15", "M30", "H1", "H4", "D1"]
DB_PATH = "data/market_data.db"
N_BARS = 5000  # broker 限


def main():
    puller = MT5Puller()
    if not puller.connect():
        log.error("MT5 连接失败, 退出")
        return 1

    conn = sqlite3.connect(DB_PATH)

    summary = []
    for tf in TIMEFRAMES:
        result = puller.pull_history(SYMBOL, tf, N_BARS)
        if result.error:
            log.error(f"{tf}: pull 失败 {result.error}")
            continue

        # 按 time 对齐更新 db
        rows = [(b["spread"], SYMBOL, tf, b["time"]) for b in result.bars]
        cur = conn.execute(
            "UPDATE bars SET spread=? WHERE symbol=? AND timeframe=? AND time=?",
            rows[0],  # 占位, 下面用 executemany
        )
        conn.executemany(
            "UPDATE bars SET spread=? WHERE symbol=? AND timeframe=? AND time=?",
            rows,
        )
        conn.commit()

        # 统计
        total = conn.execute(
            "SELECT COUNT(*) FROM bars WHERE symbol=? AND timeframe=?",
            (SYMBOL, tf),
        ).fetchone()[0]
        with_spread = conn.execute(
            "SELECT COUNT(*) FROM bars WHERE symbol=? AND timeframe=? AND spread>0",
            (SYMBOL, tf),
        ).fetchone()[0]
        sample = result.bars[len(result.bars) // 2]["spread"] if result.bars else 0
        summary.append((tf, total, with_spread, len(result.bars), sample))
        log.info(f"{tf}: pulled {len(result.bars)} bar, "
                 f"db total={total} with_spread={with_spread} sample_mid={sample}")

    conn.close()
    puller.shutdown()

    # 落盘报告
    out = Path("data/charts/p2_backfill_spread.txt")
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write("P2: spread backfill 报告 (2026-06-03)\n")
        f.write("=" * 60 + "\n\n")
        f.write(f"DB: {DB_PATH}\n")
        f.write(f"Symbol: {SYMBOL}\n")
        f.write(f"Source: MT5 copy_rates_from_pos (spread points * 0.01 = USD/oz)\n\n")
        f.write(f"{'Timeframe':<8s} {'Total':>8s} {'WithSpread':>12s} {'Pulled':>8s} {'Sample':>8s}\n")
        f.write("-" * 60 + "\n")
        for tf, total, ws, pulled, sample in summary:
            f.write(f"{tf:<8s} {total:>8d} {ws:>12d} {pulled:>8d} {sample:>8d}\n")
        f.write("\n老 bar (broker 不返回的) 保留 spread=0, 运行时 fallback 0.13 USD\n")
    log.info(f"报告: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
