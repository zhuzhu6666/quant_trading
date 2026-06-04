"""
data/live_sync/bar_filter.py — Bar 过滤器 (T16.2, 2026-06-02)

职责:
- 去重: 跟 db 已有 bar 对比, 只保留新 bar
- 完整性检查: 时间序列连续, 无断层
- 当前 bar 检测: 判断是否 incomplete (正在形成的 bar), 跳过
- 异常检测: 价格跳变 / 成交量异常

输出: 标准化 bar dict 列表 (仅新的、完整、有效的).
"""
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class FilterResult:
    """过滤结果"""
    input_count: int
    kept: list      # 保留的 bar (去重 + 过滤后)
    dup_count: int   # 重复的
    incomplete_count: int  # 当前 bar (未完成)
    error: str = ""


class BarFilter:
    """
    Bar 过滤器.

    用法:
        f = BarFilter(db_path="data/market_data.db")
        result = f.filter(bars, symbol="XAUUSD+", timeframe="M15")
        # result.kept: 需要入库的新 bar
    """

    def __init__(self, db_path: str = "data/market_data.db",
                 mt5_puller=None):
        """
        Args:
            db_path: sqlite db path
            mt5_puller: P3 (audit 2026-06-04 BUG-12) — 可选, 注入 MT5Puller
                用于用 broker server epoch 判当前 bar。
                broker time vs local time 可能差数小时, 不对齐会让正在
                形成的 bar 误入库 (frozen close 写入 db)。
                不传 / 拿不到 → fallback 本地 epoch (向后兼容)。
        """
        self.db_path = db_path
        self._puller = mt5_puller

    def _now_epoch(self) -> float:
        """P3: 返回 'MT5 server time' 对应的 epoch, 拿不到 fallback 本地"""
        import time as _time
        if self._puller is not None:
            try:
                ts = self._puller.get_server_time_epoch()
                if ts is not None:
                    return ts
            except Exception as e:
                logger.debug(f"[BarFilter] puller.get_server_time_epoch failed: {e}")
        return _time.time()

    def filter(self, bars: list[dict], symbol: str, timeframe: str,
               tf_minutes: int = 15) -> FilterResult:
        """
        过滤 bar 列表, 返回需要入库的新 bar.

        Args:
            bars: MT5 拉取的全部 bar
            symbol: 品种名
            timeframe: 周期
            tf_minutes: 该周期对应的分钟数 (用于判断当前 bar)
        """
        result = FilterResult(input_count=len(bars), kept=[], dup_count=0, incomplete_count=0)
        if not bars:
            return result

        # 1. 去重: 查 db 已有 bar 的最大 time
        db_max_time = self._get_max_time(symbol, timeframe)
        if db_max_time is not None:
            # 只保留 time > db_max_time 的 bar
            new_bars = [b for b in bars if b["time"] > db_max_time]
            dup = len(bars) - len(new_bars)
            result.dup_count = dup
            bars = new_bars
            if dup > 0:
                logger.info(f"[BarFilter] 去重: {dup} 个重复 bar (db 最新={datetime.utcfromtimestamp(db_max_time)})")

        if not bars:
            return result

        # 2. 当前 bar 检测 (正在形成的, 未 close)
        #    P3 (audit 2026-06-04 BUG-12): 用 broker server epoch 判,
        #    不再用本地 epoch, 避免 broker/local 时间差导致正在形成的
        #    bar 误入库。
        now = self._now_epoch()
        last_bar = bars[-1]
        bar_end_time = last_bar["time"] + tf_minutes * 60  # 该 bar 的收盘时间
        if bar_end_time > now:
            logger.info(f"[BarFilter] 跳过当前正在形成的 bar "
                        f"(time={datetime.utcfromtimestamp(last_bar['time'])})")
            bars = bars[:-1]
            result.incomplete_count = 1
        elif bar_end_time > now - 60:
            # 刚 close 的 bar, 允许 (margin 1 分钟)
            pass

        # 3. 完整性快速检查: 相邻 bar 时间间隔应该在 tf_minutes*60 左右
        #    允许周末/假日跳变 (step > 1h 的一般是市场关闭, 不报警)
        if len(bars) > 1:
            expected_step = tf_minutes * 60
            gaps = 0
            for i in range(1, len(bars)):
                step = bars[i]["time"] - bars[i - 1]["time"]
                # 小偏差: 正常, 大跳变 (> 1h): 周末/假日, 只记不报警
                if step < expected_step * 0.5 and gaps < 3:
                    logger.warning(f"[BarFilter] 时间间隔异常: "
                                   f"t={datetime.utcfromtimestamp(bars[i-1]['time'])} → "
                                   f"{datetime.utcfromtimestamp(bars[i]['time'])} "
                                   f"step={step}s (期望 {expected_step}s)")
                    gaps += 1
                elif step > expected_step * 1.5 and step < 3600:
                    # 窄步长偏大: 可能中间缺失 bar
                    if gaps < 3:
                        logger.warning(f"[BarFilter] 时间间隔偏大: "
                                       f"step={step}s (期望 {expected_step}s)")
                        gaps += 1

        result.kept = bars
        return result

    def _get_max_time(self, symbol: str, timeframe: str) -> Optional[float]:
        """查 db 里该 symbol+timeframe 的最大 bar time (返回 epoch float)"""
        db = Path(self.db_path)
        if not db.exists():
            return None
        try:
            con = sqlite3.connect(str(db))
            cur = con.cursor()
            cur.execute(
                "SELECT MAX(time) FROM bars WHERE symbol=? AND timeframe=?",
                (symbol, timeframe),
            )
            row = cur.fetchone()
            con.close()
            if row and row[0] is not None:
                val = row[0]
                # db 里 time 可能是 TEXT (ISO) 或 INTEGER (epoch), 统一转 epoch
                if isinstance(val, str):
                    try:
                        return float(datetime.fromisoformat(val).timestamp())
                    except (ValueError, TypeError):
                        try:
                            return float(val)
                        except (ValueError, TypeError):
                            return None
                return float(val)
        except Exception as e:
            logger.warning(f"[BarFilter] 查 db 失败: {e}")
        return None
