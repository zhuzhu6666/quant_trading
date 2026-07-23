from backend.services.live_data_sync_helpers import DATA_SYNC_CRON


def test_data_sync_is_offset_from_m5_decision_boundary():
    assert DATA_SYNC_CRON == "1-56/5 * * * *"
