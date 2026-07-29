from scripts.pgbackrest_backup import build_observation
from scripts.verify_state_restore import verify_manifest


def test_pgbackrest_observation_uses_machine_readable_backup_summary():
    observation = build_observation(
        pgbackrest_info=[
            {
                "name": "quant-state-v1",
                "status": {"code": 0, "message": "ok"},
                "archive": [{"min": "0001", "max": "0002"}],
                "backup": [
                    {
                        "label": "20260729-010101F",
                        "type": "full",
                        "timestamp": {"start": 100.0, "stop": 120.0},
                    }
                ],
            }
        ],
        stanza="quant-state-v1",
        command="full",
        archive_status={"status": "available", "archive_mode": "on", "archive_lag_seconds": 3.0},
    )

    assert observation["status"] == "healthy"
    assert observation["backup"]["latest_backup"]["label"] == "20260729-010101F"
    assert observation["postgres_archive"]["archive_lag_seconds"] == 3.0


def test_restore_verification_fails_closed_on_count_or_integrity_difference():
    expected = {
        "state_schema": {"current_version": 12},
        "table_counts": {
            "trade_outcome_review": 10,
            "experience_memory": 10,
            "brain_memory": 5,
        },
    }
    actual = {
        "state_schema": {"current_version": 12, "ok": True},
        "table_counts": {
            "trade_outcome_review": 10,
            "experience_memory": 9,
            "brain_memory": 5,
        },
        "memory_integrity": {"status": "degraded"},
    }

    report = verify_manifest(expected, actual)

    assert report["ok"] is False
    assert report["table_counts"]["mismatches"]["experience_memory"] == {
        "expected": 10,
        "actual": 9,
    }
    assert report["requires_manual_promotion"] is True
