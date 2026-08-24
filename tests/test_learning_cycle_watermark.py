from backend.core.db import connect_sqlite
from backend.services import autonomous_learning
from backend.services.learning_cycle_watermark import LearningCycleWatermarkService


def _db(path):
    conn = connect_sqlite(path)
    conn.execute(
        "CREATE TABLE event ("
        "event_id TEXT, event_type TEXT NOT NULL, observed_at REAL NOT NULL DEFAULT 0"
        ")"
    )
    conn.execute("CREATE TABLE runtime_kv (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at REAL NOT NULL)")
    conn.commit()
    conn.close()


def test_watermark_runs_once_then_only_when_new_facts_arrive(tmp_path):
    db_path = tmp_path / "state.db"
    _db(db_path)
    conn = connect_sqlite(db_path)
    conn.execute(
        "INSERT INTO event (event_id, event_type, observed_at) VALUES (?, ?, ?)",
        ("event-seed", "risk_decision", 100.0),
    )
    conn.commit()
    conn.close()
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
    assert changed["current"]["source_fingerprint"] == first["current"]["source_fingerprint"]
    assert changed["reason"] == "canonical_fact_frontier_advanced"


def test_watermark_source_identity_changes_after_rebuild(tmp_path):
    db_path = tmp_path / "state.db"
    _db(db_path)
    conn = connect_sqlite(db_path)
    conn.execute(
        "INSERT INTO event (event_id, event_type, observed_at) VALUES (?, ?, ?)",
        ("event-before", "trade_review", 123.0),
    )
    conn.commit()
    conn.close()
    service = LearningCycleWatermarkService(db_path)
    first = service.evaluate()
    service.mark_completed(first["current"])

    conn = connect_sqlite(db_path)
    conn.execute("DELETE FROM event")
    conn.execute(
        "INSERT INTO event (event_id, event_type, observed_at) VALUES (?, ?, ?)",
        ("event-after-rebuild", "trade_review", 123.0),
    )
    conn.commit()
    conn.close()

    changed = service.evaluate()
    assert changed["should_run"] is True
    assert changed["reason"] == "canonical_source_identity_changed"
    assert changed["current"]["source_fingerprint"] != first["current"]["source_fingerprint"]


def test_legacy_watermark_without_source_identity_must_run(tmp_path):
    db_path = tmp_path / "state.db"
    _db(db_path)
    conn = connect_sqlite(db_path)
    conn.execute(
        "INSERT INTO runtime_kv (key, value_json, updated_at) VALUES (?, ?, ?)",
        ("autonomous_learning.fact_watermark.v1", '{"fingerprint":"old"}', 1.0),
    )
    conn.commit()
    conn.close()

    result = LearningCycleWatermarkService(db_path).evaluate()
    assert result["should_run"] is True
    assert result["reason"] == "legacy_watermark_missing_source_identity"


def test_empty_canonical_source_has_stable_identity(tmp_path):
    db_path = tmp_path / "state.db"
    _db(db_path)
    service = LearningCycleWatermarkService(db_path)

    first = service.current()
    assert first["source_identity_status"] == "empty"
    service.mark_completed(first)

    second = service.evaluate()
    assert second["status"] == "no_new_facts"
    assert second["current"]["source_fingerprint"] == first["source_fingerprint"]


def test_count_regression_must_run_even_if_identity_is_reused(tmp_path):
    db_path = tmp_path / "state.db"
    _db(db_path)
    conn = connect_sqlite(db_path)
    conn.execute(
        "INSERT INTO event (event_id, event_type, observed_at) VALUES (?, ?, ?)",
        ("event-1", "trade_review", 123.0),
    )
    conn.commit()
    conn.close()
    service = LearningCycleWatermarkService(db_path)
    first = service.evaluate()
    service.mark_completed(first["current"])

    conn = connect_sqlite(db_path)
    conn.execute("DELETE FROM event")
    conn.commit()
    conn.close()

    result = service.evaluate()
    assert result["should_run"] is True
    assert result["reason"] == "canonical_source_count_regressed"


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
