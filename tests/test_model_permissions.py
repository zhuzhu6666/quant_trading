import json
import sqlite3

from backend.services.model_permissions import (
    list_model_permission_audits,
    validate_model_artifact,
)
from research.position_quality_lightgbm import PositionQualityLightGBMService


def test_model_permission_allows_shadow_advisory_artifact(tmp_path):
    db_path = tmp_path / "state.db"
    artifact = {
        "model_type": "demo_model",
        "capabilities": {
            "live_trading": False,
            "advisory_only": True,
            "shadow_only": True,
            "can_place_orders": False,
            "can_close_positions": False,
            "can_change_risk_limits": False,
        },
    }

    result = validate_model_artifact(artifact, model_type="demo_model", db_path=db_path)

    assert result["ok"] is True
    assert result["status"] == "allowed"
    audits = list_model_permission_audits(db_path=db_path)
    assert audits["count"] == 1
    assert audits["items"][0]["status"] == "allowed"


def test_model_permission_blocks_live_or_mutating_artifact(tmp_path):
    db_path = tmp_path / "state.db"
    artifact = {
        "model_type": "unsafe_model",
        "capabilities": {
            "live_trading": True,
            "advisory_only": True,
            "shadow_only": True,
            "can_close_positions": True,
            "can_bypass_risk_policy": True,
        },
    }

    result = validate_model_artifact(artifact, model_type="unsafe_model", db_path=db_path)

    assert result["ok"] is False
    assert result["status"] == "blocked"
    violations = {item["capability"] for item in result["violations"]}
    assert "live_trading" in violations
    assert "can_close_positions" in violations
    assert "can_bypass_risk_policy" in violations


def test_position_quality_shadow_run_blocks_unsafe_artifact_before_scoring(tmp_path):
    db_path = tmp_path / "state.db"
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    model_file = artifact_dir / "unsafe.joblib"
    model_file.write_bytes(b"not-a-real-model")
    artifact_path = artifact_dir / "unsafe.json"
    artifact_path.write_text(
        json.dumps(
            {
                "model_type": "position_quality_lightgbm",
                "model_version": "x",
                "artifact_path": str(artifact_path),
                "model_file": str(model_file),
                "capabilities": {
                    "live_trading": True,
                    "advisory_only": True,
                    "shadow_only": True,
                    "can_place_orders": True,
                },
            }
        ),
        encoding="utf-8",
    )
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE trade_outcome_review (
            review_id TEXT PRIMARY KEY,
            trade_id TEXT DEFAULT '',
            position_id TEXT DEFAULT '',
            entry_decision_id TEXT DEFAULT '',
            exit_decision_id TEXT DEFAULT '',
            entry_quality REAL DEFAULT 0.0,
            hold_quality REAL DEFAULT 0.0,
            exit_quality REAL DEFAULT 0.0,
            regime_fit_score REAL DEFAULT 0.0,
            execution_quality REAL DEFAULT 0.0,
            pnl REAL DEFAULT 0.0,
            mae REAL DEFAULT 0.0,
            mfe REAL DEFAULT 0.0,
            outcome_label TEXT DEFAULT '',
            failure_tags_json TEXT DEFAULT '[]',
            summary_text TEXT DEFAULT '',
            review_json TEXT DEFAULT '{}',
            created_at REAL NOT NULL DEFAULT 0.0
        )
        """
    )
    conn.commit()
    conn.close()

    service = PositionQualityLightGBMService(db_path=db_path, artifact_dir=artifact_dir)
    result = service.score_samples(artifact_path=artifact_path, limit=1)

    assert result["ok"] is False
    assert result["error"] == "model_permission_violation"
    assert result["permission"]["status"] == "blocked"
