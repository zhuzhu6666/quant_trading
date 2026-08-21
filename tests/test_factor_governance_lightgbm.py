import json
import sqlite3

from backend.services.canonical_v2 import (
    ensure_sqlite_schema,
    record_decision_event,
    record_review,
)
from research.factor_governance_lightgbm import (
    FEATURE_NAMES,
    MODEL_TYPE,
    MODEL_VERSION,
    FEATURE_SCHEMA_VERSION,
    FactorGovernanceLightGBMService,
)


def _create_factor_reviews(path, *, contaminated_indices=None):
    contaminated_indices = set(contaminated_indices or ())
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    ensure_sqlite_schema(conn)
    conn.execute(
        """
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
        )
        """
    )
    for i in range(18):
        positive = i % 2 == 0
        factor = "momentum_factor" if positive else "weak_factor"
        pnl = 2.0 if positive else -2.0
        entry_contribution = 0.4 if positive else -0.4
        hold_contribution = 0.2 if positive else -0.3
        exit_contribution = 0.2 if positive else -0.2
        net_contribution = 0.8 if positive else -0.9
        review_payload = {
            "case": i,
            "execution_quality_state": "full",
            "execution_quality_evidence": {
                "schema_version": "execution_quality_evidence.v2",
                "evidence_state": "full",
            },
            "factor_contributions": {
                factor: {
                    "entry_contribution": entry_contribution,
                    "hold_contribution": hold_contribution,
                    "exit_contribution": exit_contribution,
                    "net_contribution": net_contribution,
                    "confidence": 0.8,
                }
            },
        }
        if i in contaminated_indices:
            review_payload["system_issue_context"] = {
                "system_contaminated": True,
                "contaminates_learning": True,
                "labels": ["market_data_stale", "signal_execution_delay"],
            }
        decision_id = f"dec_{i}"
        record_decision_event(
            conn,
            decision_id=decision_id,
            event_type="entry_signal",
            symbol="XAUUSD+",
            timeframe="M5",
            decision_ts=1000.0 + i,
            trade_id=f"trade_{i}",
            position_id=f"pos_{i}",
            regime_id="trend=strong|volatility=high" if positive else "trend=weak|volatility=low",
            regime_confidence=0.8,
            factor_snapshots=[
                {
                    "decision_id": decision_id,
                    "factor": factor,
                    "source": "registry",
                    "raw_value": 1.5 if positive else -1.5,
                    "normalized_value": 0.6 if positive else -0.6,
                    "direction": 1.0 if positive else -1.0,
                    "base_weight": 0.3,
                    "policy_weight": 0.3,
                    "shadow_score": 0.5,
                    "health_score": 72.0 if positive else 30.0,
                    "gated": False,
                    "contribution_score": 0.18 if positive else -0.18,
                }
            ],
        )
        record_review(
            conn,
            review_id=f"rev_{i}",
            trade_id=f"trade_{i}",
            position_id=f"pos_{i}",
            entry_decision_id=decision_id,
            entry_quality=0.8 if positive else 0.2,
            hold_quality=0.7 if positive else 0.2,
            exit_quality=0.7 if positive else 0.2,
            regime_fit_score=0.8 if positive else 0.3,
            execution_quality=0.8 if positive else 0.4,
            pnl=pnl,
            mae=0.4 if positive else 2.4,
            mfe=2.8 if positive else 0.3,
            outcome_label="small_win" if positive else "bad_loss",
            failure_tags=[],
            review=review_payload,
            created_at=1000.0 + i,
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
    assert rows == []
    assert shadow["mutation_eligible"] is False
    assert shadow["materialization_blocked_reason"] == "blocked_by_model_quality_gate"


def test_factor_governance_lightgbm_skips_system_contaminated_reviews(tmp_path):
    db_path = tmp_path / "state.db"
    artifact_dir = tmp_path / "artifacts"
    _create_factor_reviews(db_path, contaminated_indices={1})

    service = FactorGovernanceLightGBMService(db_path=db_path, artifact_dir=artifact_dir)
    samples = service.load_samples(limit=20)

    assert len(samples) == 11
    assert all(sample["review_id"] != "rev_1" for sample in samples)


def test_factor_replay_samples_cannot_satisfy_real_distinct_trade_gate(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _create_factor_reviews(db_path)
    service = FactorGovernanceLightGBMService(
        db_path=db_path,
        artifact_dir=tmp_path / "artifacts",
    )
    samples = service.load_samples(limit=20)
    replay_samples = [
        {
            **samples[index % len(samples)],
            "sample_id": f"replay-{index}",
            "trade_id": f"replay-trade-{index}",
            "review_id": f"replay-review-{index}",
            "source": "historical_replay",
        }
        for index in range(25)
    ]
    monkeypatch.setattr(
        "backend.services.parity_replay.load_parity_learning_samples",
        lambda kind: replay_samples if kind == "factor" else [],
    )

    result = service.train(limit=20, min_samples=20, register=False)

    if result.get("error") == "dependency_missing":
        return
    assert result["ok"] is False
    assert result["status"] == "blocked"
    assert result["error"] == "insufficient_distinct_factor_trades"
    assert result["distinct_trade_count"] == 12
    assert result["replay_distinct_trade_count"] == 25


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
                (
                    f"fg_demo_{i}",
                    MODEL_TYPE,
                    "1.0",
                    f"rev_{i % 18}",
                    "weak_factor",
                    0.10 if i % 2 else 0.05,
                    0.90 if i % 2 else 0.95,
                    10.0 + i,
                )
                for i in range(20)
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
    monkeypatch.setattr(
        "backend.services.model_influence.ModelInfluenceService.policy_for",
        classmethod(lambda cls, model_type, cfg: {
            "artifact_path": "test-artifact.json",
        }),
    )
    monkeypatch.setattr(
        "backend.services.model_influence_governance.ModelInfluenceGovernanceService.evaluate_artifact",
        lambda self, path: {
            "schema_version": "model_promotion_gate.v1",
            "passed": True,
            "reason": "promotion_gate_passed",
            "artifact_sha256": "test-artifact",
            "failed_checks": [],
            "checks": [],
            "metrics": {
                "data_quality": {
                    "factor_generation": "runtime_bounded_v1",
                    "lineage_hash": "test-lineage",
                    "label_contract_hash": "test-label-contract",
                }
            },
        },
    )

    result = service.materialize_demo_governance_advisories(min_factor_sample_count=2)

    assert result["materialized"] is True
    assert result["count"] == 1
    assert result["items"][0]["action"] == "downweight"
    evidence = result["items"][0]["evidence"]
    assert evidence["bridge"]["automatic_demo"] is True
    assert evidence["bridge"]["demo_nursery"] is False
    assert evidence["bridge"]["autonomy_mode"] == "demo_autonomous"
    assert evidence["governed_action"] == "downweight"
    assert evidence["direct_model_application"] is False
    assert evidence["candidate_id"] == "factor_model:" + result["items"][0]["suggestion_id"]

    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT source_agent, source_kind, proposal_stage, action "
            "FROM brain_governance_candidate WHERE candidate_id=?",
            (evidence["candidate_id"],),
        ).fetchone()
        policy_suggestion_count = conn.execute(
            "SELECT COUNT(*) FROM policy_suggestion"
        ).fetchone()[0]
    finally:
        conn.close()
    assert row == (
        "factor_pruning_governance",
        "factor_governance_model_candidate",
        "brain_candidate",
        "downweight",
    )
    assert policy_suggestion_count == 0


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

    assert result["reason"] == "blocked_by_model_quality_gate"
    assert result["mutation_eligible"] is False
    row = sqlite3.connect(str(db_path)).execute(
        "SELECT status, review_note FROM policy_suggestion WHERE suggestion_id='fgm_stale'"
    ).fetchone()
    assert row == ("approved", "")


def test_factor_model_candidate_hard_blocks_factor_coverage_below_20(tmp_path):
    db_path = tmp_path / "state.db"
    _create_factor_reviews(db_path)
    service = FactorGovernanceLightGBMService(
        db_path=db_path,
        artifact_dir=tmp_path / "artifacts",
    )
    items = [
        {
            "inference_id": f"low_coverage_{index}",
            "factor": "weak_factor",
            "review_id": f"rev_{index}",
            "weakness_score": 0.92,
            "result": {
                "promotion_gate": {"passed": True},
                "mutation_eligible": True,
            },
        }
        for index in range(2)
    ]
    result = service.build_advisories(
        items=items,
        materialize=True,
        governed_action="downweight",
        min_weak_sample_count=2,
        min_factor_sample_count=1,
        evidence_context_by_factor={
            "weak_factor": {
                "promotion_gate": {"passed": True},
                "mutation_eligible": True,
                "artifact_sha256": "artifact",
                "model_version": "6.0",
                "factor_generation": "runtime_bounded_v1",
                "lineage_hash": "lineage",
                "label_contract_hash": "label",
                "sample_count": 19,
                "weak_sample_count": 2,
                "min_weakness_score": 0.85,
                "avg_weakness_score": 0.92,
                "active_factor_context": {
                    "used_in_score": True,
                    "role": "alpha",
                    "weight": 0.25,
                },
                "counter_evidence_refs": {"required_before_bridge": True},
            }
        },
    )

    assert result["materialized"] is False
    assert result["candidate_count"] == 0
    assert result["materialization"]["blocked_reasons"] == {
        "missing_model_candidate_contract": 1
    }
    assert sqlite3.connect(str(db_path)).execute(
        "SELECT COUNT(*) FROM policy_suggestion"
    ).fetchone()[0] == 0


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
    assert MODEL_VERSION == "6.0"
    assert FEATURE_SCHEMA_VERSION.startswith("pit.v4")


# ── 批次 F: 因子×regime 条件绩效(canonical decision payload) ──

REGIME_CONDITIONAL_FEATURES = {
    "same_regime_positive_rate",
    "same_regime_pnl_avg",
    "same_regime_sample_count",
}


def test_batch_f_feature_names_include_regime_conditional_dimensions():
    """FEATURE_NAMES 必须包含同 regime 条件绩效特征(因子×regime 真条件绩效)。"""
    missing = REGIME_CONDITIONAL_FEATURES - set(FEATURE_NAMES)
    assert not missing, f"同 regime 条件特征缺失: {missing}"


def test_batch_f_samples_carry_same_regime_conditional_features(tmp_path):
    """load_samples 产出的样本 features 必须含 same_regime_* 特征。

    fixture 中 momentum_factor 只在 trend=strong|volatility=high 出现(全赢),
    weak_factor 只在 trend=weak|volatility=low 出现(全输)——同 regime 条件
    绩效必须能区分这两种因子,而不是全部相同。
    """
    db_path = tmp_path / "state.db"
    artifact_dir = tmp_path / "artifacts"
    _create_factor_reviews(db_path)

    service = FactorGovernanceLightGBMService(db_path=db_path, artifact_dir=artifact_dir)
    samples = service.load_samples(limit=50)
    assert samples
    momentum = [s for s in samples if s["factor"] == "momentum_factor"]
    weak = [s for s in samples if s["factor"] == "weak_factor"]
    assert momentum and weak, f"fixture 因子未全部产出样本: momentum={len(momentum)} weak={len(weak)}"

    for sample in samples:
        feats = sample["features"]
        for name in REGIME_CONDITIONAL_FEATURES:
            assert name in feats, f"{name} 未进入样本特征"
            assert isinstance(feats[name], (int, float)), f"{name} 非数值: {feats[name]!r}"

    # momentum_factor 在 strong/high regime 下历史全赢 → 同 regime 胜率高
    m_positive = sum(s["features"].get("same_regime_positive_rate", 0.0) for s in momentum)
    m_avg_pnl = sum(s["features"].get("same_regime_pnl_avg", 0.0) for s in momentum)
    # weak_factor 在 weak/low regime 下历史全输 → 同 regime 胜率低
    w_positive = sum(s["features"].get("same_regime_positive_rate", 0.0) for s in weak)
    w_avg_pnl = sum(s["features"].get("same_regime_pnl_avg", 0.0) for s in weak)
    # 样本量足够时(≥5 个同 regime 历史),条件绩效必须有区分度
    m_n = sum(s["features"].get("same_regime_sample_count", 0.0) for s in momentum) / max(len(momentum), 1)
    assert m_n >= 3.0, f"momentum_factor 同 regime 历史样本不足: {m_n}"
    assert m_positive > w_positive, f"同 regime 胜率无区分度: momentum={m_positive} weak={w_positive}"
    assert m_avg_pnl > w_avg_pnl, f"同 regime pnl 无区分度: momentum={m_avg_pnl} weak={w_avg_pnl}"


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
    assert result["feature_schema_version"].startswith("pit.v4")
    assert result["metrics"]["feature_count"] == len(FEATURE_NAMES)
    assert REGIME_FEATURES.issubset(set(FEATURE_NAMES))


def test_re_review_quarantined_factor_reuses_newest_model(tmp_path, monkeypatch):
    """A quarantined factor whose model evidence is frozen at an old verdict
    is re-scored by the newest artifact (mode=quarantine_review), idempotently.

    This is the automated replacement for a human re-evaluating a freeze:
    no new trade reviews exist for quarantined factors, so the routine sweep
    never reaches them and only this path refreshes their model evidence.
    """
    import time

    db_path = tmp_path / "state.db"
    artifact_dir = tmp_path / "artifacts"
    _create_factor_reviews(db_path)

    service = FactorGovernanceLightGBMService(db_path=db_path, artifact_dir=artifact_dir)
    result = service.train(limit=20, min_samples=6, register=False)
    if not result["ok"]:
        assert result["error"] == "dependency_missing"
        return
    artifact_path = result["artifact_path"]

    # 模拟 7月2日那次 manual_shadow_eval_final:weak_factor 被旧模型打成 0.97
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO factor_governance_shadow_audit
        (inference_id, model_type, model_version, artifact_path, review_id,
         trade_id, position_id, factor, mode, positive_score, weakness_score,
         prediction, payload_json, result_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "factor_governance_lightgbm:rev_1:weak_factor:1782979155085",
            MODEL_TYPE,
            "5.0",
            "old_artifact_7_2",
            "rev_1",
            "trade_1",
            "pos_1",
            "weak_factor",
            "manual_shadow_eval_final_1782979134",
            0.03,
            0.97,
            0,
            "{}",
            "{}",
            1000.0,
        ),
    )
    conn.commit()
    conn.close()

    review = service.re_review_quarantined_factor(
        factor="weak_factor",
        artifact_path=artifact_path,
    )
    assert review["ok"] is True
    assert review["factor"] == "weak_factor"
    assert review["count"] >= 1
    assert review["artifact_path"] == artifact_path

    again = service.re_review_quarantined_factor(
        factor="weak_factor",
        artifact_path=artifact_path,
    )
    assert again["ok"] is True
    assert again.get("skipped") is True

    rows = sqlite3.connect(str(db_path)).execute(
        """
        SELECT mode, weakness_score, artifact_path
        FROM factor_governance_shadow_audit
        WHERE factor='weak_factor'
        ORDER BY created_at DESC
        """
    ).fetchall()
    assert rows[0][0] == "quarantine_review"
    assert rows[0][2] == artifact_path
    # 新推断的 weakness 来自最新模型,不是旧的 0.97 硬编码
    assert 0.0 <= rows[0][1] <= 1.0


def test_re_review_quarantined_factor_reports_no_historical_samples(tmp_path):
    """A factor with no historical review rows cannot be re-reviewed; the
    caller keeps the strict restore path (never widens risk)."""
    import sqlite3

    db_path = tmp_path / "state.db"
    artifact_dir = tmp_path / "artifacts"
    service = FactorGovernanceLightGBMService(db_path=db_path, artifact_dir=artifact_dir)

    result = service.re_review_quarantined_factor(factor="never_traded")
    assert result["ok"] is False
    assert result["error"] in {"artifact_missing", "no_historical_review_samples"}
