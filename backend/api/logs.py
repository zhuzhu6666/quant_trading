"""REST API: 实时日志查看 — GET /api/logs/tail?lines=50"""
from pathlib import Path

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from backend.core.auth import RequireUser
from backend.core.paths import LOGS_DIR

router = APIRouter(prefix="/api/logs", tags=["logs"])


@router.get("/tail")
async def tail_logs(_user: RequireUser, lines: int = Query(default=50, ge=1, le=500)):
    """返回 backend.log 最后 N 行。"""
    log_path = LOGS_DIR / "backend.log"
    if not log_path.exists():
        return JSONResponse({"lines": [], "total": 0, "file": str(log_path)})

    try:
        # 读文件末段 (预估每行 ~200 字节, 读 lines*300 字节兜底)
        chunk_size = max(lines * 300, 4096)
        with open(log_path, "rb") as f:
            f.seek(0, 2)  # 末尾
            size = f.tell()
            if size == 0:
                return JSONResponse({"lines": [], "total": 0, "file": str(log_path)})
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
        import re
        _ansi_escape = re.compile(r'\x1b\[[0-9;]*m')
        tail = [_ansi_escape.sub('', line) for line in tail]
        return JSONResponse({
            "lines": tail,
            "total": len(tail),
            "file": str(log_path),
        })
    except Exception as e:
        return JSONResponse(
            {"lines": [], "total": 0, "error": str(e)},
            status_code=500,
        )
