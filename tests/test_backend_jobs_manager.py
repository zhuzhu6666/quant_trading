"""JobManager delegates all research jobs to the PostgreSQL queue."""
import re
from pathlib import Path

import pytest

from backend.jobs.manager import JobManager
from backend.jobs.state import JobState


def test_submit_enqueues_supported_job_without_api_execution_callback():
    class _Queue:
        def __init__(self):
            self.calls = []

        def enqueue(self, kind, params, **kwargs):
            self.calls.append((kind, dict(params), dict(kwargs)))
            return JobState(id="durable-1", kind=kind, params=dict(params))

    queue = _Queue()
    manager = JobManager(persistent_queue=queue)
    state = manager.submit(
        "backtest",
        {"symbol": "XAUUSD+", "_idempotency_key": "request-1", "_max_attempts": 4},
    )

    assert state.id == "durable-1"
    assert queue.calls == [
        (
            "backtest",
            {"symbol": "XAUUSD+", "_idempotency_key": "request-1", "_max_attempts": 4},
            {"idempotency_key": "request-1", "priority": 0, "max_attempts": 4},
        )
    ]


def test_unknown_job_kind_has_no_local_execution_fallback():
    class _Queue:
        def enqueue(self, *_args, **_kwargs):
            raise AssertionError("unsupported jobs must not reach the queue")

    manager = JobManager(persistent_queue=_Queue())
    with pytest.raises(ValueError, match="unsupported_persistent_job_kind"):
        manager.submit("local_light", {})


def test_get_list_and_cancel_delegate_to_durable_queue():
    state = JobState(id="job-1", kind="discover")

    class _Queue:
        def get(self, job_id):
            assert job_id == "job-1"
            return state

        def list(self, *, kind=None, status=None):
            assert (kind, status) == ("discover", "queued")
            return [state]

        def request_cancel(self, job_id):
            assert job_id == "job-1"
            return True

    manager = JobManager(persistent_queue=_Queue())
    assert manager.get("job-1") is state
    assert manager.list(kind="discover", status="queued") == [state]
    assert manager.cancel("job-1") is True


def test_every_production_literal_job_submission_has_a_persistent_worker_handler():
    root = Path(__file__).resolve().parents[1]
    submitted: set[str] = set()
    pattern = re.compile(r"\.submit\(\s*[\"']([a-z0-9_]+)[\"']")
    for path in (root / "backend").rglob("*.py"):
        submitted.update(pattern.findall(path.read_text(encoding="utf-8")))

    from backend.jobs.handlers import PERSISTENT_JOB_HANDLERS

    assert submitted == JobManager.PERSISTENT_JOB_KINDS
    assert submitted == set(PERSISTENT_JOB_HANDLERS)
