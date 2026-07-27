import json
import sqlite3

from research.open_quality_lightgbm import MODEL_TYPE, OpenQualityLightGBMService


def _init_db(path):
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE autonomous_learning_sample (
            sample_id TEXT PRIMARY KEY,
            sample_type TEXT NOT NULL,
            source_table TEXT DEFAULT '',
            source_id TEXT DEFAULT '',
            decision_id TEXT DEFAULT '',
            trade_id TEXT DEFAULT '',
            position_id TEXT DEFAULT '',
            symbol TEXT DEFAULT '',
            timeframe TEXT DEFAULT '',
            event_ts REAL NOT NULL DEFAULT 0.0,
            label_status TEXT DEFAULT 'pending',
            integrity TEXT DEFAULT 'full',
            train_weight REAL DEFAULT 1.0,
            features_json TEXT DEFAULT '{}',
            verdict_json TEXT DEFAULT '{}',
            label_json TEXT DEFAULT '{}',
            trace_json TEXT DEFAULT '{}',
            evidence_contract_json TEXT DEFAULT '{}',
            config_version INTEGER DEFAULT 0,
            config_hash TEXT DEFAULT '',
            evolution_run_id TEXT DEFAULT '',
            created_at REAL NOT NULL DEFAULT 0.0,
            updated_at REAL NOT NULL DEFAULT 0.0
        );
        """
    )
    contract = {
        "schema_version": "learning_evidence_contract.v1",
        "allowed_uses": ["audit", "explainability", "supervised_training"],
        "model_ready": True,
    }
    for idx in range(12):
        good = idx % 3 != 0
        action_score = 0.72 if good else 0.31
        same_count = 0 if good else 3
        features = {
            "action_score": action_score,
            "action": {
                "direction": 1 if idx % 2 == 0 else -1,
                "score": action_score,
                "tactical_score": action_score * 0.8,
                "macro_score": action_score * 0.2,
                "n_active_factors": 20 + idx,
                "n_abstain_factors": 3,
            },
            "entry_cluster": {
                "same_direction_open_count_before": same_count,
                "same_direction_open_count_after": same_count + 1,
                "pyramid_depth": max(0, same_count),
                "is_pyramid": same_count > 0,
                "recent_same_direction_entries": {"5m": same_count, "15m": same_count, "30m": same_count},
                "same_direction_api_volume_before": same_count * 100.0,
                "same_direction_api_volume_after": (same_count + 1) * 100.0,
                "open_position_count_before": same_count,
                "open_position_count_after": same_count + 1,
            },
            "event_context": {"event_near": not good, "multiplier": 0.2 if not good else 1.0},
            "decision_quality_context": {
                "schema_version": "decision_quality_context.v1",
                "composer_version": "factor_roles.v2",
                "factor_roles": {"rsi": "alpha", "atr": "risk"},
                "n_active_alpha_factors": 1,
                "factor_conflict_ratio": 0.1 if good else 0.7,
                "positive_contribution_abs": 2.0 if good else 0.4,
                "negative_contribution_abs": 0.3 if good else 2.2,
            },
            "bar_context": {"schema_version": "decision_bar.v1", "complete": True},
            "market_micro_context": {"quote_fresh": True, "quote_age_seconds": 0.1},
        }
        label = {
            "label": "open_outcome",
            "outcome_label": "good_win" if good else "bad_loss",
            "pnl": 1.0 if good else -1.0,
        }
        conn.execute(
            """
            INSERT INTO autonomous_learning_sample
            (sample_id, sample_type, source_table, source_id, decision_id, trade_id,
             position_id, symbol, timeframe, event_ts, label_status, integrity,
             train_weight, features_json, label_json, trace_json,
            evidence_contract_json, config_hash, created_at, updated_at)
            VALUES (?, 'shadow_open_decision', 'decision_ledger', ?, ?, ?, ?,
                    'XAUUSD+', 'M5', ?, 'matured', 'full', 1.0, ?, ?, ?, ?, 'cfg-current', ?, ?)
            """,
            (
                f"als_open_{idx}",
                f"dec_{idx}",
                f"dec_{idx}",
                f"trade_{idx}",
                f"pos_{idx}",
                1000.0 + idx,
                json.dumps(features),
                json.dumps(label),
                json.dumps({"decision_id": f"dec_{idx}", "position_id": f"pos_{idx}"}),
                json.dumps(contract),
                1000.0 + idx,
                1000.0 + idx,
            ),
        )
    conn.commit()
    conn.close()


def test_open_quality_lightgbm_trains_or_reports_dependency(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _init_db(db_path)
    service = OpenQualityLightGBMService(db_path=db_path, artifact_dir=tmp_path / "artifacts")
    replay_samples = [
        {
            **sample,
            "sample_id": f"replay-{sample['sample_id']}",
            "trade_id": f"replay-{sample['trade_id']}",
            "source": "historical_replay",
        }
        for sample in service.load_samples(limit=20)[:6]
    ]
    monkeypatch.setattr(
        "backend.services.parity_replay.load_parity_learning_samples",
        lambda kind: replay_samples if kind == "open" else [],
    )

    result = service.train(limit=20, min_samples=6, holdout_ratio=0.25, register=False)

    if not result.get("ok"):
        assert result["error"] == "dependency_missing"
        return
    assert result["model_type"] == MODEL_TYPE
    assert result["metrics"]["split"] == "time_ordered_grouped_purged"
    assert result["feature_schema_version"] == "pit.v2.open_lineage"
    assert result["metrics"]["holdout"]["rule_accuracy"] is not None
    assert result["metrics"]["holdout"]["majority_baseline_accuracy"] is not None
    assert "model_lift_vs_rule" in result["metrics"]["holdout"]
    assert result["metrics"]["sample_count"] == 12
    assert result["metrics"]["replay_sample_count"] == 6
    assert result["metrics"]["real_holdout_count"] == result["metrics"]["holdout"]["count"]
    comparison = result["metrics"]["augmentation_comparison"]
    if comparison["selected"] == "augmented":
        baseline = comparison["baseline_real_holdout"]
        augmented = comparison["augmented_real_holdout"]
        comparable = [
            key for key in ("accuracy", "balanced_accuracy", "auc")
            if baseline[key] is not None and augmented[key] is not None
        ]
        assert all(augmented[key] >= baseline[key] for key in comparable)
        assert any(augmented[key] > baseline[key] for key in comparable)

    shadow = service.score_samples(artifact_path=result["artifact_path"], limit=5)
    assert shadow["ok"] is True
    assert shadow["count"] == 5
    audits = service.list_audits(limit=10)
    assert audits["count"] == 5
