"""Resolve project root and key directories from anywhere in the process."""
from pathlib import Path

# backend/main.py → backend/core/paths.py: project root = parents[2]
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]

DATA_DIR: Path = PROJECT_ROOT / "data"
LOGS_DIR: Path = PROJECT_ROOT / "logs"
CONFIG_DIR: Path = PROJECT_ROOT / "config"
CHARTS_DIR: Path = DATA_DIR / "charts"
DB_PATH: Path = DATA_DIR / "market_data.db"


def ensure_logs_dir() -> Path:
    """Create logs dir if missing. Returns path."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return LOGS_DIR
