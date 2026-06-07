"""Centralized loguru setup. Idempotent (safe to call multiple times)."""
import sys
from loguru import logger

from backend.core.paths import LOGS_DIR, ensure_logs_dir

_CONFIGURED = False


def setup_logging(level: str = "INFO") -> None:
    """Configure loguru to write to stderr + logs/backend.log."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    ensure_logs_dir()
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> — <level>{message}</level>",
    )
    logger.add(
        LOGS_DIR / "backend.log",
        level="DEBUG",
        rotation="10 MB",
        retention="7 days",
        encoding="utf-8",
    )
    _CONFIGURED = True
