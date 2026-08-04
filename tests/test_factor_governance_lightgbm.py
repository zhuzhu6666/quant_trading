import json
import sqlite3

from research.factor_governance_lightgbm import (
    FEATURE_NAMES,
    MODEL_TYPE,
    MODEL_VERSION,
    FEATURE_SCHEMA_VERSION,
    FactorGovernanceLightGBMService,
)


def _create_factor_reviews(path):
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
        CREATE TABLE factor_contribution_review (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            review_id TEXT NOT NULL,
            trade_id TEXT DEFAULT '',
            factor TEXT NOT NULL,
            entry_contribution REAL DEFAULT 0.0,
            hold_contribution REAL DEFAULT 0.0,
            exit_contribution REAL DEFAULT 0.0,
            net_contribution REAL DEFAULT 0.0,
            confidence REAL DEFAULT 0.0,
            notes TEXT DEFAULT ''
        );
        CREATE TABLE policy_suggestion (
            suggestion_id TEXT PRIMARY KEY,
            scope_type TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            action TEXT NOT NULL,
            confidence REAL DEFAULT 0.0,
            reason TEXT DEFAULT '',
            evidence_json TEXT DEFAULT '{}',
            status TEXT DEFAULT 'proposed',
            reviewed_at REAL DEFAULT 0.0,
            review_note TEXT DEFAULT '',
            created_at REAL NOT NULL DEFAULT 0.0
        );
        """
    )
    for i in range(18):
        positive = i % 2 == 0
        conn.execute(
            """
            INSERT INTO trade_outcome_review
            (review_id, trade_id, position_id, entry_quality, hold_quality,
             exit_quality, regime_fit_score, execution_quality, pnl, mae, mfe,
             outcome_label, review_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"rev_{i}",
                f"trade_{i}",
                f"pos_{i}",
                0.8 if positive else 0.2,
                0.7 if positive else 0.2,
                0.7 if positive else 0.2,
                0.8 if positive else 0.3,
                0.8 if positive else 0.4,
                2.0 if positive else -2.0,
                0.4 if positive else 2.4,
                2.8 if positive else 0.3,
                "small_win" if positive else "bad_loss",
                json.dumps({"case": i}),
                1000.0 + i,
            ),
        )
        conn.execute(
            """
            INSERT INTO factor_contribution_review
            (review_id, trade_id, factor, entry_contribution, hold_contribution,
             exit_contribution, net_contribution, confidence, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"rev_{i}",
                f"trade_{i}",
                "momentum_factor" if positive else "weak_factor",
                0.4 if positive else -0.4,
                0.2 if positive else -0.3,
                0.2 if positive else -0.2,
                0.8 if positive else -0.9,
                0.8,
                "",
            ),
        )
    conn.commit()
    conn.close()


def test_factor_governance_lightgbm_trains_or_reports_missing_dependency(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    artifact_dir = tmp_path / "artifacts"
    _create_factor_reviews(db_path)

    service = FactorGovernanceLightGBMService(db_path=db_path, artifact_dir=artifact_dir)
    samples = service.load_samples(limit=20)
    assert len(samples) == 12
    assert {sample["label_source"] for sample in samples} == {
        "next_same_factor_outcome_from_rolling_history"
    }
    replay_samples = [
        {
            **sample,
            "sample_id": f"replay-{sample['sample_id']}",
            "trade_id": f"replay-{sample['trade_id']}",
            "review_id": f"replay-{sample['review_id']}",
            "source": "historical_replay",
        }
        for sample in samples[:6]
    ]
    monkeypatch.setattr(
        "backend.services.parity_replay.load_parity_learning_samples",
        lambda kind: replay_samples if kind == "factor" else [],
    )

    result = service.train(limit=20, min_samples=6, register=False)

    if not result["ok"]:
        assert result["error"] == "dependency_missing"
        return

    assert result["model_type"] == MODEL_TYPE
    assert result["feature_schema_version"] == FEATURE_SCHEMA_VERSION
    assert result["metrics"]["distinct_trade_count"] == 12
    assert result["metrics"]["train_trade_count"] + result["metrics"]["holdout_trade_count"] == 12
    assert result["metrics"]["holdout"]["majority_baseline_accuracy"] is not None
    assert result["metrics"]["holdout"]["balanced_accuracy"] is not None
    assert result["metrics"]["replay_sample_count"] == 6
    assert result["metrics"]["real_holdout_count"] == result["metrics"]["holdout"]["count"]
    assert result["metrics"]["distinct_trade_count"] == 12
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
    assert result["capabilities"]["live_trading"] is False
    assert result["capabilities"]["can_change_factor_weights"] is False
    shadow = service.score_samples(
        artifact_path=result["artifact_path"],
        limit=10,
        materialize=True,
        min_weakness_score=0.1,
    )
    assert shadow["ok"] is True
    scored_samples = service.load_samples(limit=10)
    assert shadow["count"] == len(scored_samples)
    audits = service.list_audits(limit=20)
    assert audits["count"] == len(scored_samples)
    assert audits["items"][0]["result"]["capabilities"]["shadow_only"] is True
    rows = sqlite3.connect(str(db_path)).execute(
        "SELECT scope_type, action, status, evidence_json FROM policy_suggestion"
    ).fetchall()
    assert rows
    assert rows[0][0] == "factor"
    assert rows[0][1] == "review_factor_weight_or_template"
    assert rows[0][2] == "proposed"
    evidence = json.loads(rows[0][3])
    assert evidence["source_agent"] == "lightgbm_shadow_models"
    assert evidence["agent_context"]["schema_version"] == "agent_generation_context.v1"
    assert evidence["agent_context"]["authority_verdict"]["advisory_only"] is True


def test_factor_governance_lightgbm_skips_system_contaminated_reviews(tmp_path):
    db_path = tmp_path / "state.db"
    artifact_dir = tmp_path / "artifacts"
    _create_factor_reviews(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        system_issue = {
            "schema_version": "trade_review_system_issue.v1",
            "system_contaminated": True,
            "contaminates_learning": True,
            "labels": ["market_data_stale", "signal_execution_delay"],
        }
        conn.execute(
            """
            UPDATE trade_outcome_review
            SET review_json=?
            WHERE review_id='rev_1'
            """,
            (json.dumps({"case": 1, "system_issue_context": system_issue}),),
        )
        conn.execute(
            """
            UPDATE factor_contribution_review
            SET notes=?
            WHERE review_id='rev_1'
            """,
            (json.dumps({"system_contaminated": True}),),
        )
        conn.commit()
    finally:
        conn.close()

    service = FactorGovernanceLightGBMService(db_path=db_path, artifact_dir=artifact_dir)
    samples = service.load_samples(limit=20)

    assert len(samples) == 11
    assert all(sample["review_id"] != "rev_1" for sample in samples)


def test_factor_governance_demo_bridge_emits_whitelisted_downweight(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _create_factor_reviews(db_path)
    service = FactorGovernanceLightGBMService(db_path=db_path, artifact_dir=tmp_path / "artifacts")

    conn = sqlite3.connect(str(db_path))
    try:
        conn.executemany(
            """
            INSERT INTO factor_governance_shadow_audit
            (inference_id, model_type, model_version, review_id, factor,
             mode, positive_score, weakness_score, prediction, created_at)
            VALUES (?, ?, ?, ?, ?, 'shadow', ?, ?, 0, ?)
            """,
            [
                ("fg_demo_1", MODEL_TYPE, "1.0", "rev_1", "weak_factor", 0.10, 0.90, 10.0),
                ("fg_demo_2", MODEL_TYPE, "1.0", "rev_3", "weak_factor", 0.05, 0.95, 11.0),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(
        "backend.services.factor_catalog.build_factor_catalog",
        lambda _db_path: [
            {
                "factor_id": "weak_factor",
                "used_in_score": True,
                "role": "alpha",
                "weight": 0.25,
                "health_score": 32.0,
                "health_status": "DECAYING",
            }
        ],
    )
    monkeypatch.setattr(
        "backend.services.model_influence.ModelInfluenceService.active_policy",
        classmethod(lambda cls, model_type, cfg: {
            "stage": "demo_canary",
            "feature_schema_version": "pit.v2",
            "allowed_effects": ["suggest_downweight"],
            "artifact_path": "",
        }),
    )

    result = service.materialize_demo_governance_advisories()

    assert result["materialized"] is True
    assert result["count"] == 1
    assert result["items"][0]["action"] == "downweight"
    evidence = result["items"][0]["evidence"]
    assert evidence["bridge"]["automatic_demo"] is True
    assert evidence["bridge"]["demo_nursery"] is True
    assert evidence["governed_action"] == "downweight"
    assert evidence["direct_model_application"] is False

    row = sqlite3.connect(str(db_path)).execute(
        "SELECT action, status FROM policy_suggestion WHERE suggestion_id=?",
        (result["items"][0]["suggestion_id"],),
    ).fetchone()
    assert row == ("downweight", "proposed")


def test_factor_governance_demo_bridge_supersedes_inactive_target(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _create_factor_reviews(db_path)
    service = FactorGovernanceLightGBMService(db_path=db_path, artifact_dir=tmp_path / "artifacts")
    evidence = {
        "model_type": MODEL_TYPE,
        "source_agent": "lightgbm_shadow_models",
        "bridge": {"automatic_demo": True, "demo_nursery": True},
    }
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence,
             evidence_json, status, created_at)
            VALUES ('fgm_stale', 'factor', 'quarantined_factor', 'downweight',
                    0.9, ?, 'approved', 10.0)
            """,
            (json.dumps(evidence),),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr("backend.services.factor_catalog.build_factor_catalog", lambda _db_path: [])
    result = service.materialize_demo_governance_advisories()

    assert result["stale_superseded"] == 1
    row = sqlite3.connect(str(db_path)).execute(
        "SELECT status, review_note FROM policy_suggestion WHERE suggestion_id='fgm_stale'"
    ).fetchone()
    assert row == ("superseded", "superseded: factor is no longer active in runtime score")


# ── 批次 A: regime 特征进入治理模型 ─────────────────────────────

REGIME_FEATURES = {
    "current_regime_fit_score",
    "rolling_regime_fit_avg",
    "rolling_regime_fit_min",
}


def test_batch_a_feature_names_include_regime_dimensions():
    """FEATURE_NAMES 必须包含 regime 条件维度，不能只被 SQL SELECT 后丢弃。"""
    missing = REGIME_FEATURES - set(FEATURE_NAMES)
    assert not missing, f"regime 特征缺失: {missing}"


def test_batch_a_samples_carry_regime_features(tmp_path):
    """load_samples 产出的每个样本 features 必须含 regime 维度且来自 regime_fit_score。"""
    db_path = tmp_path / "state.db"
    artifact_dir = tmp_path / "artifacts"
    _create_factor_reviews(db_path)

    service = FactorGovernanceLightGBMService(db_path=db_path, artifact_dir=artifact_dir)
    samples = service.load_samples(limit=20)
    assert samples
    for sample in samples:
        feats = sample["features"]
        for name in REGIME_FEATURES:
            assert name in feats, f"{name} 未进入样本特征"
            assert isinstance(feats[name], (int, float)), f"{name} 非数值: {feats[name]!r}"
    # fixture 里 regime_fit_score 有区分度(0.8/0.3)，当前特征不应全为 0
    any_nonzero = any(
        sample["features"].get("current_regime_fit_score", 0.0) != 0.0
        for sample in samples
    )
    # 当前行 regime_fit_score 至少遇到一个非零
    assert any_nonzero


def test_batch_a_train_reports_version_bump():
    """治理模型版本号必须因加入 regime 特征而升版(schema 版本标识特征集)。"""
    # 只验证常量已定义且版本字符串是新 schema；train 本身在
    # test_batch_a_train_schema_bump 验证
    assert MODEL_VERSION == "5.0"
    assert FEATURE_SCHEMA_VERSION.startswith("pit.v3")


def test_batch_a_train_schema_bump(tmp_path, monkeypatch):
    """训练产出 feature_count 含 regime 维度且 schema 版本为 v3。"""
    db_path = tmp_path / "state.db"
    artifact_dir = tmp_path / "artifacts"
    _create_factor_reviews(db_path)

    service = FactorGovernanceLightGBMService(db_path=db_path, artifact_dir=artifact_dir)
    samples = service.load_samples(limit=20)
    replay_samples = [
        {
            **sample,
            "sample_id": f"replay-{sample['sample_id']}",
            "trade_id": f"replay-{sample['trade_id']}",
            "review_id": f"replay-{sample['review_id']}",
            "source": "historical_replay",
        }
        for sample in samples[:6]
    ]
    monkeypatch.setattr(
        "backend.services.parity_replay.load_parity_learning_samples",
        lambda kind: replay_samples if kind == "factor" else [],
    )

    result = service.train(limit=20, min_samples=6, register=False)

    if not result["ok"]:
        assert result["error"] == "dependency_missing"
        return
    assert result["feature_schema_version"] == FEATURE_SCHEMA_VERSION
    assert result["feature_schema_version"].startswith("pit.v3")
    assert result["metrics"]["feature_count"] == len(FEATURE_NAMES)
    assert REGIME_FEATURES.issubset(set(FEATURE_NAMES))
