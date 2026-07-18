from backend.services import live_service


def test_offmarket_high_load_skips_when_market_open(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.core.db.DATA_DIR", tmp_path)
    db_path = tmp_path / "offmarket-audit.db"
    live_service._live_state["market_session"] = {
        "status": "open_confirmed",
        "high_load_allowed": False,
        "high_load_profile": "disabled",
    }

    result = live_service._scheduled_offmarket_position_quality_lightgbm(db_path=db_path)

    assert result["ok"] is True
    assert result["skipped"] is True
    assert result["audit"]["status"] == "skipped"
    assert result["audit"]["session_status"] == "open_confirmed"


def test_offmarket_high_load_runs_training_when_closed(monkeypatch, tmp_path):
    db_path = tmp_path / "offmarket-training.db"
    live_service._live_state["market_session"] = {
        "status": "closed_pending_positions",
        "high_load_allowed": True,
        "high_load_profile": "limited_with_positions",
    }

    class FakeService:
        def __init__(self, db_path=None):
            self.db_path = db_path

        def train(self, **kwargs):
            assert kwargs["limit"] == 250
            assert kwargs["register"] is True
            return {
                "ok": True,
                "artifact_path": str(tmp_path / "artifact.json"),
                "metrics": {"sample_count": 20},
            }

        def score_samples(self, **kwargs):
            assert kwargs["limit"] == 30
            assert kwargs["mode"] == "offmarket_shadow_after_train"
            return {"ok": True, "count": 30}

    monkeypatch.setattr(
        "research.position_quality_lightgbm.PositionQualityLightGBMService",
        FakeService,
    )

    result = live_service._scheduled_offmarket_position_quality_lightgbm(db_path=db_path)

    assert result["ok"] is True
    assert result["status"] == "done"
    assert result["audit"]["status"] == "done"
    assert result["audit"]["high_load_profile"] == "limited_with_positions"
