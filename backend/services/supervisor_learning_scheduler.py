from __future__ import annotations

import threading
from pathlib import Path

from loguru import logger

from backend.core.db import STATE_DB
from backend.services.supervisor_counterfactual import evaluate_counterfactuals

_scheduler_thread: threading.Thread | None = None
_stop_event = threading.Event()


def run_supervisor_learning_cycle(
    *,
    db_path: str | Path = STATE_DB,
    limit: int = 200,
    materialize_advisories: bool = False,
) -> dict:
    """Run warm supervisor evidence work without creating governance advisories.

    ``materialize_advisories`` remains as a compatibility keyword, but the
    scheduled learning plane no longer writes the legacy advisory path.  Any
    governance mutation must come from the existing V16 candidate bridge and
    Coordinator chain.
    """
    counterfactual = evaluate_counterfactuals(
        db_path=db_path,
        limit=limit,
        materialize=True,
    )
    if materialize_advisories:
        logger.info(
            "[supervisor_learning] legacy advisory materialization disabled; "
            "use the V16 candidate bridge"
        )
    return {
        "schema_version": "supervisor_learning_cycle.v1",
        "counterfactual_count": int(counterfactual.get("count") or 0),
        "advisory_days": [],
        "advisory_count": 0,
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
                logger.info("[supervisor_learning] scheduled run completed: {}", result)
            except Exception as exc:
                logger.warning("[supervisor_learning] scheduled run failed: {}", exc)
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
