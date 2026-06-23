"""backend/core/db.py — 统一数据库路径常量 + 连接管理。

所有数据库路径集中定义，不再硬编码。
DuckDB 保留时序数据，SQLite 收纳运行时状态。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

# ═══════════════════════════════════════════
# 项目根 (兼容 backend/ 和顶层导入)
# ═══════════════════════════════════════════
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = _PROJECT_ROOT / "data"

# ═══════════════════════════════════════════
# DuckDB — 时序/市场数据 (不可合并)
# ═══════════════════════════════════════════
DUCKDB_BARS    = DATA_DIR / "ctrader_data.duckdb"   # K线 + 外部数据(COT/ETF)
DUCKDB_TICKS   = DATA_DIR / "ticks.duckdb"           # Dukascopy tick
DUCKDB_L2      = DATA_DIR / "l2.duckdb"              # L2 订单簿深度
DUCKDB_TRADES  = DATA_DIR / "trades.duckdb"          # 交易记录(归因用)
DUCKDB_EVENTS  = DATA_DIR / "events.duckdb"          # 经济事件日历

# ═══════════════════════════════════════════
# SQLite — 运行时状态 (统一为 state.db)
# ═══════════════════════════════════════════
STATE_DB       = DATA_DIR / "state.db"               # 所有运行时状态
EXPERIMENTS_DB = DATA_DIR / "experiments.db"         # 实验记录(独立)

# 兼容旧路径 (逐步迁移后删除)
LEGACY_ANALYTICS_DB    = DATA_DIR / "analytics.db"
LEGACY_DECISION_LOG_DB = DATA_DIR / "decision_log.db"


# ═══════════════════════════════════════════
# SQLite 连接管理 (线程安全, WAL 模式)
# ═══════════════════════════════════════════

def _init_sqlite_db(db_path: Path, ddl: str) -> None:
    """初始化 SQLite 数据库，建表 (幂等)。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(ddl)
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════
# state.db 完整 DDL
# ═══════════════════════════════════════════

STATE_DB_DDL = """
-- 策略表现 (原 analytics.db)
CREATE TABLE IF NOT EXISTS strategy_perf (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_name TEXT NOT NULL,
    symbol TEXT, timeframe TEXT,
    total_pnl REAL DEFAULT 0.0,
    total_trades INTEGER DEFAULT 0,
    win_rate REAL DEFAULT 0.0,
    sharpe REAL DEFAULT 0.0,
    max_drawdown REAL DEFAULT 0.0,
    meta_json TEXT DEFAULT '{}',
    updated_at REAL
);

-- 决策日志 (原 decision_log.db)
CREATE TABLE IF NOT EXISTS decision_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER, ts REAL, bar_date TEXT,
    decision_type TEXT NOT NULL,
    strategy TEXT, regime TEXT,
    direction INTEGER,
    confidence REAL, factor_scores TEXT,
    decision TEXT,
    meta TEXT DEFAULT '{}',
    created_at REAL
);

-- 金丝雀状态
CREATE TABLE IF NOT EXISTS canary_state (
    factor_name TEXT PRIMARY KEY,
    stage TEXT NOT NULL DEFAULT 'SHADOW',
    oos_bars INTEGER DEFAULT 0,
    cumulative_pnl REAL DEFAULT 0.0,
    promote_time REAL DEFAULT 0.0,
    events_json TEXT DEFAULT '[]',
    updated_at REAL DEFAULT 0.0
);

-- 影子因子虚拟交易绩效
CREATE TABLE IF NOT EXISTS shadow_factor_perf (
    factor TEXT PRIMARY KEY,
    source TEXT DEFAULT 'shadow',
    symbol TEXT DEFAULT '',
    timeframe TEXT DEFAULT '',
    oos_bars INTEGER DEFAULT 0,
    cumulative_pnl REAL DEFAULT 0.0,
    hit_rate REAL DEFAULT 0.0,
    max_drawdown REAL DEFAULT 0.0,
    last_signal REAL DEFAULT 0.0,
    metrics_json TEXT DEFAULT '{}',
    updated_at REAL DEFAULT 0.0
);

-- 影子因子逐 bar 虚拟交易明细 (按需抽样写入)
CREATE TABLE IF NOT EXISTS shadow_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    factor TEXT NOT NULL,
    symbol TEXT DEFAULT '',
    timeframe TEXT DEFAULT '',
    ts REAL,
    signal REAL DEFAULT 0.0,
    position INTEGER DEFAULT 0,
    pnl REAL DEFAULT 0.0,
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_shadow_trades_factor_ts ON shadow_trades(factor, ts);
-- 因子生命周期事件 (原 factor_lifecycle_log.jsonl)
CREATE TABLE IF NOT EXISTS lifecycle_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    event TEXT NOT NULL,
    factor TEXT NOT NULL,
    source TEXT DEFAULT '',
    description TEXT DEFAULT '',
    score REAL DEFAULT 0.0,
    status TEXT DEFAULT '',
    reason TEXT DEFAULT ''
);

-- 权重历史 (原 factor_weight_history.jsonl)
CREATE TABLE IF NOT EXISTS weight_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    factor TEXT NOT NULL,
    old_weight REAL, new_weight REAL,
    reason TEXT DEFAULT ''
);

-- 归因快照 (原 factor_attribution.json)
CREATE TABLE IF NOT EXISTS attribution_snapshot (
    factor TEXT PRIMARY KEY,
    data_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);

-- 因子健康报告 (原 factor_health_report.json)
CREATE TABLE IF NOT EXISTS factor_health (
    factor TEXT PRIMARY KEY,
    score REAL DEFAULT 50.0,
    status TEXT DEFAULT 'UNKNOWN',
    section TEXT DEFAULT 'unknown',
    components_json TEXT DEFAULT '{}',
    n_obs INTEGER DEFAULT 0,
    rolling_ic REAL DEFAULT 0.0,
    updated_at REAL
);

-- 进化事件 (原 evolution_story.jsonl)
CREATE TABLE IF NOT EXISTS evolution_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);

-- 任务状态 (原 jobs.jsonl)
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    params_json TEXT DEFAULT '{}',
    result_json TEXT DEFAULT '{}',
    progress REAL DEFAULT 0.0,
    error TEXT DEFAULT '',
    created_at REAL,
    updated_at REAL
);

-- 参数调优 (原 param_tune_state.json)
CREATE TABLE IF NOT EXISTS param_tune (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at REAL
);

-- 校准器 (原 calibrator_bucket.json)
CREATE TABLE IF NOT EXISTS calibrator (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    data_json TEXT NOT NULL,
    updated_at REAL
);

-- 同步健康 (原 sync_health.json)
CREATE TABLE IF NOT EXISTS sync_health (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status_json TEXT NOT NULL DEFAULT '{}',
    updated_at REAL
);

CREATE INDEX IF NOT EXISTS idx_decision_log_ts ON decision_log(ts);
CREATE INDEX IF NOT EXISTS idx_decision_log_type ON decision_log(decision_type);
CREATE INDEX IF NOT EXISTS idx_lifecycle_events_factor ON lifecycle_events(factor);
CREATE INDEX IF NOT EXISTS idx_lifecycle_events_event ON lifecycle_events(event);
CREATE INDEX IF NOT EXISTS idx_evolution_events_type ON evolution_events(event_type);
CREATE INDEX IF NOT EXISTS idx_weight_history_factor ON weight_history(factor);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_shadow_factor_perf_updated ON shadow_factor_perf(updated_at);
"""


def init_state_db() -> None:
    """初始化 state.db (幂等, 启动时调用)."""
    _init_sqlite_db(STATE_DB, STATE_DB_DDL)


def get_state_conn() -> sqlite3.Connection:
    """获取 state.db 连接 (每次新建, 调用方负责关闭)."""
    conn = sqlite3.connect(str(STATE_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ═══════════════════════════════════════════
# experiments.db DDL
# ═══════════════════════════════════════════

EXPERIMENTS_DB_DDL = """
CREATE TABLE IF NOT EXISTS experiments (
    run_id TEXT PRIMARY KEY,
    experiment_type TEXT,
    params_json TEXT DEFAULT '{}',
    metrics_json TEXT DEFAULT '{}',
    tags_json TEXT DEFAULT '[]',
    artifacts_json TEXT DEFAULT '[]',
    status TEXT DEFAULT 'running',
    timestamp REAL,
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_experiments_type ON experiments(experiment_type);
CREATE INDEX IF NOT EXISTS idx_experiments_status ON experiments(status);
"""


def init_experiments_db() -> None:
    """初始化 experiments.db (幂等)."""
    _init_sqlite_db(EXPERIMENTS_DB, EXPERIMENTS_DB_DDL)


# ═══════════════════════════════════════════
# 启动初始化
# ═══════════════════════════════════════════
_init_lock = threading.Lock()
_initialized = False


def init_all() -> None:
    """应用启动时调用一次，初始化所有数据库。"""
    global _initialized
    with _init_lock:
        if _initialized:
            return
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        init_state_db()
        init_experiments_db()
        _initialized = True
