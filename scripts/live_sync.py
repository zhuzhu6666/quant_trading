"""
scripts/live_sync.py — 实时数据同步 CLI (T16.6, 2026-06-02)

用法:
    # 首次全量回填 (M15 / H1 / D1)
    python scripts/live_sync.py --mode once --type full --timeframes M15,H1,D1 --n-bars 5000

    # 日常增量 (上次 sync 之后的新 bar)
    python scripts/live_sync.py --mode once --type incremental --timeframes M15,H1

    # daemon 模式 (每 60 秒拉一次, 持续运行)
    python scripts/live_sync.py --mode daemon --interval 60 --timeframes M15

    # 查看状态
    python scripts/live_sync.py --mode status
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def main():
    import argparse
    parser = argparse.ArgumentParser(description="MT5 实时数据同步 (live sync)")
    parser.add_argument("--mode", default="once",
                        choices=["once", "daemon", "status"])
    parser.add_argument("--type", default="incremental",
                        choices=["full", "incremental"],
                        help="full=全量回填, incremental=增量拉取 (默认)")
    parser.add_argument("--symbol", default="XAUUSD+")
    parser.add_argument("--timeframes", default="M15,H1,D1",
                        help="逗号分隔, 例如 M5,M15,H1,D1")
    parser.add_argument("--n-bars", type=int, default=5000,
                        help="全量回填时的拉取数 (默认 5000)")
    parser.add_argument("--interval", type=int, default=60,
                        help="daemon 模式间隔 (秒)")
    parser.add_argument("--max-runs", type=int, default=0,
                        help="daemon 最大运行次数 (0=无限)")
    parser.add_argument("--db-path", default="data/market_data.db")
    args = parser.parse_args()

    timeframes = [t.strip() for t in args.timeframes.split(",")]

    if args.mode == "status":
        from data.live_sync.db_inserter import DBInserter
        from data.store import DataStore
        ins = DBInserter(db_path=args.db_path)
        status = ins.load_status()
        if status:
            print(f"=== 上次 sync 状态 ===")
            print(f"  last_sync: {status.last_sync_utc}")
            for tf, info in status.per_tf.items():
                print(f"  {tf}: {info}")
        else:
            print("暂无 sync 状态 (尚未跑过 live_sync)")
        # 各 tf 当前 bar 数
        store = DataStore(args.db_path)
        print()
        print(f"=== 当前 db bar 数 ===")
        for tf in timeframes:
            cnt = store.bar_count(args.symbol, tf)
            print(f"  {tf}: {cnt} bars")
        return

    if args.mode == "once":
        from data.live_sync.orchestrator import SyncOrchestrator
        orch = SyncOrchestrator(db_path=args.db_path)
        is_full = (args.type == "full")
        report = orch.run_once(full=is_full, n_bars=args.n_bars, symbol=args.symbol,
                               timeframes=timeframes)
        orch.print_report(report)
        orch.shutdown()

    elif args.mode == "daemon":
        from data.live_sync.daemon import SyncDaemon
        daemon = SyncDaemon(symbol=args.symbol, timeframes=timeframes,
                            db_path=args.db_path)
        print(f"[live_sync] daemon 启动, interval={args.interval}s, timeframes={timeframes}")
        print(f"[live_sync] Ctrl+C 停止")
        try:
            daemon.run_daemon(interval_sec=args.interval, max_runs=args.max_runs)
        except KeyboardInterrupt:
            print(f"\n[live_sync] 收到中断, 退出")
            daemon.stop()


if __name__ == "__main__":
    main()
