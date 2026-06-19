"""Centralized loguru setup. Idempotent (safe to call multiple times).

audit v9: 增强版 — 同时捕获 stdlib logging → loguru,
DEBUG 级别写专用文件, INFO 写主日志, 全系统日志统一入口。
"""
import sys
import logging as _stdlib_logging
from loguru import logger

from backend.core.paths import LOGS_DIR, ensure_logs_dir

_CONFIGURED = False


class _PropagateHandler(_stdlib_logging.Handler):
    """把 stdlib logging 记录转发到 loguru."""

    def emit(self, record: _stdlib_logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = "DEBUG"
        frame = _stdlib_logging.currentframe()
        depth = 2
        while frame and frame.f_code.co_filename == __file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def setup_logging(level: str = "INFO") -> None:
    """Configure loguru to write to stderr + logs/backend.log + logs/debug.log.

    - stderr: INFO level, 彩色格式
    - backend.log: INFO level, 10MB 轮转, 7 天保留
    - debug.log: DEBUG level, 50MB 轮转, 3 天保留 (全量调试日志)
    - 同时拦截 stdlib logging → loguru
    """
    global _CONFIGURED
    if _CONFIGURED:
        return
    ensure_logs_dir()
    logger.remove()

    # stderr: 简洁格式
    logger.add(
        sys.stderr,
        level=level.upper(),
        format=(
            "<green>{time:HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> — "
            "<level>{message}</level>"
        ),
    )

    # 主日志: INFO+
    logger.add(
        LOGS_DIR / "backend.log",
        level="INFO",
        rotation="10 MB",
        retention="7 days",
        encoding="utf-8",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} — {message}",
    )

    # 全量调试日志: DEBUG+
    logger.add(
        LOGS_DIR / "debug.log",
        level="DEBUG",
        rotation="50 MB",
        retention="3 days",
        encoding="utf-8",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} — {message}",
    )

    # 拦截 stdlib logging → loguru
    root = _stdlib_logging.getLogger()
    root.handlers = [_PropagateHandler()]
    root.setLevel(_stdlib_logging.DEBUG)

    # Windows asyncio ProactorEventLoop 在客户端异常断连时会抛
    # ConnectionResetError [WinError 10054]，这是已知的无害错误，
    # 升到 WARNING 避免刷屏
    _stdlib_logging.getLogger("asyncio").setLevel(_stdlib_logging.WARNING)

    _CONFIGURED = True
    logger.info("[logging] loguru initialized: backend.log (INFO) + debug.log (DEBUG) + stderr")
