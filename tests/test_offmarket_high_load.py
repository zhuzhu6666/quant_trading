import json
import sqlite3
from decimal import Decimal

from backend.services import live_service
from backend.services.learning_research_jobs import (
    _claim_training_window,
    _record_offmarket_audit,
    run_offmarket_position_quality_job,
)


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
    assert result["shadow_refresh"] == {
        "ok": False,
        "skipped": True,
        "reason": "market_session_not_safe_for_shadow_refresh",
        "models": {},
    }


def test_offmarket_quality_uses_projection_when_live_session_is_missing(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        live_service,
        "_live_state_get",
        lambda key, default=None, clone=False: {},
    )

    def fake_quality_job(*, session, db_path):
        captured["session"] = session
        captured["db_path"] = db_path
        return {"ok": True}

    monkeypatch.setattr(
        "backend.services.learning_research_jobs.run_offmarket_position_quality_job",
        fake_quality_job,
    )

    result = live_service._scheduled_offmarket_position_quality_lightgbm()

    assert result == {"ok": True}
    assert captured == {"session": None, "db_path": None}


def test_offmarket_high_load_runs_training_when_closed(monkeypatch, tmp_path):
    db_path = tmp_path / "offmarket-training.db"
    live_service._live_state["market_session"] = {
        "status": "closed_pending_positions",
        "high_load_allowed": True,
        "high_load_profile": "limited_with_positions",
    }
    trained = []

    def fake_service(model_type, train_limit):
        class FakeService:
            def __init__(self, db_path=None):
                self.db_path = db_path

            def train(self, **kwargs):
                assert kwargs["limit"] == train_limit
                assert kwargs["register"] is True
                trained.append(model_type)
                return {
                    "ok": True,
                    "artifact_path": str(tmp_path / f"{model_type}.json"),
                    "metrics": {"sample_count": 20},
                }

            def score_samples(self, **kwargs):
                assert kwargs["limit"] == 30
                assert kwargs["mode"] == "offmarket_shadow_after_train"
                return {"ok": True, "count": 30}

        return FakeService

    monkeypatch.setattr(
        "research.position_quality_lightgbm.PositionQualityLightGBMService",
        fake_service("position_quality_lightgbm", 250),
    )
    monkeypatch.setattr(
        "research.factor_governance_lightgbm.FactorGovernanceLightGBMService",
        fake_service("factor_governance_lightgbm", 5000),
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
    assert result["audit"]["status"] == "done"
    assert result["audit"]["high_load_profile"] == "limited_with_positions"
    assert trained == ["position_quality_lightgbm", "factor_governance_lightgbm"]
    assert "shadow" in result["result"]["models"]["factor_governance_lightgbm"]


def test_offmarket_position_failure_does_not_hide_factor_model_result(monkeypatch, tmp_path):
    db_path = tmp_path / "offmarket-isolated-models.db"
    live_service._live_state["market_session"] = {
        "status": "closed_pending_positions",
        "high_load_allowed": True,
        "high_load_profile": "limited_with_positions",
    }

    def fake_service(model_type):
        class FakeService:
            def __init__(self, db_path=None):
                self.db_path = db_path

            def train(self, **kwargs):
                if model_type == "position_quality_lightgbm":
                    raise RuntimeError("position samples are sparse")
                return {
                    "ok": True,
                    "status": "trained",
                    "artifact_path": str(tmp_path / f"{model_type}.json"),
                    "metrics": {"sample_count": 100},
                }

            def score_samples(self, **kwargs):
                return {"ok": True, "count": 1, "model_type": model_type}

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
    assert result["result"]["models"]["position_quality_lightgbm"]["train"]["status"] == "failed"
    assert result["result"]["models"]["factor_governance_lightgbm"]["train"]["status"] == "trained"
    assert result["result"]["models"]["factor_governance_lightgbm"]["shadow"]["ok"] is True


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
    assert calls["train"] == 3


def test_training_only_runs_position_quality_once_without_shadow_or_governance(monkeypatch, tmp_path):
    db_path = tmp_path / "training-only.db"
    session = {
        "status": "closed_confirmed",
        "high_load_allowed": True,
        "high_load_profile": "full",
        "now_ts": 1782390600.0,
        "seconds_to_open": 1800.0,
    }
    calls = {"train": 0, "score": 0}

    class FakePositionQuality:
        def __init__(self, db_path=None):
            self.db_path = db_path
            self.last_data_quality = {
                "unique_review_bytes": 100,
                "selected_verdict_bytes": 200,
                "input_bytes_estimate": 300,
            }

        def train(self, **kwargs):
            calls["train"] += 1
            assert kwargs["limit"] == 4000
            assert kwargs["horizon_minutes"] == 30
            assert kwargs["register"] is False
            return {
                "ok": True,
                "status": "trained",
                "artifact_path": str(tmp_path / "position-quality.json"),
                "data_quality": dict(self.last_data_quality),
            }

        def score_samples(self, **_kwargs):
            calls["score"] += 1
            raise AssertionError("training_only must not score shadow samples")

    monkeypatch.setattr(
        "research.position_quality_lightgbm.PositionQualityLightGBMService",
        FakePositionQuality,
    )

    first = run_offmarket_position_quality_job(
        session=session,
        db_path=db_path,
        execution_mode="training_only",
    )
    second = run_offmarket_position_quality_job(
        session=session,
        db_path=db_path,
        execution_mode="training_only",
    )

    assert first["status"] == "done"
    assert first["result"]["models"].keys() == {"position_quality_lightgbm"}
    assert first["result"]["models"]["position_quality_lightgbm"]["shadow"]["skipped"] is True
    assert first["result"]["governance"]["skipped"] is True
    assert first["result"]["promoted_models"] == []
    assert first["audit"]["status"] == "done"
    assert second["skipped"] is True
    assert second["reason"] == "training_window_already_completed"
    assert calls == {"train": 1, "score": 0}


def test_training_window_guard_blocks_fresh_running_window(tmp_path):
    db_path = tmp_path / "running-window.db"
    session = {"status": "closed_confirmed", "high_load_profile": "full"}
    payload = {"training_window_key": "full:next_open:123"}
    first = _claim_training_window(
        db_path=db_path,
        job_name="position_quality",
        window_key=payload["training_window_key"],
        session=session,
        payload=payload,
        started_at=100.0,
    )
    second = _claim_training_window(
        db_path=db_path,
        job_name="position_quality",
        window_key=payload["training_window_key"],
        session=session,
        payload=payload,
        started_at=101.0,
    )
    assert first["claimed"] is True
    assert second["claimed"] is False
    assert second["reason"] == "training_window_running"


def test_training_window_guard_marks_stale_worker_process_loss(tmp_path):
    db_path = tmp_path / "stale-window.db"
    window_key = "full:next_open:456"
    _record_offmarket_audit(
        job_name="position_quality",
        status="running",
        session={"status": "closed_confirmed", "high_load_profile": "full"},
        payload={"training_window_key": window_key},
        started_at=1.0,
        db_path=db_path,
        training_window_key=window_key,
        heartbeat_at=1.0,
    )
    result = _claim_training_window(
        db_path=db_path,
        job_name="position_quality",
        window_key=window_key,
        session={"status": "closed_confirmed", "high_load_profile": "full"},
        payload={"training_window_key": window_key},
        started_at=2.0,
    )
    assert result["claimed"] is False
    assert result["reason"] == "training_window_aborted_process_loss"


def test_training_only_can_retry_one_unfinished_terminal_window(tmp_path):
    db_path = tmp_path / "retry-window.db"
    window_key = "full:next_open:789"
    initial = _record_offmarket_audit(
        job_name="position_quality",
        status="aborted_process_loss",
        session={"status": "closed_confirmed", "high_load_profile": "full"},
        payload={"training_window_key": window_key},
        result={"training_artifact": {"created": False}},
        db_path=db_path,
        training_window_key=window_key,
        audit_id="position_quality:window:retry",
    )

    retry = _claim_training_window(
        db_path=db_path,
        job_name="position_quality",
        window_key=window_key,
        session={"status": "closed_confirmed", "high_load_profile": "full"},
        payload={"training_window_key": window_key},
        started_at=2.0,
        allow_retry_terminal=True,
    )
    assert retry["claimed"] is True
    assert retry["retry_count"] == 1
    assert retry["audit_id"] == initial["audit_id"]

    _record_offmarket_audit(
        job_name="position_quality",
        status="failed",
        session={"status": "closed_confirmed", "high_load_profile": "full"},
        payload={"training_window_key": window_key, "retry_count": 1},
        db_path=db_path,
        training_window_key=window_key,
        audit_id=initial["audit_id"],
    )
    no_third_retry = _claim_training_window(
        db_path=db_path,
        job_name="position_quality",
        window_key=window_key,
        session={"status": "closed_confirmed", "high_load_profile": "full"},
        payload={"training_window_key": window_key},
        started_at=3.0,
        allow_retry_terminal=True,
    )
    assert no_third_retry["claimed"] is False
    assert no_third_retry["reason"] == "training_window_already_terminal"


def test_offmarket_audit_serializes_decimal_result_without_losing_audit(tmp_path):
    db_path = tmp_path / "decimal-audit.db"

    audit = _record_offmarket_audit(
        job_name="position_quality",
        status="blocked_memory_budget",
        session={"status": "closed_confirmed", "high_load_profile": "full"},
        payload={"input_bytes": Decimal("36910849")},
        result={"peak_rss_bytes": Decimal("123.5")},
        db_path=db_path,
    )

    assert audit["status"] == "blocked_memory_budget"
    conn = sqlite3.connect(db_path)
    try:
        payload_json, result_json = conn.execute(
            "SELECT payload_json, result_json FROM offmarket_high_load_job_audit"
        ).fetchone()
    finally:
        conn.close()
    assert json.loads(payload_json)["input_bytes"] == "36910849"
    assert json.loads(result_json)["peak_rss_bytes"] == "123.5"
