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
import threading
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)


class DataStore:
    """时序数据存储 — 线程安全单例, 避免并发 _init_db 和 SQLite 锁争用。"""
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, db_path: str = "data/market_data.db"):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
                    cls._instance.db_path = None
        return cls._instance

    def __init__(self, db_path: str = "data/market_data.db"):
        if self._initialized:
            return
        with self.__class__._lock:
            if self._initialized:
                return
            self.db_path = Path(db_path)
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._init_db()
            self._initialized = True

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path), timeout=1.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self):
        with self._conn() as conn:
            # WAL 模式: 支持并发读写, 避免 live loop 线程和 API 同时读 SQLite 时阻塞5s
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bars (
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    time INTEGER NOT NULL,
                    open REAL, high REAL, low REAL, close REAL,
                    volume REAL DEFAULT 0,
                    spread INTEGER DEFAULT 0,
                    UNIQUE(symbol, timeframe, time)
                )
            """)
            # 兼容旧 db：检测到没 spread 列就 ALTER TABLE 加
            cols = [r[1] for r in conn.execute("PRAGMA table_info(bars)").fetchall()]
            if "spread" not in cols:
                conn.execute("ALTER TABLE bars ADD COLUMN spread INTEGER DEFAULT 0")
                logger.info("[DataStore] bars 表新增 spread 列 (旧库迁移)")
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
            # P0-ETF (2026-06-03): ETF 持仓/资金流表
            # 跟 etf_daily (price-only) 区分, 主键 (symbol, date)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS etf_holdings (
                    symbol TEXT NOT NULL,
                    date TEXT NOT NULL,
                    total_tonnes REAL,         -- GLD = 金衡盎司 / 32150.7, SLV 同
                    total_shares REAL,         -- shares outstanding (百万股)
                    aum_usd REAL,              -- AUM in USD
                    PRIMARY KEY (symbol, date)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_etf_holdings_sym_date
                ON etf_holdings(symbol, date)
            """)
            # P0-CB (2026-06-03): 央行黄金月度净买入 (吨)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cb_gold (
                    country TEXT NOT NULL,    -- 'CHINA' / 'RUSSIA' / 'TURKEY' / 'INDIA' / 'TOTAL'
                    date TEXT NOT NULL,       -- 月末
                    total_tonnes REAL,        -- 累计持仓 (吨)
                    monthly_chg_tonnes REAL,  -- 当月净买入 (吨), 可负
                    PRIMARY KEY (country, date)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_cb_gold_country_date
                ON cb_gold(country, date)
            """)
            # P0-COT (2026-06-03): CFTC COT 黄金持仓 (周度)
            # 含 4 类持仓者: M_Money(投机) / Prod_Merc(商业) / Swap(互换) / Other
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cot_gold (
                    report_date TEXT NOT NULL,         -- 周报日期 (YYYY-MM-DD)
                    open_interest INTEGER,             -- 总未平仓合约
                    mm_long INTEGER,                  -- Managed Money (非商业/投机) 多
                    mm_short INTEGER,                 -- Managed Money 空
                    mm_spread INTEGER,                -- Managed Money 跨期
                    pm_long INTEGER,                  -- Producer/Merchant (商业/对冲) 多
                    pm_short INTEGER,                 -- Producer/Merchant 空
                    swap_long INTEGER,                -- Swap 互换 多
                    swap_short INTEGER,               -- Swap 互换 空
                    other_long INTEGER,               -- Other Reportable 多
                    other_short INTEGER,              -- Other Reportable 空
                    PRIMARY KEY (report_date)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_cot_gold_date
                ON cot_gold(report_date)
            """)

    def insert_bar(self, bar: dict, symbol: str, timeframe: str):
        """插入单根完成bar"""
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO bars
                   (symbol, timeframe, time, open, high, low, close, volume, spread)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (symbol, timeframe, int(bar["time"]),
                 bar["open"], bar["high"], bar["low"], bar["close"],
                 bar.get("volume", 0), int(bar.get("spread", 0) or 0)),
            )

    def insert_bars(self, bars: list[dict], symbol: str, timeframe: str):
        """批量插入bar"""
        with self._conn() as conn:
            conn.executemany(
                """INSERT OR REPLACE INTO bars
                   (symbol, timeframe, time, open, high, low, close, volume, spread)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(symbol, timeframe, int(b["time"]),
                  b["open"], b["high"], b["low"], b["close"],
                  b.get("volume", 0), int(b.get("spread", 0) or 0)) for b in bars],
            )

    def load_bars(self, symbol: str, timeframe: str,
                  start: str | None = None, end: str | None = None,
                  limit: int | None = None) -> pd.DataFrame:
        """加载历史bar → DataFrame.

        Args:
            symbol: 品种 (e.g. "XAUUSD+")
            timeframe: 周期 (e.g. "M15")
            start: ISO 时间字符串或 None
            end: ISO 时间字符串或 None
            limit: 最多返回 N 根 (None=全部). 与 start/end 组合时:
                - 同时给 limit + (start/end): 仍下推到 SQL (反序+LIMIT 拿最近 N 段)
                - 只给 limit: 拿最近 N 根 (SQL 反序 + LIMIT N, 再 Python 排正序)
                - 不给 limit: 全部 (老行为)
        """
        query = "SELECT * FROM bars WHERE symbol=? AND timeframe=?"
        params = [symbol, timeframe]

        if start:
            query += " AND time >= ?"
            params.append(int(pd.Timestamp(start).timestamp()))
        if end:
            query += " AND time <= ?"
            params.append(int(pd.Timestamp(end).timestamp()))

        if limit is not None:
            # ★ 反序 + LIMIT 拿最近 N 根, 走 idx_bars_sym_tf_time 索引
            query += " ORDER BY time DESC LIMIT ?"
            params.append(int(limit))
        else:
            query += " ORDER BY time ASC"

        with self._conn() as conn:
            df = pd.read_sql_query(query, conn, params=params)

        if not df.empty:
            df["time"] = pd.to_datetime(df["time"], unit="s")
            df.set_index("time", inplace=True)
            if limit is not None:
                # 反序拉的, 排回正序保持向后兼容 (其它调用方期望升序)
                df = df.sort_index()
        return df

    def insert_ticks(self, ticks: list[dict], symbol: str):
        """批量插入tick"""
        with self._conn() as conn:
            conn.executemany(
                "INSERT INTO ticks (symbol, time, bid, ask, last, volume) VALUES (?,?,?,?,?,?)",
                [(symbol, t["time"], t["bid"], t["ask"], t["last"], t.get("volume", 0))
                 for t in ticks],
            )

    def insert_etf_holding(self, symbol: str, date: str,
                           total_tonnes: float | None = None,
                           total_shares: float | None = None,
                           aum_usd: float | None = None):
        """插入/更新单日 ETF 持仓 (INSERT OR REPLACE)"""
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO etf_holdings
                   (symbol, date, total_tonnes, total_shares, aum_usd)
                   VALUES (?, ?, ?, ?, ?)""",
                (symbol, date, total_tonnes, total_shares, aum_usd),
            )

    def insert_cb_gold(self, country: str, date: str,
                       total_tonnes: float | None = None,
                       monthly_chg_tonnes: float | None = None):
        """插入/更新单月央行黄金 (INSERT OR REPLACE)"""
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO cb_gold
                   (country, date, total_tonnes, monthly_chg_tonnes)
                   VALUES (?, ?, ?, ?)""",
                (country, date, total_tonnes, monthly_chg_tonnes),
            )

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
        """插入/更新单周 COT 黄金 (INSERT OR REPLACE)"""
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO cot_gold
                   (report_date, open_interest, mm_long, mm_short, mm_spread,
                    pm_long, pm_short, swap_long, swap_short, other_long, other_short)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (report_date, open_interest, mm_long, mm_short, mm_spread,
                 pm_long, pm_short, swap_long, swap_short, other_long, other_short),
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
