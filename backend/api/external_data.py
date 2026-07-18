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

from backend.core.auth import RequireUser
from backend.core.static_feature_flags import shared_static_feature_flags
from backend.jobs import get_job_manager
from backend.services.api_fact_views import external_data_status_fact_payload
from backend.services.external_data_refresh import run_external_data_refresh
from backend.services.mutation_audit import record_api_mutation
from pydantic import BaseModel

router = APIRouter(prefix="/api/data", tags=["data"])
alias_router = APIRouter(prefix="/api/external-data", tags=["data"])

REFRESH_SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "refresh_external_data.py"
PYTHON = sys.executable or "python"

# ── 后台刷新任务存储 ──
import threading as _threading
_refresh_jobs: dict[str, dict] = {}  # job_id → {status, output, started_at, finished_at}
_refresh_jobs_lock = _threading.Lock()


def _run_script(*args: str) -> str:
    """同步执行 refresh_external_data.py 并返回 stdout"""
    if not REFRESH_SCRIPT.exists():
        raise HTTPException(status_code=500, detail=f"Script not found: {REFRESH_SCRIPT}")
    result = subprocess.run(
        [PYTHON, str(REFRESH_SCRIPT), *args],
        capture_output=True, text=True, timeout=300,
        encoding="utf-8", errors="replace",  # v11-fix: 显式 UTF-8, 解决 Windows GBK 默认编码导致的中文乱码
    )
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=result.stderr or result.stdout)
    return result.stdout


def _run_script_bg(job_id: str, *args: str):
    """后台线程：执行刷新脚本，更新 job 状态"""
    with _refresh_jobs_lock:
        job = _refresh_jobs.get(job_id)
    if not job:
        return
    try:
        with _refresh_jobs_lock:
            job["status"] = "running"
        source = "all"
        force = "--force" in args
        if "--source" in args:
            source_index = args.index("--source") + 1
            if source_index < len(args):
                source = str(args[source_index])
        result = run_external_data_refresh(
            {"source": source, "force": force},
            lambda _step, _pct, _message: None,
        )
        job["status"] = "completed"
        job["output"] = list(result.get("output") or [])
    except Exception as e:
        job["status"] = "failed"
        job["output"] = [str(e)]
    finally:
        job["finished_at"] = time.time()


def _cleanup_stale_jobs(max_age: float = 3600, max_size: int = 500):
    """清理超过 1 小时的旧 job, 且上限 500 条防止内存泄漏。"""
    now = time.time()
    with _refresh_jobs_lock:
        stale = [k for k, v in _refresh_jobs.items()
                 if (now - v.get("started_at", 0)) > max_age]
        for k in stale:
            _refresh_jobs.pop(k, None)
        if len(_refresh_jobs) > max_size:
            sorted_keys = sorted(
                _refresh_jobs,
                key=lambda k: _refresh_jobs[k].get("started_at", 0),
                reverse=True,
            )
            for k in sorted_keys[max_size:]:
                _refresh_jobs.pop(k, None)


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
def get_external_status(_user: RequireUser):
    """返回所有外部数据源的时效状态"""
    try:
        stdout = _run_script("--status", "--json")
        try:
            payload = json.loads(stdout)
            sources = payload.get("sources") or []
        except Exception:
            sources = _parse_status(stdout)
        return external_data_status_fact_payload(
            {"sources": sources if sources else stdout.strip().splitlines()}
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@alias_router.get("/status")
def get_external_status_alias(_user: RequireUser):
    return get_external_status(_user)


class RefreshRequest(BaseModel):
    source: str = "all"
    force: bool = False


@router.post("/external-refresh")
def trigger_refresh(_user: RequireUser, req: RefreshRequest = RefreshRequest()):
    """触发外部数据刷新（后台异步），立即返回 job_id"""
    params = {"source": req.source, "force": req.force}
    durable = bool(shared_static_feature_flags().pg_job_queue_v2_enabled)
    if durable:
        state = get_job_manager().submit(
            "external_refresh",
            params,
            lambda progress: run_external_data_refresh(params, progress),
        )
        job_id = state.id
        job_status = state.status
    else:
        _cleanup_stale_jobs()
        job_id = uuid.uuid4().hex[:12]
        with _refresh_jobs_lock:
            _refresh_jobs[job_id] = {
                "status": "pending",
                "output": [],
                "started_at": time.time(),
                "finished_at": None,
            }
        args = ["--once"]
        if req.source and req.source != "all":
            args += ["--source", req.source]
        if req.force:
            args.append("--force")
        Thread(target=_run_script_bg, args=(job_id, *args), daemon=True).start()
        job_status = "pending"

    result = {
        "job_id": job_id,
        "status": "started",
        "job_status": job_status,
        "durable": durable,
        "message": "刷新已启动，GET /api/data/external-refresh/{job_id} 查进度",
    }
    record_api_mutation(
        user=_user,
        endpoint="/api/data/external-refresh",
        action="external_refresh",
        status="applied",
        result={"source": req.source, "force": req.force, **result},
    )
    return result


@alias_router.post("/refresh")
def trigger_refresh_alias(_user: RequireUser, req: RefreshRequest = RefreshRequest()):
    return trigger_refresh(_user, req)


@router.get("/external-refresh/{job_id}")
def get_refresh_status(_user: RequireUser, job_id: str):
    """查询刷新任务进度"""
    if shared_static_feature_flags().pg_job_queue_v2_enabled:
        state = get_job_manager().get(job_id)
        if state is not None:
            payload = state.to_dict()
            status_map = {
                "done": "completed",
                "error": "failed",
                "retry_wait": "pending",
                "queued": "pending",
            }
            return {
                "job_id": state.id,
                "status": status_map.get(state.status, state.status),
                "output": list((state.result or {}).get("output") or [])
                if isinstance(state.result, dict)
                else [],
                "started_at": payload.get("started_at"),
                "finished_at": payload.get("finished_at"),
                "error": state.error or "",
                "durable": True,
            }
    with _refresh_jobs_lock:
        job = _refresh_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"job_id={job_id} not found")
    return dict(job)  # 返回 copy, 防 torn read


@alias_router.get("/refresh/{job_id}")
def get_refresh_status_alias(_user: RequireUser, job_id: str):
    return get_refresh_status(_user, job_id)
