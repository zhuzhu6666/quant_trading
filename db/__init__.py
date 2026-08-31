"""db — analytics & run-tracking database layer.

Holds tables that are NOT in ``data/market_data.db`` (which is the raw
market store).  These are post-run analytics tables used for downstream
research, regime-conditional analysis, and ML feature extraction.

Conventions:
  * One SQLite file (``data/analytics.db``) — kept separate from
    market_data.db so a ``rm`` of one doesn't kill the other.
  * All tables have a ``run_id`` so multiple backtests / paper runs
    can be compared side-by-side.
  * ``bar_ts`` is a unix epoch in seconds (UTC), matching
    ``data.duckdb_store.DuckDBDataStore`` so the two DBs can be joined cheaply.
"""

from .schema import SCHEMA, TABLE_NAMES  # noqa: F401
# AnalyticsStore 已移除(2026-08: 死表 strategy_perf 无调用)，历史实现见 git log db/store.py
