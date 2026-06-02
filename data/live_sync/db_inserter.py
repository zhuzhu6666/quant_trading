"""
data/live_sync/db_inserter.py — 数据库插入 (T16.3, 2026-06-02)

包装 data.store.DataStore, 加:
- 多 timeframe 一次性插入
- 错误重试 (指数退避)
- sync 状态记录 (last_sync.json)
- 日志

跟 data/store.py 的 DataStore.insert_bars 完全兼容.
"""
import json
import logging
import time as _time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from data.store import DataStore

logger = logging.getLogger(__name__)


@dataclass
class InsertResult:
    """单次插入结果"""
    symbol: str
    timeframe: str
    inserted: int
    total_db_bars: int
    duration_sec: float
    error: str = ""


@dataclass
class SyncStatus:
    """持久化的 sync 状态 (last_sync.json)"""
    last_sync_utc: str
    per_tf: dict   # { "M15": { "last_bar_time": 1234567890, "n_bars": 50000 } }


class DBInserter:
    """
    数据库插入器.

    用法:
        ins = DBInserter()
        results = ins.insert_multi(bars_by_tf, symbol="XAUUSD+", timeframe="M15")
        ins.save_status(results, symbol)
    """

    def __init__(self, db_path: str = "data/market_data.db",
                 status_dir: str = "data/charts"):
        self.store = DataStore(db_path)
        self.status_dir = Path(status_dir)
        self.status_dir.mkdir(parents=True, exist_ok=True)
        self._status_path = self.status_dir / "live_sync_status.json"

    def insert_bars(self, bars: list[dict], symbol: str, timeframe: str,
                    max_retries: int = 3) -> InsertResult:
        """插入单个 timeframe 的 bar"""
        import time as _time
        t0 = _time.time()
        result = InsertResult(symbol=symbol, timeframe=timeframe, inserted=0, total_db_bars=0, duration_sec=0)
        if not bars:
            result.total_db_bars = self.store.bar_count(symbol, timeframe)
            return result

        for attempt in range(max_retries):
            try:
                self.store.insert_bars(bars, symbol, timeframe)
                break
            except Exception as e:
                if attempt < max_retries - 1:
                    wait = 2 ** attempt
                    logger.warning(f"[DBInserter] 插入失败 (attempt {attempt+1}): {e}, {wait}s 后重试")
                    _time.sleep(wait)
                else:
                    result.error = f"{type(e).__name__}: {e}"
                    return result

        result.inserted = len(bars)
        result.duration_sec = _time.time() - t0
        result.total_db_bars = self.store.bar_count(symbol, timeframe)
        logger.info(f"[DBInserter] {symbol} {timeframe}: +{result.inserted} bars "
                    f"(总 {result.total_db_bars}, {result.duration_sec:.2f}s)")
        return result

    def load_status(self) -> Optional[SyncStatus]:
        """加载上次 sync 状态"""
        if not self._status_path.exists():
            return None
        try:
            d = json.loads(self._status_path.read_text(encoding="utf-8"))
            return SyncStatus(**d)
        except Exception:
            return None

    def save_status(self, results: list[InsertResult], symbol: str):
        """保存 sync 状态"""
        status = self.load_status()
        per_tf = (status.per_tf if status else {})
        for r in results:
            per_tf[r.timeframe] = {
                "inserted_last": r.inserted,
                "total_bars": r.total_db_bars,
            }
        import time as _time
        s = SyncStatus(
            last_sync_utc=datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            per_tf=per_tf,
        )
        self._status_path.write_text(json.dumps(asdict(s), ensure_ascii=False, indent=2),
                                     encoding="utf-8")
        logger.info(f"[DBInserter] sync 状态已保存: {self._status_path}")
