"""StructuredLogger — 把 stdlib logging 输出格式化为 JSON。

设计:
- 工厂函数 setup_structured_logging(level, run_id) 替换全局 logging formatter
- 每条 log 携带 run_id(每次 paper 启动或 daily job 一个 UUID)
- 不破坏已有 logger 的 name 行为,只是改输出格式
- 与 loguru 系 Alerter 并存:alerter 走自己的通道,本模块管 stdlib 日志

JSON schema:
{
  "ts": "2026-06-10T12:34:56.789Z",
  "level": "INFO",
  "logger": "alpha.factor_health",
  "msg": "evaluated 22 factors",
  "run_id": "abc123def456",
  "module": "factor_health",
  "lineno": 84
}
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# 进程级 run_id(可在 lifespan / daily job 启动时重置)
_run_id: str = uuid.uuid4().hex[:12]


def current_run_id() -> str:
    return _run_id


def reset_run_id(new_id: Optional[str] = None) -> str:
    """重置 run_id(每次 paper 启动或 daily job 启动时调用)。

    Args:
        new_id: 显式指定;若 None 则生成新的 UUID 短串。
    """
    global _run_id
    _run_id = new_id or uuid.uuid4().hex[:12]
    return _run_id


class JsonFormatter(logging.Formatter):
    """JSON formatter for stdlib logging."""

    def __init__(self, run_id_provider=None) -> None:
        super().__init__()
        self._run_id_provider = run_id_provider or current_run_id

    def format(self, record: logging.LogRecord) -> str:
        try:
            ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()
        except Exception:  # noqa: BLE001
            ts = f"{time.time():.3f}"
        payload: Dict[str, Any] = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "module": record.module,
            "lineno": record.lineno,
        }
        try:
            run_id = self._run_id_provider() if callable(self._run_id_provider) else self._run_id_provider
            if run_id:
                payload["run_id"] = run_id
        except Exception:  # noqa: BLE001
            pass
        # 任何 extra 字段都平铺进来
        std_attrs = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys()) | {
            "message", "asctime", "exc_info", "exc_text", "stack_info", "filename", "funcName", "pathname", "process", "processName", "thread", "threadName", "args"
        }
        for k, v in record.__dict__.items():
            if k in std_attrs or k.startswith("_"):
                continue
            try:
                json.dumps(v)
                payload[k] = v
            except (TypeError, ValueError):
                payload[k] = repr(v)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def setup_structured_logging(level: str = "INFO", run_id: Optional[str] = None, force: bool = False) -> str:
    """安装 JSON formatter 到 root logger。

    Args:
        level: 日志级别(字符串, "DEBUG"/"INFO"/...)
        run_id: 显式指定 run_id;None 则生成新的。
        force: True 时覆盖现有 handler;False 时只在没有 JSON handler 时添加。
    Returns:
        实际使用的 run_id。
    """
    if run_id is not None:
        reset_run_id(run_id)
    rid = current_run_id()
    root = logging.getLogger()
    if not force:
        for h in root.handlers:
            if isinstance(getattr(h, "formatter", None), JsonFormatter):
                return rid
    # 清空再装(force 或 没有 JSON handler)
    if force:
        for h in list(root.handlers):
            root.removeHandler(h)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter(current_run_id))
    root.addHandler(handler)
    try:
        root.setLevel(getattr(logging, level.upper(), logging.INFO))
    except Exception:  # noqa: BLE001
        root.setLevel(logging.INFO)
    return rid
