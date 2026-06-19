"""
data/duckdb_store.py — DuckDB 时序数据存储 (Phase 6.1)

替代 SQLite，提供列式存储 + 向量化查询。
接口与 DataStore 完全一致 (load_bars / insert_bars / insert_ticks 等)。

DuckDB 优势:
  - 列式存储 → 聚合查询 10-50x 快于 SQLite
  - MVCC 快照读 → 并发读写不阻塞
  - 向量化执行 → 时间序列窗口函数原生优化
  - 零配置部署 → pip install duckdb
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import duckdb
import pandas as pd

logger = logging.getLogger(__name__)


class DuckDBDataStore:
    """DuckDB 时序数据存储 — 线程安全单例，兼容 DataStore API。

    Schema:
      bars(symbol, timeframe, time, open, high, low, close, volume, spread)
      ticks(symbol, time, bid, ask, last, volume, flags)
      etf_holdings / cb_gold / cot_gold (同 SQLite)
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls, db_path: str = "data/ctrader_data.duckdb"):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
                    cls._instance.db_path = None
        return cls._instance

    def __init__(self, db_path: str = "data/ctrader_data.duckdb"):
        if self._initialized:
            return
        with self.__class__._lock:
            if self._initialized:
                return
            self.db_path = Path(db_path)
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._init_db()
            self._initialized = True

    def _get_conn(self) -> duckdb.DuckDBPyConnection:
        """获取 DuckDB 连接 (每次调用新建，轻量)"""
        return duckdb.connect(str(self.db_path))

    def _init_db(self):
        conn = self._get_conn()
        try:
            # DuckDB 自动处理并发，不需要 PRAGMA
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bars (
                    symbol VARCHAR NOT NULL,
                    timeframe VARCHAR NOT NULL,
                    time BIGINT NOT NULL,
                    open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
                    volume DOUBLE DEFAULT 0,
                    spread INTEGER DEFAULT 0,
                    UNIQUE(symbol, timeframe, time)
                )
            """)
            # 迁移: 旧表可能没有 spread 列
            try:
                conn.execute("ALTER TABLE bars ADD COLUMN IF NOT EXISTS spread INTEGER DEFAULT 0")
            except Exception:
                pass
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_bars_sym_tf_time
                ON bars(symbol, timeframe, time)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ticks (
                    symbol VARCHAR NOT NULL,
                    time DOUBLE NOT NULL,
                    bid DOUBLE, ask DOUBLE, last DOUBLE,
                    volume DOUBLE DEFAULT 0,
                    flags INTEGER DEFAULT 0
                )
            """)
            # DuckDB 1.x: UNIQUE INDEX 不是约束, INSERT OR REPLACE 无法用.
            # 改用普通 INDEX + 纯 INSERT (增量 tick 按时间序, 无重复).
            # 先尝试删旧 UNIQUE INDEX (若表已存在), 再建普通 INDEX.
            try:
                conn.execute("DROP INDEX IF EXISTS idx_ticks_sym_time")
            except Exception:
                pass
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_ticks_sym_time
                ON ticks(symbol, time)
            """)
            # ETF 持仓
            conn.execute("""
                CREATE TABLE IF NOT EXISTS etf_holdings (
                    symbol VARCHAR NOT NULL,
                    date VARCHAR NOT NULL,
                    total_tonnes DOUBLE,
                    total_shares DOUBLE,
                    aum_usd DOUBLE,
                    PRIMARY KEY (symbol, date)
                )
            """)
            # 央行黄金
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cb_gold (
                    country VARCHAR NOT NULL,
                    date VARCHAR NOT NULL,
                    total_tonnes DOUBLE,
                    monthly_chg_tonnes DOUBLE,
                    PRIMARY KEY (country, date)
                )
            """)
            # COT 黄金
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cot_gold (
                    report_date VARCHAR NOT NULL,
                    open_interest BIGINT,
                    mm_long BIGINT, mm_short BIGINT, mm_spread BIGINT,
                    pm_long BIGINT, pm_short BIGINT,
                    swap_long BIGINT, swap_short BIGINT,
                    other_long BIGINT, other_short BIGINT,
                    PRIMARY KEY (report_date)
                )
            """)
            # 宏观日度数据
            conn.execute("""
                CREATE TABLE IF NOT EXISTS macro_daily (
                    series VARCHAR NOT NULL,
                    date VARCHAR NOT NULL,
                    value DOUBLE,
                    PRIMARY KEY (series, date)
                )
            """)
            # ETF 日线
            conn.execute("""
                CREATE TABLE IF NOT EXISTS etf_daily (
                    symbol VARCHAR NOT NULL,
                    date VARCHAR NOT NULL,
                    close DOUBLE,
                    PRIMARY KEY (symbol, date)
                )
            """)
            # 经济事件
            conn.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    date VARCHAR NOT NULL,
                    type VARCHAR NOT NULL,
                    description VARCHAR,
                    importance INTEGER DEFAULT 0,
                    PRIMARY KEY (date, type)
                )
            """)
            logger.info("[DuckDB] tables initialized: %s", self.db_path)
        finally:
            conn.close()

    # ── Bars ───────────────────────────────────────────────

    def insert_bar(self, bar: dict, symbol: str, timeframe: str):
        conn = self._get_conn()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO bars
                (symbol, timeframe, time, open, high, low, close, volume, spread)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [symbol, timeframe, int(bar["time"]),
                  bar["open"], bar["high"], bar["low"], bar["close"],
                  bar.get("volume", 0), int(bar.get("spread", 0) or 0)])
        finally:
            conn.close()

    def insert_bars(self, bars: list[dict], symbol: str = "", timeframe: str = "") -> int:
        if not bars:
            return 0
        rows = [
            (symbol, timeframe, int(b["time"]),
             b["open"], b["high"], b["low"], b["close"],
             b.get("volume", 0), int(b.get("spread", 0) or 0))
            for b in bars
        ]
        conn = self._get_conn()
        try:
            conn.executemany("""
                INSERT OR REPLACE INTO bars
                (symbol, timeframe, time, open, high, low, close, volume, spread)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, rows)
        finally:
            conn.close()
        return len(rows)

    # (P1-a: first insert_ticks removed — duplicate, second one below is the live version)

    def load_bars(self, symbol: str, timeframe: str,
                  start: str | None = None, end: str | None = None,
                  limit: int | None = None) -> pd.DataFrame:
        """加载历史 bar → DataFrame (API 与 DataStore 完全兼容)"""
        conn = self._get_conn()
        try:
            query = "SELECT * FROM bars WHERE symbol=? AND timeframe=?"
            params = [symbol, timeframe]

            if start:
                query += " AND time >= ?"
                params.append(int(pd.Timestamp(start).timestamp()))
            if end:
                query += " AND time <= ?"
                params.append(int(pd.Timestamp(end).timestamp()))

            if limit is not None:
                query += " ORDER BY time DESC LIMIT ?"
                params.append(int(limit))
            else:
                query += " ORDER BY time ASC"

            df = conn.execute(query, params).df()

            if not df.empty:
                df["time"] = pd.to_datetime(df["time"], unit="s")
                df.set_index("time", inplace=True)
                df["time"] = df.index  # 保留为列供因子计算使用
                if limit is not None:
                    df = df.sort_index()
            return df
        finally:
            conn.close()

    # ── Ticks ──────────────────────────────────────────────

    def insert_ticks(self, ticks: list[dict], symbol: str):
        if not ticks:
            return
        conn = self._get_conn()
        try:
            rows = [(symbol, t["time"], t.get("bid", 0), t.get("ask", 0),
                     t.get("last", 0), t.get("volume", 0), t.get("flags", 0))
                    for t in ticks]
            # 批量写入 (MT5 tick 增量顺时间序, 无重复)
            for i in range(0, len(rows), 5000):
                batch = rows[i:i + 5000]
                placeholders = ", ".join(["(?, ?, ?, ?, ?, ?, ?)"] * len(batch))
                flat = [item for row in batch for item in row]
                conn.execute(
                    f"INSERT INTO ticks (symbol, time, bid, ask, last, volume, flags) VALUES {placeholders}",
                    flat,
                )
        finally:
            conn.close()

    def load_ticks(self, symbol: str,
                   start: float | None = None, end: float | None = None,
                   limit: int = 10000) -> pd.DataFrame:
        """加载 tick 数据"""
        conn = self._get_conn()
        try:
            query = "SELECT * FROM ticks WHERE symbol=?"
            params = [symbol]
            if start is not None:
                query += " AND time >= ?"
                params.append(start)
            if end is not None:
                query += " AND time <= ?"
                params.append(end)
            query += " ORDER BY time ASC LIMIT ?"
            params.append(limit)
            return conn.execute(query, params).df()
        finally:
            conn.close()

    def tick_count(self, symbol: str) -> int:
        conn = self._get_conn()
        try:
            r = conn.execute(
                "SELECT COUNT(*) FROM ticks WHERE symbol=?", [symbol]
            ).fetchone()
            return int(r[0]) if r else 0
        finally:
            conn.close()

    # ── ETF / CB / COT ─────────────────────────────────────

    def insert_etf_holding(self, symbol: str, date: str,
                           total_tonnes: float | None = None,
                           total_shares: float | None = None,
                           aum_usd: float | None = None):
        conn = self._get_conn()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO etf_holdings
                (symbol, date, total_tonnes, total_shares, aum_usd)
                VALUES (?, ?, ?, ?, ?)
            """, [symbol, date, total_tonnes, total_shares, aum_usd])
        finally:
            conn.close()

    def insert_cb_gold(self, country: str, date: str,
                       total_tonnes: float | None = None,
                       monthly_chg_tonnes: float | None = None):
        conn = self._get_conn()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO cb_gold
                (country, date, total_tonnes, monthly_chg_tonnes)
                VALUES (?, ?, ?, ?)
            """, [country, date, total_tonnes, monthly_chg_tonnes])
        finally:
            conn.close()

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
        conn = self._get_conn()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO cot_gold
                (report_date, open_interest, mm_long, mm_short, mm_spread,
                 pm_long, pm_short, swap_long, swap_short, other_long, other_short)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [report_date, open_interest, mm_long, mm_short, mm_spread,
                  pm_long, pm_short, swap_long, swap_short, other_long, other_short])
        finally:
            conn.close()

    # ── 统计 ───────────────────────────────────────────────

    def bar_count(self, symbol: str, timeframe: str) -> int:
        conn = self._get_conn()
        try:
            r = conn.execute(
                "SELECT COUNT(*) FROM bars WHERE symbol=? AND timeframe=?",
                [symbol, timeframe],
            ).fetchone()
            return int(r[0]) if r else 0
        finally:
            conn.close()

    def summary(self) -> list[tuple]:
        """返回 (symbol, timeframe, count) 列表"""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT symbol, timeframe, COUNT(*) as cnt FROM bars "
                "GROUP BY symbol, timeframe ORDER BY symbol, timeframe"
            ).fetchall()
            return [(r[0], r[1], r[2]) for r in rows]
        finally:
            conn.close()

    def latest_bar_time(self, symbol: str, timeframe: str) -> int | None:
        """返回最新 bar 的 epoch 秒"""
        conn = self._get_conn()
        try:
            r = conn.execute(
                "SELECT MAX(time) FROM bars WHERE symbol=? AND timeframe=?",
                [symbol, timeframe],
            ).fetchone()
            return int(r[0]) if r and r[0] else None
        finally:
            conn.close()
