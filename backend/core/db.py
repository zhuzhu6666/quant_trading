"""backend/core/db.py — 统一数据库路径常量 + 连接管理。

所有数据库路径集中定义，不再硬编码。
DuckDB 保留时序数据，SQLite 收纳运行时状态。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Final

import duckdb

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

_SQLITE_EXTS: Final[set[str]] = {".db", ".sqlite", ".sqlite3"}
_DUCKDB_EXTS: Final[set[str]] = {".duckdb"}
_KNOWN_DUCKDB_PATHS: Final[set[Path]] = {
    DUCKDB_BARS.resolve(),
    DUCKDB_TICKS.resolve(),
    DUCKDB_L2.resolve(),
    DUCKDB_TRADES.resolve(),
    DUCKDB_EVENTS.resolve(),
}
_KNOWN_SQLITE_PATHS: Final[set[Path]] = {
    STATE_DB.resolve(),
    EXPERIMENTS_DB.resolve(),
    LEGACY_ANALYTICS_DB.resolve(),
    LEGACY_DECISION_LOG_DB.resolve(),
}


# ═══════════════════════════════════════════
# SQLite 连接管理 (线程安全, WAL 模式)
# ═══════════════════════════════════════════

def _init_sqlite_db(db_path: Path, ddl: str) -> None:
    """初始化 SQLite 数据库，建表 (幂等)。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect_sqlite(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(ddl)
    conn.commit()
    conn.close()


def _normalize_db_path(db_path: str | Path) -> Path:
    path = Path(db_path).expanduser()
    return path.resolve() if path.is_absolute() else path


def is_duckdb_path(db_path: str | Path) -> bool:
    path = _normalize_db_path(db_path)
    return path.suffix.lower() in _DUCKDB_EXTS or path.resolve() in _KNOWN_DUCKDB_PATHS


def is_sqlite_path(db_path: str | Path) -> bool:
    path = _normalize_db_path(db_path)
    return path.suffix.lower() in _SQLITE_EXTS or path.resolve() in _KNOWN_SQLITE_PATHS


def connect_sqlite(db_path: str | Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open a SQLite connection and reject DuckDB files early."""
    path = _normalize_db_path(db_path)
    if is_duckdb_path(path):
        raise ValueError(f"Refusing to open DuckDB file with sqlite3: {path}")
    if read_only:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    return sqlite3.connect(str(path))


def connect_duckdb(db_path: str | Path, *, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Open a DuckDB connection and reject SQLite files early."""
    path = _normalize_db_path(db_path)
    if is_sqlite_path(path):
        raise ValueError(f"Refusing to open SQLite file with DuckDB: {path}")
    if path.suffix.lower() not in _DUCKDB_EXTS and path.resolve() not in _KNOWN_DUCKDB_PATHS:
        raise ValueError(f"Unknown DuckDB target path: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(path), read_only=read_only)


def ensure_sqlite_columns(db_path: str | Path, table: str, columns: dict[str, str]) -> None:
    """Best-effort SQLite column migrations for long-lived local files."""
    conn = connect_sqlite(db_path)
    try:
        existing = {
            str(row[1])
            for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        }
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(f'ALTER TABLE "{table}" ADD COLUMN {ddl}')
        conn.commit()
    finally:
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

-- 结构化决策账本
CREATE TABLE IF NOT EXISTS decision_ledger (
    decision_id TEXT PRIMARY KEY,
    trade_id TEXT DEFAULT '',
    position_id TEXT DEFAULT '',
    event_type TEXT NOT NULL,
    symbol TEXT DEFAULT '',
    timeframe TEXT DEFAULT '',
    decision_ts REAL NOT NULL DEFAULT 0.0,
    regime_id TEXT DEFAULT '',
    regime_confidence REAL DEFAULT 0.0,
    portfolio_state_json TEXT DEFAULT '{}',
    risk_state_json TEXT DEFAULT '{}',
    policy_version TEXT DEFAULT '',
    factor_set_version TEXT DEFAULT '',
    action_score REAL DEFAULT 0.0,
    action_reason TEXT DEFAULT '',
    action_json TEXT DEFAULT '{}',
    created_at REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS decision_factor_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL,
    factor TEXT NOT NULL,
    source TEXT DEFAULT 'registry',
    raw_value REAL DEFAULT 0.0,
    normalized_value REAL DEFAULT 0.0,
    direction REAL DEFAULT 0.0,
    base_weight REAL DEFAULT 0.0,
    policy_weight REAL DEFAULT 0.0,
    shadow_score REAL DEFAULT 0.0,
    health_score REAL DEFAULT 0.0,
    gated INTEGER DEFAULT 0,
    gated_reason TEXT DEFAULT '',
    contribution_score REAL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS order_lifecycle_event (
    event_id TEXT PRIMARY KEY,
    decision_id TEXT DEFAULT '',
    trade_id TEXT DEFAULT '',
    order_id TEXT DEFAULT '',
    broker_order_id TEXT DEFAULT '',
    event_type TEXT NOT NULL,
    event_ts REAL NOT NULL DEFAULT 0.0,
    price REAL DEFAULT 0.0,
    volume REAL DEFAULT 0.0,
    status TEXT DEFAULT '',
    details_json TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS position_lifecycle_event (
    event_id TEXT PRIMARY KEY,
    position_id TEXT NOT NULL,
    trade_id TEXT DEFAULT '',
    symbol TEXT DEFAULT '',
    event_type TEXT NOT NULL,
    event_ts REAL NOT NULL DEFAULT 0.0,
    net_volume REAL DEFAULT 0.0,
    avg_price REAL DEFAULT 0.0,
    unrealized_pnl REAL DEFAULT 0.0,
    realized_pnl REAL DEFAULT 0.0,
    details_json TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS trade_outcome_review (
    review_id TEXT PRIMARY KEY,
    trade_id TEXT DEFAULT '',
    position_id TEXT DEFAULT '',
    entry_decision_id TEXT DEFAULT '',
    exit_decision_id TEXT DEFAULT '',
    entry_quality REAL DEFAULT 0.0,
    hold_quality REAL DEFAULT 0.0,
    exit_quality REAL DEFAULT 0.0,
    regime_fit_score REAL DEFAULT 0.0,
    execution_quality REAL DEFAULT 0.0,
    pnl REAL DEFAULT 0.0,
    mae REAL DEFAULT 0.0,
    mfe REAL DEFAULT 0.0,
    outcome_label TEXT DEFAULT '',
    failure_tags_json TEXT DEFAULT '[]',
    summary_text TEXT DEFAULT '',
    review_json TEXT DEFAULT '{}',
    created_at REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS factor_contribution_review (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id TEXT NOT NULL,
    trade_id TEXT DEFAULT '',
    factor TEXT NOT NULL,
    entry_contribution REAL DEFAULT 0.0,
    hold_contribution REAL DEFAULT 0.0,
    exit_contribution REAL DEFAULT 0.0,
    net_contribution REAL DEFAULT 0.0,
    confidence REAL DEFAULT 0.0,
    notes TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS experience_memory (
    experience_id TEXT PRIMARY KEY,
    trade_id TEXT DEFAULT '',
    regime_id TEXT DEFAULT '',
    setup_hash TEXT DEFAULT '',
    decision_context_json TEXT DEFAULT '{}',
    outcome_label TEXT DEFAULT '',
    reward_score REAL DEFAULT 0.0,
    failure_tags_json TEXT DEFAULT '[]',
    recommended_action TEXT DEFAULT '',
    evidence_strength REAL DEFAULT 0.0,
    artifact_version TEXT DEFAULT 'v1',
    created_at REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS experience_pattern_stats (
    scope_type TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    sample_count INTEGER DEFAULT 0,
    win_count INTEGER DEFAULT 0,
    bad_loss_count INTEGER DEFAULT 0,
    avg_reward REAL DEFAULT 0.0,
    last_outcome_label TEXT DEFAULT '',
    recommended_action TEXT DEFAULT '',
    updated_at REAL NOT NULL DEFAULT 0.0,
    PRIMARY KEY (scope_type, scope_key)
);

CREATE TABLE IF NOT EXISTS policy_suggestion (
    suggestion_id TEXT PRIMARY KEY,
    scope_type TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    action TEXT NOT NULL,
    confidence REAL DEFAULT 0.0,
    reason TEXT DEFAULT '',
    evidence_json TEXT DEFAULT '{}',
    status TEXT DEFAULT 'proposed',
    reviewed_at REAL DEFAULT 0.0,
    review_note TEXT DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS learning_application_log (
    application_id TEXT PRIMARY KEY,
    cycle_ts REAL NOT NULL DEFAULT 0.0,
    scope_type TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    action TEXT NOT NULL,
    bias_multiplier REAL DEFAULT 1.0,
    old_weight REAL DEFAULT 0.0,
    new_weight REAL DEFAULT 0.0,
    suggestion_ids_json TEXT DEFAULT '[]',
    status TEXT DEFAULT 'applied',
    details_json TEXT DEFAULT '{}',
    created_at REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS learning_application_effect (
    application_id TEXT PRIMARY KEY,
    scope_type TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    action TEXT NOT NULL,
    status TEXT DEFAULT 'observing',
    observed_trade_count INTEGER DEFAULT 0,
    baseline_trade_count INTEGER DEFAULT 0,
    post_avg_reward REAL DEFAULT 0.0,
    baseline_avg_reward REAL DEFAULT 0.0,
    delta_avg_reward REAL DEFAULT 0.0,
    post_win_rate REAL DEFAULT 0.0,
    baseline_win_rate REAL DEFAULT 0.0,
    decision_json TEXT DEFAULT '{}',
    last_review_at REAL DEFAULT 0.0,
    updated_at REAL NOT NULL DEFAULT 0.0,
    created_at REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS parameter_template_registry (
    template_id TEXT PRIMARY KEY,
    factor_id TEXT NOT NULL,
    regime_key TEXT DEFAULT '',
    template_version TEXT NOT NULL,
    template_role TEXT DEFAULT 'default',
    factor_family TEXT DEFAULT '',
    formula_version TEXT DEFAULT '',
    base_parameter_version TEXT DEFAULT '',
    parameters_json TEXT DEFAULT '{}',
    applicable_regimes_json TEXT DEFAULT '[]',
    avoid_regimes_json TEXT DEFAULT '[]',
    holding_profile_hint_json TEXT DEFAULT '{}',
    evidence_json TEXT DEFAULT '{}',
    source TEXT DEFAULT 'derived',
    active INTEGER DEFAULT 0,
    created_at REAL NOT NULL DEFAULT 0.0,
    updated_at REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS parameter_template_active (
    factor_id TEXT NOT NULL,
    regime_key TEXT DEFAULT '',
    template_id TEXT NOT NULL,
    template_version TEXT NOT NULL,
    status TEXT DEFAULT 'active',
    suggestion_id TEXT DEFAULT '',
    context_json TEXT DEFAULT '{}',
    activated_at REAL NOT NULL DEFAULT 0.0,
    updated_at REAL NOT NULL DEFAULT 0.0,
    PRIMARY KEY (factor_id, regime_key)
);

CREATE TABLE IF NOT EXISTS parameter_template_switch_log (
    switch_id TEXT PRIMARY KEY,
    factor_id TEXT NOT NULL,
    regime_key TEXT DEFAULT '',
    old_template_id TEXT DEFAULT '',
    new_template_id TEXT NOT NULL,
    suggestion_id TEXT DEFAULT '',
    risk_verdict_json TEXT DEFAULT '{}',
    context_json TEXT DEFAULT '{}',
    status TEXT DEFAULT 'applied',
    created_at REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS parameter_template_release_candidate (
    candidate_id TEXT PRIMARY KEY,
    factor_id TEXT NOT NULL,
    template_id TEXT NOT NULL,
    regime_key TEXT DEFAULT '',
    status TEXT DEFAULT 'pending_review',
    boundary_json TEXT DEFAULT '{}',
    validation_summary_json TEXT DEFAULT '{}',
    validation_report_path TEXT DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0.0,
    updated_at REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS runtime_kv (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL DEFAULT '{}',
    updated_at REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS recovery_position_state (
    position_id INTEGER PRIMARY KEY,
    broker TEXT DEFAULT 'ctrader',
    symbol TEXT DEFAULT '',
    direction INTEGER DEFAULT 0,
    open_price REAL DEFAULT 0.0,
    volume REAL DEFAULT 0.0,
    first_seen_at REAL DEFAULT 0.0,
    last_seen_at REAL DEFAULT 0.0,
    status TEXT DEFAULT 'open',
    strategy_name TEXT DEFAULT '',
    entry_decision_id TEXT DEFAULT '',
    context_integrity TEXT DEFAULT 'full',
    recovery_meta_json TEXT DEFAULT '{}',
    closed_at REAL DEFAULT 0.0,
    close_reason TEXT DEFAULT '',
    close_pnl REAL DEFAULT 0.0
);

CREATE INDEX IF NOT EXISTS idx_decision_log_ts ON decision_log(ts);
CREATE INDEX IF NOT EXISTS idx_decision_log_type ON decision_log(decision_type);
CREATE INDEX IF NOT EXISTS idx_lifecycle_events_factor ON lifecycle_events(factor);
CREATE INDEX IF NOT EXISTS idx_lifecycle_events_event ON lifecycle_events(event);
CREATE INDEX IF NOT EXISTS idx_evolution_events_type ON evolution_events(event_type);
CREATE INDEX IF NOT EXISTS idx_weight_history_factor ON weight_history(factor);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_shadow_factor_perf_updated ON shadow_factor_perf(updated_at);
CREATE INDEX IF NOT EXISTS idx_decision_ledger_ts ON decision_ledger(decision_ts);
CREATE INDEX IF NOT EXISTS idx_decision_ledger_pos_event ON decision_ledger(position_id, event_type);
CREATE INDEX IF NOT EXISTS idx_decision_factor_snapshot_decision ON decision_factor_snapshot(decision_id);
CREATE INDEX IF NOT EXISTS idx_order_lifecycle_trade ON order_lifecycle_event(trade_id, event_ts);
CREATE INDEX IF NOT EXISTS idx_position_lifecycle_pos ON position_lifecycle_event(position_id, event_ts);
CREATE INDEX IF NOT EXISTS idx_trade_outcome_review_trade ON trade_outcome_review(trade_id);
CREATE INDEX IF NOT EXISTS idx_factor_contribution_review_trade ON factor_contribution_review(trade_id);
CREATE INDEX IF NOT EXISTS idx_experience_memory_trade ON experience_memory(trade_id);
CREATE INDEX IF NOT EXISTS idx_experience_memory_regime ON experience_memory(regime_id, created_at);
CREATE INDEX IF NOT EXISTS idx_policy_suggestion_scope ON policy_suggestion(scope_type, scope_key, status);
CREATE INDEX IF NOT EXISTS idx_learning_application_scope ON learning_application_log(scope_type, scope_key, cycle_ts);
CREATE INDEX IF NOT EXISTS idx_learning_application_effect_scope ON learning_application_effect(scope_type, scope_key, status, updated_at);
CREATE INDEX IF NOT EXISTS idx_parameter_template_registry_factor ON parameter_template_registry(factor_id, regime_key, template_version, active);
CREATE INDEX IF NOT EXISTS idx_parameter_template_active_factor ON parameter_template_active(factor_id, regime_key, updated_at);
CREATE INDEX IF NOT EXISTS idx_parameter_template_switch_log_factor ON parameter_template_switch_log(factor_id, regime_key, created_at);
CREATE INDEX IF NOT EXISTS idx_parameter_template_release_candidate_factor ON parameter_template_release_candidate(factor_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_runtime_kv_updated ON runtime_kv(updated_at);
CREATE INDEX IF NOT EXISTS idx_recovery_position_status ON recovery_position_state(status, broker, last_seen_at);

-- cTrader 原始成交记录 (归因锚点)
CREATE TABLE IF NOT EXISTS ctrader_deals (
    deal_id     INTEGER PRIMARY KEY,
    position_id INTEGER NOT NULL,
    order_id    INTEGER DEFAULT 0,
    symbol_id   INTEGER DEFAULT 0,
    volume      INTEGER DEFAULT 0,
    filled_volume INTEGER DEFAULT 0,
    exec_price  REAL DEFAULT 0.0,
    trade_side  TEXT DEFAULT '',
    deal_status INTEGER DEFAULT 0,
    exec_timestamp REAL DEFAULT 0.0,
    commission   REAL DEFAULT 0.0,
    entry_price  REAL DEFAULT 0.0,
    gross_profit REAL DEFAULT 0.0,
    swap         REAL DEFAULT 0.0,
    close_commission REAL DEFAULT 0.0,
    balance      REAL DEFAULT 0.0,
    closed_volume INTEGER DEFAULT 0,
    is_close     INTEGER DEFAULT 0,
    fetched_at   REAL NOT NULL DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS idx_ctrader_deals_pos ON ctrader_deals(position_id);
CREATE INDEX IF NOT EXISTS idx_ctrader_deals_ts  ON ctrader_deals(exec_timestamp);
"""


def init_state_db() -> None:
    """初始化 state.db (幂等, 启动时调用)."""
    _init_sqlite_db(STATE_DB, STATE_DB_DDL)


def get_state_conn() -> sqlite3.Connection:
    """获取 state.db 连接 (每次新建, 调用方负责关闭)."""
    conn = connect_sqlite(STATE_DB)
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
    EXPERIMENTS_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = connect_sqlite(EXPERIMENTS_DB)
    try:
        conn.execute("""
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
            )
        """)
        existing = {
            str(row[1])
            for row in conn.execute('PRAGMA table_info("experiments")').fetchall()
        }
        for name, ddl in {
            "experiment_type": "experiment_type TEXT",
            "params_json": "params_json TEXT DEFAULT '{}'",
            "metrics_json": "metrics_json TEXT DEFAULT '{}'",
            "tags_json": "tags_json TEXT DEFAULT '[]'",
            "artifacts_json": "artifacts_json TEXT DEFAULT '[]'",
            "status": "status TEXT DEFAULT 'running'",
            "timestamp": "timestamp REAL",
            "created_at": "created_at REAL",
        }.items():
            if name not in existing:
                conn.execute(f'ALTER TABLE "experiments" ADD COLUMN {ddl}')
        conn.execute("CREATE INDEX IF NOT EXISTS idx_experiments_type ON experiments(experiment_type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_experiments_status ON experiments(status)")
        conn.commit()
    finally:
        conn.close()


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

