"""Resolve project root and key directories. Uses backend/core/db.py for DB paths."""

from pathlib import Path

PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
BACKEND_DIR: Path = Path(__file__).resolve().parents[1]

DATA_DIR: Path = PROJECT_ROOT / "data"
LOGS_DIR: Path = PROJECT_ROOT / "logs"
CONFIG_DIR: Path = PROJECT_ROOT / "config"
CHARTS_DIR: Path = DATA_DIR / "charts"

# 从统一数据库模块导入路径常量 (向后兼容)
from backend.core.db import (
    DUCKDB_BARS, DUCKDB_TICKS, DUCKDB_L2, DUCKDB_TRADES, DUCKDB_EVENTS,
    STATE_DB, EXPERIMENTS_DB,
)

# 旧 DB_PATH 别名 → 指向统一常量
DB_PATH: Path = DUCKDB_BARS


def ensure_logs_dir() -> Path:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return LOGS_DIR
