#!/usr/bin/env python
"""Dukascopy 增量拉取 — 查 DB 最新时间戳，只补缺失的小时"""
import sys, os, time, struct, lzma, urllib.request, calendar
from datetime import datetime, timezone, timedelta
from pathlib import Path

# 切换工作目录到项目根 (兼容 Linux/Windows)
_ROOT = Path(__file__).resolve().parent.parent.parent
os.chdir(str(_ROOT))
BASE = "https://datafeed.dukascopy.com/datafeed/XAUUSD"

import duckdb
import pandas as pd

PIPET = 0.001


def download_hour(y, m0, d, h):
    """下载单小时 .bi5，已存在则跳过"""
    url = f"{BASE}/{y}/{m0:02d}/{d:02d}/{h:02d}h_ticks.bi5"
    local = Path(f"data/dukascopy_raw/XAUUSD/{y}/{m0:02d}/{d:02d}/{h:02d}h_ticks.bi5")
    if local.exists() and local.stat().st_size > 0:
        return True
    local.parent.mkdir(parents=True, exist_ok=True)
    try:
        r = urllib.request.urlopen(
            urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}),
            timeout=15,
        )
        local.write_bytes(r.read())
        return True
    except Exception:
        return False


def import_to_db(y, m0, d, h):
    """解压 .bi5 并写入 ticks.duckdb"""
    local = Path(f"data/dukascopy_raw/XAUUSD/{y}/{m0:02d}/{d:02d}/{h:02d}h_ticks.bi5")
    if not local.exists() or local.stat().st_size == 0:
        return 0
    try:
        raw = local.read_bytes()
        dec = lzma.decompress(raw)
        n = len(dec) // 20
        if n == 0:
            return 0
        base = int(datetime(y, m0 + 1, d, h, tzinfo=timezone.utc).timestamp())
        records = []
        for i in range(n):
            # Dukascopy .bi5 tick layout: ms offset, ask, bid, ask volume, bid volume.
            tick_ms, ask_raw, bid_raw, ask_volume, bid_volume = struct.unpack(
                ">3I2f", dec[i * 20 : (i + 1) * 20]
            )
            if bid_raw <= 0 or ask_raw <= 0:
                continue
            records.append((
                "XAUUSD+", base + tick_ms / 1000.0,
                bid_raw * PIPET, ask_raw * PIPET,
                (bid_raw + ask_raw) * PIPET / 2,
                float(max(ask_volume, 0.0) + max(bid_volume, 0.0)),
            ))
        if records:
            df = pd.DataFrame(records, columns=["symbol", "time", "bid", "ask", "last", "volume"])
            c = duckdb.connect("data/ticks.duckdb")
            c.execute("INSERT OR IGNORE INTO ticks SELECT * FROM df")
            c.close()
            return len(records)
        return 0
    except Exception:
        return 0


# ── 查 DB 最新时间戳，只补之后的小时 ──
c = duckdb.connect("data/ticks.duckdb")
last_ts = c.execute("SELECT MAX(time) FROM ticks").fetchone()[0]
c.close()

if last_ts is None:
    # DB 全空：拉最近 7 天
    start = datetime.now(timezone.utc) - timedelta(days=7)
else:
    start = datetime.fromtimestamp(last_ts, tz=timezone.utc)

now = datetime.now(timezone.utc)
total = 0

# 从 start 的小时边界开始，逐小时拉到 now
hour_start = start.replace(minute=0, second=0, microsecond=0)
current = hour_start
while current < now:
    # 跳过周末 (周六全天 + 周日 0-21h, 但保留周日 22-23h)
    if current.weekday() == 5:  # Saturday
        current += timedelta(hours=24)
        continue
    if current.weekday() == 6 and current.hour < 22:  # Sunday before 22:00 UTC
        current += timedelta(hours=1)
        continue
    y, m0, d, h = current.year, current.month - 1, current.day, current.hour
    ok = download_hour(y, m0, d, h)
    if ok:
        n = import_to_db(y, m0, d, h)
        total += n
    current += timedelta(hours=1)

c = duckdb.connect("data/ticks.duckdb")
r = c.execute("SELECT COUNT(*), MAX(time) FROM ticks").fetchone()
c.close()
print(f"✅ Dukascopy 增量完成: +{total} ticks", flush=True)
print(f"   DB: {r[0]:,} ticks | 最新: {datetime.fromtimestamp(r[1], tz=timezone.utc) if r[1] else 'N/A'}", flush=True)
