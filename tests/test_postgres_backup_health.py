from backend.core.db import STATE_DB_DDL, connect_sqlite
from backend.services.postgres_backup_health import PostgresBackupHealthService


def test_postgres_backup_health_publishes_sanitized_pgbackrest_observation(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()

    service = PostgresBackupHealthService(db_path)
    published = service.publish(
        {
            "ok": True,
            "status": "healthy",
            "stanza": "quant-state-v1",
            "secret_token": "must-not-persist",
            "backup": {"label": "20260729-010101F", "cipher_pass": "must-not-persist"},
            "restore_drill": {"status": "healthy", "requires_manual_promotion": True},
        }
    )
    latest = service.latest()

    assert published["status"] == "healthy"
    assert latest["ok"] is True
    assert latest["stanza"] == "quant-state-v1"
    assert "secret_token" not in latest
    assert "cipher_pass" not in latest["backup"]
    assert latest["boundary"]["does_not_authorize_trading"] is True


def test_postgres_backup_health_requires_a_successful_restore_drill_for_healthy(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()

    service = PostgresBackupHealthService(db_path)
    service.publish({"ok": True, "status": "healthy", "stanza": "quant-state-v1"})
    before_drill = service.latest()
    service.record_restore_drill(
        {
            "ok": True,
            "state_schema": {"status": "matched"},
            "table_counts": {"status": "matched"},
            "memory_integrity": {"status": "healthy"},
        }
    )
    after_drill = service.latest()

    assert before_drill["status"] == "degraded"
    assert before_drill["reason_code"] == "restore_drill_missing"
    assert after_drill["status"] == "healthy"
    assert after_drill["ok"] is True
    assert after_drill["restore_drill"]["requires_manual_promotion"] is True


def test_postgres_backup_health_reports_missing_observation_explicitly(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()

    latest = PostgresBackupHealthService(db_path).latest()

    assert latest["status"] == "missing"
    assert latest["ok"] is False
    assert latest["reason_code"] == "backup_health_observation_missing"
