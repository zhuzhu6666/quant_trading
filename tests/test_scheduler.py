import time
from datetime import datetime, timezone

import pytest

from backend.runtime.scheduler import HAS_APSCHEDULER, InProcessScheduler, _TimerJob
from backend.services import learning_backfill
from backend.services import supervisor_learning_scheduler


def _interval(cron_expr: str) -> float:
    return _TimerJob("test", cron_expr, lambda: None)._parse_interval_seconds()


@pytest.mark.parametrize(
    ("cron_expr", "expected"),
    [
        ("*/5 * * * *", 300.0),
        ("0 * * * *", 3600.0),
        ("20 * * * *", 3600.0),
        ("0 */6 * * *", 21600.0),
        ("0 3 * * *", 86400.0),
        ("30 1 * * *", 86400.0),
        ("0 5 * * 0", 604800.0),
        ("0 4 1 */3 *", 8035200.0),
    ],
)
def test_timer_scheduler_cron_fallback_preserves_frequency(cron_expr, expected):
    assert _interval(cron_expr) == expected


def test_supervisor_advisory_days_use_explicit_business_timezone(monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is not None
            return datetime(2026, 7, 1, 16, 30, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setenv("QUANT_ADVISORY_TZ", "Asia/Shanghai")
    monkeypatch.setattr(supervisor_learning_scheduler, "datetime", FixedDateTime)

    assert supervisor_learning_scheduler._local_days_for_advisory() == [
        "2026-07-02",
        "2026-07-01",
    ]


def test_learning_backfill_stop_cancels_delayed_run(monkeypatch):
    calls = []
    monkeypatch.setattr(learning_backfill, "run_learning_backfill", lambda **_: calls.append("ran"))

    assert learning_backfill.schedule_learning_backfill(delay_sec=10.0)
    learning_backfill.stop_learning_backfill()
    if learning_backfill._backfill_thread is not None:
        learning_backfill._backfill_thread.join(timeout=1.0)

    time.sleep(0.02)
    assert calls == []


@pytest.mark.skipif(not HAS_APSCHEDULER, reason="APScheduler backend not installed")
def test_apscheduler_add_job_before_start_does_not_require_next_run_time():
    scheduler = InProcessScheduler()
    scheduler.clear()

    assert scheduler.add_job("unit_prestart", "0 * * * *", lambda: None)
    info = scheduler.get_job("unit_prestart")

    assert info is not None
    assert info.name == "unit_prestart"
    assert info.next_run_time == 0.0
    scheduler.clear()
