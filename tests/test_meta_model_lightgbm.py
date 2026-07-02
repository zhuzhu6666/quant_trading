import json
import sqlite3

from research.meta_model_lightgbm import MODEL_TYPE, MetaModelLightGBMService


def _create_reviews(path):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE trade_outcome_review (
            review_id TEXT PRIMARY KEY,
            trade_id TEXT DEFAULT '',
            position_id TEXT DEFAULT '',
            pnl REAL DEFAULT 0.0,
            mae REAL DEFAULT 0.0,
            mfe REAL DEFAULT 0.0,
            outcome_label TEXT DEFAULT '',
            review_json TEXT DEFAULT '{}',
            created_at REAL DEFAULT 0.0
        )
        """
    )
    labels = [
        ("bad_loss", -3.2),
        ("small_loss", -0.6),
        ("small_win", 0.8),
        ("good_win", 3.5),
        ("bad_loss", -2.8),
        ("small_win", 1.1),
        ("good_win", 2.6),
        ("small_loss", -0.4),
        ("bad_loss", -3.8),
        ("good_win", 3.1),
        ("small_win", 0.9),
        ("bad_loss", -2.4),
        ("good_win", 2.9),
        ("small_loss", -0.5),
        ("small_win", 0.7),
        ("bad_loss", -3.5),
        ("good_win", 3.4),
        ("small_win", 1.0),
    ]
    for idx, (outcome, pnl) in enumerate(labels):
        is_loss = pnl < 0
        payload = {
            "close_reason": "thesis_broken" if outcome == "bad_loss" else "broker_close" if pnl > 0 else "time_decay",
            "profit_capture_ratio": 0.2 if is_loss else 0.75,
            "giveback_ratio": 0.8 if is_loss else 0.2,
            "holding_efficiency": 0.2 if is_loss else 0.7,
            "mfe": 1.0 + idx * 0.1,
            "mae": 0.6 + idx * 0.05,
        }
        conn.execute(
            """
            INSERT INTO trade_outcome_review
            (review_id, trade_id, position_id, pnl, mae, mfe, outcome_label, review_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"review_{idx}",
                f"trade_{idx}",
                f"pos_{idx}",
                pnl,
                payload["mae"],
                payload["mfe"],
                outcome,
                json.dumps(payload),
                1000.0 + idx,
            ),
        )
    conn.commit()
    conn.close()


def _create_meta_signal_tables(path):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE decision_ledger (
            decision_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            risk_state_json TEXT DEFAULT '{}',
            created_at REAL DEFAULT 0.0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE position_lifecycle_event (
            event_id TEXT PRIMARY KEY,
            position_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            event_ts REAL DEFAULT 0.0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE position_quality_shadow_audit (
            inference_id TEXT PRIMARY KEY,
            prediction INTEGER DEFAULT 0,
            result_json TEXT DEFAULT '{}',
            created_at REAL DEFAULT 0.0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE factor_governance_shadow_audit (
            inference_id TEXT PRIMARY KEY,
            prediction INTEGER DEFAULT 0,
            result_json TEXT DEFAULT '{}',
            created_at REAL DEFAULT 0.0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE supervisor_counterfactual_review (
            counterfactual_id TEXT PRIMARY KEY,
            position_id TEXT NOT NULL,
            close_ts REAL DEFAULT 0.0,
            label TEXT DEFAULT ''
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE llm_advisory_audit (
            audit_id TEXT PRIMARY KEY,
            status TEXT DEFAULT '',
            created_at REAL DEFAULT 0.0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE model_permission_audit (
            audit_id TEXT PRIMARY KEY,
            status TEXT DEFAULT '',
            created_at REAL DEFAULT 0.0
        )
        """
    )
    conn.execute(
        "INSERT INTO decision_ledger VALUES ('d1', 'supervisor_close', ?, 1005.0)",
        (json.dumps({"policy_verdict": {"allowed": False, "reason": "late_session"}}),),
    )
    conn.execute(
        "INSERT INTO decision_ledger VALUES ('d2', 'supervisor_tighten', ?, 1006.0)",
        (json.dumps({"policy_verdict": {"allowed": True, "reason": "risk_reducing_action"}}),),
    )
    conn.execute("INSERT INTO position_lifecycle_event VALUES ('p1', 'pos_1', 'amend_skipped', 1006.0)")
    conn.execute("INSERT INTO position_lifecycle_event VALUES ('p2', 'pos_1', 'amend_failed', 1006.5)")
    conn.execute(
        "INSERT INTO position_quality_shadow_audit VALUES ('pq1', 0, ?, 1006.0)",
        (json.dumps({"prediction_label": "weak_position_quality"}),),
    )
    conn.execute(
        "INSERT INTO factor_governance_shadow_audit VALUES ('fg1', 0, ?, 1006.0)",
        (json.dumps({"prediction_label": "weak_factor_contribution"}),),
    )
    conn.execute("INSERT INTO supervisor_counterfactual_review VALUES ('cf1', 'pos_1', 1006.0, 'premature_tighten')")
    conn.execute("INSERT INTO llm_advisory_audit VALUES ('llm1', 'error', 1006.0)")
    conn.execute("INSERT INTO model_permission_audit VALUES ('mpa1', 'blocked', 1006.0)")
    conn.commit()
    conn.close()


def test_meta_model_lightgbm_trains_or_reports_missing_dependency(tmp_path):
    db_path = tmp_path / "state.db"
    artifact_dir = tmp_path / "artifacts"
    _create_reviews(db_path)

    service = MetaModelLightGBMService(db_path=db_path, artifact_dir=artifact_dir)
    result = service.train(limit=20, window=4, horizon=3, min_samples=6, register=False)

    if not result["ok"]:
        assert result["error"] == "dependency_missing"
        return

    assert result["model_type"] == MODEL_TYPE
    assert result["metrics"]["holdout"]["rule_accuracy"] is not None
    assert result["metrics"]["holdout"]["majority_baseline_accuracy"] is not None
    assert "model_lift_vs_rule" in result["metrics"]["holdout"]
    assert "rule_lift_vs_majority" in result["metrics"]["holdout"]
    assert result["metrics"]["governance_readiness"]["status"] in {
        "model_shadow_candidate",
        "rule_sidecar_candidate",
        "blocked_by_baseline",
    }
    assert result["metrics"]["governance_readiness"]["recommended_source"] in {
        "model_shadow_candidate",
        "rule_sidecar_candidate",
        "simple_baseline_observer",
    }
    assert result["capabilities"]["live_trading"] is False
    assert result["capabilities"]["can_change_risk_limits"] is False
    shadow = service.score_samples(
        artifact_path=result["artifact_path"],
        limit=12,
        window=4,
        horizon=3,
        materialize_ledger=True,
    )
    assert shadow["ok"] is True
    assert shadow["count"] == 11
    audits = service.list_audits(limit=20)
    assert audits["count"] == 11
    assert audits["items"][0]["result"]["capabilities"]["shadow_only"] is True
    assert audits["items"][0]["posture"] in {"contract", "observe", "recover"}
    assert "future_window" in audits["items"][0]["payload"]
    assert any(item["ledger_decision_id"] for item in audits["items"])
    report = service.build_shadow_report(limit=20)
    assert report["schema_version"] == "meta_model_shadow_report.v1"
    assert report["evaluated_count"] == 11
    assert set(report["confusion_matrix"]) == {"contract", "observe", "recover"}
    assert report["rule_comparison"]["compared_count"] == 11
    assert report["capabilities"]["live_trading"] is False
    assert report["artifact_summary"]["model_version"] == "1.1"


def test_meta_model_lightgbm_blocks_unsafe_artifact(tmp_path):
    db_path = tmp_path / "state.db"
    artifact_dir = tmp_path / "artifacts"
    _create_reviews(db_path)
    service = MetaModelLightGBMService(db_path=db_path, artifact_dir=artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / "unsafe.json"
    artifact_path.write_text(
        json.dumps(
            {
                "model_type": MODEL_TYPE,
                "model_file": str(artifact_dir / "missing.joblib"),
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

    result = service.score_samples(artifact_path=artifact_path, limit=5)

    assert result["ok"] is False
    assert result["error"] == "model_permission_violation"
    assert result["permission"]["status"] == "blocked"


def test_meta_model_lightgbm_v2_features_include_risk_and_shadow_signals(tmp_path):
    db_path = tmp_path / "state.db"
    _create_reviews(db_path)
    _create_meta_signal_tables(db_path)

    service = MetaModelLightGBMService(db_path=db_path, artifact_dir=tmp_path / "artifacts")
    samples = service.load_samples(limit=10, window=4, horizon=3)
    enriched = next(item for item in samples if item["created_at"] >= 1007.0)
    features = enriched["features"]

    assert features["future_window_trade_count"] == 3.0
    assert features["risk_blocked_count"] >= 1.0
    assert features["risk_allowed_count"] >= 1.0
    assert features["supervisor_close_count"] >= 1.0
    assert features["supervisor_tighten_count"] >= 1.0
    assert features["amend_skipped_count"] >= 1.0
    assert features["amend_failed_count"] >= 1.0
    assert features["position_quality_weak_rate"] == 1.0
    assert features["factor_governance_weak_rate"] == 1.0
    assert features["counterfactual_premature_rate"] == 1.0
    assert features["llm_error_rate"] == 1.0
    assert features["permission_block_rate"] == 1.0
    assert enriched["future_window"]["count"] == 3
