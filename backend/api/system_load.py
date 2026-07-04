"""Lightweight server load endpoint for the web console."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from backend.core.auth import RequireUser

router = APIRouter(prefix="/api/system", tags=["system"])

_CPU_LOCK = threading.Lock()
_LAST_CPU_TOTAL: int | None = None
_LAST_CPU_IDLE: int | None = None


def _read_proc_stat() -> tuple[int, int] | None:
    try:
        line = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0]
        parts = [int(item) for item in line.split()[1:]]
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
        return sum(parts), idle
    except Exception:
        return None


def _cpu_percent_from_proc(load1: float, cores: int) -> float:
    global _LAST_CPU_IDLE, _LAST_CPU_TOTAL
    current = _read_proc_stat()
    if current is None:
        return min(100.0, max(0.0, (load1 / max(cores, 1)) * 100.0))

    total, idle = current
    with _CPU_LOCK:
        if _LAST_CPU_TOTAL is None or _LAST_CPU_IDLE is None:
            _LAST_CPU_TOTAL = total
            _LAST_CPU_IDLE = idle
            return min(100.0, max(0.0, (load1 / max(cores, 1)) * 100.0))
        total_delta = total - _LAST_CPU_TOTAL
        idle_delta = idle - _LAST_CPU_IDLE
        _LAST_CPU_TOTAL = total
        _LAST_CPU_IDLE = idle

    if total_delta <= 0:
        return 0.0
    return min(100.0, max(0.0, (1.0 - idle_delta / total_delta) * 100.0))


def _read_meminfo() -> dict[str, float]:
    values: dict[str, float] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, raw = line.split(":", 1)
            number = float(raw.strip().split()[0]) * 1024.0
            values[key] = number
    except Exception:
        return {"total_bytes": 0.0, "available_bytes": 0.0, "used_bytes": 0.0, "percent": 0.0}

    total = values.get("MemTotal", 0.0)
    available = values.get("MemAvailable", 0.0)
    used = max(0.0, total - available)
    percent = (used / total * 100.0) if total > 0 else 0.0
    return {
        "total_bytes": total,
        "available_bytes": available,
        "used_bytes": used,
        "percent": percent,
    }


def _disk_usage(path: str = "/") -> dict[str, Any]:
    try:
        stat = os.statvfs(path)
        total = float(stat.f_blocks * stat.f_frsize)
        free = float(stat.f_bavail * stat.f_frsize)
        used = max(0.0, total - free)
        percent = (used / total * 100.0) if total > 0 else 0.0
        return {
            "path": path,
            "total_bytes": total,
            "free_bytes": free,
            "used_bytes": used,
            "percent": percent,
        }
    except Exception as exc:
        return {"path": path, "error": str(exc), "percent": 0.0}


def _process_rss_bytes() -> float:
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return float(line.split()[1]) * 1024.0
    except Exception:
        return 0.0
    return 0.0


@router.get("/load")
def system_load(_user: RequireUser) -> dict[str, Any]:
    """Return current host load using only Linux procfs and stdlib calls."""
    cores = int(os.cpu_count() or 1)
    try:
        load1, load5, load15 = os.getloadavg()
    except OSError:
        load1 = load5 = load15 = 0.0

    return {
        "ok": True,
        "ts": time.time(),
        "cpu": {
            "percent": _cpu_percent_from_proc(load1, cores),
            "load1": load1,
            "load5": load5,
            "load15": load15,
            "cores": cores,
        },
        "memory": _read_meminfo(),
        "disk": _disk_usage("/"),
        "process": {
            "pid": os.getpid(),
            "rss_bytes": _process_rss_bytes(),
        },
    }
