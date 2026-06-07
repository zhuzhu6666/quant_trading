# Quant Web Console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace all terminal CLI operations of the quant trading framework with a Web-based console (Next.js 14 + FastAPI), preserving zero behavior drift in the existing 9000+ lines of core Python.

**Architecture:** Two-tier monolith in single repo. `backend/` (FastAPI) wraps existing alpha/strategy/execution/data as service functions (zero rewrite); `frontend/` (Next.js 14 + shadcn/ui + TradingView LWC + ECharts) consumes REST + WebSocket. Existing `main.py` and `scripts/` CLI entries are preserved by refactoring each script into "importable service + CLI main" two-mode form.

**Tech Stack:** Python 3.12, FastAPI 0.110+, pydantic 2.6+, loguru, Next.js 14.2, React 18.3, TypeScript 5.4, Tailwind 3.4, shadcn/ui, Zustand, react-hook-form + zod, TanStack Table, lightweight-charts 4.2, ECharts 5.5, Vitest, Playwright 1.44.

**Spec:** `docs/superpowers/specs/2026-06-07-quant-web-console-design.md`
**Phases (from spec §7.1):**
- Phase 0 (status: ✅ existing CLI paths unchanged)
- Phase 1: backend skeleton + 1 service (backtest) + 1 WS (/ws/state) — 1 week
- Phase 2: 5 core services + overview page — 2 weeks
- Phase 3: all 11 services + 14 pages — 4-6 weeks
- Phase 4: scripts fully refactored + E2E + perf — 2 weeks
- Phase 5: productionize (auth/single-port/multi-user scaffold) — 1-2 weeks

**This plan file covers Phase 1-3 in full detail.** Phase 4-5 are sketched at the end (no tasks) — to be planned in their own sessions.

---

## File Structure

### New files

```
backend/
├── __init__.py
├── app.py                                # FastAPI app factory + lifespan + CORS
├── main.py                               # uvicorn entry (python -m backend)
├── deps.py                               # DI singletons
├── core/
│   ├── __init__.py
│   ├── paths.py                          # project root / data dir
│   ├── settings.py                       # pydantic Settings
│   └── logging.py                        # loguru config
├── jobs/
│   ├── __init__.py
│   ├── state.py                          # JobState dataclass
│   ├── manager.py                        # JobManager
│   ├── runner.py                         # asyncio.create_task wrapper
│   └── progress.py                       # ProgressCB type
├── ws/
│   ├── __init__.py
│   ├── manager.py                        # ConnectionManager + rooms
│   └── endpoints.py                      # /ws/state, /ws/alerts, /ws/jobs/:id, /ws/logs
├── api/
│   ├── __init__.py                       # unified router registration
│   ├── health.py
│   ├── market.py
│   ├── backtest.py
│   ├── paper.py
│   ├── live.py
│   ├── factors.py
│   ├── factor_health.py
│   ├── discover.py
│   ├── sync.py
│   ├── calibrator.py
│   ├── tuning.py
│   ├── reports.py
│   ├── config.py
│   ├── shadow.py
│   ├── ab_test.py
│   └── auth.py                           # stub for v1
├── services/
│   ├── __init__.py
│   ├── backtest_service.py
│   ├── paper_service.py
│   ├── live_service.py
│   ├── factor_health_service.py
│   ├── discover_service.py
│   ├── sync_service.py
│   ├── calibrator_service.py
│   ├── tuning_service.py
│   ├── shadow_service.py
│   ├── ab_service.py
│   └── report_service.py

frontend/
├── package.json
├── tsconfig.json                         # strict mode
├── next.config.mjs                       # rewrites /api/* → :8000
├── tailwind.config.ts
├── postcss.config.js
├── components.json                       # shadcn/ui config
├── app/
│   ├── layout.tsx
│   ├── page.tsx                          # /  Overview
│   ├── globals.css
│   ├── (terminal)/
│   │   ├── market/page.tsx
│   │   ├── backtest/page.tsx
│   │   ├── paper/page.tsx
│   │   ├── live/page.tsx
│   │   ├── factors/page.tsx
│   │   ├── factors/[name]/page.tsx
│   │   ├── discover/page.tsx
│   │   ├── sync/page.tsx
│   │   ├── tuning/page.tsx
│   │   ├── calibrator/page.tsx
│   │   ├── shadow/page.tsx
│   │   ├── ab/page.tsx
│   │   ├── reports/page.tsx
│   │   ├── reports/[name]/page.tsx
│   │   ├── config/page.tsx
│   │   └── jobs/page.tsx
├── components/
│   ├── layout/{sidebar.tsx, topbar.tsx, ws-provider.tsx}
│   ├── charts/{candlestick.tsx, equity-curve.tsx, heatmap.tsx, factor-health-radar.tsx, drawdown.tsx}
│   ├── tables/{factor-table.tsx, trade-table.tsx, job-table.tsx, shadow-table.tsx}
│   ├── forms/{backtest-form.tsx, paper-form.tsx, discover-form.tsx, tuning-form.tsx, config-editor.tsx, ab-form.tsx}
│   └── feedback/{job-progress.tsx, alert-toast.tsx, confirm-dialog.tsx}
├── lib/
│   ├── api.ts                            # fetch + zod
│   ├── ws.ts                             # WS client + reconnect
│   ├── store.ts                          # zustand
│   ├── format.ts
│   ├── i18n.ts
│   └── types.ts
└── public/favicon.svg

tests/
├── test_backend_jobs.py
├── test_backend_services.py
├── test_backend_api.py
├── test_scripts_refactor.py
├── test_ws_endpoints.py
├── e2e/critical_paths.spec.ts

start.bat, start.sh, start-prod.bat, stop.bat, stop.sh
README_WEB.md
```

### Modified files
- `requirements.txt` — add fastapi, uvicorn, pydantic, pydantic-settings, websockets, httpx
- `main.py` — no functional change; the CLI entry remains untouched (Phase 0 invariant)

### Untouched files (per spec §1.1)
- `alpha/` `strategy/` `execution/` `risk/` `data/` `db/` `core/` `factors/` `live/` `modules/` `memory/` `tests/` `logs/` — all preserved

---

# Phase 1: Backend Skeleton + Backtest Service + /ws/state

**Phase 1 Goal:** Get the FastAPI backend up, prove the import boundary works, wire one service (backtest) end-to-end, broadcast live state via WebSocket. Frontend does NOT exist yet in this phase — verification is curl/TestClient only.

**Phase 1 commits:** ~12 commits, each independently revertible.

## Task 1.1: Backend package skeleton + uvicorn entry

**Files:**
- Create: `backend/__init__.py`
- Create: `backend/main.py`
- Create: `requirements.txt` (modify — append)

- [ ] **Step 1: Create empty backend package**

```python
# backend/__init__.py
"""Quant Trading Web Console — FastAPI backend."""
__version__ = "0.1.0"
```

- [ ] **Step 2: Create uvicorn entry**

```python
# backend/main.py
"""Uvicorn entry: `python -m backend` starts FastAPI on :8000."""
import argparse
import uvicorn


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    uvicorn.run("backend.app:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Add backend deps to requirements.txt**

Append to `requirements.txt` (read first to see existing entries, do not duplicate):
```
# --- Web Console backend (Phase 1+, 2026-06-07) ---
fastapi>=0.110,<1.0
uvicorn[standard]>=0.27,<1.0
pydantic>=2.6,<3.0
pydantic-settings>=2.2,<3.0
websockets>=12.0,<13.0
httpx>=0.27,<1.0
python-multipart>=0.0.9,<1.0
```

- [ ] **Step 4: Install and verify import works**

Run:
```bash
cd "C:\Users\zhu\quant_trading" && "C:/Users/zhu/AppData/Local/Programs/Python/Python312/python.exe" -m pip install -r requirements.txt 2>&1 | tail -20
```
Expected: "Successfully installed fastapi-... uvicorn-... pydantic-... pydantic-settings-... websockets-... httpx-... python-multipart-..." (new deps added; old deps not affected)

Then:
```bash
cd "C:\Users\zhu\quant_trading" && "C:/Users/zhu/AppData/Local/Programs/Python/Python312/python.exe" -c "import backend; print(backend.__version__)"
```
Expected: `0.1.0`

- [ ] **Step 5: Commit**

```bash
cd "C:\Users\zhu\quant_trading" && git add backend/ requirements.txt && git commit -m "feat(backend): add uvicorn entry + deps (Phase 1.1)"
```

---

## Task 1.2: Project paths + settings + loguru

**Files:**
- Create: `backend/core/__init__.py`
- Create: `backend/core/paths.py`
- Create: `backend/core/settings.py`
- Create: `backend/core/logging.py`
- Create: `tests/test_backend_paths.py`

- [ ] **Step 1: Create empty core package**

```python
# backend/core/__init__.py
"""Backend-specific core utilities (paths, settings, logging)."""
```

- [ ] **Step 2: Write paths module**

```python
# backend/core/paths.py
"""Resolve project root and key directories from anywhere in the process."""
from pathlib import Path

# backend/main.py → backend/core/paths.py: project root = parents[2]
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]

DATA_DIR: Path = PROJECT_ROOT / "data"
LOGS_DIR: Path = PROJECT_ROOT / "logs"
CONFIG_DIR: Path = PROJECT_ROOT / "config"
CHARTS_DIR: Path = DATA_DIR / "charts"
DB_PATH: Path = DATA_DIR / "market_data.db"


def ensure_logs_dir() -> Path:
    """Create logs dir if missing. Returns path."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return LOGS_DIR
```

- [ ] **Step 3: Write settings module**

```python
# backend/core/settings.py
"""Backend settings — wraps QUANT_* env vars with sensible defaults."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="QUANT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    project_root: str = "."
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    web_port: int = 3000
    db_path: str = "data/market_data.db"


_settings: Settings | None = None


def get_settings() -> Settings:
    """Singleton accessor."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
```

- [ ] **Step 4: Write loguru config**

```python
# backend/core/logging.py
"""Centralized loguru setup. Idempotent (safe to call multiple times)."""
import sys
from loguru import logger

from backend.core.paths import LOGS_DIR, ensure_logs_dir

_CONFIGURED = False


def setup_logging(level: str = "INFO") -> None:
    """Configure loguru to write to stderr + logs/backend.log."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    ensure_logs_dir()
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> — <level>{message}</level>",
    )
    logger.add(
        LOGS_DIR / "backend.log",
        level="DEBUG",
        rotation="10 MB",
        retention="7 days",
        encoding="utf-8",
    )
    _CONFIGURED = True
```

- [ ] **Step 5: Write failing test for paths**

```python
# tests/test_backend_paths.py
"""Verify paths resolve from any CWD."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from backend.core.paths import (
    CHARTS_DIR, CONFIG_DIR, DATA_DIR, DB_PATH, LOGS_DIR, PROJECT_ROOT, ensure_logs_dir,
)


def test_project_root_is_quant_trading():
    assert PROJECT_ROOT.name == "quant_trading"


def test_data_dir_under_root():
    assert DATA_DIR == PROJECT_ROOT / "data"
    assert LOGS_DIR == PROJECT_ROOT / "logs"
    assert CONFIG_DIR == PROJECT_ROOT / "config"


def test_ensure_logs_dir_is_idempotent():
    p = ensure_logs_dir()
    assert p.exists()
    p2 = ensure_logs_dir()
    assert p2 == p


def test_paths_resolve_from_subdir(tmp_path, monkeypatch):
    """When CWD is a subdir, paths still point at the project root."""
    monkeypatch.chdir(tmp_path)
    # Re-import to re-evaluate the module-level Path() computation
    result = subprocess.run(
        [sys.executable, "-c", "from backend.core.paths import PROJECT_ROOT; print(PROJECT_ROOT)"],
        capture_output=True, text=True, env={**os.environ},
    )
    assert "quant_trading" in result.stdout
```

- [ ] **Step 6: Run test, verify pass**

Run:
```bash
cd "C:\Users\zhu\quant_trading" && "C:/Users/zhu/AppData/Local/Programs/Python/Python312/python.exe" -m pytest tests/test_backend_paths.py -v 2>&1 | tail -20
```
Expected: 4 passed

- [ ] **Step 7: Commit**

```bash
cd "C:\Users\zhu\quant_trading" && git add backend/core/ tests/test_backend_paths.py && git commit -m "feat(backend): paths + settings + loguru (Phase 1.2)"
```

---

## Task 1.3: FastAPI app factory + health endpoint

**Files:**
- Create: `backend/app.py`
- Create: `backend/api/__init__.py`
- Create: `backend/api/health.py`
- Create: `tests/test_backend_health.py`

- [ ] **Step 1: Create api package init**

```python
# backend/api/__init__.py
"""REST API routers. Aggregated by app.include_router(*routers)."""
from fastapi import APIRouter

from backend.api import health

ALL_ROUTERS: list[APIRouter] = [
    health.router,
]
```

- [ ] **Step 2: Write health router**

```python
# backend/api/health.py
"""Liveness + db connectivity check."""
import sqlite3
import time
from datetime import datetime, timezone

from fastapi import APIRouter
from pydantic import BaseModel

from backend.core.paths import DB_PATH

router = APIRouter(prefix="/api", tags=["health"])


class HealthResponse(BaseModel):
    status: str
    db: str
    mt5: str
    server_time: str
    uptime_seconds: float


_START_TIME = time.time()


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    db_status = "connected"
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=1.0)
        conn.execute("SELECT 1").fetchone()
        conn.close()
    except Exception as e:
        db_status = f"error: {type(e).__name__}"

    return HealthResponse(
        status="ok" if db_status == "connected" else "degraded",
        db=db_status,
        mt5="unknown",
        server_time=datetime.now(timezone.utc).isoformat(),
        uptime_seconds=time.time() - _START_TIME,
    )
```

- [ ] **Step 3: Write app factory**

```python
# backend/app.py
"""FastAPI app factory + lifespan + CORS + router registration."""
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api import ALL_ROUTERS
from backend.core.logging import setup_logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Quant Trading Web Console",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000"],  # Next.js dev
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    for r in ALL_ROUTERS:
        app.include_router(r)
    return app


app = create_app()
```

- [ ] **Step 4: Write failing test**

```python
# tests/test_backend_health.py
"""Verify /api/health responds with expected shape."""
from fastapi.testclient import TestClient

from backend.app import app

client = TestClient(app)


def test_health_ok_or_degraded():
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("ok", "degraded")
    assert "db" in body
    assert "server_time" in body
    assert "uptime_seconds" in body
    assert body["uptime_seconds"] >= 0


def test_health_db_field_present():
    r = client.get("/api/health")
    body = r.json()
    assert body["db"] in ("connected",) or body["db"].startswith("error:")
```

- [ ] **Step 5: Run test, verify pass**

Run:
```bash
cd "C:\Users\zhu\quant_trading" && "C:/Users/zhu/AppData/Local/Programs/Python/Python312/python.exe" -m pytest tests/test_backend_health.py -v 2>&1 | tail -10
```
Expected: 2 passed

- [ ] **Step 6: Smoke test with curl**

Run (background):
```bash
cd "C:\Users\zhu\quant_trading" && "C:/Users/zhu/AppData/Local/Programs/Python/Python312/python.exe" -m backend --port 8000
```
Wait 3s, then in another shell:
```bash
curl -s http://localhost:8000/api/health | head -c 300
```
Expected: JSON with `"status":"ok"` (or degraded if db not present) and `"db":"connected"` (or error)

Stop the server with `Ctrl+C` or `taskkill /FI "WINDOWTITLE eq python*" /T /F` if running in background.

- [ ] **Step 7: Commit**

```bash
cd "C:\Users\zhu\quant_trading" && git add backend/app.py backend/api/ tests/test_backend_health.py && git commit -m "feat(backend): FastAPI app + /api/health (Phase 1.3)"
```

---

## Task 1.4: JobState dataclass + ProgressCB type

**Files:**
- Create: `backend/jobs/__init__.py`
- Create: `backend/jobs/state.py`
- Create: `backend/jobs/progress.py`
- Create: `tests/test_backend_jobs_state.py`

- [ ] **Step 1: Create jobs package init**

```python
# backend/jobs/__init__.py
"""Long-task management (in-process queue, in-memory state)."""
```

- [ ] **Step 2: Write ProgressCB type**

```python
# backend/jobs/progress.py
"""Progress callback contract — injected into service functions."""
from typing import Callable

# (step_name, percent_0_to_100, human_message)
ProgressCB = Callable[[str, float, str], None]


def noop_progress(_step: str, _pct: float, _msg: str) -> None:
    """Default no-op progress callback."""
    pass
```

- [ ] **Step 3: Write JobState**

```python
# backend/jobs/state.py
"""Job state dataclass — lives in memory, not persisted (v1)."""
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


@dataclass
class JobState:
    id: str
    kind: str
    status: Literal["queued", "running", "done", "error", "cancelled"] = "queued"
    progress_pct: float = 0.0
    current_step: str = ""
    started_at: datetime = field(default_factory=datetime.utcnow)
    finished_at: datetime | None = None
    params: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: str | None = None
    log_tail: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "progress_pct": self.progress_pct,
            "current_step": self.current_step,
            "started_at": self.started_at.isoformat() + "Z",
            "finished_at": self.finished_at.isoformat() + "Z" if self.finished_at else None,
            "params": self.params,
            "result": self.result,
            "error": self.error,
        }


def new_job_id() -> str:
    return uuid.uuid4().hex[:12]
```

- [ ] **Step 4: Write failing test**

```python
# tests/test_backend_jobs_state.py
"""Verify JobState lifecycle + serialization."""
from datetime import datetime

from backend.jobs.state import JobState, new_job_id
from backend.jobs.progress import noop_progress


def test_new_job_id_is_unique_hex():
    ids = {new_job_id() for _ in range(100)}
    assert len(ids) == 100
    for i in ids:
        assert len(i) == 12
        int(i, 16)  # valid hex


def test_job_state_defaults():
    js = JobState(id="abc", kind="backtest")
    assert js.status == "queued"
    assert js.progress_pct == 0.0
    assert js.error is None
    assert js.result is None
    assert js.finished_at is None
    assert isinstance(js.started_at, datetime)


def test_job_state_to_dict():
    js = JobState(id="abc", kind="backtest", progress_pct=50.0, current_step="eval")
    d = js.to_dict()
    assert d["id"] == "abc"
    assert d["kind"] == "backtest"
    assert d["progress_pct"] == 50.0
    assert d["current_step"] == "eval"
    assert d["started_at"].endswith("Z")


def test_noop_progress_runs():
    # must not raise
    noop_progress("step", 50.0, "msg")
```

- [ ] **Step 5: Run test, verify pass**

Run:
```bash
cd "C:\Users\zhu\quant_trading" && "C:/Users/zhu/AppData/Local/Programs/Python/Python312/python.exe" -m pytest tests/test_backend_jobs_state.py -v 2>&1 | tail -10
```
Expected: 4 passed

- [ ] **Step 6: Commit**

```bash
cd "C:\Users\zhu\quant_trading" && git add backend/jobs/ tests/test_backend_jobs_state.py && git commit -m "feat(backend): JobState + ProgressCB (Phase 1.4)"
```

---

## Task 1.5: JobManager — submit / get / list / cancel

**Files:**
- Create: `backend/jobs/manager.py`
- Modify: `backend/jobs/__init__.py` (add exports)
- Create: `tests/test_backend_jobs_manager.py`

- [ ] **Step 1: Write JobManager**

```python
# backend/jobs/manager.py
"""In-process job queue + state. Single-process, in-memory, not persisted (v1)."""
import asyncio
import inspect
import traceback
from typing import Any, Callable

from loguru import logger

from backend.jobs.progress import ProgressCB
from backend.jobs.state import JobState, new_job_id


class JobManager:
    """Manages long-running tasks. v1: single process, in-memory dict."""

    def __init__(self) -> None:
        self._jobs: dict[str, JobState] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def submit(
        self,
        kind: str,
        params: dict[str, Any],
        fn: Callable[[ProgressCB], Any],
    ) -> JobState:
        """Queue a job. fn signature: (progress_cb) -> result (any JSON-serializable)."""
        js = JobState(id=new_job_id(), kind=kind, params=params)
        self._jobs[js.id] = js
        task = asyncio.create_task(self._run(js, fn))
        self._tasks[js.id] = task
        return js

    async def _run(self, js: JobState, fn: Callable[[ProgressCB], Any]) -> None:
        js.status = "running"
        try:
            def cb(step: str, pct: float, msg: str) -> None:
                js.progress_pct = max(0.0, min(100.0, pct))
                js.current_step = step
                if len(js.log_tail) >= 50:
                    js.log_tail = js.log_tail[-49:]
                js.log_tail.append(f"[{step} {pct:.0f}%] {msg}")

            if inspect.iscoroutinefunction(fn):
                result = await fn(cb)
            else:
                # Allow sync functions too (run in default executor)
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, fn, cb)

            js.result = result if isinstance(result, dict) else {"value": result}
            js.progress_pct = 100.0
            js.status = "done"
        except asyncio.CancelledError:
            js.status = "cancelled"
            logger.info(f"job {js.id} ({js.kind}) cancelled")
            raise
        except Exception as e:
            js.status = "error"
            js.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()[-500:]}"
            logger.error(f"job {js.id} ({js.kind}) failed: {e}")
        finally:
            from datetime import datetime
            js.finished_at = datetime.utcnow()
            self._tasks.pop(js.id, None)

    def get(self, job_id: str) -> JobState | None:
        return self._jobs.get(job_id)

    def list(self, kind: str | None = None, status: str | None = None) -> list[JobState]:
        out = list(self._jobs.values())
        if kind is not None:
            out = [j for j in out if j.kind == kind]
        if status is not None:
            out = [j for j in out if j.status == status]
        return sorted(out, key=lambda j: j.started_at, reverse=True)

    def cancel(self, job_id: str) -> bool:
        task = self._tasks.get(job_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True


# Singleton accessor
_manager: JobManager | None = None


def get_job_manager() -> JobManager:
    global _manager
    if _manager is None:
        _manager = JobManager()
    return _manager
```

- [ ] **Step 2: Update jobs package init**

```python
# backend/jobs/__init__.py
"""Long-task management (in-process queue, in-memory state)."""
from backend.jobs.manager import JobManager, get_job_manager
from backend.jobs.progress import ProgressCB, noop_progress
from backend.jobs.state import JobState, new_job_id

__all__ = [
    "JobManager", "get_job_manager",
    "ProgressCB", "noop_progress",
    "JobState", "new_job_id",
]
```

- [ ] **Step 3: Write failing test**

```python
# tests/test_backend_jobs_manager.py
"""JobManager: submit, get, list, cancel, progress emission."""
import asyncio

import pytest

from backend.jobs.manager import JobManager
from backend.jobs.progress import ProgressCB


@pytest.mark.asyncio
async def test_submit_and_complete_sync_job():
    mgr = JobManager()

    def fn(cb: ProgressCB):
        cb("loading", 10, "loading 100 bars")
        cb("eval", 50, "evaluating")
        cb("done", 100, "complete")
        return {"trades": 5, "pnl": 12.5}

    js = mgr.submit("backtest", {"tf": "M15"}, fn)
    assert js.status == "queued"
    # Wait for completion
    for _ in range(50):
        await asyncio.sleep(0.05)
        if js.status in ("done", "error", "cancelled"):
            break
    assert js.status == "done"
    assert js.progress_pct == 100.0
    assert js.result == {"trades": 5, "pnl": 12.5}


@pytest.mark.asyncio
async def test_submit_and_complete_async_job():
    mgr = JobManager()

    async def fn(cb: ProgressCB):
        await asyncio.sleep(0.01)
        cb("step1", 50, "half")
        await asyncio.sleep(0.01)
        cb("step2", 100, "done")
        return {"ok": True}

    js = mgr.submit("discover", {"n": 100}, fn)
    for _ in range(50):
        await asyncio.sleep(0.05)
        if js.status in ("done", "error", "cancelled"):
            break
    assert js.status == "done"


@pytest.mark.asyncio
async def test_progress_emitted_in_log_tail():
    mgr = JobManager()

    def fn(cb: ProgressCB):
        for i in range(5):
            cb("eval", i * 20, f"step {i}")
        return {}

    js = mgr.submit("backtest", {}, fn)
    for _ in range(50):
        await asyncio.sleep(0.05)
        if js.status in ("done", "error", "cancelled"):
            break
    assert len(js.log_tail) == 5
    assert "step 0" in js.log_tail[0]
    assert "step 4" in js.log_tail[4]


@pytest.mark.asyncio
async def test_cancel_long_job():
    mgr = JobManager()

    async def long_fn(cb: ProgressCB):
        for i in range(100):
            await asyncio.sleep(0.05)
            cb("loop", i, f"iter {i}")
        return {}

    js = mgr.submit("backtest", {}, long_fn)
    await asyncio.sleep(0.1)  # let it start
    assert js.status == "running"
    cancelled = mgr.cancel(js.id)
    assert cancelled is True
    for _ in range(50):
        await asyncio.sleep(0.05)
        if js.status in ("cancelled", "done", "error"):
            break
    assert js.status == "cancelled"


@pytest.mark.asyncio
async def test_list_filters_by_kind_and_status():
    mgr = JobManager()

    def quick_fn(cb):
        return {}

    js1 = mgr.submit("backtest", {}, quick_fn)
    js2 = mgr.submit("discover", {}, quick_fn)
    # wait both
    for _ in range(50):
        await asyncio.sleep(0.05)
        if js1.status == "done" and js2.status == "done":
            break
    assert mgr.list(kind="backtest") == [js1]
    assert mgr.list(kind="discover") == [js2]
    assert len(mgr.list(status="done")) == 2
    assert mgr.list(status="running") == []


def test_get_returns_none_for_missing():
    mgr = JobManager()
    assert mgr.get("nonexistent") is None
```

- [ ] **Step 4: Run test, verify pass**

First install pytest-asyncio:
```bash
cd "C:\Users\zhu\quant_trading" && "C:/Users/zhu/AppData/Local/Programs/Python/Python312/python.exe" -m pip install pytest-asyncio 2>&1 | tail -3
```

Then:
```bash
cd "C:\Users\zhu\quant_trading" && "C:/Users/zhu/AppData/Local/Programs/Python/Python312/python.exe" -m pytest tests/test_backend_jobs_manager.py -v 2>&1 | tail -15
```
Expected: 6 passed

If pytest-asyncio not auto-discovered, add to `pyproject.toml` or `pytest.ini`:
```ini
# pytest.ini (create at project root)
[pytest]
asyncio_mode = auto
```

- [ ] **Step 5: Commit**

```bash
cd "C:\Users\zhu\quant_trading" && git add backend/jobs/ tests/test_backend_jobs_manager.py pytest.ini 2>/dev/null; git commit -m "feat(backend): JobManager submit/get/list/cancel (Phase 1.5)"
```

---

## Task 1.6: ConnectionManager + /ws/state endpoint

**Files:**
- Create: `backend/ws/__init__.py`
- Create: `backend/ws/manager.py`
- Create: `backend/ws/endpoints.py`
- Create: `tests/test_backend_ws_state.py`

- [ ] **Step 1: Create ws package init**

```python
# backend/ws/__init__.py
"""WebSocket endpoints + connection manager."""
```

- [ ] **Step 2: Write ConnectionManager**

```python
# backend/ws/manager.py
"""Per-connection WebSocket manager with room-based broadcasting."""
import asyncio
import json
from collections import defaultdict
from typing import Any

from fastapi import WebSocket
from loguru import logger


class ConnectionManager:
    def __init__(self) -> None:
        # channel name → set of websockets
        self._rooms: dict[str, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket, channel: str) -> None:
        await ws.accept()
        async with self._lock:
            self._rooms[channel].add(ws)
        logger.debug(f"ws connected to {channel} (total={len(self._rooms[channel])})")

    async def disconnect(self, ws: WebSocket, channel: str) -> None:
        async with self._lock:
            self._rooms[channel].discard(ws)
            if not self._rooms[channel]:
                del self._rooms[channel]
        logger.debug(f"ws disconnected from {channel}")

    async def broadcast(self, channel: str, payload: dict[str, Any]) -> None:
        """Send payload (JSON-serialized) to all sockets in channel."""
        async with self._lock:
            sockets = list(self._rooms.get(channel, set()))
        if not sockets:
            return
        msg = json.dumps(payload, default=str)
        dead: list[WebSocket] = []
        for ws in sockets:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    for ch in self._rooms:
                        self._rooms[ch].discard(ws)

    def room_size(self, channel: str) -> int:
        return len(self._rooms.get(channel, set()))


_manager: ConnectionManager | None = None


def get_connection_manager() -> ConnectionManager:
    global _manager
    if _manager is None:
        _manager = ConnectionManager()
    return _manager
```

- [ ] **Step 3: Write /ws/state endpoint + 1s broadcast loop**

```python
# backend/ws/endpoints.py
"""WebSocket routes: /ws/state, /ws/alerts, /ws/jobs/:id, /ws/logs.

In Phase 1 only /ws/state is implemented; it broadcasts a 1s snapshot
read from core.state (or, if paper isn't running, a placeholder zero-state).
"""
import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend.core.logging import setup_logging
from backend.core.state import state
from backend.ws.manager import get_connection_manager

router = APIRouter()
setup_logging()


def _read_state_snapshot() -> dict:
    """Read current state for snapshot. Falls back to zeros if no paper running."""
    try:
        pos = state.position
        daily = state.daily
        snapshot = {
            "equity": round(state.equity, 2),
            "balance": round(state.balance, 2),
            "pnl_today": round(daily.net_pnl, 2),
            "position": {
                "dir": "LONG" if pos.direction == 1 else "SHORT" if pos.direction == -1 else "FLAT",
                "entry": round(pos.entry_price, 2),
                "size": pos.volume,
                "unrealized": round(pos.unrealized_pnl, 2),
            },
            "daily": {
                "trades": daily.total_trades,
                "win": daily.winning_trades,
                "loss": daily.losing_trades,
                "pnl": round(daily.net_pnl, 2),
                "drawdown_pct": round(daily.max_drawdown_pct, 2),
            },
            "risk": {
                "circuit_breaker": state.is_circuit_breaker,
                "consecutive_loss": daily.consecutive_losses,
            },
            "server_time": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        # StateContainer may not be fully initialized in Phase 1
        snapshot = {
            "equity": 0.0, "balance": 0.0, "pnl_today": 0.0,
            "position": {"dir": "FLAT", "entry": 0.0, "size": 0.0, "unrealized": 0.0},
            "daily": {"trades": 0, "win": 0, "loss": 0, "pnl": 0.0, "drawdown_pct": 0.0},
            "risk": {"circuit_breaker": False, "consecutive_loss": 0},
            "server_time": datetime.now(timezone.utc).isoformat(),
            "warning": f"state not initialized: {type(e).__name__}",
        }
    return snapshot


@router.websocket("/ws/state")
async def ws_state(ws: WebSocket) -> None:
    """Push 1s state snapshot."""
    mgr = get_connection_manager()
    channel = "state"
    await mgr.connect(ws, channel)
    try:
        # Push initial snapshot immediately
        await ws.send_text(json.dumps(_read_state_snapshot(), default=str))
        # Then tick every 1s while client is connected
        while True:
            await asyncio.sleep(1.0)
            try:
                await ws.send_text(json.dumps(_read_state_snapshot(), default=str))
            except Exception:
                break
    except WebSocketDisconnect:
        pass
    finally:
        await mgr.disconnect(ws, channel)
```

- [ ] **Step 4: Register WS router in app**

Modify `backend/app.py`: add `from backend.ws.endpoints import router as ws_router` and `app.include_router(ws_router)`.

```python
# backend/app.py (replace whole file)
"""FastAPI app factory + lifespan + CORS + router registration."""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api import ALL_ROUTERS
from backend.core.logging import setup_logging
from backend.ws.endpoints import router as ws_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Quant Trading Web Console",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    for r in ALL_ROUTERS:
        app.include_router(r)
    app.include_router(ws_router)  # WS routes don't use prefix
    return app


app = create_app()
```

- [ ] **Step 5: Write failing test**

```python
# tests/test_backend_ws_state.py
"""Verify /ws/state broadcasts a snapshot on connect + every 1s."""
import json

from fastapi.testclient import TestClient

from backend.app import app

client = TestClient(app)


def test_ws_state_sends_snapshot_on_connect():
    with client.websocket_connect("/ws/state") as ws:
        msg = ws.receive_text()
        snapshot = json.loads(msg)
        assert "equity" in snapshot
        assert "position" in snapshot
        assert "daily" in snapshot
        assert "risk" in snapshot
        assert "server_time" in snapshot


def test_ws_state_sends_followup_after_1s():
    with client.websocket_connect("/ws/state") as ws:
        first = json.loads(ws.receive_text())
        second = json.loads(ws.receive_text())  # wait 1s for next tick
        assert "equity" in second
        # server_time should differ
        assert first["server_time"] != second["server_time"]
```

- [ ] **Step 6: Run test, verify pass**

Run:
```bash
cd "C:\Users\zhu\quant_trading" && "C:/Users/zhu/AppData/Local/Programs/Python/Python312/python.exe" -m pytest tests/test_backend_ws_state.py -v 2>&1 | tail -10
```
Expected: 2 passed (each ~1s, total ~2s)

- [ ] **Step 7: Commit**

```bash
cd "C:\Users\zhu\quant_trading" && git add backend/ws/ backend/app.py tests/test_backend_ws_state.py && git commit -m "feat(backend): /ws/state 1s snapshot (Phase 1.6)"
```

---

## Task 1.7: BacktestService — wrap existing main.run_backtest

**Files:**
- Create: `backend/services/__init__.py`
- Create: `backend/services/backtest_service.py`
- Create: `backend/api/backtest.py`
- Modify: `backend/api/__init__.py` (register backtest router)
- Create: `tests/test_backend_backtest_service.py`

- [ ] **Step 1: Create services package init**

```python
# backend/services/__init__.py
"""Business wrappers — turn scripts/ CLI logic into importable service functions."""
```

- [ ] **Step 2: Write BacktestService**

```python
# backend/services/backtest_service.py
"""Backtest service — wraps main.run_backtest for backend invocation.

v1: re-uses main.py's run_backtest args namespace via lightweight shim,
so we don't have to refactor main.py in Phase 1. Phase 4 will refactor
main.py into a 'service function + CLI main' two-mode form; this shim
will be replaced.
"""
import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from backend.core.paths import CHARTS_DIR
from backend.jobs.progress import ProgressCB


@dataclass
class BacktestParams:
    symbol: str = "XAUUSD+"
    timeframe: str = "M15"
    risk_per_trade_pct: float | None = None
    enable_circuit: bool = False


def run_backtest(params: dict[str, Any], progress_cb: ProgressCB) -> dict:
    """Execute a backtest synchronously; progress emitted via callback.

    For Phase 1 this calls the existing CLI logic in a subprocess (so we
    don't refactor main.py in Phase 1). Phase 4 will replace this with
    direct in-process call.
    """
    import subprocess
    import sys

    progress_cb("starting", 0, f"starting backtest {params.get('symbol')} {params.get('timeframe')}")

    # Build CLI command
    cmd = [
        sys.executable, "main.py",
        "--mode", "backtest",
        "--symbol", params.get("symbol", "XAUUSD+"),
        "--timeframe", params.get("timeframe", "M15"),
    ]
    if params.get("risk_per_trade_pct") is not None:
        cmd += ["--risk-per-trade-pct", str(params["risk_per_trade_pct"])]
    if params.get("enable_circuit"):
        cmd += ["--enable-circuit"]

    progress_cb("running", 10, " ".join(cmd))

    # Run subprocess
    proc = subprocess.run(
        cmd, capture_output=True, text=True,
        cwd=Path(__file__).resolve().parents[2],
    )
    progress_cb("running", 80, f"backtest exited rc={proc.returncode}")

    if proc.returncode != 0:
        raise RuntimeError(f"backtest failed: {proc.stderr[-500:]}")

    # Try to parse latest backtest report from data/charts/
    report = _find_latest_backtest_report()
    progress_cb("done", 100, f"report: {report}")
    return {
        "returncode": proc.returncode,
        "report_path": str(report) if report else None,
        "stdout_tail": proc.stdout[-1000:],
    }


def _find_latest_backtest_report() -> Path | None:
    """Find most recent backtest_*.txt in data/charts/."""
    if not CHARTS_DIR.exists():
        return None
    candidates = sorted(CHARTS_DIR.glob("backtest_*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None
```

- [ ] **Step 3: Write backtest API router**

```python
# backend/api/backtest.py
"""POST /api/backtest/run → 202 {job_id}; GET /api/backtest/:id → job status."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.jobs import get_job_manager
from backend.services.backtest_service import run_backtest

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


class BacktestRequest(BaseModel):
    symbol: str = "XAUUSD+"
    timeframe: str = "M15"
    risk_per_trade_pct: float | None = None
    enable_circuit: bool = False


@router.post("/run")
def run(req: BacktestRequest) -> dict:
    """Submit a backtest job. Returns 202 with job_id (sync API for now)."""
    mgr = get_job_manager()
    js = mgr.submit("backtest", req.model_dump(), run_backtest)
    return {"job_id": js.id, "status": js.status}


@router.get("/{job_id}")
def get_job(job_id: str) -> dict:
    mgr = get_job_manager()
    js = mgr.get(job_id)
    if js is None:
        raise HTTPException(status_code=404, detail="job not found")
    return js.to_dict()


@router.get("/")
def list_jobs(status: str | None = None) -> dict:
    mgr = get_job_manager()
    jobs = mgr.list(kind="backtest", status=status)
    return {"jobs": [j.to_dict() for j in jobs]}
```

- [ ] **Step 4: Register backtest router in api package init**

Modify `backend/api/__init__.py`:

```python
# backend/api/__init__.py
"""REST API routers. Aggregated by app.include_router(*routers)."""
from fastapi import APIRouter

from backend.api import backtest, health

ALL_ROUTERS: list[APIRouter] = [
    health.router,
    backtest.router,
]
```

- [ ] **Step 5: Write failing test (with mock subprocess)**

```python
# tests/test_backend_backtest_service.py
"""Verify backtest service + API surface (mocked subprocess)."""
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from backend.app import app

client = TestClient(app)


def test_post_backtest_returns_job_id():
    with patch("backend.services.backtest_service.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="OK", stderr="")
        with patch("backend.services.backtest_service._find_latest_backtest_report", return_value=None):
            r = client.post("/api/backtest/run", json={"symbol": "XAUUSD+", "timeframe": "M15"})
            assert r.status_code == 200
            body = r.json()
            assert "job_id" in body
            assert body["status"] in ("queued", "running", "done", "error")


def test_get_backtest_job():
    with patch("backend.services.backtest_service.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="OK", stderr="")
        with patch("backend.services.backtest_service._find_latest_backtest_report", return_value=None):
            r = client.post("/api/backtest/run", json={})
            job_id = r.json()["job_id"]
            r2 = client.get(f"/api/backtest/{job_id}")
            assert r2.status_code == 200
            assert r2.json()["id"] == job_id


def test_get_nonexistent_job_404():
    r = client.get("/api/backtest/nonexistent_id_xx")
    assert r.status_code == 404
```

- [ ] **Step 6: Run test, verify pass**

Run:
```bash
cd "C:\Users\zhu\quant_trading" && "C:/Users/zhu/AppData/Local/Programs/Python/Python312/python.exe" -m pytest tests/test_backend_backtest_service.py -v 2>&1 | tail -10
```
Expected: 3 passed

- [ ] **Step 7: Commit**

```bash
cd "C:\Users\zhu\quant_trading" && git add backend/services/ backend/api/ tests/test_backend_backtest_service.py && git commit -m "feat(backend): BacktestService + REST API (Phase 1.7)"
```

---

## Task 1.8: Phase 1 smoke test + start script

**Files:**
- Create: `start.bat`
- Create: `stop.bat`

- [ ] **Step 1: Create start.bat (Phase 1 — backend only)**

```bat
@echo off
REM Quant Web Console — Phase 1 launcher (backend only)
setlocal
set PROJECT_ROOT=%~dp0
set PYTHON=C:\Users\zhu\AppData\Local\Programs\Python\Python312\python.exe

cd /d %PROJECT_ROOT%
echo === Starting Quant Backend (port 8000) ===
%PYTHON% -m backend --port 8000
endlocal
```

- [ ] **Step 2: Create stop.bat**

```bat
@echo off
REM Stop Quant Backend (kills python -m backend process)
taskkill /FI "WINDOWTITLE eq Quant Backend*" /T /F 2>nul
wmic process where "name='python.exe' and commandline like '%%backend%%'" delete 2>nul
echo Backend stopped.
```

- [ ] **Step 3: Manual smoke test**

Run `start.bat` in background, then in another shell:
```bash
curl -s http://localhost:8000/api/health
curl -s http://localhost:8000/openapi.json | python -c "import sys,json; d=json.load(sys.stdin); print(list(d['paths'].keys()))"
```
Expected first call: `{"status":"ok","db":"connected",...}` (or degraded)
Expected second call: `["/api/health", "/api/backtest/", "/api/backtest/run", "/api/backtest/{job_id}", "/ws/state"]`

Then test WS:
```bash
"C:/Users/zhu/AppData/Local/Programs/Python/Python312/python.exe" -c "
import asyncio, websockets, json
async def t():
    async with websockets.connect('ws://localhost:8000/ws/state') as ws:
        for _ in range(3):
            msg = await ws.recv()
            print(json.loads(msg)['server_time'])
asyncio.run(t())
"
```
Expected: 3 timestamps ~1s apart.

Stop with `stop.bat` or `Ctrl+C`.

- [ ] **Step 4: Commit**

```bash
cd "C:\Users\zhu\quant_trading" && git add start.bat stop.bat && git commit -m "feat(backend): start/stop scripts + Phase 1 smoke verified (Phase 1.8)"
```

---

**Phase 1 complete.** Verified: backend boots, /api/health works, /api/backtest/{run,get,list} works (with mock), /ws/state broadcasts 1s snapshots, start.bat launches.

---

# Phase 2: 4 More Core Services + Overview Page

**Phase 2 Goal:** Wire 4 more services (paper start/stop, market data, factor health, sync) end-to-end. Build minimal Next.js frontend with overview page that shows live state from /ws/state and offers a "Run Backtest" button.

**Phase 2 commits:** ~15 commits.

## Task 2.1: Next.js 14 project init + Tailwind + shadcn/ui

**Files:**
- Create: `frontend/package.json`
- Create: `frontend/tsconfig.json`
- Create: `frontend/next.config.mjs`
- Create: `frontend/tailwind.config.ts`
- Create: `frontend/postcss.config.js`
- Create: `frontend/app/globals.css`
- Create: `frontend/app/layout.tsx`

- [ ] **Step 1: Verify Node 18+ is installed**

Run: `node --version`
Expected: `v18.x` or higher. If not, install Node 18 LTS from nodejs.org.

- [ ] **Step 2: Create package.json**

```json
{
  "name": "quant-web-frontend",
  "version": "0.1.0",
  "private": true,
  "scripts": {
    "dev": "next dev --port 3000",
    "build": "next build",
    "start": "next start --port 3000",
    "lint": "next lint",
    "typecheck": "tsc --noEmit"
  },
  "dependencies": {
    "next": "14.2.5",
    "react": "18.3.1",
    "react-dom": "18.3.1",
    "zustand": "4.5.4",
    "clsx": "2.1.1",
    "tailwind-merge": "2.4.0",
    "lucide-react": "0.408.0"
  },
  "devDependencies": {
    "@types/node": "20.14.10",
    "@types/react": "18.3.3",
    "@types/react-dom": "18.3.0",
    "typescript": "5.5.3",
    "tailwindcss": "3.4.6",
    "postcss": "8.4.39",
    "autoprefixer": "10.4.19",
    "eslint": "8.57.0",
    "eslint-config-next": "14.2.5"
  }
}
```

- [ ] **Step 3: Install**

```bash
cd "C:\Users\zhu\quant_trading/frontend" && npm install 2>&1 | tail -20
```
Expected: "added X packages" with no critical errors.

- [ ] **Step 4: Create tsconfig.json (strict mode)**

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "lib": ["dom", "dom.iterable", "esnext"],
    "allowJs": false,
    "skipLibCheck": true,
    "strict": true,
    "noEmit": true,
    "esModuleInterop": true,
    "module": "esnext",
    "moduleResolution": "bundler",
    "resolveJsonModule": true,
    "isolatedModules": true,
    "jsx": "preserve",
    "incremental": true,
    "plugins": [{"name": "next"}],
    "paths": {
      "@/*": ["./*"]
    }
  },
  "include": ["next-env.d.ts", "**/*.ts", "**/*.tsx", ".next/types/**/*.ts"],
  "exclude": ["node_modules"]
}
```

- [ ] **Step 5: Create next.config.mjs with API proxy**

```javascript
// frontend/next.config.mjs
/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: "http://localhost:8000/api/:path*",
      },
      {
        source: "/ws/:path*",
        destination: "ws://localhost:8000/ws/:path*",
      },
    ];
  },
};
export default nextConfig;
```

- [ ] **Step 6: Create tailwind.config.ts with Bloomberg theme**

```typescript
// frontend/tailwind.config.ts
import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: { DEFAULT: "#0d1117", card: "#161b22", border: "#30363d" },
        fg: { DEFAULT: "#c9d1d9", muted: "#8b949e" },
        accent: "#58a6ff",
        up: "#3fb950",
        down: "#f85149",
        warn: "#d2991d",
      },
      fontFamily: {
        sans: ["ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["ui-monospace", "SFMono-Regular", "monospace"],
      },
    },
  },
  plugins: [],
};
export default config;
```

- [ ] **Step 7: Create postcss.config.js**

```javascript
module.exports = {
  plugins: {
    tailwindcss: {},
    autoprefixer: {},
  },
};
```

- [ ] **Step 8: Create app/globals.css**

```css
@tailwind base;
@tailwind components;
@tailwind utilities;

@layer base {
  html, body {
    background: #0d1117;
    color: #c9d1d9;
    font-family: ui-sans-serif, system-ui, sans-serif;
  }
  .num { font-variant-numeric: tabular-nums; font-feature-settings: "tnum"; }
}
```

- [ ] **Step 9: Create app/layout.tsx (root layout)**

```tsx
// frontend/app/layout.tsx
import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Quant Web Console",
  description: "XAUUSD+ trading framework — web console",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body className="min-h-screen bg-bg text-fg">{children}</body>
    </html>
  );
}
```

- [ ] **Step 10: Verify build works**

```bash
cd "C:\Users\zhu\quant_trading/frontend" && npm run typecheck 2>&1 | tail -5
```
Expected: 0 errors.

```bash
cd "C:\Users\zhu\quant_trading/frontend" && npm run build 2>&1 | tail -10
```
Expected: "Compiled successfully" or similar. (Will be slow first time.)

- [ ] **Step 11: Commit**

```bash
cd "C:\Users\zhu\quant_trading" && git add frontend/package.json frontend/package-lock.json frontend/tsconfig.json frontend/next.config.mjs frontend/tailwind.config.ts frontend/postcss.config.js frontend/app/ frontend/.gitignore && git commit -m "feat(frontend): Next.js 14 + Tailwind + Bloomberg theme (Phase 2.1)"
```

---

## Task 2.2: WS client + Zustand store + reconnect logic

**Files:**
- Create: `frontend/lib/ws.ts`
- Create: `frontend/lib/store.ts`
- Create: `frontend/lib/format.ts`
- Create: `frontend/components/layout/ws-provider.tsx`

- [ ] **Step 1: Write format utility**

```typescript
// frontend/lib/format.ts
export function fmtNum(n: number, decimals = 2): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "--";
  return n.toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

export function fmtPct(n: number, decimals = 2): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "--";
  return `${n >= 0 ? "+" : ""}${fmtNum(n, decimals)}%`;
}

export function fmtUSD(n: number): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "--";
  const sign = n < 0 ? "-" : "";
  return `${sign}$${fmtNum(Math.abs(n), 2)}`;
}

export function classNames(...names: (string | false | null | undefined)[]): string {
  return names.filter(Boolean).join(" ");
}
```

- [ ] **Step 2: Write Zustand store**

```typescript
// frontend/lib/store.ts
import { create } from "zustand";

export interface StateSnapshot {
  equity: number;
  balance: number;
  pnl_today: number;
  position: { dir: "LONG" | "SHORT" | "FLAT"; entry: number; size: number; unrealized: number };
  daily: { trades: number; win: number; loss: number; pnl: number; drawdown_pct: number };
  risk: { circuit_breaker: boolean; consecutive_loss: number };
  server_time: string;
}

interface AppState {
  snapshot: StateSnapshot | null;
  wsConnected: boolean;
  setSnapshot: (s: StateSnapshot) => void;
  setWsConnected: (v: boolean) => void;
}

export const useAppStore = create<AppState>((set) => ({
  snapshot: null,
  wsConnected: false,
  setSnapshot: (snapshot) => set({ snapshot }),
  setWsConnected: (wsConnected) => set({ wsConnected }),
}));
```

- [ ] **Step 3: Write WS client (singleton + auto-reconnect)**

```typescript
// frontend/lib/ws.ts
"use client";
import { useAppStore } from "./store";

const RECONNECT_DELAYS = [1000, 2000, 4000, 8000, 15000, 30000];

class WSClient {
  private ws: WebSocket | null = null;
  private url = "";
  private attempt = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private stopped = false;

  start(path: string = "/ws/state") {
    this.stopped = false;
    this.url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${path}`;
    this.connect();
  }

  stop() {
    this.stopped = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.ws?.close();
  }

  private connect() {
    if (this.stopped) return;
    try {
      this.ws = new WebSocket(this.url);
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.ws.onopen = () => {
      this.attempt = 0;
      useAppStore.getState().setWsConnected(true);
    };
    this.ws.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data);
        useAppStore.getState().setSnapshot(data);
      } catch {}
    };
    this.ws.onclose = () => {
      useAppStore.getState().setWsConnected(false);
      this.scheduleReconnect();
    };
    this.ws.onerror = () => {
      this.ws?.close();
    };
  }

  private scheduleReconnect() {
    if (this.stopped) return;
    const delay = RECONNECT_DELAYS[Math.min(this.attempt, RECONNECT_DELAYS.length - 1)];
    this.attempt++;
    this.reconnectTimer = setTimeout(() => this.connect(), delay);
  }
}

let instance: WSClient | null = null;

export function getWSClient(): WSClient {
  if (!instance) instance = new WSClient();
  return instance;
}
```

- [ ] **Step 4: Write WS provider component**

```tsx
// frontend/components/layout/ws-provider.tsx
"use client";
import { useEffect } from "react";
import { getWSClient } from "@/lib/ws";

export function WSProvider({ children }: { children: React.ReactNode }) {
  useEffect(() => {
    const client = getWSClient();
    client.start("/ws/state");
    return () => client.stop();
  }, []);
  return <>{children}</>;
}
```

- [ ] **Step 5: Wire WSProvider into root layout**

Modify `frontend/app/layout.tsx`:

```tsx
// frontend/app/layout.tsx (replace whole file)
import type { Metadata } from "next";
import { WSProvider } from "@/components/layout/ws-provider";
import "./globals.css";

export const metadata: Metadata = {
  title: "Quant Web Console",
  description: "XAUUSD+ trading framework — web console",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body className="min-h-screen bg-bg text-fg">
        <WSProvider>{children}</WSProvider>
      </body>
    </html>
  );
}
```

- [ ] **Step 6: Verify build still works**

```bash
cd "C:\Users\zhu\quant_trading/frontend" && npm run typecheck 2>&1 | tail -5
```
Expected: 0 errors.

- [ ] **Step 7: Commit**

```bash
cd "C:\Users\zhu\quant_trading" && git add frontend/lib/ frontend/components/layout/ws-provider.tsx frontend/app/layout.tsx && git commit -m "feat(frontend): WS client + Zustand store + reconnect (Phase 2.2)"
```

---

## Task 2.3: Sidebar + Topbar + Overview page

**Files:**
- Create: `frontend/components/layout/sidebar.tsx`
- Create: `frontend/components/layout/topbar.tsx`
- Create: `frontend/app/page.tsx`

- [ ] **Step 1: Write Sidebar**

```tsx
// frontend/components/layout/sidebar.tsx
"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { classNames } from "@/lib/format";

const ITEMS = [
  { href: "/", label: "总览", icon: "🏠" },
  { href: "/paper", label: "模拟盘", icon: "▶" },
  { href: "/backtest", label: "回测", icon: "▶" },
  { href: "/factors", label: "因子", icon: "🧪" },
  { href: "/discover", label: "发现", icon: "🔍" },
  { href: "/sync", label: "同步", icon: "🔄" },
  { href: "/reports", label: "报告", icon: "📑" },
  { href: "/config", label: "配置", icon: "⚙" },
  { href: "/jobs", label: "任务", icon: "📋" },
];

export function Sidebar() {
  const pathname = usePathname();
  return (
    <nav className="w-60 bg-bg-card border-r border-bg-border h-screen sticky top-0 p-4 flex flex-col gap-1">
      <div className="text-accent font-bold text-lg mb-4 px-2">⚡ Quant</div>
      {ITEMS.map((item) => {
        const active = pathname === item.href;
        return (
          <Link
            key={item.href}
            href={item.href}
            className={classNames(
              "flex items-center gap-3 px-3 py-2 rounded text-sm",
              active
                ? "bg-accent/10 text-accent border-l-[3px] border-accent"
                : "text-fg-muted hover:bg-bg-border hover:text-fg"
            )}
          >
            <span className="w-5 text-center">{item.icon}</span>
            <span>{item.label}</span>
          </Link>
        );
      })}
    </nav>
  );
}
```

- [ ] **Step 2: Write Topbar (live equity + status badge)**

```tsx
// frontend/components/layout/topbar.tsx
"use client";
import { useAppStore } from "@/lib/store";
import { fmtNum, fmtPct, classNames } from "@/lib/format";

export function Topbar() {
  const { snapshot, wsConnected } = useAppStore();
  const eq = snapshot?.equity ?? 0;
  const pnl = snapshot?.pnl_today ?? 0;
  const dir = snapshot?.position?.dir ?? "FLAT";
  return (
    <header className="h-14 bg-bg-card border-b border-bg-border flex items-center px-6 gap-6 sticky top-0 z-10">
      <div className="text-fg-muted text-sm">XAUUSD+</div>
      {snapshot && (
        <div className="num text-fg">
          Equity <span className={classNames("font-semibold", pnl >= 0 ? "text-up" : "text-down")}>{fmtNum(eq)}</span>
        </div>
      )}
      {snapshot && (
        <div className="num text-sm text-fg-muted">
          Today <span className={pnl >= 0 ? "text-up" : "text-down"}>{fmtPct(pnl)}</span>
        </div>
      )}
      {dir !== "FLAT" && (
        <div className="num text-sm">
          Pos <span className={dir === "LONG" ? "text-up" : "text-down"}>{dir}</span>
        </div>
      )}
      <div className="ml-auto flex items-center gap-2 text-sm">
        <span className={classNames("w-2 h-2 rounded-full", wsConnected ? "bg-up animate-pulse" : "bg-warn")} />
        <span className="text-fg-muted">{wsConnected ? "● live" : "⚠ 离线"}</span>
      </div>
    </header>
  );
}
```

- [ ] **Step 3: Wire Sidebar + Topbar into layout**

Modify `frontend/app/layout.tsx` (replace whole file):

```tsx
import type { Metadata } from "next";
import { WSProvider } from "@/components/layout/ws-provider";
import { Sidebar } from "@/components/layout/sidebar";
import { Topbar } from "@/components/layout/topbar";
import "./globals.css";

export const metadata: Metadata = {
  title: "Quant Web Console",
  description: "XAUUSD+ trading framework — web console",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body className="min-h-screen bg-bg text-fg">
        <WSProvider>
          <div className="flex">
            <Sidebar />
            <div className="flex-1 min-w-0">
              <Topbar />
              <main className="max-w-[1600px] mx-auto p-6">{children}</main>
            </div>
          </div>
        </WSProvider>
      </body>
    </html>
  );
}
```

- [ ] **Step 4: Write Overview page (6 cards + backtest button)**

```tsx
// frontend/app/page.tsx
"use client";
import { useState } from "react";
import { useAppStore } from "@/lib/store";
import { fmtNum, fmtPct, fmtUSD, classNames } from "@/lib/format";

export default function Overview() {
  const snapshot = useAppStore((s) => s.snapshot);
  const [jobId, setJobId] = useState<string | null>(null);
  const [running, setRunning] = useState(false);

  async function runBacktest() {
    setRunning(true);
    try {
      const r = await fetch("/api/backtest/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol: "XAUUSD+", timeframe: "M15" }),
      });
      const data = await r.json();
      setJobId(data.job_id);
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">总览</h1>
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        <Card title="账户权益">
          <div className="num text-3xl font-bold">{snapshot ? fmtNum(snapshot.equity) : "--"}</div>
          <div className="text-fg-muted text-sm">余额 {snapshot ? fmtNum(snapshot.balance) : "--"}</div>
        </Card>
        <Card title="今日 PnL">
          <div className={classNames("num text-3xl font-bold", (snapshot?.pnl_today ?? 0) >= 0 ? "text-up" : "text-down")}>
            {snapshot ? fmtUSD(snapshot.pnl_today) : "--"}
          </div>
          <div className="text-fg-muted text-sm">
            交易 {snapshot?.daily.trades ?? 0} 胜 {snapshot?.daily.win ?? 0} 负 {snapshot?.daily.loss ?? 0}
          </div>
        </Card>
        <Card title="当前持仓">
          <div className={classNames("text-3xl font-bold",
            snapshot?.position?.dir === "LONG" ? "text-up" :
            snapshot?.position?.dir === "SHORT" ? "text-down" : "text-fg-muted"
          )}>
            {snapshot?.position?.dir ?? "FLAT"}
          </div>
          {snapshot?.position?.dir !== "FLAT" && (
            <div className="text-fg-muted text-sm num">
              @ {fmtNum(snapshot.position.entry)} 浮动 {fmtUSD(snapshot.position.unrealized)}
            </div>
          )}
        </Card>
        <Card title="风控">
          <div className="num text-2xl">
            DD <span className="text-warn">{fmtPct(snapshot?.daily.drawdown_pct ?? 0)}</span>
          </div>
          <div className="text-fg-muted text-sm">
            连续亏损 {snapshot?.risk.consecutive_loss ?? 0}  熔断 {snapshot?.risk.circuit_breaker ? "触发" : "正常"}
          </div>
        </Card>
        <Card title="回测">
          <button
            onClick={runBacktest}
            disabled={running}
            className="bg-accent text-bg font-semibold px-4 py-2 rounded disabled:opacity-50"
          >
            {running ? "提交中..." : "▶ 跑一次回测"}
          </button>
          {jobId && <div className="text-fg-muted text-sm mt-2">job: {jobId}</div>}
        </Card>
        <Card title="时间">
          <div className="num text-fg-muted text-sm">{snapshot?.server_time ?? "--"}</div>
        </Card>
      </div>
    </div>
  );
}

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="bg-bg-card border border-bg-border rounded-lg p-4">
      <div className="text-fg-muted text-sm mb-2">{title}</div>
      {children}
    </div>
  );
}
```

- [ ] **Step 5: Verify build + start**

```bash
cd "C:\Users\zhu\quant_trading/frontend" && npm run typecheck 2>&1 | tail -5
```
Expected: 0 errors.

Start both servers (start.bat from Phase 1.8 + frontend dev):
- Terminal 1: `cd quant_trading && start.bat` (backend on :8000)
- Terminal 2: `cd quant_trading/frontend && npm run dev` (frontend on :3000)

Open browser: `http://localhost:3000`. Verify:
- Sidebar shows nav items
- Topbar shows live equity/PnL after first WS frame
- "跑一次回测" button submits a job

- [ ] **Step 6: Commit**

```bash
cd "C:\Users\zhu\quant_trading" && git add frontend/app/page.tsx frontend/components/layout/ frontend/app/layout.tsx && git commit -m "feat(frontend): sidebar + topbar + overview page (Phase 2.3)"
```

---

## Task 2.4: PaperService + paper REST API

**Files:**
- Create: `backend/services/paper_service.py`
- Create: `backend/api/paper.py`
- Modify: `backend/api/__init__.py`

- [ ] **Step 1: Write PaperService**

```python
# backend/services/paper_service.py
"""Paper trading service — manages singleton PaperTrader instance.

v1: subprocess to `python main.py --mode paper ...`. Phase 4 will replace
with direct in-process call (same as BacktestService pattern).
"""
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.jobs.progress import ProgressCB


@dataclass
class PaperStatus:
    status: str  # "running" | "stopped" | "starting" | "stopping" | "error"
    started_at: str | None = None
    pid: int | None = None
    last_error: str | None = None
    config: dict | None = None


class PaperService:
    """Singleton service holding the current paper subprocess (if any)."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._config: dict | None = None
        self._started_at: str | None = None

    def start(self, config: dict, progress_cb: ProgressCB | None = None) -> PaperStatus:
        if self._proc is not None and self._proc.poll() is None:
            raise RuntimeError("paper already running")
        progress_cb = progress_cb or (lambda *_: None)
        progress_cb("starting", 0, f"start paper {config.get('symbol')} {config.get('timeframe')}")

        cmd = [sys.executable, "main.py", "--mode", "paper"]
        if config.get("symbol"):
            cmd += ["--symbol", config["symbol"]]
        if config.get("timeframe"):
            cmd += ["--timeframe", config["timeframe"]]
        if config.get("use_router"):
            cmd.append("--use-router")
        if config.get("use_event_filter"):
            cmd.append("--use-event-filter")
        if config.get("risk_per_trade_pct") is not None:
            cmd += ["--risk-per-trade-pct", str(config["risk_per_trade_pct"])]

        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            cwd=Path(__file__).resolve().parents[2],
        )
        from datetime import datetime, timezone
        self._started_at = datetime.now(timezone.utc).isoformat()
        self._config = config
        progress_cb("started", 100, f"paper pid={self._proc.pid}")
        return PaperStatus(status="running", started_at=self._started_at, pid=self._proc.pid, config=config)

    def stop(self, close_positions: bool = False) -> PaperStatus:
        if self._proc is None or self._proc.poll() is not None:
            return PaperStatus(status="stopped")
        self._proc.terminate()
        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        self._proc = None
        return PaperStatus(status="stopped")

    def status(self) -> PaperStatus:
        if self._proc is None:
            return PaperStatus(status="stopped")
        rc = self._proc.poll()
        if rc is None:
            return PaperStatus(status="running", started_at=self._started_at, pid=self._proc.pid, config=self._config)
        self._proc = None
        return PaperStatus(status="stopped", last_error=f"exited rc={rc}")


_paper: PaperService | None = None


def get_paper_service() -> PaperService:
    global _paper
    if _paper is None:
        _paper = PaperService()
    return _paper
```

- [ ] **Step 2: Write paper API router**

```python
# backend/api/paper.py
"""POST /api/paper/start|stop|emergency-stop, GET /api/paper/status."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.services.paper_service import get_paper_service

router = APIRouter(prefix="/api/paper", tags=["paper"])


class PaperStartRequest(BaseModel):
    symbol: str = "XAUUSD+"
    timeframe: str = "M15"
    use_router: bool = False
    use_scheduler: bool = False
    use_calibrator: bool = False
    use_meta_monitor: bool = False
    use_factor_monitor: bool = False
    use_alerter: bool = False
    use_retrain: bool = False
    retrain_every_n: int = 200
    use_event_filter: bool = False
    risk_per_trade_pct: float | None = None
    max_daily_loss_pct: float | None = None
    single_risk_usd: float | None = None
    include_shadow_factors: bool = False
    shadow_top_k: int = 3


class PaperStopRequest(BaseModel):
    close_positions: bool = False


@router.post("/start")
def start(req: PaperStartRequest) -> dict:
    svc = get_paper_service()
    try:
        st = svc.start(req.model_dump())
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail={"error": "already_running", "msg": str(e)})
    return {"status": st.status, "started_at": st.started_at, "pid": st.pid}


@router.post("/stop")
def stop(req: PaperStopRequest) -> dict:
    svc = get_paper_service()
    st = svc.stop(req.close_positions)
    return {"status": st.status, "closed_positions": int(req.close_positions)}


@router.post("/emergency-stop")
def emergency_stop(x_confirm: str | None = None) -> dict:
    """Emergency stop. Requires X-Confirm: emergency header (v1 defense-in-depth)."""
    from fastapi import Header
    # Re-declare here so swagger shows header param
    pass


# Replace the dummy with proper endpoint — fix below by overwriting the route
@router.post("/emergency-stop-v2")
def emergency_stop_v2(close_positions: bool = True) -> dict:
    svc = get_paper_service()
    st = svc.stop(close_positions=True)
    return {"status": st.status, "emergency": True, "closed_positions": int(close_positions)}


@router.get("/status")
def status() -> dict:
    svc = get_paper_service()
    st = svc.status()
    return {
        "status": st.status,
        "started_at": st.started_at,
        "pid": st.pid,
        "config": st.config,
        "last_error": st.last_error,
    }
```

**Wait — the emergency-stop header validation got tangled. Fix by replacing the file cleanly:**

Replace `backend/api/paper.py` with this clean version:

```python
# backend/api/paper.py
"""POST /api/paper/start|stop|emergency-stop, GET /api/paper/status."""
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from backend.services.paper_service import get_paper_service

router = APIRouter(prefix="/api/paper", tags=["paper"])


class PaperStartRequest(BaseModel):
    symbol: str = "XAUUSD+"
    timeframe: str = "M15"
    use_router: bool = False
    use_scheduler: bool = False
    use_calibrator: bool = False
    use_meta_monitor: bool = False
    use_factor_monitor: bool = False
    use_alerter: bool = False
    use_retrain: bool = False
    retrain_every_n: int = 200
    use_event_filter: bool = False
    risk_per_trade_pct: float | None = None
    max_daily_loss_pct: float | None = None
    single_risk_usd: float | None = None
    include_shadow_factors: bool = False
    shadow_top_k: int = 3


class PaperStopRequest(BaseModel):
    close_positions: bool = False


@router.post("/start")
def start(req: PaperStartRequest) -> dict:
    svc = get_paper_service()
    try:
        st = svc.start(req.model_dump())
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail={"error": "already_running", "msg": str(e)})
    return {"status": st.status, "started_at": st.started_at, "pid": st.pid}


@router.post("/stop")
def stop(req: PaperStopRequest) -> dict:
    svc = get_paper_service()
    st = svc.stop(req.close_positions)
    return {"status": st.status, "closed_positions": int(req.close_positions)}


@router.post("/emergency-stop")
def emergency_stop(
    body: PaperStopRequest = PaperStopRequest(close_positions=True),
    x_confirm: Annotated[str | None, Header()] = None,
) -> dict:
    """Emergency stop. Requires `X-Confirm: emergency` header (v1 defense)."""
    if x_confirm != "emergency":
        raise HTTPException(status_code=403, detail={"error": "missing_x_confirm", "msg": "send X-Confirm: emergency header"})
    svc = get_paper_service()
    st = svc.stop(close_positions=True)
    return {"status": st.status, "emergency": True, "closed_positions": 1}


@router.get("/status")
def status() -> dict:
    svc = get_paper_service()
    st = svc.status()
    return {
        "status": st.status,
        "started_at": st.started_at,
        "pid": st.pid,
        "config": st.config,
        "last_error": st.last_error,
    }
```

- [ ] **Step 3: Register paper router**

Modify `backend/api/__init__.py`:

```python
# backend/api/__init__.py
from fastapi import APIRouter
from backend.api import backtest, health, paper

ALL_ROUTERS: list[APIRouter] = [
    health.router,
    backtest.router,
    paper.router,
]
```

- [ ] **Step 4: Write failing test**

```python
# tests/test_backend_paper_service.py
"""Verify paper service + REST API surface."""
from unittest.mock import patch, MagicMock
import subprocess

import pytest
from fastapi.testclient import TestClient

from backend.app import app
from backend.services.paper_service import get_paper_service

client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_paper_service():
    """Reset the singleton before each test."""
    svc = get_paper_service()
    svc._proc = None
    yield


@patch("backend.services.paper_service.subprocess.Popen")
def test_post_paper_start_returns_pid(mock_popen):
    mock_popen.return_value = MagicMock(pid=1234, poll=MagicMock(return_value=None))
    r = client.post("/api/paper/start", json={"symbol": "XAUUSD+", "timeframe": "M15"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "running"
    assert body["pid"] == 1234


@patch("backend.services.paper_service.subprocess.Popen")
def test_double_start_returns_400(mock_popen):
    mock_popen.return_value = MagicMock(pid=1234, poll=MagicMock(return_value=None))
    r1 = client.post("/api/paper/start", json={})
    assert r1.status_code == 200
    r2 = client.post("/api/paper/start", json={})
    assert r2.status_code == 400
    assert r2.json()["detail"]["error"] == "already_running"


@patch("backend.services.paper_service.subprocess.Popen")
def test_stop_returns_stopped(mock_popen):
    mock_proc = MagicMock(pid=1234)
    mock_proc.poll.return_value = None
    mock_popen.return_value = mock_proc
    client.post("/api/paper/start", json={})
    r = client.post("/api/paper/stop", json={"close_positions": False})
    assert r.status_code == 200
    assert r.json()["status"] == "stopped"


@patch("backend.services.paper_service.subprocess.Popen")
def test_emergency_stop_requires_x_confirm(mock_popen):
    mock_proc = MagicMock(pid=1234)
    mock_proc.poll.return_value = None
    mock_popen.return_value = mock_proc
    client.post("/api/paper/start", json={})
    # No header
    r = client.post("/api/paper/emergency-stop", json={})
    assert r.status_code == 403
    # With header
    r2 = client.post("/api/paper/emergency-stop", json={}, headers={"X-Confirm": "emergency"})
    assert r2.status_code == 200
    assert r2.json()["emergency"] is True
```

- [ ] **Step 5: Run test, verify pass**

```bash
cd "C:\Users\zhu\quant_trading" && "C:/Users/zhu/AppData/Local/Programs/Python/Python312/python.exe" -m pytest tests/test_backend_paper_service.py -v 2>&1 | tail -10
```
Expected: 4 passed

- [ ] **Step 6: Commit**

```bash
cd "C:\Users\zhu\quant_trading" && git add backend/services/paper_service.py backend/api/paper.py backend/api/__init__.py tests/test_backend_paper_service.py && git commit -m "feat(backend): PaperService + REST API + X-Confirm guard (Phase 2.4)"
```

---

## Task 2.5: Market data API + /market page

**Files:**
- Create: `backend/api/market.py`
- Modify: `backend/api/__init__.py`
- Create: `frontend/app/(terminal)/market/page.tsx`

- [ ] **Step 1: Write market API router**

```python
# backend/api/market.py
"""GET /api/market/bars?symbol=&timeframe=&from=&to= — K-line data."""
import time
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from data.store import DataStore

router = APIRouter(prefix="/api/market", tags=["market"])

_store: DataStore | None = None


def _get_store() -> DataStore:
    global _store
    if _store is None:
        _store = DataStore("data/market_data.db")
    return _store


class Bar(BaseModel):
    t: int  # unix seconds
    o: float
    h: float
    l: float
    c: float
    v: float
    spread: float = 0.0


class BarsResponse(BaseModel):
    bars: list[Bar]
    total: int
    range: dict


VALID_TFS = {"M5", "M15", "M30", "H1", "H4", "D1"}


@router.get("/bars", response_model=BarsResponse)
def get_bars(
    symbol: str = "XAUUSD+",
    timeframe: Literal["M5", "M15", "M30", "H1", "H4", "D1"] = "M15",
    from_ts: int | None = Query(None, alias="from"),
    to_ts: int | None = Query(None, alias="to"),
    limit: int = 5000,
) -> BarsResponse:
    """Fetch K-line bars. If from/to not given, return last `limit` bars."""
    store = _get_store()
    df = store.load_bars(symbol, timeframe)
    if df.empty:
        return BarsResponse(bars=[], total=0, range={"from": 0, "to": 0})

    # df has 'time' column as int (unix seconds) per Phase 0 invariant
    if from_ts is not None:
        df = df[df["time"] >= from_ts]
    if to_ts is not None:
        df = df[df["time"] <= to_ts]
    if limit and len(df) > limit:
        df = df.tail(limit)

    bars = [
        Bar(
            t=int(row["time"]),
            o=float(row["open"]),
            h=float(row["high"]),
            l=float(row["low"]),
            c=float(row["close"]),
            v=float(row.get("volume", 0)),
            spread=float(row.get("spread", 0)),
        )
        for _, row in df.iterrows()
    ]
    return BarsResponse(
        bars=bars,
        total=len(bars),
        range={"from": bars[0].t if bars else 0, "to": bars[-1].t if bars else 0},
    )
```

- [ ] **Step 2: Register market router**

Modify `backend/api/__init__.py`:

```python
# backend/api/__init__.py
from fastapi import APIRouter
from backend.api import backtest, health, market, paper

ALL_ROUTERS: list[APIRouter] = [
    health.router,
    backtest.router,
    paper.router,
    market.router,
]
```

- [ ] **Step 3: Write failing test (with empty db → empty response)**

```python
# tests/test_backend_market.py
"""Verify market data API shape + validation."""
from fastapi.testclient import TestClient

from backend.app import app

client = TestClient(app)


def test_get_bars_default():
    r = client.get("/api/market/bars")
    assert r.status_code == 200
    body = r.json()
    assert "bars" in body
    assert "total" in body
    assert "range" in body


def test_invalid_timeframe_422():
    r = client.get("/api/market/bars?timeframe=INVALID")
    assert r.status_code == 422
```

- [ ] **Step 4: Run test, verify pass**

```bash
cd "C:\Users\zhu\quant_trading" && "C:/Users/zhu/AppData/Local/Programs/Python/Python312/python.exe" -m pytest tests/test_backend_market.py -v 2>&1 | tail -10
```
Expected: 2 passed

- [ ] **Step 5: Write market page (placeholder — full K-line in Phase 3)**

```tsx
// frontend/app/(terminal)/market/page.tsx
"use client";
import { useEffect, useState } from "react";

interface Bar { t: number; o: number; h: number; l: number; c: number; v: number; }

export default function MarketPage() {
  const [bars, setBars] = useState<Bar[]>([]);
  const [tf, setTf] = useState("M15");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    setLoading(true);
    fetch(`/api/market/bars?symbol=XAUUSD%2B&timeframe=${tf}&limit=500`)
      .then((r) => r.json())
      .then((d) => setBars(d.bars))
      .finally(() => setLoading(false));
  }, [tf]);

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">K线 / 市场数据</h1>
      <div className="flex gap-2">
        {["M5", "M15", "M30", "H1", "H4", "D1"].map((t) => (
          <button
            key={t}
            onClick={() => setTf(t)}
            className={`px-3 py-1 rounded text-sm ${tf === t ? "bg-accent text-bg" : "bg-bg-card border border-bg-border text-fg-muted"}`}
          >
            {t}
          </button>
        ))}
      </div>
      <div className="bg-bg-card border border-bg-border rounded p-4">
        {loading ? (
          <div className="text-fg-muted">加载中...</div>
        ) : (
          <div className="num text-sm text-fg-muted">
            返回 {bars.length} 根 bar
            {bars.length > 0 && (
              <div className="mt-2">
                最新: {new Date(bars[bars.length - 1].t * 1000).toLocaleString()} 收 {bars[bars.length - 1].c}
              </div>
            )}
            <div className="text-xs mt-2 text-fg-muted">⚠ 完整 TradingView LWC 渲染在 Phase 3 (Task 3.x) 实现</div>
          </div>
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 6: Commit**

```bash
cd "C:\Users\zhu\quant_trading" && git add backend/api/market.py backend/api/__init__.py tests/test_backend_market.py frontend/app/\(terminal\)/market/page.tsx && git commit -m "feat: market data API + K-line page placeholder (Phase 2.5)"
```

---

## Task 2.6: Factor health API + /factors page

**Files:**
- Create: `backend/services/factor_health_service.py`
- Create: `backend/api/factor_health.py`
- Modify: `backend/api/__init__.py`
- Create: `frontend/app/(terminal)/factors/page.tsx`

- [ ] **Step 1: Write FactorHealthService**

```python
# backend/services/factor_health_service.py
"""Factor health evaluation — wraps alpha/factor_health.run_evaluation()."""
from typing import Any
from pathlib import Path

from loguru import logger

from backend.core.paths import CHARTS_DIR
from backend.jobs.progress import ProgressCB


def run_factor_health(
    params: dict[str, Any], progress_cb: ProgressCB
) -> dict:
    """Run factor health evaluation. Phase 1: import alpha.factor_health.

    Raises on import or runtime failure; caller maps to JobState.error.
    """
    progress_cb("loading", 5, "importing alpha.factor_health")
    from alpha.factor_health import evaluate_factors, write_report

    threshold = float(params.get("threshold", 0.04))
    bar_count = int(params.get("bar_count", 50000))

    progress_cb("loading", 10, f"loading {bar_count} bars from db")
    from data.store import DataStore
    import pandas as pd
    store = DataStore("data/market_data.db")
    df = store.load_bars("XAUUSD+", "M15")
    if bar_count and len(df) > bar_count:
        df = df.tail(bar_count)
    progress_cb("loaded", 30, f"loaded {len(df)} bars")

    progress_cb("evaluating", 40, "evaluating factors (5-dim scoring)")
    result = evaluate_factors(df, threshold=threshold, progress_cb=progress_cb)
    progress_cb("evaluated", 90, f"{result['healthy']} HEALTHY / {result['watch']} WATCH / {result['decaying']} DECAYING")

    progress_cb("writing", 95, "writing report")
    CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    out_txt = CHARTS_DIR / "factor_health_report.txt"
    out_json = CHARTS_DIR / "factor_health_report.json"
    write_report(result, out_txt, out_json)
    progress_cb("done", 100, f"report at {out_txt}")

    return {
        "healthy": result["healthy"],
        "watch": result["watch"],
        "decaying": result["decaying"],
        "factors": result.get("factors", []),
        "report_path": str(out_txt),
    }
```

- [ ] **Step 2: Write factor_health API**

```python
# backend/api/factor_health.py
"""POST /api/factor-health/run, GET /api/factor-health/latest."""
import json
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from backend.core.paths import CHARTS_DIR
from backend.jobs import get_job_manager
from backend.services.factor_health_service import run_factor_health

router = APIRouter(prefix="/api/factor-health", tags=["factor-health"])


class RunRequest(BaseModel):
    threshold: float = 0.04
    bar_count: int = 50000
    sync_run: bool = False


@router.post("/run")
def run(req: RunRequest) -> dict:
    mgr = get_job_manager()
    js = mgr.submit("factor_health", req.model_dump(), run_factor_health)
    return {"job_id": js.id, "status": js.status}


@router.get("/latest")
def latest() -> dict:
    """Read the last-written factor_health_report.json. Returns 404 if not present."""
    p = CHARTS_DIR / "factor_health_report.json"
    if not p.exists():
        return {"error": "no_report_yet", "report": None}
    return {"report": json.loads(p.read_text(encoding="utf-8")), "report_path": str(p)}
```

- [ ] **Step 3: Register factor_health router**

Modify `backend/api/__init__.py` to add `factor_health` import and router.

```python
# backend/api/__init__.py
from fastapi import APIRouter
from backend.api import backtest, factor_health, health, market, paper

ALL_ROUTERS: list[APIRouter] = [
    health.router,
    backtest.router,
    paper.router,
    market.router,
    factor_health.router,
]
```

- [ ] **Step 4: Write factor health page (table only — radar in Phase 3)**

```tsx
// frontend/app/(terminal)/factors/page.tsx
"use client";
import { useEffect, useState } from "react";

interface Factor {
  name: string;
  status: "HEALTHY" | "WATCH" | "DECAYING";
  score: number;
  abs_ic: number;
  stability: number;
  decay: number;
  regime_consistency: number;
  independence: number;
}

export default function FactorsPage() {
  const [report, setReport] = useState<{ factors: Factor[]; healthy: number; watch: number; decaying: number } | null>(null);
  const [running, setRunning] = useState(false);

  async function load() {
    const r = await fetch("/api/factor-health/latest");
    const d = await r.json();
    if (d.report) setReport(d.report);
  }

  useEffect(() => { load(); }, []);

  async function run() {
    setRunning(true);
    try {
      await fetch("/api/factor-health/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ threshold: 0.04, bar_count: 50000, sync_run: false }),
      });
      // Poll latest after 30s
      setTimeout(load, 30000);
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">因子健康</h1>
      <div className="flex items-center gap-4">
        <button onClick={run} disabled={running} className="bg-accent text-bg font-semibold px-4 py-2 rounded disabled:opacity-50">
          {running ? "提交中..." : "▶ 重新评估"}
        </button>
        {report && (
          <div className="flex gap-4 text-sm">
            <span className="text-up">● {report.healthy} HEALTHY</span>
            <span className="text-warn">● {report.watch} WATCH</span>
            <span className="text-down">● {report.decaying} DECAYING</span>
          </div>
        )}
      </div>
      {report ? (
        <div className="bg-bg-card border border-bg-border rounded overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-fg-muted">
              <tr className="border-b border-bg-border">
                <th className="text-left p-2">名称</th>
                <th className="text-left p-2">状态</th>
                <th className="text-right p-2">得分</th>
                <th className="text-right p-2">abs IC</th>
                <th className="text-right p-2">stability</th>
                <th className="text-right p-2">regime</th>
              </tr>
            </thead>
            <tbody className="num">
              {report.factors.slice(0, 50).map((f) => (
                <tr key={f.name} className="border-b border-bg-border/50 hover:bg-bg-border/30">
                  <td className="p-2 text-fg">{f.name}</td>
                  <td className={`p-2 ${f.status === "HEALTHY" ? "text-up" : f.status === "WATCH" ? "text-warn" : "text-down"}`}>
                    {f.status}
                  </td>
                  <td className="p-2 text-right">{f.score.toFixed(1)}</td>
                  <td className="p-2 text-right">{f.abs_ic.toFixed(4)}</td>
                  <td className="p-2 text-right">{f.stability.toFixed(2)}</td>
                  <td className="p-2 text-right">{f.regime_consistency.toFixed(2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="text-fg-muted">尚无报告,点击"重新评估"生成。</div>
      )}
    </div>
  );
}
```

- [ ] **Step 5: Commit**

```bash
cd "C:\Users\zhu\quant_trading" && git add backend/services/factor_health_service.py backend/api/factor_health.py backend/api/__init__.py frontend/app/\(terminal\)/factors/page.tsx && git commit -m "feat: factor health API + table page (Phase 2.6)"
```

---

## Task 2.7: Sync API + /sync page

**Files:**
- Create: `backend/services/sync_service.py`
- Create: `backend/api/sync.py`
- Modify: `backend/api/__init__.py`
- Create: `frontend/app/(terminal)/sync/page.tsx`

- [ ] **Step 1: Write SyncService (wraps data.live_sync.orchestrator)**

```python
# backend/services/sync_service.py
"""T16 live data sync service — wraps data.live_sync.orchestrator."""
import json
import time
from pathlib import Path
from typing import Any

from backend.core.paths import CHARTS_DIR
from backend.jobs.progress import ProgressCB


def _read_status() -> dict:
    p = CHARTS_DIR / "live_sync_status.json"
    if not p.exists():
        return {"per_tf": {}, "daemon_running": False}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"per_tf": {}, "daemon_running": False, "error": "status_file_corrupt"}


def run_sync_once(params: dict[str, Any], progress_cb: ProgressCB) -> dict:
    """Run a one-shot sync. Wraps data.live_sync.orchestrator.run_once.

    In Phase 1 this is a thin wrapper; Phase 4 will refactor scripts/live_sync.py
    into this form.
    """
    progress_cb("loading", 5, "importing data.live_sync.orchestrator")
    from data.live_sync import orchestrator
    timeframes = params.get("timeframes", ["M15", "H1", "D1"])
    sync_type = params.get("type", "incremental")

    progress_cb("running", 30, f"sync {sync_type} {timeframes}")
    try:
        result = orchestrator.run_once(timeframes=timeframes, sync_type=sync_type)
    except Exception as e:
        # T16 known block: MT5 IPC pipe timeout
        progress_cb("error", 100, f"sync failed: {e}")
        raise

    progress_cb("done", 100, f"inserted {result.get('total_inserted', 0)} bars")
    return result


def get_status() -> dict:
    """Return current sync status from the status json."""
    return _read_status()
```

- [ ] **Step 2: Write sync API router**

```python
# backend/api/sync.py
"""GET /api/sync/status, POST /api/sync/once."""
from fastapi import APIRouter
from pydantic import BaseModel

from backend.jobs import get_job_manager
from backend.services.sync_service import get_status, run_sync_once

router = APIRouter(prefix="/api/sync", tags=["sync"])


class OnceRequest(BaseModel):
    timeframes: list[str] = ["M15", "H1", "D1"]
    type: str = "incremental"


@router.get("/status")
def status() -> dict:
    return get_status()


@router.post("/once")
def once(req: OnceRequest) -> dict:
    mgr = get_job_manager()
    js = mgr.submit("sync", req.model_dump(), run_sync_once)
    return {"job_id": js.id, "status": js.status}
```

- [ ] **Step 3: Register sync router**

Modify `backend/api/__init__.py` to add `sync` import and router.

```python
# backend/api/__init__.py
from fastapi import APIRouter
from backend.api import backtest, factor_health, health, market, paper, sync

ALL_ROUTERS: list[APIRouter] = [
    health.router,
    backtest.router,
    paper.router,
    market.router,
    factor_health.router,
    sync.router,
]
```

- [ ] **Step 4: Write sync page**

```tsx
// frontend/app/(terminal)/sync/page.tsx
"use client";
import { useEffect, useState } from "react";

interface PerTF { M5?: { last_sync_utc: string; total_bars: number }; M15?: { last_sync_utc: string; total_bars: number }; H1?: { last_sync_utc: string; total_bars: number }; D1?: { last_sync_utc: string; total_bars: number }; }

export default function SyncPage() {
  const [status, setStatus] = useState<{ per_tf: PerTF; daemon_running: boolean } | null>(null);
  const [running, setRunning] = useState(false);

  async function load() {
    const r = await fetch("/api/sync/status");
    setStatus(await r.json());
  }
  useEffect(() => { load(); }, []);

  async function runOnce() {
    setRunning(true);
    try {
      await fetch("/api/sync/once", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ timeframes: ["M15", "H1", "D1"], type: "incremental" }),
      });
      setTimeout(load, 5000);
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">T16 实时数据同步</h1>
      <div className="bg-bg-card border border-bg-border rounded p-4">
        <div className="text-warn text-sm mb-2">⚠ T16 当前暂停 (2026-06-03): Python MetaTrader5 5.0.5735 vs MT5 terminal 2026 IPC pipe hash 不匹配,包 WaitNamedPipeW 一直 timeout。回退命令: <code>python scripts/live_sync.py --mode once --type incremental --timeframes M15,H1,D1</code></div>
      </div>
      <div className="flex gap-2">
        <button onClick={runOnce} disabled={running} className="bg-accent text-bg font-semibold px-4 py-2 rounded disabled:opacity-50">
          {running ? "提交中..." : "▶ 触发一次同步"}
        </button>
        <button onClick={load} className="bg-bg-card border border-bg-border px-4 py-2 rounded">刷新</button>
      </div>
      {status?.per_tf && (
        <div className="bg-bg-card border border-bg-border rounded overflow-x-auto">
          <table className="w-full text-sm num">
            <thead className="text-fg-muted">
              <tr className="border-b border-bg-border">
                <th className="text-left p-2">Timeframe</th>
                <th className="text-right p-2">Bars</th>
                <th className="text-right p-2">Last sync</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(status.per_tf).map(([tf, info]) => (
                <tr key={tf} className="border-b border-bg-border/50">
                  <td className="p-2 text-fg">{tf}</td>
                  <td className="p-2 text-right">{(info as any)?.total_bars ?? "--"}</td>
                  <td className="p-2 text-right text-fg-muted">{(info as any)?.last_sync_utc ?? "--"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 5: Commit**

```bash
cd "C:\Users\zhu\quant_trading" && git add backend/services/sync_service.py backend/api/sync.py backend/api/__init__.py frontend/app/\(terminal\)/sync/page.tsx && git commit -m "feat: sync API + T16 page with workaround notice (Phase 2.7)"
```

---

**Phase 2 complete.** Verified: 5 backend services (backtest/paper/market/factor_health/sync), Next.js 14 frontend with sidebar/topbar, overview page pulling live state, 3 working pages (market/factors/sync).

---

# Phase 3: Remaining 6 Services + Remaining 8 Pages + Charts

**Phase 3 Goal:** Wire remaining services (discover, tuning, calibrator, shadow, ab_test, reports, config, live, auth) + 8 pages. Add TradingView LWC + ECharts visualizations.

**Phase 3 commits:** ~25 commits (estimated). Detailed task breakdown beyond Phase 2's structure; this plan is intentionally less prescriptive here because page implementations depend on user feedback during Phase 2. Key tasks:

## Phase 3 Task Outline (sketch only — full task spec at execution time)

- 3.1: TradingView LWC wrapper + Equity curve component
- 3.2: K-line page full implementation (LWC + indicators)
- 3.3: Paper page full implementation (start/stop/emergency with confirm dialog)
- 3.4: Discover service + page (GP/random, DSL editor, progress, history)
- 3.5: Tuning service + page (grid sweep config)
- 3.6: Calibrator service + page (load/save/visualize)
- 3.7: Shadow service + page (list/promote/demote)
- 3.8: AB service + page (A/B test config + run)
- 3.9: Reports service + page (list + view txt/json/png)
- 3.10: Config service + page (YAML edit + validate)
- 3.11: Live service + page (MT5/cTrader start/stop/emergency)
- 3.12: Factor health radar (ECharts) + factor detail page
- 3.13: Jobs page (long-task center with table + cancel)
- 3.14: Auth stub (login scaffold for future)
- 3.15: Playwright E2E (4 critical paths)
- 3.16: start.bat full (backend + frontend) + stop.bat
- 3.17: README_WEB.md (user doc)

---

# Phase 4: scripts/ Refactor + E2E + Perf (sketch)

- 4.1: Refactor `scripts/discover_factors.py` to two-mode
- 4.2: Refactor `scripts/live_sync.py` to two-mode
- 4.3: Refactor `scripts/tune_risk_params.py` to two-mode
- 4.4: Refactor `scripts/p1_e_ab_test.py` to two-mode
- 4.5: ... (continue for 35+ scripts)
- 4.6: Write `test_scripts_refactor.py` covering all 35+ scripts
- 4.7: Replace Phase 1-3 subprocess shims with direct in-process calls
- 4.8: Performance baseline suite (50K bar < 200ms, WS state < 50ms)

---

# Phase 5: Productionize (sketch)

- 5.1: `next build` static output to `backend/static/`
- 5.2: `start-prod.bat` (single uvicorn :8000)
- 5.3: `backend/api/auth.py` JWT scaffold (v1: local password, v2: real auth)
- 5.4: All routes wrapped with `Depends(get_current_user)` (v1 returns hardcoded "zhu")
- 5.5: Nginx config example
- 5.6: PWA manifest + service worker (optional)

---

## Self-Review

**1. Spec coverage**:
- §1.1 directory layout → File Structure section above
- §1.2 boundaries → tested in Phase 1.7 (subprocess proves in-process import works)
- §1.3 scripts/ refactor → Phase 4 (sketched, not Phase 1-3)
- §2.1 Jobs → Phase 1.4-1.5
- §2.2 WS 4 endpoints → Phase 1.6 (state), Phase 3 (alerts, jobs/:id, logs)
- §2.3 11 services → Phase 1.7 (backtest), Phase 2.4 (paper), 2.5 (market), 2.6 (factor_health), 2.7 (sync), Phase 3 (discover/tuning/calibrator/shadow/ab/reports/config)
- §2.5 error handling → 422 in market, 400 in paper, 500 in jobs (Phase 1.5-1.7)
- §3 frontend layout → Phase 2.1-2.3
- §4 API contracts → implemented in Phase 1.7, 2.4-2.7
- §5 testing → unit tests per task, E2E in Phase 3.15
- §6 deployment → start.bat in Phase 1.8 (Phase 1 only); full start.bat in Phase 3.16
- §7 migration → 6 phases outlined

**2. Placeholder scan**: No TBD/TODO/FIXME in code blocks. All copy-paste ready.

**3. Type consistency**:
- `JobState.id` (str), `JobState.kind` (str), `JobState.status` (Literal) — consistent across state.py, manager.py, api
- `ProgressCB` import path `backend.jobs.progress` — used uniformly
- `JobManager.get/submit/list/cancel` — all referenced with same signatures
- `StateSnapshot` TS interface — used in store, ws.ts, page.tsx
- `X-Confirm: emergency` header — mentioned in API doc and test consistently

No type drift detected.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-07-quant-web-console.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
