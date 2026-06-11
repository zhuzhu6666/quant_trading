"""
GET /api/data/external-status — 外部数据源时效状态
POST /api/data/external-refresh — 一键刷新外部数据
"""
import json
import subprocess
import sys
from pathlib import Path
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/data", tags=["data"])

REFRESH_SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "refresh_external_data.py"
PYTHON = sys.executable or "python"


def _run_script(*args: str) -> str:
    """执行 refresh_external_data.py 并返回 stdout"""
    if not REFRESH_SCRIPT.exists():
        raise HTTPException(status_code=500, detail=f"Script not found: {REFRESH_SCRIPT}")
    result = subprocess.run(
        [PYTHON, str(REFRESH_SCRIPT), *args],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=result.stderr or result.stdout)
    return result.stdout


def _parse_status(stdout: str) -> list[dict]:
    """解析 --status 的输出为结构化列表"""
    sources = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("数据源") or line.startswith("-"):
            continue
        parts = [p for p in line.split() if p]
        if len(parts) < 3:
            continue
        # 格式: cot_gold 2026-05-26 ⚠ 过期 X天前
        table = parts[0]
        latest = parts[1]
        full_text = " ".join(parts[2:])
        stale = "过期" in full_text
        # 取说明部分 (去掉 ⚠ 和 "过期"/"正常")
        note = full_text.replace("⚠", "").replace("✓", "").replace("过期", "").replace("正常", "").strip()
        sources.append({
            "table": table,
            "latest": latest,
            "stale": stale,
            "note": note,
        })
    return sources


@router.get("/external-status")
def get_external_status():
    """返回所有外部数据源的时效状态"""
    try:
        stdout = _run_script("--status")
        sources = _parse_status(stdout)
        return {"sources": sources if sources else stdout.strip().splitlines()}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class RefreshRequest(BaseModel):
    source: str = "all"
    force: bool = False


@router.post("/external-refresh")
def trigger_refresh(req: RefreshRequest = RefreshRequest()):
    """触发外部数据刷新"""
    args = ["--once"]
    if req.source and req.source != "all":
        args = ["--source", req.source]
    if req.force:
        args.append("--force")
    try:
        stdout = _run_script(*args)
        return {"status": "ok", "output": stdout.strip().splitlines()[-10:]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
