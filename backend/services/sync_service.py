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
