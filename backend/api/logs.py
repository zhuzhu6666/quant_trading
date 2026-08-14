"""REST API: 实时日志查看 — GET /api/logs/tail?source=backend&lines=50"""
import re
import time
from typing import Literal

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from backend.core.auth import RequireUser
from backend.core.paths import LOGS_DIR

router = APIRouter(prefix="/api/logs", tags=["logs"])

LogSource = Literal["backend", "live_loop", "alerts", "debug"]
LOG_SOURCE_FILES: dict[LogSource, str] = {
    "backend": "backend.log",
    "live_loop": "live_loop.log",
    "alerts": "alerts.log",
    "debug": "debug.log",
}
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


@router.get("/tail")
async def tail_logs(
    _user: RequireUser,
    lines: int = Query(default=100, ge=1, le=500),
    source: LogSource = Query(default="backend"),
):
    """返回允许的日志源最后 N 行；source 只接受固定的服务日志白名单。"""
    log_path = LOGS_DIR / LOG_SOURCE_FILES[source]
    if not log_path.exists():
        return JSONResponse({
            "lines": [],
            "total": 0,
            "source": source,
            "file": LOG_SOURCE_FILES[source],
            "size_bytes": 0,
            "observed_at": time.time(),
        })

    try:
        # 读文件末段 (预估每行 ~200 字节, 读 lines*300 字节兜底)
        chunk_size = max(lines * 300, 4096)
        with open(log_path, "rb") as f:
            f.seek(0, 2)  # 末尾
            size = f.tell()
            if size == 0:
                return JSONResponse({
                    "lines": [],
                    "total": 0,
                    "source": source,
                    "file": LOG_SOURCE_FILES[source],
                    "size_bytes": 0,
                    "observed_at": time.time(),
                })
            read_pos = max(0, size - chunk_size)
            f.seek(read_pos)
            raw = f.read()
        text = raw.decode("utf-8", errors="replace")
        all_lines = text.splitlines()
        # 去掉首行不完整 (从文件中间开始读)
        if read_pos > 0 and all_lines:
            all_lines = all_lines[1:]
        tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
        # 剥离 ANSI 控制符 (loguru stderr 彩色输出被重定向到文件时引入)
        tail = [_ANSI_ESCAPE.sub("", line) for line in tail]
        return JSONResponse({
            "lines": tail,
            "total": len(tail),
            "source": source,
            "file": LOG_SOURCE_FILES[source],
            "size_bytes": size,
            "observed_at": time.time(),
        })
    except Exception as e:
        return JSONResponse(
            {"lines": [], "total": 0, "source": source, "file": LOG_SOURCE_FILES[source], "error": str(e)},
            status_code=500,
        )
