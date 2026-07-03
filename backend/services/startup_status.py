from __future__ import annotations

import threading
import time
from typing import Any

_LOCK = threading.Lock()
_ISSUES: list[dict[str, Any]] = []


def record_startup_issue(component: str, status: str, message: str, *, blocking: bool = False) -> None:
    with _LOCK:
        _ISSUES.append(
            {
                "component": str(component or ""),
                "status": str(status or "degraded"),
                "message": str(message or "")[:500],
                "blocking": bool(blocking),
                "created_at": time.time(),
            }
        )


def clear_startup_issues() -> None:
    with _LOCK:
        _ISSUES.clear()


def startup_issues() -> list[dict[str, Any]]:
    with _LOCK:
        return [dict(item) for item in _ISSUES]
