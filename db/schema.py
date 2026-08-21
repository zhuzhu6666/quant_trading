"""db.schema — DDL for analytics tables.

Kept as plain SQL strings so it's easy to copy-paste into a sqlite
shell, run a migration script, or inspect with ``.schema``.

Add new tables here, then call ``AnalyticsStore(db_path)`` to
create-if-not-exist on next open.
"""

SCHEMA: dict[str, str] = {
    "strategy_perf": """
        CREATE TABLE IF NOT EXISTS strategy_perf (
            run_id          INTEGER NOT NULL,
            bar_ts          REAL    NOT NULL,
            bar_date        TEXT    NOT NULL,           -- 'YYYY-MM-DD' UTC
            strategy        TEXT    NOT NULL,
            regime          TEXT    NOT NULL DEFAULT '',-- 'TRENDING_UP|HIGH_VOL' or ''
            direction       INTEGER NOT NULL DEFAULT 0,  -- 1=long, -1=short, 0=flat
            hold_bars       INTEGER NOT NULL DEFAULT 0,  -- bars since open; 0=flat
            unrealized_pnl  REAL    NOT NULL DEFAULT 0,
            cum_pnl         REAL    NOT NULL DEFAULT 0, -- net PnL from run start
            position_open   INTEGER NOT NULL DEFAULT 0,  -- 0/1 boolean
            meta            TEXT    NOT NULL DEFAULT '',-- JSON, e.g. open_price/sl/tp
            PRIMARY KEY (run_id, bar_ts, strategy)
        )
    """,
    "idx_strategy_perf_run_date": """
        CREATE INDEX IF NOT EXISTS idx_strategy_perf_run_date
            ON strategy_perf(run_id, bar_date)
    """,
    "idx_strategy_perf_strategy": """
        CREATE INDEX IF NOT EXISTS idx_strategy_perf_strategy
            ON strategy_perf(strategy, bar_ts)
    """,

}


# Ordered list of DDL statements to apply on init.  Tables first,
# then indexes that depend on them.
DDL: list[str] = [
    SCHEMA["strategy_perf"],
    SCHEMA["idx_strategy_perf_run_date"],
    SCHEMA["idx_strategy_perf_strategy"],
]

TABLE_NAMES: list[str] = ["strategy_perf"]
