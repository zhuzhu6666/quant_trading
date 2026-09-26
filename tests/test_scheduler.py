import time

from backend.runtime.scheduler import InProcessScheduler
from backend.services import learning_backfill
from backend.services import supervisor_learning_scheduler


def test_supervisor_learning_cycle_keeps_advisories_observation_only(monkeypatch):
    class _Watermark:
        def __init__(self, *, db_path):
            self.db_path = db_path

        def evaluate(self):
            return {"should_run": True, "status": "new_facts"}

    monkeypatch.setattr(
        supervisor_learning_scheduler,
        "LearningCycleWatermarkService",
        _Watermark,
    )
    monkeypatch.setattr(
        supervisor_learning_scheduler,
        "evaluate_counterfactuals",
        lambda **_: {"count": 3},
    )

    result = supervisor_learning_scheduler.run_supervisor_learning_cycle(
        materialize_advisories=True,
    )

    assert result["counterfactual_count"] == 3
    assert result["advisory_days"] == []
    assert result["advisory_count"] == 0


def test_supervisor_learning_cycle_skips_without_new_canonical_facts(monkeypatch):
    class _Watermark:
        def __init__(self, *, db_path):
            self.db_path = db_path

        def evaluate(self):
            return {"should_run": False, "status": "no_new_facts"}

    monkeypatch.setattr(
        supervisor_learning_scheduler,
        "LearningCycleWatermarkService",
        _Watermark,
    )
    monkeypatch.setattr(
        supervisor_learning_scheduler,
        "evaluate_counterfactuals",
        lambda **_: (_ for _ in ()).throw(
            AssertionError("no-new-fact supervisor cycle must not scan reviews")
        ),
    )

    result = supervisor_learning_scheduler.run_supervisor_learning_cycle()

    assert result["status"] == "skipped_no_new_facts"
    assert result["counterfactual_count"] == 0


def test_learning_backfill_stop_cancels_delayed_run(monkeypatch):
    calls = []
    monkeypatch.setattr(learning_backfill, "run_learning_backfill", lambda **_: calls.append("ran"))

    assert learning_backfill.schedule_learning_backfill(delay_sec=10.0)
    learning_backfill.stop_learning_backfill()
    if learning_backfill._backfill_thread is not None:
        learning_backfill._backfill_thread.join(timeout=1.0)

    time.sleep(0.02)
    assert calls == []


def test_apscheduler_add_job_before_start_does_not_require_next_run_time():
    scheduler = InProcessScheduler()
    scheduler.clear()

    assert scheduler.add_job("unit_prestart", "0 * * * *", lambda: None)
    info = scheduler.get_job("unit_prestart")

    assert info is not None
    assert info.name == "unit_prestart"
    assert info.next_run_time == 0.0
    scheduler.clear()
