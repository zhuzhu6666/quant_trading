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

    # ── Decision Log (P9 / Task 16) ──────────────────────────────

    "decision_log": """
        CREATE TABLE IF NOT EXISTS decision_log (
            log_id          INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id          INTEGER NOT NULL,
            ts              REAL    NOT NULL,
            bar_date        TEXT    NOT NULL,
            decision_type   TEXT    NOT NULL,
            strategy        TEXT,
            regime          TEXT,
            direction       INTEGER,
            confidence      REAL,
            factor_scores   TEXT,
            decision        TEXT    NOT NULL,
            meta            TEXT    NOT NULL DEFAULT ''
        )
    """,
    "idx_decision_log_run_ts": """
        CREATE INDEX IF NOT EXISTS idx_decision_log_run_ts
            ON decision_log(run_id, ts)
    """,
    "idx_decision_log_strategy": """
        CREATE INDEX IF NOT EXISTS idx_decision_log_strategy
            ON decision_log(strategy, ts)
    """,
    "idx_decision_log_type": """
        CREATE INDEX IF NOT EXISTS idx_decision_log_type
            ON decision_log(decision_type, ts)
    """,
}


# Ordered list of DDL statements to apply on init.  Tables first,
# then indexes that depend on them.
DDL: list[str] = [
    SCHEMA["strategy_perf"],
    SCHEMA["idx_strategy_perf_run_date"],
    SCHEMA["idx_strategy_perf_strategy"],
    SCHEMA["decision_log"],
    SCHEMA["idx_decision_log_run_ts"],
    SCHEMA["idx_decision_log_strategy"],
    SCHEMA["idx_decision_log_type"],
]

TABLE_NAMES: list[str] = ["strategy_perf", "decision_log"]

# Decision-log-specific DDL (used by DecisionLogStore in its own DB file).
DECISION_LOG_DDL: list[str] = [
    SCHEMA["decision_log"],
    SCHEMA["idx_decision_log_run_ts"],
    SCHEMA["idx_decision_log_strategy"],
    SCHEMA["idx_decision_log_type"],
]
