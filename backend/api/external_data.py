"""
GET  /api/data/external-status   — 外部数据源时效状态
POST /api/data/external-refresh  — 一键刷新（后台异步，返回 job_id）
GET  /api/data/external-refresh/{job_id} — 查询刷新进度
"""
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from threading import Thread
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/data", tags=["data"])

REFRESH_SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "refresh_external_data.py"
PYTHON = sys.executable or "python"

# ── 后台刷新任务存储 ──
_refresh_jobs: dict[str, dict] = {}  # job_id → {status, output, started_at, finished_at}


def _run_script(*args: str) -> str:
    """同步执行 refresh_external_data.py 并返回 stdout"""
    if not REFRESH_SCRIPT.exists():
        raise HTTPException(status_code=500, detail=f"Script not found: {REFRESH_SCRIPT}")
    result = subprocess.run(
        [PYTHON, str(REFRESH_SCRIPT), *args],
        capture_output=True, text=True, timeout=300,
        errors="replace",  # audit 2026-06-12: Windows GBK 编码下 refresh_external_data.py 输出包含 ✓/✗/⚠ 等 Unicode 字符会崩
    )
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=result.stderr or result.stdout)
    return result.stdout


def _run_script_bg(job_id: str, *args: str):
    """后台线程：执行刷新脚本，更新 job 状态"""
    job = _refresh_jobs.get(job_id)
    if not job:
        return
    try:
        job["status"] = "running"
        result = subprocess.run(
            [PYTHON, str(REFRESH_SCRIPT), *args],
            capture_output=True, text=True, timeout=300,
            errors="replace",  # audit 2026-06-12: 同上，GBK 编码防护
        )
        if result.returncode == 0:
            job["status"] = "completed"
            job["output"] = result.stdout.strip().splitlines()[-20:]
        else:
            job["status"] = "failed"
            job["output"] = (result.stderr or result.stdout).strip().splitlines()[-20:]
    except Exception as e:
        job["status"] = "failed"
        job["output"] = [str(e)]
    finally:
        job["finished_at"] = time.time()


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
        table = parts[0]
        latest = parts[1]
        full_text = " ".join(parts[2:])
        stale = "过期" in full_text
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
    """触发外部数据刷新（后台异步），立即返回 job_id"""
    job_id = uuid.uuid4().hex[:12]
    _refresh_jobs[job_id] = {
        "status": "pending",
        "output": [],
        "started_at": time.time(),
        "finished_at": None,
    }
    args = ["--once"]
    if req.source and req.source != "all":
        args = ["--source", req.source]
    if req.force:
        args.append("--force")

    t = Thread(target=_run_script_bg, args=(job_id, *args), daemon=True)
    t.start()

    return {
        "job_id": job_id,
        "status": "started",
        "message": "刷新已启动，GET /api/data/external-refresh/{job_id} 查进度",
    }


@router.get("/external-refresh/{job_id}")
def get_refresh_status(job_id: str):
    """查询刷新任务进度"""
    job = _refresh_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"job_id={job_id} not found")
    return job
