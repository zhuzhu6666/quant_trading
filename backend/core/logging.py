"""Centralized loguru setup. Idempotent (safe to call multiple times).

audit v9: 增强版 — 同时捕获 stdlib logging → loguru,
DEBUG 级别写专用文件, INFO 写主日志, 全系统日志统一入口。

audit v10 (2026-08-19): pytest/测试进程下跳过文件 sink（backend.log / debug.log），
只写 stderr。根因：测试子进程 import 后端模块时同样触发 setup_logging，把 pytest
堆栈、测试触发的一次性 lifespan/boot 错误写进生产日志文件，导致前端 /api/logs/tail
面板大量历史"错误"刷屏。检测依据 = PYTEST_CURRENT_TEST 环境变量（pytest 自动注入）。
"""
import os
import sys
import logging as _stdlib_logging
from loguru import logger

from backend.core.paths import LOGS_DIR, ensure_logs_dir

_CONFIGURED = False


def _is_under_pytest() -> bool:
    """True 当运行在 pytest 中（PYTEST_CURRENT_TEST 由 pytest 自动注入）。"""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    if os.environ.get("PYTEST_VERSION"):
        return True
    return False


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
    - pytest/测试进程: 仅 stderr, 不写任何日志文件（避免污染生产日志）
    """
    global _CONFIGURED
    if _CONFIGURED:
        return
    ensure_logs_dir()
    logger.remove()

    under_pytest = _is_under_pytest()

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

    if not under_pytest:
        # 主日志: INFO+ (仅生产/常规进程)
        logger.add(
            LOGS_DIR / "backend.log",
            level="INFO",
            rotation="10 MB",
            retention="7 days",
            encoding="utf-8",
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} — {message}",
        )

        # 全量调试日志: DEBUG+ (仅生产/常规进程)
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
    logger.info(
        "[logging] loguru initialized: stderr"
        + ("" if under_pytest else " + backend.log (INFO) + debug.log (DEBUG)")
        + (" (pytest: file sinks disabled)" if under_pytest else "")
    )
