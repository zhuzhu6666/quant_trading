"""
data/live_sync/mt5_puller.py — MT5 实时 bar 拉取 (T16.1, 2026-06-02)

L1 数据层补全. 跟现有 data/store.py + scripts/p1_c_sync_live_bars.py 整合.

职责:
- 拉 N 根历史 (initial backfill)
- 拉"上次 sync 之后" (incremental, 默认 1 根 = 当前正在形成的)
- 字段映射: MT5 (tick_volume) → DataStore (volume)
- 多 timeframe 支持 (M5/M15/M30/H1/H4/D1)

输出: 标准化 bar dict 列表, 每个含 {time, open, high, low, close, volume}.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import numpy as np

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None

logger = logging.getLogger(__name__)


# ── MT5 timeframe 映射 ─────────────────────────────────────────
TIMEFRAME_MAP = {
    "M5": ("M5", 5),
    "M15": ("M15", 15),
    "M30": ("M30", 30),
    "H1": ("H1", 60),
    "H4": ("H4", 240),
    "D1": ("D1", 1440),
}
# MT5 enum
try:
    if mt5:
        TIMEFRAME_ENUM = {
            "M5": mt5.TIMEFRAME_M5,
            "M15": mt5.TIMEFRAME_M15,
            "M30": mt5.TIMEFRAME_M30,
            "H1": mt5.TIMEFRAME_H1,
            "H4": mt5.TIMEFRAME_H4,
            "D1": mt5.TIMEFRAME_D1,
        }
    else:
        TIMEFRAME_ENUM = {}
except AttributeError:
    TIMEFRAME_ENUM = {}


@dataclass
class PullResult:
    """拉取结果"""
    symbol: str
    timeframe: str
    n_bars: int
    first_time: float
    last_time: float
    last_close: float
    bars: list  # list[dict] — 标准 bar 格式 {time, open, high, low, close, volume}
    elapsed_sec: float = 0.0
    error: str = ""


class MT5Puller:
    """
    MT5 实时 bar 拉取器.

    用法:
        puller = MT5Puller()
        result = puller.pull_history("XAUUSD+", "M15", n=5000)
        if result.error:
            print(f"failed: {result.error}")
        else:
            print(f"got {result.n_bars} bars, {result.first_time} → {result.last_time}")
        puller.shutdown()

    拉最近 1 根 (current bar):
        result = puller.pull_recent("XAUUSD+", "M15", n=1)
    """

    def __init__(self):
        if mt5 is None:
            raise ImportError("MetaTrader5 不可用, 请 pip install MetaTrader5")
        self._connected = False

    def connect(self) -> bool:
        """连接 MT5 (依赖已登录的 MT5 终端)"""
        if self._connected:
            return True
        if not mt5.initialize():
            err = mt5.last_error()
            logger.error(f"[MT5Puller] initialize 失败: {err}")
            return False
        self._connected = True
        logger.info(f"[MT5Puller] 已连接 MT5")
        return True

    def shutdown(self):
        if self._connected:
            mt5.shutdown()
            self._connected = False
            logger.info(f"[MT5Puller] MT5 shutdown")

    def _check_connect(self) -> bool:
        if not self._connected:
            return self.connect()
        return True

    def _normalize_bar(self, raw) -> dict:
        """MT5 raw bar dict → 标准化 bar dict (字段映射)"""
        return {
            "time": int(raw["time"]),
            "open": float(raw["open"]),
            "high": float(raw["high"]),
            "low": float(raw["low"]),
            "close": float(raw["close"]),
            "volume": int(raw["tick_volume"] if raw["tick_volume"] > 0 else raw["real_volume"]),
            "spread": int(raw["spread"]),
        }

    def pull_history(self, symbol: str, timeframe: str, n: int = 5000) -> PullResult:
        """
        拉最近 N 根完整 bar (历史回填用).

        Args:
            symbol: e.g. "XAUUSD+"
            timeframe: e.g. "M15"
            n: 拉取根数
        """
        import time as _time
        t0 = _time.time()
        result = PullResult(symbol=symbol, timeframe=timeframe, n_bars=0,
                            first_time=0, last_time=0, last_close=0, bars=[])

        if not self._check_connect():
            result.error = "MT5 not connected"
            return result

        tf_enum = TIMEFRAME_ENUM.get(timeframe)
        if tf_enum is None:
            result.error = f"Unknown timeframe: {timeframe}. Allowed: {list(TIMEFRAME_ENUM)}"
            return result

        try:
            rates = mt5.copy_rates_from_pos(symbol, tf_enum, 0, n)
        except Exception as e:
            result.error = f"copy_rates_from_pos 异常: {type(e).__name__}: {e}"
            return result

        if rates is None or len(rates) == 0:
            err = mt5.last_error()
            result.error = f"copy_rates_from_pos 返回空: {err}"
            return result

        bars = [self._normalize_bar(r) for r in rates]
        result.n_bars = len(bars)
        result.first_time = bars[0]["time"]
        result.last_time = bars[-1]["time"]
        result.last_close = bars[-1]["close"]
        result.bars = bars
        result.elapsed_sec = _time.time() - t0
        logger.info(f"[MT5Puller] {symbol} {timeframe}: 拉 {result.n_bars} bar, "
                    f"range {datetime.utcfromtimestamp(result.first_time)} → "
                    f"{datetime.utcfromtimestamp(result.last_time)} "
                    f"({result.elapsed_sec:.2f}s)")
        return result

    def pull_incremental(self, symbol: str, timeframe: str,
                         since_time: float, max_bars: int = 200) -> PullResult:
        """
        增量拉取: 拉 since_time 之后的新 bar (不重复拉历史).

        Args:
            since_time: 上次 sync 时的最新 bar time (epoch)
            max_bars: 安全上限, 避免单次拉太多
        """
        import time as _time
        t0 = _time.time()
        result = PullResult(symbol=symbol, timeframe=timeframe, n_bars=0,
                            first_time=0, last_time=0, last_close=0, bars=[])

        if not self._check_connect():
            result.error = "MT5 not connected"
            return result

        tf_enum = TIMEFRAME_ENUM.get(timeframe)
        if tf_enum is None:
            result.error = f"Unknown timeframe: {timeframe}"
            return result

        # 拉 N 根 (N = max_bars, 后续过滤 since_time 之后)
        try:
            rates = mt5.copy_rates_from_pos(symbol, tf_enum, 0, max_bars)
        except Exception as e:
            result.error = f"copy_rates_from_pos 异常: {type(e).__name__}: {e}"
            return result

        if rates is None or len(rates) == 0:
            err = mt5.last_error()
            result.error = f"copy_rates_from_pos 返回空: {err}"
            return result

        all_bars = [self._normalize_bar(r) for r in rates]
        # 过滤: time > since_time (注意: 完整 bar 才能用, 当前 bar 跳过)
        # 当前 bar 判定: rate 的 time > 应该是 N*15, 但 mt5 在 bar close 前会更新 close
        # 简单办法: 只取 time > since_time (不取 = since_time, 因为 since_time 的 bar 已入库)
        new_bars = [b for b in all_bars if b["time"] > since_time]

        # 当前正在形成的 bar: time 是 future (下个 bar 的起点) — 跳过
        # 判断: 假设 N 根 bar 时间均匀, last_time - first_time ≈ (n-1) * tf_minutes * 60
        tf_minutes = TIMEFRAME_MAP.get(timeframe, ("M15", 15))[1]
        if len(all_bars) >= 2:
            expected_span = (len(all_bars) - 1) * tf_minutes * 60
            actual_span = all_bars[-1]["time"] - all_bars[0]["time"]
            # 如果实际跨度 < 期望, 最后 1 根是当前正在形成的 (incomplete)
            if actual_span < expected_span - tf_minutes * 60:
                # 最后 1 根跳过
                if new_bars and new_bars[-1]["time"] == all_bars[-1]["time"]:
                    new_bars = new_bars[:-1]
                    logger.info(f"[MT5Puller] 跳过当前正在形成的 bar "
                                f"(time={all_bars[-1]['time']})")

        result.bars = new_bars
        result.n_bars = len(new_bars)
        if new_bars:
            result.first_time = new_bars[0]["time"]
            result.last_time = new_bars[-1]["time"]
            result.last_close = new_bars[-1]["close"]
        result.elapsed_sec = _time.time() - t0
        logger.info(f"[MT5Puller] 增量: {symbol} {timeframe} since={since_time} → "
                    f"{result.n_bars} 新 bar ({result.elapsed_sec:.2f}s)")
        return result

    def get_server_time(self) -> Optional[datetime]:
        """MT5 服务器当前时间 (用于诊断时间漂移)"""
        if not self._check_connect():
            return None
        tick = mt5.symbol_info_tick("XAUUSD+")
        if tick is None:
            return None
        return datetime.utcfromtimestamp(tick.time)
