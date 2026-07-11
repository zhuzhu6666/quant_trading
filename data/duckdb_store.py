"""
data/duckdb_store.py — DuckDB 时序数据存储 (Phase 6.1)

替代 SQLite，提供列式存储 + 向量化查询。
接口与 DataStore 完全一致。

DuckDB 优势:
  - 列式存储 → 聚合查询 10-50x 快于 SQLite
  - MVCC 快照读 → 并发读写不阻塞
  - 向量化执行 → 时间序列窗口函数原生优化
  - 零配置部署 → pip install duckdb
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import duckdb
import pandas as pd

from backend.core.db import (
    DUCKDB_BARS_CURRENT,
    DUCKDB_BARS_LEGACY,
    DUCKDB_BARS_MONTHLY_DIR,
    DUCKDB_EXTERNAL,
    bars_monthly_path,
    connect_duckdb,
    duckdb_readonly_connection,
    ensure_bars_table,
    refresh_current_bars_link,
)
from data.external_schema import cot_release_at, ensure_external_schema, etf_release_at, macro_release_at

logger = logging.getLogger(__name__)


class DuckDBDataStore:
    """DuckDB 时序数据存储 — 线程安全单例，兼容 DataStore API。

    Schema:
      bars(symbol, timeframe, time, open, high, low, close, volume, spread)
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
            self._monthly_bars = self.db_path.name in {
                DUCKDB_BARS_LEGACY.name,
                DUCKDB_BARS_CURRENT.name,
            }
            self._external_only = self.db_path.name == DUCKDB_EXTERNAL.name
            self._externalized_legacy = self.db_path.name == DUCKDB_BARS_LEGACY.name
            self.bars_db_path = DUCKDB_BARS_CURRENT if self._monthly_bars else self.db_path
            self.external_db_path = DUCKDB_EXTERNAL if self._externalized_legacy else self.db_path
            self._init_db()
            if self._external_only or self._externalized_legacy:
                ensure_external_schema(self.external_db_path)
            self._initialized = True

    def _get_conn(self) -> duckdb.DuckDBPyConnection:
        """获取非 bars DuckDB 连接 (每次调用新建，轻量)"""
        return connect_duckdb(self.external_db_path)

    def _get_bars_conn(self, ts: int | float | None = None) -> duckdb.DuckDBPyConnection:
        """Open the DuckDB file that owns bars for the given timestamp."""
        path = bars_monthly_path(ts) if self._monthly_bars else self.bars_db_path
        conn = connect_duckdb(path)
        ensure_bars_table(conn)
        if self._monthly_bars:
            refresh_current_bars_link()
        return conn

    def _bar_read_paths(self) -> list[Path]:
        if not self._monthly_bars:
            return [self.bars_db_path]
        paths = sorted(DUCKDB_BARS_MONTHLY_DIR.glob("bars_*.duckdb"))
        if paths:
            return paths
        # Cold-start compatibility before migration: read the legacy monolith.
        return [self.db_path]

    @staticmethod
    def _to_epoch(value: str | int | float | None) -> int | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return int(value)
        return int(pd.Timestamp(value).timestamp())

    def _init_db(self):
        conn = self._get_conn()
        try:
            # DuckDB 自动处理并发，不需要 PRAGMA. bars 由月库承载；
            # legacy ctrader_data.duckdb 的外部表写入自动跳到 external_data.duckdb。
            if not self._monthly_bars and not self._external_only:
                ensure_bars_table(conn)
            # ETF 持仓
            conn.execute("""
                CREATE TABLE IF NOT EXISTS etf_holdings (
                    symbol VARCHAR NOT NULL,
                    date VARCHAR NOT NULL,
                    total_tonnes DOUBLE,
                    total_shares DOUBLE,
                    aum_usd DOUBLE,
                    release_at DOUBLE DEFAULT 0,
                    fetched_at DOUBLE DEFAULT 0,
                    source VARCHAR DEFAULT 'unknown',
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
                    release_at DOUBLE DEFAULT 0,
                    fetched_at DOUBLE DEFAULT 0,
                    source VARCHAR DEFAULT 'unknown',
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
                    release_at DOUBLE DEFAULT 0,
                    fetched_at DOUBLE DEFAULT 0,
                    source VARCHAR DEFAULT 'unknown',
                    PRIMARY KEY (report_date)
                )
            """)
            # 宏观日度数据
            conn.execute("""
                CREATE TABLE IF NOT EXISTS macro_daily (
                    series VARCHAR NOT NULL,
                    date VARCHAR NOT NULL,
                    value DOUBLE,
                    release_at DOUBLE DEFAULT 0,
                    fetched_at DOUBLE DEFAULT 0,
                    source VARCHAR DEFAULT 'unknown',
                    PRIMARY KEY (series, date)
                )
            """)
            # ETF 日线
            conn.execute("""
                CREATE TABLE IF NOT EXISTS etf_daily (
                    symbol VARCHAR NOT NULL,
                    date VARCHAR NOT NULL,
                    close DOUBLE,
                    release_at DOUBLE DEFAULT 0,
                    fetched_at DOUBLE DEFAULT 0,
                    source VARCHAR DEFAULT 'unknown',
                    PRIMARY KEY (symbol, date)
                )
            """)
            for table in ("etf_holdings", "cb_gold", "cot_gold", "macro_daily", "etf_daily"):
                conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS release_at DOUBLE DEFAULT 0")
                conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS fetched_at DOUBLE DEFAULT 0")
                conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS source VARCHAR DEFAULT 'unknown'")
            logger.info("[DuckDB] tables initialized: %s", self.db_path)
        finally:
            conn.close()

    # ── Bars ───────────────────────────────────────────────

    def insert_bar(self, bar: dict, symbol: str, timeframe: str):
        self.insert_bars([bar], symbol, timeframe)

    def insert_bars(self, bars: list[dict], symbol: str = "", timeframe: str = "") -> int:
        if not bars:
            return 0
        rows = [
            (symbol, timeframe, int(b["time"]),
             b["open"], b["high"], b["low"], b["close"],
             b.get("volume", 0), int(b.get("spread", 0) or 0))
            for b in bars
        ]
        grouped: dict[str, list[tuple]] = {}
        for row in rows:
            key = str(bars_monthly_path(row[2]) if self._monthly_bars else self.bars_db_path)
            grouped.setdefault(key, []).append(row)

        for path_str, batch in grouped.items():
            conn = connect_duckdb(path_str)
            try:
                ensure_bars_table(conn)
                conn.executemany("""
                    INSERT OR REPLACE INTO bars
                    (symbol, timeframe, time, open, high, low, close, volume, spread)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, batch)
            finally:
                conn.close()
        if self._monthly_bars:
            refresh_current_bars_link()
        return len(rows)

    def load_bars(self, symbol: str, timeframe: str,
                  start: str | None = None, end: str | None = None,
                  limit: int | None = None) -> pd.DataFrame:
        """加载历史 bar → DataFrame (API 与 DataStore 完全兼容)"""
        start_ts = self._to_epoch(start)
        end_ts = self._to_epoch(end)
        frames: list[pd.DataFrame] = []
        paths = self._bar_read_paths()
        if limit is not None and start_ts is None and end_ts is None:
            paths = list(reversed(paths))

        remaining = int(limit) if limit is not None else None
        for path in paths:
            if remaining is not None and remaining <= 0:
                break
            if not path.exists():
                continue
            with duckdb_readonly_connection(path, snapshot_on_lock=True) as conn:
                query = "SELECT * FROM bars WHERE symbol=? AND timeframe=?"
                params: list = [symbol, timeframe]
                if start_ts is not None:
                    query += " AND time >= ?"
                    params.append(start_ts)
                if end_ts is not None:
                    query += " AND time <= ?"
                    params.append(end_ts)
                if remaining is not None:
                    query += " ORDER BY time DESC LIMIT ?"
                    params.append(remaining)
                else:
                    query += " ORDER BY time ASC"
                df_part = conn.execute(query, params).df()
            if df_part.empty:
                continue
            frames.append(df_part)
            if remaining is not None:
                remaining -= len(df_part)

        if not frames:
            return pd.DataFrame()
        df = pd.concat(frames, ignore_index=True)
        df = df.drop_duplicates(subset=["symbol", "timeframe", "time"], keep="last")
        df = df.sort_values("time")
        if limit is not None and len(df) > limit:
            df = df.tail(limit)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        df.set_index("time", inplace=True)
        df["time"] = df.index  # 保留为列供因子计算使用
        return df

    # ── ETF / CB / COT ─────────────────────────────────────

    def insert_etf_holding(self, symbol: str, date: str,
                           total_tonnes: float | None = None,
                           total_shares: float | None = None,
                           aum_usd: float | None = None,
                           release_at: float | None = None,
                           fetched_at: float | None = None,
                           source: str = "sec_edgar"):
        conn = self._get_conn()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO etf_holdings
                (symbol, date, total_tonnes, total_shares, aum_usd, release_at, fetched_at, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                symbol, date, total_tonnes, total_shares, aum_usd,
                float(release_at if release_at is not None else etf_release_at(None, date)),
                float(fetched_at or time.time()),
                source,
            ])
        finally:
            conn.close()

    def insert_cb_gold(self, country: str, date: str,
                       total_tonnes: float | None = None,
                       monthly_chg_tonnes: float | None = None,
                       release_at: float | None = None,
                       fetched_at: float | None = None,
                       source: str = "external"):
        conn = self._get_conn()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO cb_gold
                (country, date, total_tonnes, monthly_chg_tonnes, release_at, fetched_at, source)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, [
                country, date, total_tonnes, monthly_chg_tonnes,
                float(release_at if release_at is not None else macro_release_at(date)),
                float(fetched_at or time.time()),
                source,
            ])
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
                        other_short: int | None = None,
                        release_at: float | None = None,
                        fetched_at: float | None = None,
                        source: str = "cftc"):
        conn = self._get_conn()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO cot_gold
                (report_date, open_interest, mm_long, mm_short, mm_spread,
                 pm_long, pm_short, swap_long, swap_short, other_long, other_short,
                 release_at, fetched_at, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [report_date, open_interest, mm_long, mm_short, mm_spread,
                  pm_long, pm_short, swap_long, swap_short, other_long, other_short,
                  float(release_at if release_at is not None else cot_release_at(report_date)),
                  float(fetched_at or time.time()),
                  source])
        finally:
            conn.close()

    def insert_macro_daily(self, series: str, date: str, value: float | None,
                           release_at: float | None = None,
                           fetched_at: float | None = None,
                           source: str = "fred"):
        conn = self._get_conn()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO macro_daily
                (series, date, value, release_at, fetched_at, source)
                VALUES (?, ?, ?, ?, ?, ?)
            """, [
                series, date, value,
                float(release_at if release_at is not None else macro_release_at(date)),
                float(fetched_at or time.time()),
                source,
            ])
        finally:
            conn.close()

    # ── 统计 ───────────────────────────────────────────────

    def bar_count(self, symbol: str, timeframe: str) -> int:
        total = 0
        for path in self._bar_read_paths():
            if not path.exists():
                continue
            try:
                conn = connect_duckdb(path, read_only=True)
                try:
                    r = conn.execute(
                        "SELECT COUNT(*) FROM bars WHERE symbol=? AND timeframe=?",
                        [symbol, timeframe],
                    ).fetchone()
                    total += int(r[0]) if r else 0
                finally:
                    conn.close()
            except Exception:
                continue
        return total

    def summary(self) -> list[tuple]:
        """返回 (symbol, timeframe, count) 列表"""
        totals: dict[tuple[str, str], int] = {}
        for path in self._bar_read_paths():
            if not path.exists():
                continue
            try:
                conn = connect_duckdb(path, read_only=True)
                try:
                    rows = conn.execute(
                        "SELECT symbol, timeframe, COUNT(*) as cnt FROM bars "
                        "GROUP BY symbol, timeframe"
                    ).fetchall()
                finally:
                    conn.close()
            except Exception:
                continue
            for symbol, timeframe, count in rows:
                key = (symbol, timeframe)
                totals[key] = totals.get(key, 0) + int(count)
        return [(s, tf, cnt) for (s, tf), cnt in sorted(totals.items())]

    def latest_bar_time(self, symbol: str, timeframe: str) -> int | None:
        """返回最新 bar 的 epoch 秒"""
        latest: int | None = None
        for path in self._bar_read_paths():
            if not path.exists():
                continue
            try:
                conn = connect_duckdb(path, read_only=True)
                try:
                    r = conn.execute(
                        "SELECT MAX(time) FROM bars WHERE symbol=? AND timeframe=?",
                        [symbol, timeframe],
                    ).fetchone()
                finally:
                    conn.close()
            except Exception:
                continue
            if r and r[0]:
                value = int(r[0])
                latest = value if latest is None else max(latest, value)
        return latest

