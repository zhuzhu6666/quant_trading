"""
Data Store — 时序数据持久化

提供统一的数据存取层:
- 历史bar读写
- 当日tick存储
- DuckDB 后端 (Phase 6 迁移完成)

Schema:
  bars(symbol, timeframe, time, open, high, low, close, volume)
  ticks(symbol, time, bid, ask, last, volume)
"""

import logging
import threading
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)


class DataStore:
    """时序数据存储 — Phase 6: DuckDB 后端 (线程安全单例).

    DB 文件: data/ctrader_data.duckdb (cTrader 数据源)
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls, db_path: str = "data/ctrader_data.duckdb"):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self, db_path: str = "data/ctrader_data.duckdb"):
        if self._initialized:
            return
        with self.__class__._lock:
            if self._initialized:
                return
            # Phase 6: 统一使用 DuckDB, 旧 .db 路径自动重定向
            p = Path(db_path)
            if p.suffix == ".db":
                db_path = str(p.with_suffix(".duckdb"))
            from data.duckdb_store import DuckDBDataStore
            self._backend = DuckDBDataStore(db_path)
            self.db_path = self._backend.db_path
            self._initialized = True

    def insert_bar(self, bar: dict, symbol: str, timeframe: str):
        self._backend.insert_bar(bar, symbol, timeframe)

    def insert_bars(self, bars: list[dict], symbol: str, timeframe: str):
        self._backend.insert_bars(bars, symbol, timeframe)

    def load_bars(self, symbol: str, timeframe: str,
                  start: str | None = None, end: str | None = None,
                  limit: int | None = None) -> pd.DataFrame:
        return self._backend.load_bars(symbol, timeframe, start=start, end=end, limit=limit)

    def insert_ticks(self, ticks: list[dict], symbol: str):
        self._backend.insert_ticks(ticks, symbol)

    def insert_etf_holding(self, symbol: str, date: str,
                           total_tonnes: float | None = None,
                           total_shares: float | None = None,
                           aum_usd: float | None = None):
        self._backend.insert_etf_holding(symbol, date, total_tonnes, total_shares, aum_usd)

    def insert_cb_gold(self, country: str, date: str,
                       total_tonnes: float | None = None,
                       monthly_chg_tonnes: float | None = None):
        self._backend.insert_cb_gold(country, date, total_tonnes, monthly_chg_tonnes)

    def insert_cot_gold(self, report_date: str,
                        open_interest: int | None = None,
                        mm_long: int | None = None,
                        mm_short: int | None = None,
                        mm_spread: int | None = None,
                        pm_long: int | None = None,
                        pm_short: int | None = None,
                        swap_long: int | None = None,
                        swap_short: int | None = None,
                        other_long: int | None = None,
                        other_short: int | None = None):
        self._backend.insert_cot_gold(report_date, open_interest,
                                      mm_long, mm_short, mm_spread,
                                      pm_long, pm_short, swap_long, swap_short,
                                      other_long, other_short)

    def bar_count(self, symbol: str, timeframe: str) -> int:
        return self._backend.bar_count(symbol, timeframe)

    def summary(self) -> list[tuple]:
        return self._backend.summary()
