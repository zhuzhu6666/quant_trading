from __future__ import annotations

import threading

import pytest

from backend.jobs.pg_queue import ClaimedJob
from backend.jobs.state import JobState
from backend.jobs.worker import PersistentJobWorker


class _FakeQueue:
    def __init__(
        self,
        *,
        cancel_on_heartbeat: bool = False,
        claim_lost_on_heartbeat: bool = False,
        heartbeat_errors: int = 0,
    ) -> None:
        self.claimed = False
        self.cancel_on_heartbeat = cancel_on_heartbeat
        self.claim_lost_on_heartbeat = claim_lost_on_heartbeat
        self.heartbeat_errors = heartbeat_errors
        self.heartbeats = 0
        self.heartbeat_succeeded = threading.Event()
        self.completed = []
        self.cancelled = []
        self.failed = []
        self.claim_kwargs = []

    def claim(self, **kwargs):
        self.claim_kwargs.append(dict(kwargs))
        if self.claimed:
            return None
        self.claimed = True
        return ClaimedJob(
            state=JobState(id="job-1", kind="backtest", params={"value": 7}),
            claim_token="claim-1",
            worker_id="worker-1",
        )

    def heartbeat(self, *_args, **_kwargs):
        self.heartbeats += 1
        if self.heartbeat_errors > 0:
            self.heartbeat_errors -= 1
            raise RuntimeError("transient_heartbeat_failure")
        self.heartbeat_succeeded.set()
        if self.claim_lost_on_heartbeat:
            return {"ok": False, "cancel_requested": False, "reason": "claim_not_owned"}
        return {"ok": True, "cancel_requested": self.cancel_on_heartbeat}

    def complete(self, job_id, claim_token, result):
        self.completed.append((job_id, claim_token, result))
        return "done"

    def acknowledge_cancel(self, job_id, claim_token):
        self.cancelled.append((job_id, claim_token))
        return True

    def fail(self, job_id, claim_token, error, **kwargs):
        self.failed.append((job_id, claim_token, str(error), kwargs))
        return "retry_wait"


def test_worker_completes_claimed_handler_and_publishes_result():
    queue = _FakeQueue()

    def handler(params, progress):
        progress("calculate", 50, "half")
        return {"value": params["value"] * 2}

    worker = PersistentJobWorker(
        queue=queue,
        worker_id="worker-1",
        handlers={"backtest": handler},
    )

    result = worker.run_once()

    assert result.status == "done"
    assert queue.completed == [("job-1", "claim-1", {"value": 14})]
    assert queue.failed == []
    assert queue.claim_kwargs == [
        {
            "worker_id": "worker-1",
            "supported_kinds": ("backtest",),
            "lease_sec": 60.0,
            "global_limit": 2,
            "kind_limits": {"backtest": 1},
            "retry_delay_sec": 5.0,
        }
    ]


def test_worker_defaults_each_registered_kind_to_fail_closed_single_concurrency():
    queue = _FakeQueue()
    worker = PersistentJobWorker(
        queue=queue,
        worker_id="worker-1",
        handlers={
            "backtest": lambda _params, _progress: {},
            "new_research_kind": lambda _params, _progress: {},
            "external_refresh": lambda _params, _progress: {},
        },
        global_limit=4,
        kind_limits={"backtest": 2},
    )

    assert worker.kind_limits == {
        "backtest": 2,
        "new_research_kind": 1,
        "external_refresh": 1,
    }


def test_worker_graceful_stop_drains_current_handler_without_abandoning_thread():
    queue = _FakeQueue()
    handler_started = threading.Event()
    allow_finish = threading.Event()

    def handler(_params, _progress):
        handler_started.set()
        assert allow_finish.wait(2.0)
        return {"drained": True}

    worker = PersistentJobWorker(
        queue=queue,
        worker_id="worker-1",
        handlers={"backtest": handler},
        heartbeat_interval_sec=0.5,
    )
    stop_event = threading.Event()
    holder = {}

    thread = threading.Thread(
        target=lambda: holder.setdefault("result", worker.run_once(stop_event=stop_event)),
        name="pytest-job-worker",
    )
    thread.start()
    assert handler_started.wait(1.0)
    stop_event.set()
    allow_finish.set()
    thread.join(2.0)

    assert not thread.is_alive()
    assert holder["result"].status == "done"
    assert queue.completed == [("job-1", "claim-1", {"drained": True})]


def test_worker_retries_a_transient_main_heartbeat_without_abandoning_handler():
    queue = _FakeQueue(heartbeat_errors=1)
    handler_started = threading.Event()
    allow_finish = threading.Event()

    def handler(_params, _progress):
        handler_started.set()
        assert allow_finish.wait(3.0)
        return {"completed_after_retry": True}

    worker = PersistentJobWorker(
        queue=queue,
        worker_id="worker-1",
        handlers={"backtest": handler},
        heartbeat_interval_sec=0.5,
    )
    holder = {}
    thread = threading.Thread(
        target=lambda: holder.setdefault("result", worker.run_once()),
        name="pytest-job-worker-heartbeat-retry",
    )
    thread.start()
    assert handler_started.wait(1.0)
    assert queue.heartbeat_succeeded.wait(2.0)
    allow_finish.set()
    thread.join(2.0)

    assert not thread.is_alive()
    assert queue.heartbeats >= 2
    assert holder["result"].status == "done"
    assert queue.completed == [
        ("job-1", "claim-1", {"completed_after_retry": True})
    ]


def test_worker_never_acknowledges_or_completes_after_claim_is_lost():
    queue = _FakeQueue(claim_lost_on_heartbeat=True)

    def handler(_params, progress):
        progress("calculate", 50, "half")
        return {"unsafe": "must_not_publish"}

    worker = PersistentJobWorker(
        queue=queue,
        worker_id="worker-1",
        handlers={"backtest": handler},
    )

    result = worker.run_once()

    assert result.status == "claim_lost"
    assert queue.cancelled == []
    assert queue.completed == []
    assert queue.failed == []


def test_worker_acknowledges_an_owned_cancel_request():
    queue = _FakeQueue(cancel_on_heartbeat=True)

    def handler(_params, progress):
        progress("calculate", 50, "half")
        return {"unsafe": "must_not_publish"}

    worker = PersistentJobWorker(
        queue=queue,
        worker_id="worker-1",
        handlers={"backtest": handler},
    )

    result = worker.run_once()

    assert result.status == "cancelled"
    assert queue.cancelled == [("job-1", "claim-1")]
    assert queue.completed == []


def test_worker_startup_rejects_missing_or_malformed_settings_yaml(monkeypatch):
    from backend.services import config_service
    from scripts import job_worker

    monkeypatch.setattr(
        config_service,
        "get_config",
        lambda: {
            "exists": False,
            "path": "/isolated/config/settings.yaml",
            "parsed": {},
        },
    )
    with pytest.raises(RuntimeError, match="job_worker_settings_missing"):
        job_worker._validate_worker_settings_yaml()

    monkeypatch.setattr(
        config_service,
        "get_config",
        lambda: {
            "exists": True,
            "path": "/isolated/config/settings.yaml",
            "parse_error": "bad yaml",
            "parsed": {},
        },
    )
    with pytest.raises(RuntimeError, match="job_worker_settings_parse_error"):
        job_worker._validate_worker_settings_yaml()


def test_worker_startup_checks_yaml_before_read_only_schema_gate(monkeypatch):
    from backend.core import db
    from scripts import job_worker

    calls = []
    monkeypatch.setattr(
        job_worker,
        "_validate_worker_settings_yaml",
        lambda: calls.append("yaml"),
    )
    monkeypatch.setattr(db, "init_state_db", lambda: calls.append("schema"))

    job_worker._validate_worker_startup()

    assert calls == ["yaml", "schema"]
