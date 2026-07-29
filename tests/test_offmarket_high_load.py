from backend.services import live_service


def test_offmarket_high_load_skips_when_market_open(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.core.db.DATA_DIR", tmp_path)
    db_path = tmp_path / "offmarket-audit.db"

    class FakeShadowService:
        def __init__(self, db_path=None):
            self.db_path = db_path

        def score_samples(self, **kwargs):
            assert kwargs["limit"] == 30
            assert kwargs["mode"] == "offmarket_shadow_refresh"
            assert kwargs["skip_existing"] is True
            return {"ok": True, "count": 0}

    monkeypatch.setattr(
        "research.position_quality_lightgbm.PositionQualityLightGBMService",
        FakeShadowService,
    )
    monkeypatch.setattr(
        "research.open_quality_lightgbm.OpenQualityLightGBMService",
        FakeShadowService,
    )
    monkeypatch.setattr(
        "research.factor_governance_lightgbm.FactorGovernanceLightGBMService",
        FakeShadowService,
    )
    monkeypatch.setattr(
        "research.meta_model_lightgbm.MetaModelLightGBMService",
        FakeShadowService,
    )
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
    assert set(result["shadow_refresh"]["models"]) == {
        "position_quality_lightgbm",
        "open_quality_lightgbm",
        "factor_governance_lightgbm",
        "meta_model_lightgbm",
    }


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


def test_offmarket_high_load_scores_all_models_after_full_training(monkeypatch, tmp_path):
    db_path = tmp_path / "offmarket-full-training.db"
    live_service._live_state["market_session"] = {
        "status": "closed_confirmed",
        "high_load_allowed": True,
        "high_load_profile": "full",
    }
    scored = []

    def fake_service(model_type):
        class FakeService:
            def __init__(self, db_path=None):
                self.db_path = db_path

            def train(self, **kwargs):
                return {
                    "ok": True,
                    "artifact_path": str(tmp_path / f"{model_type}.json"),
                    "metrics": {"sample_count": 100},
                }

            def score_samples(self, **kwargs):
                scored.append((model_type, dict(kwargs)))
                assert kwargs["limit"] == 100
                assert kwargs["mode"] == "offmarket_shadow_after_train"
                assert kwargs["skip_existing"] is False
                return {"ok": True, "count": 1}

        return FakeService

    monkeypatch.setattr(
        "research.position_quality_lightgbm.PositionQualityLightGBMService",
        fake_service("position_quality_lightgbm"),
    )
    monkeypatch.setattr(
        "research.open_quality_lightgbm.OpenQualityLightGBMService",
        fake_service("open_quality_lightgbm"),
    )
    monkeypatch.setattr(
        "research.factor_governance_lightgbm.FactorGovernanceLightGBMService",
        fake_service("factor_governance_lightgbm"),
    )
    monkeypatch.setattr(
        "research.meta_model_lightgbm.MetaModelLightGBMService",
        fake_service("meta_model_lightgbm"),
    )

    class FakeGovernance:
        def __init__(self, db_path=None):
            self.db_path = db_path

        def reconcile_active_models(self):
            return {"ok": True}

        def evaluate_artifact(self, artifact_path):
            return {"passed": False, "artifact_path": artifact_path}

    monkeypatch.setattr(
        "backend.services.model_influence_governance.ModelInfluenceGovernanceService",
        FakeGovernance,
    )

    result = live_service._scheduled_offmarket_position_quality_lightgbm(db_path=db_path)

    assert result["ok"] is True
    assert result["status"] == "done"
    assert {model_type for model_type, _ in scored} == {
        "position_quality_lightgbm",
        "open_quality_lightgbm",
        "factor_governance_lightgbm",
        "meta_model_lightgbm",
    }
    assert all("shadow" in result["result"]["models"][model_type] for model_type, _ in scored)


def test_offmarket_high_load_trains_once_per_closed_window(monkeypatch, tmp_path):
    db_path = tmp_path / "offmarket-window-idempotency.db"
    live_service._live_state["market_session"] = {
        "status": "closed_confirmed",
        "high_load_allowed": True,
        "high_load_profile": "full",
        "now_ts": 1782390600.0,
        "seconds_to_open": 1800.0,
    }
    calls = {"train": 0}

    def fake_service(model_type):
        class FakeService:
            def __init__(self, db_path=None):
                self.db_path = db_path

            def train(self, **kwargs):
                calls["train"] += 1
                return {
                    "ok": True,
                    "artifact_path": str(tmp_path / f"{model_type}.json"),
                    "metrics": {"sample_count": 100},
                }

            def score_samples(self, **kwargs):
                return {"ok": True, "count": 1}

        return FakeService

    monkeypatch.setattr(
        "research.position_quality_lightgbm.PositionQualityLightGBMService",
        fake_service("position_quality_lightgbm"),
    )
    monkeypatch.setattr(
        "research.open_quality_lightgbm.OpenQualityLightGBMService",
        fake_service("open_quality_lightgbm"),
    )
    monkeypatch.setattr(
        "research.factor_governance_lightgbm.FactorGovernanceLightGBMService",
        fake_service("factor_governance_lightgbm"),
    )
    monkeypatch.setattr(
        "research.meta_model_lightgbm.MetaModelLightGBMService",
        fake_service("meta_model_lightgbm"),
    )

    class FakeGovernance:
        def __init__(self, db_path=None):
            self.db_path = db_path

        def reconcile_active_models(self):
            return {"ok": True}

        def evaluate_artifact(self, artifact_path):
            return {"passed": False, "artifact_path": artifact_path}

    monkeypatch.setattr(
        "backend.services.model_influence_governance.ModelInfluenceGovernanceService",
        FakeGovernance,
    )

    first = live_service._scheduled_offmarket_position_quality_lightgbm(db_path=db_path)
    second = live_service._scheduled_offmarket_position_quality_lightgbm(db_path=db_path)

    assert first["status"] == "done"
    assert second["skipped"] is True
    assert second["reason"] == "training_window_already_completed"
    assert calls["train"] == 4
