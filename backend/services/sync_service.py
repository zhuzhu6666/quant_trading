"""Sync service — delegates to scripts/live_sync two-mode form."""
from typing import Any

from loguru import logger

from backend.jobs.progress import ProgressCB


def get_status() -> dict:
    """Return current sync status (from the live_sync_status.json on disk)."""
    from scripts.live_sync import get_status as _get_status
    return _get_status()


def run_sync_once(params: dict[str, Any], progress_cb: ProgressCB) -> dict:
    """Run a one-shot sync. Delegates to scripts/live_sync.run_sync_once."""
    from scripts.live_sync import run_sync_once as _run
    return _run(
        timeframes=params.get("timeframes", ["M15", "H1", "D1"]),
        sync_type=params.get("type", "incremental"),
        progress_cb=progress_cb,
    )
