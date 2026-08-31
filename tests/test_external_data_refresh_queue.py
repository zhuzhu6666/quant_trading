from __future__ import annotations

from types import SimpleNamespace

from backend.api import external_data
from backend.jobs.state import JobState
from backend.services import external_data_refresh


def test_external_refresh_uses_persistent_queue_without_daemon_thread(monkeypatch):
    submitted = []

    class Manager:
        def submit(self, kind, params):
            submitted.append((kind, dict(params)))
            return JobState(id="durable-refresh-1", kind=kind, status="queued", params=params)
    monkeypatch.setattr(external_data, "get_job_manager", lambda: Manager())


    result = external_data.trigger_refresh(
        "operator",
        external_data.RefreshRequest(source="cot", force=True),
    )

    assert result["job_id"] == "durable-refresh-1"
    assert result["status"] == "started"
    assert result["job_status"] == "queued"
    assert result["durable"] is True
    assert submitted[0][:2] == ("external_refresh", {"source": "cot", "force": True})


def test_external_refresh_worker_handler_is_bounded_and_reports_progress(
    monkeypatch,
    tmp_path,
):
    progress = []

    script = tmp_path / "refresh_external_data.py"
    script.write_text("# fixture\n", encoding="utf-8")
    monkeypatch.setattr(external_data_refresh, "REFRESH_SCRIPT", script)
    monkeypatch.setattr(
        external_data_refresh.subprocess,
        "run",
        lambda args, **kwargs: SimpleNamespace(
            args=args,
            returncode=0,
            stdout="first\nlast\n",
            stderr="",
        ),
    )

    result = external_data_refresh.run_external_data_refresh(
        {"source": "events", "force": True},
        lambda step, pct, message: progress.append((step, pct, message)),
    )

    assert result["status"] == "completed"
    assert result["source"] == "events"
    assert result["output"] == ["first", "last"]
    assert [item[0] for item in progress] == ["launch", "complete"]


def test_external_refresh_rejects_unknown_source_before_subprocess(monkeypatch):
    monkeypatch.setattr(
        external_data_refresh.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid source must fail before subprocess")
        ),
    )

    try:
        external_data_refresh.run_external_data_refresh(
            {"source": "../../unexpected"},
            lambda *_args: None,
        )
    except ValueError as exc:
        assert "invalid_external_refresh_source" in str(exc)
    else:
        raise AssertionError("invalid source was accepted")
