"""
data/live_sync/bybit_puller.py — Bybit 公开 API K线拉取器 (MT5 替代方案)

audit 2026-06-08: Python MetaTrader5 5.0.5735 vs MT5 terminal IPC hash 不匹配,
WaitNamedPipeW 一直 timeout。改用 Bybit v5 公开 API (无需 key) 拉 XAUUSDT K线,
写入本地 SQLite DB。

Bybit v5 market kline endpoint:
  GET https://api.bybit.com/v5/market/kline?category=linear&symbol=XAUUSDT&interval=15&limit=200

返回 {list: [[ts, open, high, low, close, volume, turnover], ...]}
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import requests

logger = logging.getLogger(__name__)

# 周期映射: timeframe → Bybit interval string
TIMEFRAME_INTERVAL = {
    "M5":   "5",
    "M15":  "15",
    "M30":  "30",
    "H1":   "60",
    "H4":   "240",
    "D1":   "D",
}

# Bybit 支持的最大拉取数量
MAX_LIMIT = 200

# Bybit 公开 API endpoint
BASE_URL = "https://api.bybit.com/v5/market/kline"


def fetch_kline(symbol: str = "XAUUSDT", interval: str = "15",
                limit: int = MAX_LIMIT) -> list[dict]:
    """拉取 Bybit K线, 返 dict 列表 [{time, open, high, low, close, volume}, ...].
    Raises 网络/API 错误. 返回空的 list = 无数据."""
    params = {
        "category": "linear",
        "symbol": symbol,
        "interval": interval,
        "limit": min(limit, MAX_LIMIT),
    }
    resp = requests.get(BASE_URL, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit API error: {data.get('retMsg', 'unknown')} (code={data.get('retCode')})")

    rows = data.get("result", {}).get("list", [])
    if not rows:
        return []

    # Bybit 返回的 list 是 [ts, open, high, low, close, volume, turnover]
    # ts 是毫秒时间戳
    out: list[dict] = []
    for r in rows:
        if len(r) < 6:
            continue
        try:
            out.append({
                "time": int(r[0]) // 1000,  # ms → s
                "open": float(r[1]),
                "high": float(r[2]),
                "low": float(r[3]),
                "close": float(r[4]),
                "volume": float(r[5]),
                "spread": 0,
            })
        except (ValueError, IndexError):
            continue
    return out


def sync_timeframe(db_path: str, symbol: str, timeframe: str,
                   since_time: int | None = None) -> dict:
    """拉取一个 timefra me 的 K 线并写入 DB.
    返回 {inserted, dups, error}."""
    from data.store import DataStore

    interval = TIMEFRAME_INTERVAL.get(timeframe)
    if interval is None:
        return {"inserted": 0, "dups": 0, "error": f"unknown timeframe {timeframe}"}

    # 查 DB 最新时间戳
    if since_time is None:
        store = DataStore(db_path)
        df = store.load_bars(symbol, timeframe)
        if len(df) > 0:
            since_time = int(df.index[-1].timestamp())
        else:
            since_time = 0

    try:
        rows = fetch_kline(symbol, interval, limit=MAX_LIMIT)
    except Exception as e:
        return {"inserted": 0, "dups": 0, "error": f"fetch failed: {e}"}

    if not rows:
        return {"inserted": 0, "dups": 0, "error": ""}

    # 去重: 只保留时间大于 since_time 的
    fresh = [r for r in rows if r["time"] > since_time]
    if not fresh:
        return {"inserted": 0, "dups": len(rows), "error": ""}

    # 写入 DB
    store = DataStore(db_path)
    store.insert_bars(fresh, symbol, timeframe)
    return {"inserted": len(fresh), "dups": len(rows) - len(fresh), "error": ""}


def run_sync(db_path: str = "data/market_data.db",
             timeframes: list[str] | None = None,
             symbol: str = "XAUUSDT",
             progress_cb=None) -> dict:
    """拉取所有 timeframes 的 K 线并写入 DB.
    audit 2026-06-08: 替代 MT5Puller/sync, MT5 IPC pipe 损坏时不阻塞.
    返回 {total_inserted, per_tf: [{tf, inserted, dups, error}]}."""
    if timeframes is None:
        timeframes = ["M15", "H1", "D1"]
    cb = progress_cb or (lambda *_: None)

    # Bybit 数据映射到 XAUUSD+ (项目内统一 symbol 名)
    db_symbol = "XAUUSD+"

    per_tf: list[dict] = []
    total = 0
    cb("sync", 10, f"Bybit: {symbol} -> {db_symbol}")
    for i, tf in enumerate(timeframes):
        cb("sync", 20 + 60 * i // len(timeframes), f"pulling {tf}...")
        result = sync_timeframe(db_path, db_symbol, tf)
        per_tf.append({"tf": tf, **result})
        total += result.get("inserted", 0)
        if result.get("error"):
            logger.warning(f"[BybitPuller] {tf}: {result['error']}")

    cb("done", 100, f"inserted {total} bars from Bybit")
    return {
        "total_inserted": total,
        "per_tf": per_tf,
        "source": "bybit",
        "symbol": symbol,
    }
