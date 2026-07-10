from backend.core.db import connect_sqlite
from backend.services.learning_cycle_watermark import FACT_SOURCES, LearningCycleWatermarkService


def _db(path):
    conn = connect_sqlite(path)
    for table, timestamp_column in FACT_SOURCES:
        conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, {timestamp_column} REAL NOT NULL DEFAULT 0)")
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
    conn.execute("INSERT INTO trade_outcome_review (created_at) VALUES (123.0)")
    conn.commit()
    conn.close()
    changed = service.evaluate()
    assert changed["should_run"] is True
    assert changed["current"]["fingerprint"] != first["current"]["fingerprint"]
