"""统一 CLI 入口,取代散落的 scripts/*.py 触发。

用法:
  python -m backend.runtime.cli sync start
  python -m backend.runtime.cli sync stop
  python -m backend.runtime.cli sync status
  python -m backend.runtime.cli scheduler list
  python -m backend.runtime.cli scheduler run_now auto_discover_daily

Phase 1 末只接 sync,scheduler 入口留到 Phase 2.4。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import List, Optional

logger = logging.getLogger(__name__)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


async def _sync_start(interval_sec: int) -> int:
    from backend.runtime.loop_host import LoopHost
    from backend.services.sync_service import sync_runner_factory

    host = LoopHost()
    await host.spawn("sync", sync_runner_factory, extra={"interval_sec": interval_sec})
    try:
        # 阻塞直到被 SIGINT
        while True:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        await host.stop("sync")
    return 0


async def _sync_stop() -> int:
    from backend.runtime.loop_host import LoopHost

    host = LoopHost()
    ok = await host.stop("sync")
    return 0 if ok else 1


async def _sync_status() -> int:
    from backend.runtime.loop_host import LoopHost

    host = LoopHost()
    print(json.dumps(host.status().get("sync", {}), ensure_ascii=False, indent=2))
    return 0


async def _scheduler_list() -> int:
    # Phase 2.4 才实现,Phase 1 给个 stub
    print(json.dumps({"status": "not_implemented", "phase": "P2.4"}, ensure_ascii=False))
    return 0


async def _scheduler_run_now(job_id: str) -> int:
    print(json.dumps({"status": "not_implemented", "phase": "P2.4", "job_id": job_id}, ensure_ascii=False))
    return 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="backend.runtime.cli")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="group", required=True)

    # sync 子命令
    sync_p = sub.add_parser("sync", help="data sync daemon control")
    sync_sub = sync_p.add_subparsers(dest="action", required=True)
    sync_sub.add_parser("start").add_argument(
        "--interval-sec", type=int, default=300, help="sync interval in seconds (default 300)"
    )
    sync_sub.add_parser("stop")
    sync_sub.add_parser("status")

    # scheduler 子命令(Phase 2.4)
    sched_p = sub.add_parser("scheduler", help="in-process scheduler control")
    sched_sub = sched_p.add_subparsers(dest="action", required=True)
    sched_sub.add_parser("list")
    rn = sched_sub.add_parser("run_now")
    rn.add_argument("job_id")

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    if args.group == "sync":
        if args.action == "start":
            return asyncio.run(_sync_start(args.interval_sec))
        if args.action == "stop":
            return asyncio.run(_sync_stop())
        if args.action == "status":
            return asyncio.run(_sync_status())
    if args.group == "scheduler":
        if args.action == "list":
            return asyncio.run(_scheduler_list())
        if args.action == "run_now":
            return asyncio.run(_scheduler_run_now(args.job_id))

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
