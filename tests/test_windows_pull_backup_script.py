from scripts.verify_state_restore import verify_restored_state


def test_windows_pull_restore_verification_requires_healthy_schema_and_memory():
    report = verify_restored_state(
        {
            "state_schema": {"current_version": 12, "ok": True},
            "table_counts": {
                "trade_outcome_review": 10,
                "experience_memory": 10,
                "brain_memory": 5,
            },
            "memory_integrity": {"status": "healthy"},
        }
    )

    assert report["ok"] is True
    assert report["source_parity"] == "not_claimed_for_offline_logical_snapshot"
    assert report["requires_manual_promotion"] is True


def test_windows_pull_restore_verification_fails_closed_when_memory_is_degraded():
    report = verify_restored_state(
        {
            "state_schema": {"current_version": 12, "ok": True},
            "table_counts": {},
            "memory_integrity": {"status": "degraded"},
        }
    )

    assert report["ok"] is False
    assert report["memory_integrity"]["status"] == "degraded"
