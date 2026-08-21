from backend.core.db import connect_sqlite
from backend.services import autonomous_learning
from backend.services.learning_cycle_watermark import LearningCycleWatermarkService


def _db(path):
    conn = connect_sqlite(path)
    conn.execute(
        "CREATE TABLE event ("
        "event_type TEXT NOT NULL, observed_at REAL NOT NULL DEFAULT 0"
        ")"
    )
    conn.execute("CREATE TABLE runtime_kv (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at REAL NOT NULL)")
    conn.commit()
    conn.close()


def test_watermark_runs_once_then_only_when_new_facts_arrive(tmp_path):
    db_path = tmp_path / "state.db"
    _db(db_path)
    service = LearningCycleWatermarkService(db_path)

    first = service.evaluate()
    assert first["should_run"] is True
    service.mark_completed(first["current"])
    assert service.evaluate()["status"] == "no_new_facts"

    conn = connect_sqlite(db_path)
    conn.execute(
        "INSERT INTO event (event_type, observed_at) VALUES (?, ?)",
        ("trade_review", 123.0),
    )
    conn.commit()
    conn.close()
    changed = service.evaluate()
    assert changed["should_run"] is True
    assert changed["current"]["fingerprint"] != first["current"]["fingerprint"]


def test_watermark_gated_cycle_executes_once_for_each_fact_frontier(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _db(db_path)
    calls = []
    monkeypatch.setattr(
        autonomous_learning,
        "run_autonomous_learning_cycle",
        lambda **kwargs: calls.append(kwargs)
        or {
            "schema_version": "autonomous_learning_cycle.v2",
            "status": "completed",
            "stages": {},
            "memory_profile": [],
        },
    )

    first = autonomous_learning.run_watermark_gated_autonomous_learning_cycle(
        db_path=db_path
    )
    skipped = autonomous_learning.run_watermark_gated_autonomous_learning_cycle(
        db_path=db_path
    )

    assert first["schema_version"] == "autonomous_learning_cycle.v2"
    assert skipped["status"] == "skipped_no_new_facts"
    assert len(calls) == 1
