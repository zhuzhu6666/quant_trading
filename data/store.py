"""
Data Store — 时序数据持久化

提供统一的数据存取层：
- 历史bar读写
- 当日tick存储
- 支持SQLite后端（未来可切InfluxDB）

Schema:
  bars(symbol, timeframe, time, open, high, low, close, volume)
  ticks(symbol, time, bid, ask, last, volume)
"""

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)


class DataStore:
    """时序数据存储"""

    def __init__(self, db_path: str = "data/market_data.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self):
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bars (
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    time INTEGER NOT NULL,
                    open REAL, high REAL, low REAL, close REAL,
                    volume REAL DEFAULT 0,
                    UNIQUE(symbol, timeframe, time)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_bars_sym_tf_time 
                ON bars(symbol, timeframe, time)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ticks (
                    symbol TEXT NOT NULL,
                    time REAL NOT NULL,
                    bid REAL, ask REAL, last REAL, volume REAL DEFAULT 0
                )
            """)

    def insert_bar(self, bar: dict, symbol: str, timeframe: str):
        """插入单根完成bar"""
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO bars 
                   (symbol, timeframe, time, open, high, low, close, volume)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (symbol, timeframe, int(bar["time"]),
                 bar["open"], bar["high"], bar["low"], bar["close"],
                 bar.get("volume", 0)),
            )

    def insert_bars(self, bars: list[dict], symbol: str, timeframe: str):
        """批量插入bar"""
        with self._conn() as conn:
            conn.executemany(
                """INSERT OR REPLACE INTO bars 
                   (symbol, timeframe, time, open, high, low, close, volume)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [(symbol, timeframe, int(b["time"]),
                  b["open"], b["high"], b["low"], b["close"],
                  b.get("volume", 0)) for b in bars],
            )

    def load_bars(self, symbol: str, timeframe: str,
                  start: str | None = None, end: str | None = None) -> pd.DataFrame:
        """加载历史bar → DataFrame"""
        query = "SELECT * FROM bars WHERE symbol=? AND timeframe=?"
        params = [symbol, timeframe]

        if start:
            query += " AND time >= ?"
            params.append(int(pd.Timestamp(start).timestamp()))
        if end:
            query += " AND time <= ?"
            params.append(int(pd.Timestamp(end).timestamp()))

        query += " ORDER BY time ASC"

        with self._conn() as conn:
            df = pd.read_sql_query(query, conn, params=params)

        if not df.empty:
            df["time"] = pd.to_datetime(df["time"], unit="s")
            df.set_index("time", inplace=True)
        return df

    def insert_ticks(self, ticks: list[dict], symbol: str):
        """批量插入tick"""
        with self._conn() as conn:
            conn.executemany(
                "INSERT INTO ticks (symbol, time, bid, ask, last, volume) VALUES (?,?,?,?,?,?)",
                [(symbol, t["time"], t["bid"], t["ask"], t["last"], t.get("volume", 0))
                 for t in ticks],
            )

    def bar_count(self, symbol: str, timeframe: str) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM bars WHERE symbol=? AND timeframe=?",
                (symbol, timeframe),
            ).fetchone()
            return row["cnt"]

    def summary(self) -> list[tuple]:
        """返回 (symbol, timeframe, count) 列表"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT symbol, timeframe, COUNT(*) as cnt FROM bars "
                "GROUP BY symbol, timeframe ORDER BY symbol, timeframe"
            ).fetchall()
            return [(r["symbol"], r["timeframe"], r["cnt"]) for r in rows]
