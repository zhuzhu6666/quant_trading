"""
data/tick_pipeline/ctrader_tick_puller.py — cTrader Tick 数据拉取器 (替换 MT5TickPuller, 2026-06-18)

从 cTrader Open API 拉取 tick 数据，存入 DuckDB ticks 表。

cTrader tick 格式:
  - ProtoOAGetTickDataReq → ProtoOAGetTickDataRes
  - tickData: [{timestamp (ms), tick (int64)}]
  - tick ÷ 100000 → 实际价格
  - 最大范围: 7 天
  - hasMore 支持分页

注意: cTrader 不含 MT5 的 bid/ask/last/flags 结构,
      用单一 tick 值填入 last 字段, bid/ask 留空.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import duckdb
import pandas as pd

from backend.core.db import connect_duckdb

logger = logging.getLogger(__name__)


@dataclass
class TickPullResult:
    """一次 tick 拉取的结果 (兼容 MT5TickPuller 接口)"""
    symbol: str
    pulled: int
    inserted: int
    duplicates: int = 0
    elapsed_sec: float = 0.0
    error: str = ""


class CTraderTickPuller:
    """cTrader tick 数据拉取器 — 替换 MT5TickPuller.

    用法:
        puller = CTraderTickPuller()
        if puller.connect():
            result = puller.pull_recent("XAUUSD", n_ticks=1000)
            print(f"Pulled {result.inserted} ticks")
        puller.shutdown()
    """

    def __init__(self, db_path: str = "data/ctrader_data.duckdb"):
        self._bridge = None
        self._connected = False
        self._owns_bridge = False
        self.db_path = db_path
        self._symbol_id: int | None = None

    def _import_bridge(self):
        from execution._env import load_env
        load_env()
        from execution.ctrader_bridge import CTraderBridge
        return CTraderBridge

    def connect(self) -> bool:
        if self._connected and self._bridge and self._bridge.is_connected:
            return True
        try:
            from backend.services.live_service import _get_ctrader, _wait_ctrader_ready

            bridge, err, warming = _get_ctrader()
            if err:
                logger.error(f"[CTraderTickPuller] shared bridge unavailable: {err}")
                return False
            if warming:
                wait_err = _wait_ctrader_ready(bridge, timeout_sec=30.0)
                if wait_err:
                    logger.error(f"[CTraderTickPuller] shared bridge warmup failed: {wait_err}")
                    return False
            self._bridge = bridge
            self._owns_bridge = False
            self._connected = bool(self._bridge and self._bridge.is_connected)
            self._symbol_id = getattr(self._bridge, "_symbol_id", None)
            if not self._connected:
                logger.error("[CTraderTickPuller] shared bridge not connected")
            return self._connected
        except Exception as e:
            logger.debug(f"[CTraderTickPuller] shared bridge fallback: {e}")
        import os
        BridgeCls = self._import_bridge()
        self._bridge = BridgeCls(
            client_id=os.getenv("CTRADER_CLIENT_ID", ""),
            client_secret=os.getenv("CTRADER_CLIENT_SECRET", ""),
            access_token=os.getenv("CTRADER_ACCESS_TOKEN", ""),
            account_id=int(os.getenv("CTRADER_ACCOUNT_ID", "0")),
            symbol="XAUUSD",
            send_orders=False,
        )
        self._owns_bridge = True
        if not self._bridge.connect():
            logger.error("[CTraderTickPuller] connect failed")
            return False
        self._connected = True
        self._symbol_id = self._bridge._symbol_id
        return True

    def shutdown(self):
        if self._owns_bridge and self._bridge and self._bridge.is_connected:
            self._bridge.disconnect()
        self._bridge = None
        self._connected = False
        self._owns_bridge = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def pull_recent(self, symbol: str = "XAUUSD", n_ticks: int = 1000,
                    lookback_sec: int = 60) -> TickPullResult:
        """拉最近 tick (增量拉取).

        Args:
            symbol: 品种
            n_ticks: 最大拉取数 (cTrader 由服务端控制, 此处仅作为参考)
            lookback_sec: lookback 秒数 (默认 60s, 按所需量调整)

        Returns:
            TickPullResult
        """
        t0 = time.time()
        result = TickPullResult(symbol=symbol, pulled=0, inserted=0)

        if not self._connected or not self._bridge:
            result.error = "cTrader not connected"
            return result

        try:
            from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAGetTickDataReq

            req = ProtoOAGetTickDataReq()
            req.ctidTraderAccountId = self._bridge.account_id
            req.symbolId = self._symbol_id
            req.type = 1  # ALL
            now_ms = int(time.time() * 1000)
            req.fromTimestamp = now_ms - lookback_sec * 1000
            req.toTimestamp = now_ms

            resp = self._bridge._send(req, timeout=30.0)
            ticks_raw = list(resp.tickData) if hasattr(resp, 'tickData') else []

            if not ticks_raw:
                result.error = "no tick data returned"
                return result

            result.pulled = len(ticks_raw)

            # 转为标准格式: {time, bid, ask, last, volume, flags}
            # cTrader tick 单值, 填入 last; bid/ask 为 0
            records = []
            for t in ticks_raw:
                price = t.tick / 100000.0
                ts_sec = t.timestamp / 1000.0
                records.append({
                    "time": ts_sec,
                    "last": price,
                    "bid": 0.0,
                    "ask": 0.0,
                    "volume": 0.0,
                    "flags": 0,
                })

            # 写入 DuckDB
            inserted = self._insert_ticks(records, symbol)
            result.inserted = inserted

        except Exception as e:
            result.error = f"{type(e).__name__}: {e}"

        result.elapsed_sec = time.time() - t0
        return result

    def pull_range(self, symbol: str = "XAUUSD",
                   from_ts: int | None = None, to_ts: int | None = None) -> TickPullResult:
        """按时间范围拉取 tick (最大 7 天).

        Args:
            symbol: 品种
            from_ts: 起始 Unix 秒 (默认 60s 前)
            to_ts: 结束 Unix 秒 (默认 now)

        Returns:
            TickPullResult
        """
        t0 = time.time()
        result = TickPullResult(symbol=symbol, pulled=0, inserted=0)

        if not self._connected:
            result.error = "cTrader not connected"
            return result

        if to_ts is None:
            to_ts = int(time.time())
        if from_ts is None:
            from_ts = to_ts - 3600  # 默认 1 小时

        # 限制 max 7 天
        max_range = 7 * 24 * 3600
        if to_ts - from_ts > max_range:
            from_ts = to_ts - max_range
            logger.warning(f"[CTraderTickPuller] 超出 7 天限制, 截断到 {from_ts}")

        try:
            from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAGetTickDataReq

            req = ProtoOAGetTickDataReq()
            req.ctidTraderAccountId = self._bridge.account_id
            req.symbolId = self._symbol_id
            req.type = 1
            req.fromTimestamp = from_ts * 1000
            req.toTimestamp = to_ts * 1000

            all_ticks = []
            has_more = True

            while has_more:
                resp = self._bridge._send(req, timeout=30.0)
                batch = list(resp.tickData) if hasattr(resp, 'tickData') else []
                all_ticks.extend(batch)
                has_more = getattr(resp, 'hasMore', False) and len(batch) > 0
                if has_more:
                    # 用最后一条时间戳继续
                    req.fromTimestamp = batch[-1].timestamp + 1

            result.pulled = len(all_ticks)
            records = []
            for t in all_ticks:
                price = t.tick / 100000.0
                records.append({
                    "time": t.timestamp / 1000.0,
                    "last": price,
                    "bid": 0.0,
                    "ask": 0.0,
                    "volume": 0.0,
                    "flags": 0,
                })

            result.inserted = self._insert_ticks(records, symbol)

        except Exception as e:
            result.error = f"{type(e).__name__}: {e}"

        result.elapsed_sec = time.time() - t0
        return result

    def _insert_ticks(self, records: list[dict], symbol: str) -> int:
        """将 tick 写入 DuckDB."""
        if not records:
            return 0
        try:
            conn = connect_duckdb(self.db_path)
            df = pd.DataFrame(records)
            df["symbol"] = symbol
            df = df[["symbol", "time", "bid", "ask", "last", "volume", "flags"]]
            conn.execute("INSERT INTO ticks SELECT * FROM df")
            conn.close()
            return len(df)
        except Exception as e:
            logger.error(f"[CTraderTickPuller] DB insert failed: {e}")
            return 0
