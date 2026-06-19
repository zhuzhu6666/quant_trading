"""
data/live_sync/ctrader_puller.py — cTrader 实时 bar 拉取 (替换 MT5Puller, 2026-06-18)

职责:
- 从 cTrader Open API 拉取 K 线 bar
- 写入 DataStore (DuckDB) 供 live_loop / evolution / ML 消费
- 兼容原 MT5Puller 的 PullResult 接口

时间戳处理:
- cTrader 返回 utcTimestampInMinutes (UTC minute), fetch_bars 转 Unix second
- 首次运行或 DB 空时全量回填 (替换旧的 MT5 数据)
- 增量运行: 取最新 bar 时间戳之后的数据
"""
from __future__ import annotations

import logging
import time as _time
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ── 接口定义 (原 mt5_puller.PullResult / TIMEFRAME_MAP) ──
from dataclasses import dataclass


@dataclass
class PullResult:
    """拉取结果"""
    symbol: str
    timeframe: str
    n_bars: int
    first_time: float
    last_time: float = 0.0
    success: bool = True
    error: str = ""


TIMEFRAME_MAP = {
    "M5": ("M5", 5),
    "M15": ("M15", 15),
    "M30": ("M30", 30),
    "H1": ("H1", 60),
    "H4": ("H4", 240),
    "D1": ("D1", 1440),
}


class CTraderPuller:
    """cTrader 实时 bar 拉取器 — 替换 MT5Puller.

    用法:
        puller = CTraderPuller()
        result = puller.pull_history("XAUUSD+", "M5", n=100)
        if result.error:
            print(f"failed: {result.error}")
        else:
            print(f"got {result.n_bars} bars")
        puller.shutdown()
    """

    def __init__(self):
        self._bridge = None
        self._connected = False

    def _import_bridge(self):
        """延迟导入 CTraderBridge (避免循环依赖)."""
        from execution._env import load_env
        load_env()
        from execution.ctrader_bridge import CTraderBridge
        return CTraderBridge

    def connect(self) -> bool:
        """连接 cTrader Open API (使用当前 env 中的凭证)."""
        if self._connected and self._bridge and self._bridge.is_connected:
            return True
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
        self._connected = self._bridge.connect()
        if not self._connected:
            logger.error("[CTraderPuller] connect failed")
        return self._connected

    def shutdown(self):
        """断开 cTrader 连接."""
        if self._bridge and self._bridge.is_connected:
            self._bridge.disconnect()
        self._bridge = None
        self._connected = False
        logger.info("[CTraderPuller] disconnected")

    def _check_connect(self) -> bool:
        if not self._connected:
            return self.connect()
        return True

    def pull_history(self, symbol: str, timeframe: str, n: int = 5000) -> PullResult:
        """拉最近 N 根完整 bar (历史回填用).

        Args:
            symbol: e.g. "XAUUSD" (cTrader 不需要 "+" 后缀)
            timeframe: e.g. "M5"
            n: 拉取根数

        Returns:
            PullResult (兼容 MT5Puller 接口)
        """
        t0 = _time.time()
        result = PullResult(
            symbol=symbol, timeframe=timeframe, n_bars=0,
            first_time=0, last_time=0, last_close=0, bars=[],
        )

        if not self._check_connect():
            result.error = "cTrader not connected"
            return result

        tf_info = TIMEFRAME_MAP.get(timeframe)
        if tf_info is None:
            result.error = f"Unknown timeframe: {timeframe}. Allowed: {list(TIMEFRAME_MAP)}"
            return result

        try:
            df = self._bridge.fetch_bars(timeframe, n)
        except Exception as e:
            result.error = f"fetch_bars 异常: {type(e).__name__}: {e}"
            return result

        if df is None or df.empty:
            result.error = "fetch_bars returned empty"
            logger.warning(f"[CTraderPuller] {symbol} {timeframe}: empty result")
            return result

        bars = []
        for idx, row in df.iterrows():
            # idx is DatetimeIndex (UTC)
            ts = int(idx.timestamp())
            bars.append({
                "time": ts,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": int(row["volume"]),
                "spread": 0,  # cTrader trendbar 不提供 spread
            })

        result.n_bars = len(bars)
        result.first_time = bars[0]["time"]
        result.last_time = bars[-1]["time"]
        result.last_close = bars[-1]["close"]
        result.bars = bars
        result.elapsed_sec = _time.time() - t0
        logger.info(f"[CTraderPuller] {symbol} {timeframe}: 拉 {result.n_bars} bar, "
                    f"range {datetime.fromtimestamp(result.first_time, tz=timezone.utc)} → "
                    f"{datetime.fromtimestamp(result.last_time, tz=timezone.utc)}, "
                    f"elapsed={result.elapsed_sec:.1f}s")
        return result

    def pull_recent(self, symbol: str, timeframe: str, n: int = 5) -> PullResult:
        """拉最近 N 根 bar (增量用). 与 pull_history 相同实现. """
        return self.pull_history(symbol, timeframe, n=n)

    def get_server_time(self) -> float | None:
        """获取 cTrader 服务器当前时间 (Unix second)."""
        if not self._check_connect():
            return None
        return _time.time()  # fallback: 本地时间; cTrader 没有单独的时间 API

    def clear_db_bars(self, symbol: str, timeframes: list[str] | None = None):
        """清除 DataStore 中指定品种的旧 bar 数据 (MT5 遗留), 准备用 cTrader 数据重填.

        Args:
            symbol: 品种名
            timeframes: 周期列表, 默认全部
        """
        from data.store import DataStore
        store = DataStore()
        if timeframes is None:
            timeframes = list(TIMEFRAME_MAP)
        import duckdb
        conn = duckdb.connect(str(store.db_path))
        try:
            for tf in timeframes:
                conn.execute(
                    "DELETE FROM bars WHERE symbol = ? AND timeframe = ?",
                    [symbol, tf]
                )
                logger.info(f"[CTraderPuller] 清除 {symbol} {tf} 旧数据")
        finally:
            conn.close()

    def check_and_migrate(self, symbol: str = "XAUUSD+",
                          timeframes: list[str] | None = None) -> bool:
        """检查 DB 中的 MT5 旧数据, 如有则清除并回填 cTrader 数据.

        Args:
            symbol: 品种
            timeframes: 要迁移的周期

        Returns:
            True 表示迁移完成
        """
        from data.store import DataStore
        store = DataStore()
        import duckdb
        conn = duckdb.connect(str(store.db_path))
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM bars WHERE symbol = ?", [symbol]
            ).fetchone()[0]
        finally:
            conn.close()

        if count == 0:
            logger.info("[CTraderPuller] DB 无旧数据, 跳过迁移")
            return True

        logger.warning(f"[CTraderPuller] DB 有 {count} 条旧 bar (MT5), 正在清除并回填 cTrader 数据...")
        if timeframes is None:
            timeframes = list(TIMEFRAME_MAP)

        self.clear_db_bars(symbol, timeframes)

        # 全量回填
        for tf in timeframes:
            result = self.pull_history(symbol, tf, n=5000)
            if result.error:
                logger.error(f"[CTraderPuller] {tf} 回填失败: {result.error}")
                continue
            # 直接写入 DataStore
            from data.store import DataStore
            DataStore().insert_bars(result.bars, symbol, tf)
            logger.info(f"[CTraderPuller] {tf} 回填 {result.n_bars} bars ✅")

        return True
