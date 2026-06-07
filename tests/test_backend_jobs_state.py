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
