"""scripts/backfill_ctrader_deals.py — 历史成交数据回填.

从 cTrader get_deals() 拉全量历史成交, 写入 state.db ctrader_deals 表.

用法:
    .venv/bin/python scripts/backfill_ctrader_deals.py [--days 30] [--max-rows 500]
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from execution._env import load_env
load_env()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("backfill_deals")


def main():
    parser = argparse.ArgumentParser(description="回填 cTrader 历史成交到 state.db")
    parser.add_argument("--days", type=int, default=30, help="回填天数 (默认 30)")
    parser.add_argument("--max-rows", type=int, default=500, help="每批最大条数 (默认 500)")
    args = parser.parse_args()

    # ── 连接 bridge ──
    from execution.ctrader_bridge import CTraderBridge

    import os
    bridge = CTraderBridge(
        client_id=os.environ.get("CTRADER_CLIENT_ID", ""),
        client_secret=os.environ.get("CTRADER_CLIENT_SECRET", ""),
        access_token=os.environ.get("CTRADER_ACCESS_TOKEN", ""),
        account_id=int(os.environ.get("CTRADER_ACCOUNT_ID", 0)),
    )

    logger.info("Connecting to cTrader...")
    if not bridge.connect():
        logger.error("Failed to connect to cTrader")
        bridge.disconnect()
        sys.exit(1)
    logger.info("Connected OK")

    # ── 分批回填 ──
    from execution.deal_sync import fetch_deals_since, store_deals
    from backend.core.db import get_state_conn

    now = int(time.time())
    days = args.days
    max_rows = args.max_rows
    total_stored = 0

    for day_offset in range(days):
        to_ts = now - day_offset * 86400
        from_ts = to_ts - 86400
        logger.info("Fetching deals: %s ~ %s (day %d/%d)",
                    time.strftime("%Y-%m-%d", time.gmtime(from_ts)),
                    time.strftime("%Y-%m-%d", time.gmtime(to_ts)),
                    day_offset + 1, days)
        deals = fetch_deals_since(bridge, from_ts=from_ts, to_ts=to_ts, max_rows=max_rows)
        if deals:
            conn = get_state_conn()
            try:
                n = store_deals(conn, deals)
                total_stored += n
            finally:
                conn.close()
        # 小间隔防速率限制
        time.sleep(0.5)

    bridge.disconnect()
    logger.info("Backfill complete: %d deals stored in ctrader_deals table", total_stored)


if __name__ == "__main__":
    main()
