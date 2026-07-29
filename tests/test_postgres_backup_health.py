import time

import pytest

from backend.core.db import STATE_DB_DDL, connect_sqlite
from backend.services.postgres_backup_health import PostgresBackupHealthService


def test_postgres_backup_health_records_client_verified_windows_pull(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()

    service = PostgresBackupHealthService(db_path)
    published = service.record_windows_pull(
        completed_at=time.time(),
        byte_count=10_485_760,
        sha256="a" * 64,
    )
    latest = service.latest()

    assert published["status"] == "healthy"
    assert published["source"] == "windows_pull"
    assert latest["status"] == "degraded"
    assert latest["reason_code"] == "restore_drill_missing"
    assert latest["backup"]["byte_count"] == 10_485_760
    assert latest["backup"]["client_archive_verified"] is True
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
    service.record_windows_pull(
        completed_at=time.time(),
        byte_count=10_485_760,
        sha256="b" * 64,
    )
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
    assert after_drill["restore_drill"]["verification_source"] == "operator_reported"


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


def test_postgres_backup_health_does_not_accept_a_retired_source_as_current(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()

    service = PostgresBackupHealthService(db_path)
    service.publish({"ok": True, "status": "healthy", "source": "pgbackrest"})
    latest = service.latest()

    assert latest["status"] == "unavailable"
    assert latest["reason_code"] == "backup_health_source_not_current"


def test_postgres_backup_health_rejects_an_invalid_windows_receipt(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(ValueError, match="windows_pull_receipt_invalid"):
        PostgresBackupHealthService(db_path).record_windows_pull(
            completed_at=time.time(),
            byte_count=0,
            sha256="not-a-sha256",
        )
