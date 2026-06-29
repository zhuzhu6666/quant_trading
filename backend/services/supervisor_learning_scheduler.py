from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger

from backend.core.db import STATE_DB
from backend.services.position_supervisor_governance import build_position_supervisor_advisories
from backend.services.supervisor_counterfactual import evaluate_counterfactuals

_scheduler_thread: threading.Thread | None = None
_stop_event = threading.Event()


def _local_days_for_advisory() -> list[str]:
    today = datetime.now().date()
    yesterday = today - timedelta(days=1)
    return [today.isoformat(), yesterday.isoformat()]


def run_supervisor_learning_cycle(
    *,
    db_path: str | Path = STATE_DB,
    limit: int = 200,
    materialize_advisories: bool = True,
) -> dict:
    counterfactual = evaluate_counterfactuals(
        db_path=db_path,
        limit=limit,
        materialize=True,
    )
    advisories = []
    if materialize_advisories:
        for day in _local_days_for_advisory():
            try:
                advisories.append(build_position_supervisor_advisories(day=day, db_path=db_path, materialize=True))
            except ValueError as exc:
                logger.debug("[supervisor_learning] advisory skipped day=%s: %s", day, exc)
    return {
        "schema_version": "supervisor_learning_cycle.v1",
        "counterfactual_count": int(counterfactual.get("count") or 0),
        "advisory_days": [item.get("day") for item in advisories],
        "advisory_count": sum(len(item.get("items") or []) for item in advisories),
    }


def schedule_supervisor_learning(
    *,
    delay_sec: float = 300.0,
    interval_sec: float = 1800.0,
    limit: int = 200,
) -> bool:
    global _scheduler_thread
    if _scheduler_thread is not None and _scheduler_thread.is_alive():
        return False
    _stop_event.clear()

    def _worker() -> None:
        if _stop_event.wait(max(0.0, delay_sec)):
            return
        while not _stop_event.is_set():
            try:
                result = run_supervisor_learning_cycle(limit=limit)
                logger.info("[supervisor_learning] scheduled run completed: %s", result)
            except Exception as exc:
                logger.warning("[supervisor_learning] scheduled run failed: %s", exc)
            if _stop_event.wait(max(60.0, interval_sec)):
                return

    _scheduler_thread = threading.Thread(
        target=_worker,
        name="supervisor_learning_scheduler",
        daemon=True,
    )
    _scheduler_thread.start()
    return True


def stop_supervisor_learning() -> None:
    _stop_event.set()
