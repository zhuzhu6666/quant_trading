"""backend/core/db.py — 统一数据库路径常量 + 连接管理。

所有数据库路径集中定义，不再硬编码。
DuckDB 保留时序数据，PostgreSQL state_v1 承载运行时状态。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import time
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, Iterator

import duckdb

from backend.core.state_store import STATE_SCHEMA, connect_state_store
from backend.core.state_schema_migrations import require_state_schema_version

# ═══════════════════════════════════════════
# 项目根 (兼容 backend/ 和顶层导入)
# ═══════════════════════════════════════════
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = _PROJECT_ROOT / "data"

# ═══════════════════════════════════════════
# DuckDB — 时序/市场数据 (不可合并)
# ═══════════════════════════════════════════
DUCKDB_BARS_LEGACY = DATA_DIR / "ctrader_data.duckdb"   # 旧 K线 + 外部数据(COT/ETF)
DUCKDB_BARS_MONTHLY_DIR = DATA_DIR / "bars_monthly"
DUCKDB_BARS_CURRENT = DATA_DIR / "bars.duckdb"           # 当前月 K线兼容链接
DUCKDB_BARS    = DUCKDB_BARS_CURRENT                     # K线当前月入口
DUCKDB_EXTERNAL = DATA_DIR / "external_data.duckdb"      # COT/ETF/宏观等外部数据
DUCKDB_TRADES  = DATA_DIR / "trades.duckdb"          # 交易记录(归因用)
DUCKDB_EVENTS  = DATA_DIR / "events.duckdb"          # 经济事件日历

# ═══════════════════════════════════════════
# PostgreSQL — 运行时状态；STATE_DB 仅作为“默认 state store”哨兵路径
# ═══════════════════════════════════════════
STATE_DB       = DATA_DIR / "state.db"               # 运行态使用此哨兵路径切到 PostgreSQL
_DEFAULT_STATE_DB = STATE_DB.resolve()
EXPERIMENTS_DB = DATA_DIR / "experiments.db"         # 实验记录(独立)

# 兼容旧路径 (逐步迁移后删除)
LEGACY_ANALYTICS_DB    = DATA_DIR / "analytics.db"
LEGACY_DECISION_LOG_DB = DATA_DIR / "decision_log.db"

_SQLITE_EXTS: Final[set[str]] = {".db", ".sqlite", ".sqlite3"}
_DUCKDB_EXTS: Final[set[str]] = {".duckdb"}
_KNOWN_DUCKDB_PATHS: Final[set[Path]] = {
    DUCKDB_BARS.resolve(),
    DUCKDB_BARS_LEGACY.resolve(),
    DUCKDB_BARS_CURRENT.resolve(),
    DUCKDB_EXTERNAL.resolve(),
    DUCKDB_TRADES.resolve(),
    DUCKDB_EVENTS.resolve(),
}
_KNOWN_SQLITE_PATHS: Final[set[Path]] = {
    STATE_DB.resolve(),
    EXPERIMENTS_DB.resolve(),
    LEGACY_ANALYTICS_DB.resolve(),
    LEGACY_DECISION_LOG_DB.resolve(),
}


def _env_value(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value is not None:
        return value
    env_path = _PROJECT_ROOT / ".env"
    if not env_path.exists():
        return default
    try:
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            key, val = raw.split("=", 1)
            if key.strip() == name:
                return val.strip().strip('"').strip("'")
    except Exception:
        return default
    return default


def state_backend() -> str:
    return _env_value("QUANT_STATE_BACKEND", "postgres").strip().lower() or "postgres"


def state_pg_dsn() -> str:
    return _env_value("QUANT_STATE_PG_DSN") or _env_value("QUANT_AUDIT_PG_DSN")


def state_pg_enabled() -> bool:
    return state_backend() == "postgres" and bool(state_pg_dsn())


def is_state_db_path(db_path: str | Path) -> bool:
    return _normalize_db_path(db_path).resolve() == STATE_DB.resolve()


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


def _configure_sqlite_connection(conn: sqlite3.Connection, *, read_only: bool = False) -> sqlite3.Connection:
    conn.execute("PRAGMA busy_timeout=30000")
    if read_only:
        try:
            conn.execute("PRAGMA query_only=ON")
        except sqlite3.Error:
            pass
    else:
        try:
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:
            pass
    return conn


def connect_sqlite(db_path: str | Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open a SQLite connection and reject DuckDB files early."""
    path = _normalize_db_path(db_path)
    if path.resolve() == _DEFAULT_STATE_DB:
        raise RuntimeError("data/state.db has migrated to PostgreSQL; use get_state_pg_conn() for runtime state")
    if is_duckdb_path(path):
        raise ValueError(f"Refusing to open DuckDB file with sqlite3: {path}")
    if read_only:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30.0)
        return _configure_sqlite_connection(conn, read_only=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    return _configure_sqlite_connection(conn, read_only=False)


def connect_duckdb(db_path: str | Path, *, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Open a DuckDB connection and reject SQLite files early."""
    path = _normalize_db_path(db_path)
    if is_sqlite_path(path):
        raise ValueError(f"Refusing to open SQLite file with DuckDB: {path}")
    if path.suffix.lower() not in _DUCKDB_EXTS and path.resolve() not in _KNOWN_DUCKDB_PATHS:
        raise ValueError(f"Unknown DuckDB target path: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(path), read_only=read_only)


_DUCKDB_LOCK_MARKERS: Final[tuple[str, ...]] = (
    "Could not set lock",
    "Conflicting lock is held",
    "database is locked",
    "different configuration",
)


def is_duckdb_lock_error(exc: Exception | str) -> bool:
    """Return True for DuckDB single-writer/read-lock conflicts."""
    msg = str(exc)
    return any(marker in msg for marker in _DUCKDB_LOCK_MARKERS)


@contextmanager
def duckdb_readonly_connection(
    db_path: str | Path,
    *,
    snapshot_on_lock: bool = True,
    snapshot_first: bool = False,
) -> Iterator[duckdb.DuckDBPyConnection]:
    """Open DuckDB read-only, optionally falling back to a temporary snapshot on lock conflicts."""
    path = _normalize_db_path(db_path)
    tmp_dir: tempfile.TemporaryDirectory[str] | None = None
    conn: duckdb.DuckDBPyConnection | None = None
    source = path.resolve() if path.is_symlink() else path

    def _connect_snapshot() -> duckdb.DuckDBPyConnection:
        nonlocal tmp_dir
        tmp_dir = tempfile.TemporaryDirectory(prefix="duckdb_snapshot_")
        snapshot = Path(tmp_dir.name) / path.name
        shutil.copy2(source, snapshot)
        wal = source.with_suffix(source.suffix + ".wal")
        if wal.exists():
            shutil.copy2(wal, snapshot.with_suffix(snapshot.suffix + ".wal"))
        return connect_duckdb(snapshot, read_only=True)

    try:
        try:
            conn = _connect_snapshot() if snapshot_first else connect_duckdb(path, read_only=True)
        except Exception as exc:
            if not snapshot_on_lock or not is_duckdb_lock_error(exc):
                raise
            conn = _connect_snapshot()
        yield conn
    finally:
        if conn is not None:
            conn.close()
        if tmp_dir is not None:
            tmp_dir.cleanup()


BAR_TABLE_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS bars (
    symbol VARCHAR NOT NULL,
    timeframe VARCHAR NOT NULL,
    time BIGINT NOT NULL,
    open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
    volume DOUBLE DEFAULT 0,
    spread INTEGER DEFAULT 0,
    UNIQUE(symbol, timeframe, time)
)
"""


def ensure_bars_table(conn: duckdb.DuckDBPyConnection) -> None:
    """Create/upgrade the standard bars table in an opened DuckDB connection."""
    conn.execute(BAR_TABLE_DDL)
    try:
        conn.execute("ALTER TABLE bars ADD COLUMN IF NOT EXISTS spread INTEGER DEFAULT 0")
    except Exception:
        pass
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bars_sym_tf_time "
        "ON bars(symbol, timeframe, time)"
    )


def bars_month_key(ts: float | int | None = None) -> str:
    """Return YYYY_MM month key using UTC market timestamps."""
    value = time.time() if ts is None else float(ts)
    return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y_%m")


def bars_monthly_path(ts: float | int | None = None) -> Path:
    """Return monthly K-line DuckDB path for a UTC epoch timestamp."""
    return DUCKDB_BARS_MONTHLY_DIR / f"bars_{bars_month_key(ts)}.duckdb"


def refresh_current_bars_link(ts: float | int | None = None) -> Path:
    """Point data/bars.duckdb at the current month database and return target."""
    target = bars_monthly_path(ts)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        conn = connect_duckdb(target)
        try:
            ensure_bars_table(conn)
        finally:
            conn.close()

    link = DUCKDB_BARS_CURRENT
    if link.exists() or link.is_symlink():
        try:
            if link.is_symlink() and link.resolve() == target.resolve():
                return target
            link.unlink()
        except OSError:
            return target
    try:
        os.symlink(os.path.relpath(target, start=link.parent), link)
    except OSError:
        # Filesystems without symlink support can still use the target path directly.
        pass
    return target


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


def state_table_exists(conn, table: str) -> bool:
    """Return whether a runtime state table exists on SQLite or PostgreSQL."""
    if conn.__class__.__module__.split(".", 1)[0] == "psycopg":
        row = conn.execute(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = current_schema() AND table_name = %s
            LIMIT 1
            """,
            (table,),
        ).fetchone()
        return row is not None
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def state_table_columns(conn, table: str) -> set[str]:
    """Return runtime state table columns without exposing engine-specific metadata SQL."""
    if conn.__class__.__module__.split(".", 1)[0] == "psycopg":
        return {
            str(row["column_name"])
            for row in conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = current_schema() AND table_name = %s
                ORDER BY ordinal_position
                """,
                (table,),
            ).fetchall()
        }
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}


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
    rollback_count INTEGER DEFAULT 0,
    evidence_hash TEXT DEFAULT '',
    dataset_hash TEXT DEFAULT '',
    evidence_end_at TEXT DEFAULT '',
    stage_evidence_hash TEXT DEFAULT '',
    fresh_evidence_bars INTEGER DEFAULT 0,
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

CREATE TABLE IF NOT EXISTS supervisor_counterfactual_review (
    counterfactual_id TEXT PRIMARY KEY,
    review_id TEXT DEFAULT '',
    trade_id TEXT DEFAULT '',
    position_id TEXT NOT NULL,
    close_ts REAL NOT NULL DEFAULT 0.0,
    close_reason TEXT DEFAULT '',
    supervisor_event_type TEXT DEFAULT '',
    supervisor_reason TEXT DEFAULT '',
    label TEXT DEFAULT '',
    confidence REAL DEFAULT 0.0,
    horizons_json TEXT DEFAULT '[]',
    evidence_json TEXT DEFAULT '{}',
    created_at REAL NOT NULL DEFAULT 0.0,
    updated_at REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS nursery_exploration_reservation (
    reservation_id TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    reason TEXT NOT NULL,
    setup_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'reserved',
    expires_at REAL NOT NULL DEFAULT 0.0,
    created_at REAL NOT NULL DEFAULT 0.0,
    updated_at REAL NOT NULL DEFAULT 0.0,
    PRIMARY KEY (reservation_id, reason)
);

CREATE TABLE IF NOT EXISTS runtime_config_snapshot (
    config_version INTEGER PRIMARY KEY AUTOINCREMENT,
    config_hash TEXT NOT NULL,
    source TEXT DEFAULT '',
    config_json TEXT NOT NULL DEFAULT '{}',
    run_id TEXT DEFAULT '',
    mutation_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS runtime_config_overlay (
    overlay_id TEXT PRIMARY KEY,
    overlay_json TEXT NOT NULL DEFAULT '{}',
    overlay_hash TEXT DEFAULT '',
    source TEXT DEFAULT '',
    run_id TEXT DEFAULT '',
    mutation_id TEXT NOT NULL DEFAULT '',
    legacy_authority_json TEXT NOT NULL DEFAULT '{}',
    updated_at REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS factor_catalog_snapshot (
    snapshot_id TEXT PRIMARY KEY,
    run_id TEXT DEFAULT '',
    catalog_hash TEXT DEFAULT '',
    catalog_json TEXT NOT NULL DEFAULT '[]',
    source TEXT DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS evolution_run (
    run_id TEXT PRIMARY KEY,
    run_type TEXT NOT NULL,
    trigger_source TEXT DEFAULT '',
    status TEXT DEFAULT 'running',
    config_version INTEGER DEFAULT 0,
    config_hash TEXT DEFAULT '',
    summary_json TEXT DEFAULT '{}',
    started_at REAL NOT NULL DEFAULT 0.0,
    ended_at REAL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS evolution_decision (
    decision_id TEXT PRIMARY KEY,
    run_id TEXT DEFAULT '',
    decision_type TEXT NOT NULL,
    scope_type TEXT DEFAULT '',
    scope_key TEXT DEFAULT '',
    action TEXT DEFAULT '',
    status TEXT DEFAULT '',
    evidence_json TEXT DEFAULT '{}',
    risk_verdict_json TEXT DEFAULT '{}',
    before_json TEXT DEFAULT '{}',
    after_json TEXT DEFAULT '{}',
    result_json TEXT DEFAULT '{}',
    rollback_json TEXT DEFAULT '{}',
    config_version INTEGER DEFAULT 0,
    config_hash TEXT DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS replay_report (
    replay_run_id TEXT PRIMARY KEY,
    scope_json TEXT NOT NULL DEFAULT '{}',
    input_dataset_hash TEXT DEFAULT '',
    runtime_config_hash TEXT DEFAULT '',
    code_version TEXT DEFAULT '',
    decision_count INTEGER DEFAULT 0,
    matched_live_count INTEGER DEFAULT 0,
    mismatch_count INTEGER DEFAULT 0,
    metric_summary_json TEXT NOT NULL DEFAULT '{}',
    replay_error TEXT DEFAULT '',
    evidence_grade TEXT DEFAULT '',
    artifact_path TEXT DEFAULT '',
    artifact_hash TEXT DEFAULT '',
    status TEXT DEFAULT 'completed',
    created_at REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS autonomy_health_snapshot (
    snapshot_id TEXT PRIMARY KEY,
    score REAL NOT NULL DEFAULT 0.0,
    posture TEXT DEFAULT '',
    blockers_json TEXT NOT NULL DEFAULT '[]',
    metrics_json TEXT NOT NULL DEFAULT '{}',
    trend_json TEXT NOT NULL DEFAULT '{}',
    source TEXT DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS autonomy_scope_approval_event (
    event_id TEXT PRIMARY KEY,
    snapshot_id TEXT DEFAULT '',
    posture TEXT DEFAULT '',
    recommendation_json TEXT NOT NULL DEFAULT '{}',
    actor TEXT DEFAULT '',
    decision TEXT DEFAULT '',
    reason TEXT DEFAULT '',
    boundary_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS autonomy_scope_enforcement_event (
    event_id TEXT PRIMARY KEY,
    snapshot_id TEXT DEFAULT '',
    posture TEXT DEFAULT '',
    recommendation_json TEXT NOT NULL DEFAULT '{}',
    current_mode TEXT DEFAULT '',
    target_mode TEXT DEFAULT '',
    status TEXT DEFAULT '',
    risk_verdict_json TEXT NOT NULL DEFAULT '{}',
    mutation_json TEXT NOT NULL DEFAULT '{}',
    actor TEXT DEFAULT '',
    reason TEXT DEFAULT '',
    boundary_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS release_run (
    run_id TEXT PRIMARY KEY,
    release_class TEXT DEFAULT '',
    status TEXT DEFAULT 'started',
    summary_json TEXT NOT NULL DEFAULT '{}',
    checklist_json TEXT NOT NULL DEFAULT '{}',
    runtime_config_hash TEXT DEFAULT '',
    replay_run_id TEXT DEFAULT '',
    replay_artifact_hash TEXT DEFAULT '',
    incident_mode TEXT DEFAULT '',
    readiness_posture TEXT DEFAULT '',
    tests_json TEXT NOT NULL DEFAULT '[]',
    rollback_ref_json TEXT NOT NULL DEFAULT '{}',
    created_by TEXT DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0.0,
    updated_at REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS release_approval_event (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    action TEXT DEFAULT '',
    actor TEXT DEFAULT '',
    decision TEXT DEFAULT '',
    reason TEXT DEFAULT '',
    evidence_refs_json TEXT NOT NULL DEFAULT '{}',
    boundary_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS incident_playbook_run (
    playbook_id TEXT PRIMARY KEY,
    scenario TEXT DEFAULT '',
    severity TEXT DEFAULT '',
    current_mode TEXT DEFAULT '',
    target_mode TEXT DEFAULT '',
    status TEXT DEFAULT '',
    steps_json TEXT NOT NULL DEFAULT '[]',
    risk_precheck_json TEXT NOT NULL DEFAULT '{}',
    release_ref_json TEXT NOT NULL DEFAULT '{}',
    boundary_json TEXT NOT NULL DEFAULT '{}',
    created_by TEXT DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS incident_playbook_event (
    event_id TEXT PRIMARY KEY,
    playbook_id TEXT NOT NULL,
    event_type TEXT DEFAULT '',
    actor TEXT DEFAULT '',
    status TEXT DEFAULT '',
    evidence_refs_json TEXT NOT NULL DEFAULT '{}',
    notes TEXT DEFAULT '',
    boundary_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS brain_state_snapshot (
    snapshot_id TEXT PRIMARY KEY,
    schema_version TEXT DEFAULT 'brain_state_snapshot.v1',
    source TEXT DEFAULT '',
    status TEXT DEFAULT 'computed',
    world_model_json TEXT NOT NULL DEFAULT '{}',
    perceptions_json TEXT NOT NULL DEFAULT '{}',
    memory_json TEXT NOT NULL DEFAULT '{}',
    hypotheses_json TEXT NOT NULL DEFAULT '[]',
    critic_json TEXT NOT NULL DEFAULT '{}',
    evidence_refs_json TEXT NOT NULL DEFAULT '{}',
    boundary_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS brain_memory (
    memory_id TEXT PRIMARY KEY,
    memory_type TEXT DEFAULT '',
    source_table TEXT DEFAULT '',
    source_id TEXT DEFAULT '',
    symbol TEXT DEFAULT '',
    timeframe TEXT DEFAULT '',
    regime TEXT DEFAULT '',
    text_summary TEXT DEFAULT '',
    structured_json TEXT NOT NULL DEFAULT '{}',
    evidence_score REAL NOT NULL DEFAULT 0.0,
    similarity_score REAL NOT NULL DEFAULT 0.0,
    polarity TEXT DEFAULT 'neutral',
    created_at REAL NOT NULL DEFAULT 0.0,
    last_used_at REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS brain_action_plan (
    plan_id TEXT PRIMARY KEY,
    snapshot_id TEXT DEFAULT '',
    hypothesis_id TEXT DEFAULT '',
    action_type TEXT DEFAULT '',
    status TEXT DEFAULT 'shadow_recorded',
    scope_json TEXT NOT NULL DEFAULT '{}',
    max_impact TEXT DEFAULT 'none_shadow_only',
    risk_class TEXT DEFAULT '',
    critic_verdict TEXT DEFAULT '',
    validation_refs_json TEXT NOT NULL DEFAULT '{}',
    rollback_plan_json TEXT NOT NULL DEFAULT '{}',
    required_services_json TEXT NOT NULL DEFAULT '[]',
    shadow_eval_json TEXT NOT NULL DEFAULT '{}',
    boundary_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS brain_action_plan_eval (
    eval_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    snapshot_id TEXT DEFAULT '',
    action_type TEXT DEFAULT '',
    scope_type TEXT DEFAULT '',
    status TEXT DEFAULT 'needs_evidence',
    comparison_verdict TEXT DEFAULT 'needs_more_evidence',
    coverage_score REAL NOT NULL DEFAULT 0.0,
    comparison_json TEXT NOT NULL DEFAULT '{}',
    evidence_refs_json TEXT NOT NULL DEFAULT '{}',
    boundary_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS brain_low_impact_execution (
    execution_id TEXT PRIMARY KEY,
    plan_id TEXT DEFAULT '',
    eval_id TEXT DEFAULT '',
    action_type TEXT DEFAULT '',
    execution_action TEXT DEFAULT '',
    status TEXT DEFAULT '',
    evidence_score REAL NOT NULL DEFAULT 0.0,
    critic_verdict TEXT DEFAULT '',
    comparison_verdict TEXT DEFAULT '',
    risk_verdict_json TEXT NOT NULL DEFAULT '{}',
    rollback_plan_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    posterior_monitor_json TEXT NOT NULL DEFAULT '{}',
    boundary_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL DEFAULT 0.0,
    updated_at REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS brain_medium_impact_governance (
    governance_id TEXT PRIMARY KEY,
    plan_id TEXT DEFAULT '',
    eval_id TEXT DEFAULT '',
    governance_action TEXT DEFAULT '',
    scope_type TEXT DEFAULT '',
    scope_key TEXT DEFAULT '',
    status TEXT DEFAULT '',
    candidate_id TEXT DEFAULT '',
    suggestion_id TEXT DEFAULT '',
    evidence_score REAL NOT NULL DEFAULT 0.0,
    critic_verdict TEXT DEFAULT '',
    comparison_verdict TEXT DEFAULT '',
    risk_verdict_json TEXT NOT NULL DEFAULT '{}',
    decision_policy_json TEXT NOT NULL DEFAULT '{}',
    rollback_plan_json TEXT NOT NULL DEFAULT '{}',
    posterior_refs_json TEXT NOT NULL DEFAULT '{}',
    autonomy_guard_json TEXT NOT NULL DEFAULT '{}',
    boundary_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL DEFAULT 0.0,
    updated_at REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS brain_governance_candidate (
    candidate_id TEXT PRIMARY KEY,
    source_agent TEXT DEFAULT '',
    source_kind TEXT DEFAULT '',
    source_ref_type TEXT DEFAULT '',
    source_ref_id TEXT DEFAULT '',
    proposal_stage TEXT DEFAULT 'brain_candidate',
    capability_scope TEXT DEFAULT '',
    scope_type TEXT DEFAULT '',
    scope_key TEXT DEFAULT '',
    action TEXT DEFAULT '',
    confidence REAL DEFAULT 0.0,
    evidence_score REAL DEFAULT 0.0,
    risk_class TEXT DEFAULT '',
    max_impact TEXT DEFAULT '',
    expected_effect_json TEXT NOT NULL DEFAULT '{}',
    evidence_refs_json TEXT NOT NULL DEFAULT '{}',
    counter_evidence_refs_json TEXT NOT NULL DEFAULT '{}',
    risk_verdict_json TEXT NOT NULL DEFAULT '{}',
    decision_policy_json TEXT NOT NULL DEFAULT '{}',
    rollback_plan_json TEXT NOT NULL DEFAULT '{}',
    lineage_json TEXT NOT NULL DEFAULT '{}',
    status TEXT DEFAULT 'active',
    submitted_suggestion_id TEXT DEFAULT '',
    submitted_at REAL DEFAULT 0.0,
    expires_at REAL DEFAULT 0.0,
    created_at REAL NOT NULL DEFAULT 0.0,
    updated_at REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS brain_governance_candidate_review (
    review_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    review_status TEXT DEFAULT '',
    bridge_ready INTEGER DEFAULT 0,
    bridge_reason TEXT DEFAULT '',
    evidence_gaps_json TEXT NOT NULL DEFAULT '[]',
    conflict_json TEXT NOT NULL DEFAULT '{}',
    bridge_preview_json TEXT NOT NULL DEFAULT '{}',
    source_reliability_json TEXT NOT NULL DEFAULT '{}',
    llm_advisory_json TEXT NOT NULL DEFAULT '{}',
    boundary_json TEXT NOT NULL DEFAULT '{}',
    evidence_fingerprint TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS v16_brain_command (
    command_id TEXT PRIMARY KEY,
    snapshot_id TEXT DEFAULT '',
    plan_id TEXT DEFAULT '',
    eval_id TEXT DEFAULT '',
    candidate_id TEXT DEFAULT '',
    target_agent TEXT DEFAULT '',
    scope_type TEXT DEFAULT '',
    scope_key TEXT DEFAULT '',
    action TEXT DEFAULT '',
    decision TEXT DEFAULT '',
    status TEXT DEFAULT '',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    delegation_json TEXT NOT NULL DEFAULT '{}',
    claim_status TEXT NOT NULL DEFAULT 'available',
    claim_token TEXT DEFAULT '',
    claimed_at REAL NOT NULL DEFAULT 0.0,
    claim_expires_at REAL NOT NULL DEFAULT 0.0,
    apply_count INTEGER NOT NULL DEFAULT 0,
    max_apply_count INTEGER NOT NULL DEFAULT 1,
    consumed_at REAL NOT NULL DEFAULT 0.0,
    consumed_mutation_id TEXT DEFAULT '',
    posterior_fingerprint TEXT DEFAULT '',
    evidence_fingerprint TEXT DEFAULT '',
    last_release_reason TEXT DEFAULT '',
    authority_issued_at REAL NOT NULL DEFAULT 0.0,
    created_at REAL NOT NULL DEFAULT 0.0,
    updated_at REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS learning_experiment_reservation (
    reservation_id TEXT PRIMARY KEY,
    scope_type TEXT NOT NULL DEFAULT '',
    scope_key TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'reserved',
    application_id TEXT NOT NULL DEFAULT '',
    mutation_id TEXT NOT NULL DEFAULT '',
    expires_at REAL NOT NULL DEFAULT 0.0,
    created_at REAL NOT NULL DEFAULT 0.0,
    updated_at REAL NOT NULL DEFAULT 0.0
);

CREATE INDEX IF NOT EXISTS idx_learning_experiment_reservation_status
    ON learning_experiment_reservation(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_learning_experiment_reservation_scope
    ON learning_experiment_reservation(scope_type, scope_key, status);

CREATE TABLE IF NOT EXISTS proposal_registry (
    proposal_id TEXT PRIMARY KEY,
    source_agent TEXT DEFAULT '',
    source_ref_type TEXT DEFAULT '',
    source_ref_id TEXT DEFAULT '',
    proposal_type TEXT DEFAULT '',
    proposal_action TEXT DEFAULT '',
    control_surface TEXT DEFAULT '',
    target_scope TEXT DEFAULT '',
    impact_level TEXT DEFAULT '',
    confidence REAL DEFAULT 0.0,
    evidence_refs_json TEXT NOT NULL DEFAULT '{}',
    counter_evidence_refs_json TEXT NOT NULL DEFAULT '{}',
    required_gate_json TEXT NOT NULL DEFAULT '[]',
    risk_verdict_json TEXT NOT NULL DEFAULT '{}',
    decision_policy_preview_json TEXT NOT NULL DEFAULT '{}',
    expected_effect_json TEXT NOT NULL DEFAULT '{}',
    rollback_plan_json TEXT NOT NULL DEFAULT '{}',
    source_reliability_json TEXT NOT NULL DEFAULT '{}',
    evidence_freshness_json TEXT NOT NULL DEFAULT '{}',
    status TEXT DEFAULT '',
    authority_state TEXT DEFAULT '',
    route_recommendation TEXT DEFAULT 'observe',
    conflict_json TEXT NOT NULL DEFAULT '{}',
    review_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL DEFAULT 0.0,
    updated_at REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS live_autonomy_unlock_event (
    event_id TEXT PRIMARY KEY,
    action TEXT DEFAULT '',
    status TEXT DEFAULT '',
    actor TEXT DEFAULT '',
    reason TEXT DEFAULT '',
    autonomy_mode_before TEXT DEFAULT '',
    autonomy_mode_after TEXT DEFAULT '',
    readiness_json TEXT NOT NULL DEFAULT '{}',
    proposal_registry_json TEXT NOT NULL DEFAULT '{}',
    risk_verdict_json TEXT NOT NULL DEFAULT '{}',
    blockers_json TEXT NOT NULL DEFAULT '[]',
    mutation_json TEXT NOT NULL DEFAULT '{}',
    boundary_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS brain_live_ready_guardrail (
    guardrail_id TEXT PRIMARY KEY,
    status TEXT DEFAULT '',
    live_capability_lock_json TEXT NOT NULL DEFAULT '{}',
    broker_local_divergence_json TEXT NOT NULL DEFAULT '{}',
    incident_control_json TEXT NOT NULL DEFAULT '{}',
    incident_memory_json TEXT NOT NULL DEFAULT '{}',
    release_rollback_json TEXT NOT NULL DEFAULT '{}',
    p3_p4_evidence_json TEXT NOT NULL DEFAULT '{}',
    action_recommendation_json TEXT NOT NULL DEFAULT '{}',
    risk_precheck_json TEXT NOT NULL DEFAULT '{}',
    boundary_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL DEFAULT 0.0,
    updated_at REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS position_supervisor_trace (
    trace_id TEXT PRIMARY KEY,
    decision_id TEXT DEFAULT '',
    position_id TEXT NOT NULL,
    trade_id TEXT DEFAULT '',
    symbol TEXT DEFAULT '',
    timeframe TEXT DEFAULT '',
    tick INTEGER DEFAULT 0,
    event_ts REAL NOT NULL DEFAULT 0.0,
    action TEXT DEFAULT '',
    summary_reason TEXT DEFAULT '',
    confidence REAL DEFAULT 0.0,
    template_id TEXT DEFAULT '',
    template_version TEXT DEFAULT '',
    stage TEXT DEFAULT '',
    outcome TEXT DEFAULT '',
    risk_action TEXT DEFAULT '',
    risk_allowed INTEGER DEFAULT 0,
    risk_reason TEXT DEFAULT '',
    execution_status TEXT DEFAULT '',
    execution_reason TEXT DEFAULT '',
    context_json TEXT DEFAULT '{}',
    verdict_json TEXT DEFAULT '{}',
    risk_verdict_json TEXT DEFAULT '{}',
    execution_json TEXT DEFAULT '{}',
    trace_integrity TEXT DEFAULT 'full',
    config_version INTEGER DEFAULT 0,
    config_hash TEXT DEFAULT '',
    evolution_run_id TEXT DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS idx_position_supervisor_trace_position_ts
ON position_supervisor_trace(position_id, event_ts);
CREATE INDEX IF NOT EXISTS idx_position_supervisor_trace_action_outcome
ON position_supervisor_trace(action, outcome, event_ts);

CREATE TABLE IF NOT EXISTS autonomous_learning_sample (
    sample_id TEXT PRIMARY KEY,
    sample_type TEXT NOT NULL,
    source_table TEXT DEFAULT '',
    source_id TEXT DEFAULT '',
    decision_id TEXT DEFAULT '',
    trade_id TEXT DEFAULT '',
    position_id TEXT DEFAULT '',
    symbol TEXT DEFAULT '',
    timeframe TEXT DEFAULT '',
    event_ts REAL NOT NULL DEFAULT 0.0,
    label_status TEXT DEFAULT 'pending',
    integrity TEXT DEFAULT 'full',
    train_weight REAL DEFAULT 1.0,
    features_json TEXT DEFAULT '{}',
    verdict_json TEXT DEFAULT '{}',
    label_json TEXT DEFAULT '{}',
    trace_json TEXT DEFAULT '{}',
    evidence_contract_json TEXT DEFAULT '{}',
    config_version INTEGER DEFAULT 0,
    config_hash TEXT DEFAULT '',
    evolution_run_id TEXT DEFAULT '',
    system_contaminated INTEGER NOT NULL DEFAULT 0,
    governance_eligible INTEGER NOT NULL DEFAULT 0,
    governance_effective_weight REAL NOT NULL DEFAULT 0.0,
    governance_eligibility_version TEXT NOT NULL DEFAULT '',
    governance_eligibility_fingerprint TEXT NOT NULL DEFAULT '',
    governance_ineligible_reason TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0.0,
    updated_at REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS model_permission_audit (
    audit_id TEXT PRIMARY KEY,
    model_type TEXT DEFAULT '',
    artifact_path TEXT DEFAULT '',
    status TEXT DEFAULT '',
    reason TEXT DEFAULT '',
    capabilities_json TEXT DEFAULT '{}',
    violations_json TEXT DEFAULT '[]',
    context_json TEXT DEFAULT '{}',
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
    source_table TEXT DEFAULT '',
    source_id TEXT DEFAULT '',
    append_source TEXT DEFAULT '',
    regime_id TEXT DEFAULT '',
    setup_hash TEXT DEFAULT '',
    decision_context_json TEXT DEFAULT '{}',
    outcome_label TEXT DEFAULT '',
    reward_score REAL DEFAULT 0.0,
    failure_tags_json TEXT DEFAULT '[]',
    recommended_action TEXT DEFAULT '',
    evidence_strength REAL DEFAULT 0.0,
    artifact_version TEXT DEFAULT 'v1',
    evolution_run_id TEXT DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS experience_pattern_stats (
    scope_type TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    sample_count INTEGER DEFAULT 0,
    win_count INTEGER DEFAULT 0,
    bad_loss_count INTEGER DEFAULT 0,
    avg_reward REAL DEFAULT 0.0,
    effective_sample_count REAL NOT NULL DEFAULT 0.0,
    weighted_win_count REAL NOT NULL DEFAULT 0.0,
    weighted_bad_loss_count REAL NOT NULL DEFAULT 0.0,
    weighted_avg_reward REAL NOT NULL DEFAULT 0.0,
    governance_eligibility_version TEXT NOT NULL DEFAULT '',
    governance_eligibility_fingerprint TEXT NOT NULL DEFAULT '',
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
    applied_mutation_id TEXT NOT NULL DEFAULT '',
    governance_eligible INTEGER NOT NULL DEFAULT 0,
    governance_eligibility_version TEXT NOT NULL DEFAULT '',
    governance_eligibility_fingerprint TEXT NOT NULL DEFAULT '',
    governance_ineligible_reason TEXT NOT NULL DEFAULT '',
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
    mutation_id TEXT NOT NULL DEFAULT '',
    governance_eligibility_version TEXT NOT NULL DEFAULT '',
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
    mutation_id TEXT NOT NULL DEFAULT '',
    governance_eligibility_version TEXT NOT NULL DEFAULT '',
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
CREATE INDEX IF NOT EXISTS idx_decision_factor_snapshot_factor ON decision_factor_snapshot(factor);
CREATE INDEX IF NOT EXISTS idx_decision_factor_snapshot_factor_id ON decision_factor_snapshot(factor, id DESC);
CREATE INDEX IF NOT EXISTS idx_order_lifecycle_trade ON order_lifecycle_event(trade_id, event_ts);
CREATE INDEX IF NOT EXISTS idx_position_lifecycle_pos ON position_lifecycle_event(position_id, event_ts);
CREATE INDEX IF NOT EXISTS idx_trade_outcome_review_trade ON trade_outcome_review(trade_id);
CREATE INDEX IF NOT EXISTS idx_supervisor_counterfactual_position ON supervisor_counterfactual_review(position_id, close_ts);
CREATE INDEX IF NOT EXISTS idx_supervisor_counterfactual_label ON supervisor_counterfactual_review(label, updated_at);
CREATE INDEX IF NOT EXISTS idx_nursery_exploration_budget ON nursery_exploration_reservation(trade_date, status, reason, setup_fingerprint);
CREATE INDEX IF NOT EXISTS idx_runtime_config_snapshot_hash ON runtime_config_snapshot(config_hash, created_at);
CREATE INDEX IF NOT EXISTS idx_evolution_run_type ON evolution_run(run_type, status, started_at);
CREATE INDEX IF NOT EXISTS idx_evolution_decision_run ON evolution_decision(run_id, decision_type, created_at);
CREATE INDEX IF NOT EXISTS idx_replay_report_created ON replay_report(created_at);
CREATE INDEX IF NOT EXISTS idx_replay_report_grade ON replay_report(evidence_grade, created_at);
CREATE INDEX IF NOT EXISTS idx_autonomy_health_snapshot_created ON autonomy_health_snapshot(created_at);
CREATE INDEX IF NOT EXISTS idx_autonomy_health_snapshot_posture ON autonomy_health_snapshot(posture, created_at);
CREATE INDEX IF NOT EXISTS idx_autonomy_scope_approval_created ON autonomy_scope_approval_event(created_at);
CREATE INDEX IF NOT EXISTS idx_autonomy_scope_approval_snapshot ON autonomy_scope_approval_event(snapshot_id, created_at);
CREATE INDEX IF NOT EXISTS idx_autonomy_scope_enforcement_created ON autonomy_scope_enforcement_event(created_at);
CREATE INDEX IF NOT EXISTS idx_autonomy_scope_enforcement_snapshot ON autonomy_scope_enforcement_event(snapshot_id, created_at);
CREATE INDEX IF NOT EXISTS idx_autonomy_scope_enforcement_status ON autonomy_scope_enforcement_event(status, created_at);
CREATE INDEX IF NOT EXISTS idx_release_run_created ON release_run(created_at);
CREATE INDEX IF NOT EXISTS idx_release_run_status ON release_run(status, created_at);
CREATE INDEX IF NOT EXISTS idx_release_approval_run ON release_approval_event(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_release_approval_decision ON release_approval_event(decision, created_at);
CREATE INDEX IF NOT EXISTS idx_incident_playbook_created ON incident_playbook_run(created_at);
CREATE INDEX IF NOT EXISTS idx_incident_playbook_scenario ON incident_playbook_run(scenario, created_at);
CREATE INDEX IF NOT EXISTS idx_incident_playbook_event_playbook ON incident_playbook_event(playbook_id, created_at);
CREATE INDEX IF NOT EXISTS idx_incident_playbook_event_type ON incident_playbook_event(event_type, created_at);
CREATE INDEX IF NOT EXISTS idx_brain_state_snapshot_created ON brain_state_snapshot(created_at);
CREATE INDEX IF NOT EXISTS idx_brain_state_snapshot_status ON brain_state_snapshot(status, created_at);
CREATE INDEX IF NOT EXISTS idx_brain_memory_source ON brain_memory(source_table, source_id);
CREATE INDEX IF NOT EXISTS idx_brain_memory_type ON brain_memory(memory_type, created_at);
CREATE INDEX IF NOT EXISTS idx_brain_memory_score ON brain_memory(evidence_score, similarity_score);
CREATE INDEX IF NOT EXISTS idx_brain_action_plan_created ON brain_action_plan(created_at);
CREATE INDEX IF NOT EXISTS idx_brain_action_plan_snapshot ON brain_action_plan(snapshot_id, created_at);
CREATE INDEX IF NOT EXISTS idx_brain_action_plan_type ON brain_action_plan(action_type, status);
CREATE INDEX IF NOT EXISTS idx_brain_action_plan_eval_created ON brain_action_plan_eval(created_at);
CREATE INDEX IF NOT EXISTS idx_brain_action_plan_eval_plan ON brain_action_plan_eval(plan_id, created_at);
CREATE INDEX IF NOT EXISTS idx_brain_action_plan_eval_scope ON brain_action_plan_eval(scope_type, status, created_at);
CREATE INDEX IF NOT EXISTS idx_brain_low_impact_execution_created ON brain_low_impact_execution(created_at);
CREATE INDEX IF NOT EXISTS idx_brain_low_impact_execution_plan ON brain_low_impact_execution(plan_id, eval_id);
CREATE INDEX IF NOT EXISTS idx_brain_low_impact_execution_status ON brain_low_impact_execution(status, created_at);
CREATE INDEX IF NOT EXISTS idx_brain_medium_governance_created ON brain_medium_impact_governance(created_at);
CREATE INDEX IF NOT EXISTS idx_brain_medium_governance_plan ON brain_medium_impact_governance(plan_id, eval_id);
CREATE INDEX IF NOT EXISTS idx_brain_medium_governance_scope ON brain_medium_impact_governance(scope_type, status, created_at);
CREATE INDEX IF NOT EXISTS idx_brain_medium_governance_candidate ON brain_medium_impact_governance(candidate_id);
CREATE INDEX IF NOT EXISTS idx_brain_governance_candidate_created ON brain_governance_candidate(created_at);
CREATE INDEX IF NOT EXISTS idx_brain_governance_candidate_stage ON brain_governance_candidate(proposal_stage, status, created_at);
CREATE INDEX IF NOT EXISTS idx_brain_governance_candidate_scope ON brain_governance_candidate(scope_type, scope_key, action);
CREATE INDEX IF NOT EXISTS idx_brain_governance_candidate_source ON brain_governance_candidate(source_agent, source_kind, created_at);
CREATE INDEX IF NOT EXISTS idx_brain_candidate_review_candidate ON brain_governance_candidate_review(candidate_id, created_at);
CREATE INDEX IF NOT EXISTS idx_brain_candidate_review_status ON brain_governance_candidate_review(review_status, created_at);
CREATE INDEX IF NOT EXISTS idx_brain_candidate_review_fingerprint
ON brain_governance_candidate_review(candidate_id, evidence_fingerprint, created_at);
CREATE INDEX IF NOT EXISTS idx_v16_brain_command_created ON v16_brain_command(created_at);
CREATE INDEX IF NOT EXISTS idx_v16_brain_command_target ON v16_brain_command(target_agent, status, created_at);
CREATE INDEX IF NOT EXISTS idx_v16_brain_command_scope ON v16_brain_command(scope_type, scope_key, created_at);
CREATE INDEX IF NOT EXISTS idx_v16_brain_command_claim ON v16_brain_command(target_agent, scope_type, claim_status, claim_expires_at);
CREATE INDEX IF NOT EXISTS idx_proposal_registry_updated ON proposal_registry(updated_at);
CREATE INDEX IF NOT EXISTS idx_proposal_registry_surface ON proposal_registry(control_surface, target_scope, status);
CREATE INDEX IF NOT EXISTS idx_proposal_registry_source ON proposal_registry(source_agent, source_ref_type, updated_at);
CREATE INDEX IF NOT EXISTS idx_proposal_registry_source_ref_updated_v2 ON proposal_registry(source_ref_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_live_autonomy_unlock_created ON live_autonomy_unlock_event(created_at);
CREATE INDEX IF NOT EXISTS idx_live_autonomy_unlock_status ON live_autonomy_unlock_event(status, created_at);
CREATE INDEX IF NOT EXISTS idx_brain_live_ready_guardrail_created ON brain_live_ready_guardrail(created_at);
CREATE INDEX IF NOT EXISTS idx_brain_live_ready_guardrail_status ON brain_live_ready_guardrail(status, created_at);
CREATE INDEX IF NOT EXISTS idx_autonomous_learning_sample_type ON autonomous_learning_sample(sample_type, label_status, event_ts);
CREATE UNIQUE INDEX IF NOT EXISTS idx_autonomous_learning_sample_source ON autonomous_learning_sample(sample_type, source_table, source_id);
CREATE INDEX IF NOT EXISTS idx_model_permission_audit_created ON model_permission_audit(created_at);
CREATE INDEX IF NOT EXISTS idx_model_permission_audit_model ON model_permission_audit(model_type, status, created_at);
CREATE INDEX IF NOT EXISTS idx_factor_contribution_review_trade ON factor_contribution_review(trade_id);
CREATE INDEX IF NOT EXISTS idx_factor_contribution_review_factor ON factor_contribution_review(factor);
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
    """Validate the PostgreSQL schema gate without writing schema or data.

    Versioned migrations are never applied by an application process.  An
    operator must run ``scripts/state_schema_migrate.py`` before deploying
    code that raises the minimum schema version.  Backend and worker runtime
    roles therefore only require read access to migration metadata at boot.
    """
    conn = get_state_pg_conn(read_only=True)
    try:
        require_state_schema_version(conn)
    finally:
        conn.close()


def get_state_pg_conn(*, read_only: bool = False):
    """Return a direct psycopg connection to the PostgreSQL state schema."""
    if not state_pg_enabled():
        raise RuntimeError("PostgreSQL state backend is not enabled")
    return connect_state_store(state_pg_dsn(), read_only=read_only, schema=STATE_SCHEMA)


def get_state_conn(*, read_only: bool = False):
    """Compatibility alias for the runtime state connection helper.

    New business code should prefer ``get_state_pg_conn`` to make the
    PostgreSQL contract explicit. A few offline tests and cold-backup tools
    still monkeypatch this name to point at an isolated SQLite fixture.
    """
    if _normalize_db_path(STATE_DB).resolve() != _DEFAULT_STATE_DB:
        conn = connect_sqlite(STATE_DB, read_only=read_only)
        conn.row_factory = sqlite3.Row
        return conn
    return get_state_pg_conn(read_only=read_only)


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

CREATE TABLE IF NOT EXISTS model_registry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_type TEXT NOT NULL,
    symbol TEXT DEFAULT 'XAUUSD+',
    timeframe TEXT DEFAULT 'M5',
    version INTEGER NOT NULL,
    artifact_path TEXT DEFAULT '',
    params_json TEXT DEFAULT '{}',
    metrics_json TEXT DEFAULT '{}',
    status TEXT DEFAULT 'active',
    created_at REAL DEFAULT 0.0,
    UNIQUE(model_type, symbol, timeframe, version)
);
CREATE INDEX IF NOT EXISTS idx_model_registry_lookup
    ON model_registry(model_type, symbol, timeframe);

CREATE TABLE IF NOT EXISTS model_shadow_candidate (
    candidate_id TEXT PRIMARY KEY,
    model_type TEXT NOT NULL,
    artifact_path TEXT NOT NULL,
    artifact_sha256 TEXT NOT NULL,
    symbol TEXT DEFAULT 'XAUUSD+',
    timeframe TEXT DEFAULT 'M5',
    status TEXT DEFAULT 'queued',
    gate_decision TEXT DEFAULT '',
    gate_json TEXT DEFAULT '{}',
    registry_version_json TEXT DEFAULT 'null',
    note TEXT DEFAULT '',
    created_at REAL DEFAULT 0.0,
    updated_at REAL DEFAULT 0.0,
    UNIQUE(model_type, artifact_sha256, symbol, timeframe)
);
CREATE INDEX IF NOT EXISTS idx_model_shadow_candidate_status
    ON model_shadow_candidate(status, updated_at);

CREATE TABLE IF NOT EXISTS model_canary_review (
    review_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    model_type TEXT NOT NULL,
    decision TEXT NOT NULL,
    report_path TEXT DEFAULT '',
    metrics_json TEXT DEFAULT '{}',
    thresholds_json TEXT DEFAULT '{}',
    issues_json TEXT DEFAULT '[]',
    note TEXT DEFAULT '',
    created_at REAL DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS idx_model_canary_review_candidate
    ON model_canary_review(candidate_id, created_at);

CREATE TABLE IF NOT EXISTS model_canary_trial (
    trial_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    status TEXT NOT NULL,
    metrics_json TEXT DEFAULT '{}',
    thresholds_json TEXT DEFAULT '{}',
    details_json TEXT DEFAULT '{}',
    created_at REAL DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS idx_model_canary_trial_candidate
    ON model_canary_trial(candidate_id, created_at);

CREATE TABLE IF NOT EXISTS model_inference_audit (
    inference_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    model_type TEXT NOT NULL,
    mode TEXT DEFAULT 'advisory',
    score REAL DEFAULT 0.0,
    prediction INTEGER DEFAULT 0,
    payload_json TEXT DEFAULT '{}',
    result_json TEXT DEFAULT '{}',
    created_at REAL DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS idx_model_inference_candidate
    ON model_inference_audit(candidate_id, created_at);
"""

_EXPERIMENTS_REQUIRED_TABLE_COLUMNS: Final[dict[str, frozenset[str]]] = {
    "experiments": frozenset({
        "run_id",
        "experiment_type",
        "params_json",
        "metrics_json",
        "tags_json",
        "artifacts_json",
        "status",
        "timestamp",
        "created_at",
    }),
    "model_registry": frozenset({
        "id", "model_type", "symbol", "timeframe", "version",
        "artifact_path", "params_json", "metrics_json", "status", "created_at",
    }),
    "model_shadow_candidate": frozenset({
        "candidate_id", "model_type", "artifact_path", "artifact_sha256",
        "symbol", "timeframe", "status", "gate_decision", "gate_json",
        "registry_version_json", "note", "created_at", "updated_at",
    }),
    "model_canary_review": frozenset({
        "review_id", "candidate_id", "model_type", "decision", "report_path",
        "metrics_json", "thresholds_json", "issues_json", "note", "created_at",
    }),
    "model_canary_trial": frozenset({
        "trial_id", "candidate_id", "status", "metrics_json",
        "thresholds_json", "details_json", "created_at",
    }),
    "model_inference_audit": frozenset({
        "inference_id", "candidate_id", "model_type", "mode", "score",
        "prediction", "payload_json", "result_json", "created_at",
    }),
}
_EXPERIMENTS_REQUIRED_INDEXES: Final[frozenset[str]] = frozenset({
    "idx_experiments_type",
    "idx_experiments_status",
    "idx_model_registry_lookup",
    "idx_model_shadow_candidate_status",
    "idx_model_canary_review_candidate",
    "idx_model_canary_trial_candidate",
    "idx_model_inference_candidate",
})


def validate_experiments_db_schema(
    db_path: str | Path = EXPERIMENTS_DB,
) -> None:
    """Validate the minimum experiments schema without creating or altering it.

    Backend and worker startup must be read-only with respect to database
    schemas.  The compatibility migration remains available to an explicit
    operator repair command, but a stale database now fails validation instead
    of being silently changed by the runtime process.  The offline experiments
    store remains optional, so startup skips this check when the file is absent.
    """

    path = _normalize_db_path(db_path)
    if not path.exists():
        raise RuntimeError(
            f"experiments schema missing at {path}; run "
            "scripts/experiments_schema_migrate.py --apply before starting runtime processes"
        )
    conn = connect_sqlite(path, read_only=True)
    try:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        missing_tables = sorted(set(_EXPERIMENTS_REQUIRED_TABLE_COLUMNS) - tables)
        if missing_tables:
            raise RuntimeError(
                "experiments schema missing tables "
                f"{','.join(missing_tables)}; run scripts/experiments_schema_migrate.py --apply "
                "before starting runtime processes"
            )
        for table, required_columns in _EXPERIMENTS_REQUIRED_TABLE_COLUMNS.items():
            existing = {
                str(row[1])
                for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            }
            missing = sorted(required_columns - existing)
            if missing:
                raise RuntimeError(
                    f"experiments schema below minimum version; {table} missing columns "
                    f"{','.join(missing)}; run scripts/experiments_schema_migrate.py --apply "
                    "before starting runtime processes"
                )
        indexes = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        missing_indexes = sorted(_EXPERIMENTS_REQUIRED_INDEXES - indexes)
        if missing_indexes:
            raise RuntimeError(
                "experiments schema missing indexes "
                f"{','.join(missing_indexes)}; run scripts/experiments_schema_migrate.py --apply "
                "before starting runtime processes"
            )
    finally:
        conn.close()


def init_experiments_db(db_path: str | Path = EXPERIMENTS_DB) -> None:
    """Explicit offline compatibility migration for ``experiments.db``.

    Runtime entry points must call :func:`validate_experiments_db_schema`
    instead.  This function is intentionally retained for the operator-owned
    ``scripts/db_doctor.py --repair`` path while the offline experiment store
    is migrated to a versioned schema.
    """
    path = _normalize_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect_sqlite(path)
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
        if "data" in existing:
            rows = conn.execute(
                "SELECT run_id, data FROM experiments "
                "WHERE (experiment_type IS NULL OR experiment_type='') AND data IS NOT NULL"
            ).fetchall()
            for row in rows:
                try:
                    payload = json.loads(row[1] or "{}")
                except Exception:
                    continue
                observed_at = float(payload.get("timestamp") or time.time())
                conn.execute(
                    """UPDATE experiments SET experiment_type=?, params_json=?, metrics_json=?,
                       tags_json=?, artifacts_json=?, status=?, timestamp=?, created_at=?
                       WHERE run_id=?""",
                    (
                        str(payload.get("experiment_type") or "unknown"),
                        json.dumps(payload.get("params") or {}),
                        json.dumps(payload.get("metrics") or {}),
                        json.dumps(payload.get("tags") or []),
                        json.dumps(payload.get("artifacts") or []),
                        str(payload.get("status") or "running"),
                        observed_at,
                        observed_at,
                        row[0],
                    ),
                )
        conn.executescript(EXPERIMENTS_DB_DDL)
        conn.commit()
    finally:
        conn.close()


def prepare_experiments_store(db_path: str | Path = EXPERIMENTS_DB) -> None:
    """Validate production; migrate only an explicitly isolated SQLite path.

    Production backend/worker callers use the canonical ``EXPERIMENTS_DB`` and
    therefore never write schema here. Tests and offline tools that pass a
    different path retain the historical self-contained fixture behavior.
    """

    path = _normalize_db_path(db_path)
    if path.resolve() == EXPERIMENTS_DB.resolve():
        validate_experiments_db_schema(path)
        return
    init_experiments_db(path)


# ═══════════════════════════════════════════
# 启动初始化
# ═══════════════════════════════════════════
_init_lock = threading.Lock()
_initialized = False


def init_all() -> None:
    """Validate runtime database prerequisites without applying schema DDL."""
    global _initialized
    with _init_lock:
        if _initialized:
            return
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        init_state_db()
        if EXPERIMENTS_DB.exists():
            validate_experiments_db_schema(EXPERIMENTS_DB)
        _initialized = True

