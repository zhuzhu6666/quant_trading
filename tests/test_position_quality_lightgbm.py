import json
import sqlite3

from research.position_quality_lightgbm import (
    MODEL_TYPE,
    PositionQualityLightGBMService,
)


def _create_reviews(path):
    conn = sqlite3.connect(str(path))
    conn.executescript(
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
        );
        CREATE TABLE position_supervisor_trace (
            trace_id TEXT PRIMARY KEY,
            position_id TEXT DEFAULT '',
            trade_id TEXT DEFAULT '',
            stage TEXT DEFAULT '',
            trace_integrity TEXT DEFAULT 'full',
            verdict_json TEXT DEFAULT '{}',
            context_json TEXT DEFAULT '{}',
            template_id TEXT DEFAULT '',
            template_version TEXT DEFAULT '',
            config_version INTEGER DEFAULT 0,
            config_hash TEXT DEFAULT '',
            event_ts REAL NOT NULL DEFAULT 0.0
        );
        """
    )
    for i in range(8):
        positive = i % 2 == 0
        payload = {
            "holding_seconds": 1252 if i == 0 else 120 + i * 10,
            "mfe": 3.0 if positive else 0.2,
            "mae": 0.5 if positive else 3.0,
            "giveback_ratio": 0.1 if positive else 0.9,
            "profit_capture_ratio": 0.7 if positive else 0.0,
            "time_in_profit": 90 if positive else 0,
            "holding_efficiency": 0.8 if positive else 0.05,
            "time_decay_score": 0.8 if positive else 0.2,
            "thesis_status": "intact" if positive else "broken",
            "regime_shift": "none",
            "close_reason": "broker_close" if positive else "thesis_broken",
        }
        conn.execute(
            """
            INSERT INTO trade_outcome_review
            (review_id, trade_id, position_id, pnl, mae, mfe, outcome_label,
             review_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"rev_{i}",
                f"trade_{i}",
                f"pos_{i}",
                1.0 if positive else -1.0,
                payload["mae"],
                payload["mfe"],
                "small_win" if positive else "bad_loss",
                json.dumps(payload),
                1000.0 + i,
            ),
        )
        conn.execute(
            """
            INSERT INTO position_supervisor_trace
            (trace_id, position_id, trade_id, stage, trace_integrity,
             verdict_json, context_json, template_id, template_version,
             config_version, config_hash, event_ts)
            VALUES (?, ?, ?, 'evaluated', 'full', ?, '{}', 'default', 'v-current',
                    1, 'cfg-current', ?)
            """,
            (
                f"trace_{i}", f"pos_{i}", f"trade_{i}",
                json.dumps({"action": "hold", "evidence": payload}),
                900.0 + i,
            ),
        )
    conn.commit()
    conn.close()


def test_position_quality_lightgbm_trains_or_reports_missing_dependency(tmp_path):
    db_path = tmp_path / "state.db"
    artifact_dir = tmp_path / "artifacts"
    _create_reviews(db_path)

    service = PositionQualityLightGBMService(db_path=db_path, artifact_dir=artifact_dir)
    samples = service.load_samples(limit=20)
    assert samples[0]["features"]["completed_bars_after_entry"] == 4.0
    result = service.train(limit=20, min_samples=4, register=False)

    if not result["ok"]:
        assert result["error"] == "dependency_missing"
        return

    assert result["model_type"] == MODEL_TYPE
    assert result["feature_schema_version"] == "pit.v2.position_h30"
    assert result["metrics"]["split"] == "time_ordered_grouped_purged"
    assert result["metrics"]["holdout"]["majority_baseline_accuracy"] is not None
    assert result["metrics"]["holdout"]["balanced_accuracy"] is not None
    assert result["metrics"]["holdout"]["negative_recall"] is not None
    assert result["capabilities"]["live_trading"] is False
    shadow = service.score_samples(artifact_path=result["artifact_path"], limit=8)
    assert shadow["ok"] is True
    assert shadow["count"] == 8
    audits = service.list_audits(limit=20)
    assert audits["count"] == 8
    assert audits["items"][0]["result"]["capabilities"]["shadow_only"] is True
