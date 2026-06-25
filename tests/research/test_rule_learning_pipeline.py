from __future__ import annotations

import json
import sqlite3

import pytest

from alpha.reflection.reviewer import TradeReviewer
from backend.api.learning import (
    DatasetModelCardRequest,
    DatasetTrainRequest,
    ModelCanaryReviewRequest,
    ModelCanaryTrialRequest,
    ModelInferenceRequest,
    ModelPipelineRunRequest,
    ModelPromotionGateRequest,
    ModelShadowQueueRequest,
    ModelShadowRunRequest,
    ModelShadowStatusRequest,
    build_learning_dataset_model_card,
    evaluate_learning_model_promotion,
    list_learning_model_canary_reviews,
    list_learning_model_canary_trials,
    list_learning_model_inference_audits,
    list_learning_model_shadow_candidates,
    queue_learning_model_shadow_candidate,
    run_governance,
    run_learning_model_shadow_validation,
    review_learning_model_canary,
    run_learning_model_canary_trial,
    run_learning_model_pipeline,
    score_learning_model_inference,
    train_learning_dataset,
    update_learning_model_shadow_candidate,
)
from backend.ledger.service import DecisionLedger
from research.features import (
    LearningDatasetBuilder,
    LearningDatasetReadiness,
    LearningDatasetValidator,
    LearningFeatureProvider,
)
from research.model_adapter import DatasetSummaryAdapter
from research.model_registry import ModelRegistry
from research.model_promotion import ModelPromotionGate
from research.model_canary import ModelCanaryReviewer
from research.model_canary_executor import ModelCanaryExecutor
from research.model_inference_contract import ModelInferenceContract
from research.model_pipeline import LearningModelPipeline
from research.model_shadow_queue import ModelShadowQueue
from research.model_shadow_runner import ModelShadowRunner
from research.offline_trainer import LearningStatisticalTrainer
from research.learning.experience_builder import ExperienceBuilder
from research.learning.governor import RuleEvolutionGovernor
from research.learning.policy_suggester import PolicySuggester


def _rows(db_path: str, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute(sql, params))
    finally:
        conn.close()


def test_rule_learning_pipeline_persists_full_chain(tmp_path):
    db_path = str(tmp_path / "state.db")
    ledger = DecisionLedger(db_path)
    reviewer = TradeReviewer(db_path)
    builder = ExperienceBuilder(db_path)
    suggester = PolicySuggester(db_path)
    gov = RuleEvolutionGovernor(db_path)

    entry_decision_id = ledger.log_decision(
        event_type="open",
        symbol="XAUUSD+",
        timeframe="M5",
        trade_id="101",
        position_id="101",
        decision_ts=900_000.0,
        action_score=0.82,
        action_reason="executed",
        action_json={"price": 3300.0},
        factor_snapshots=[
            {
                "factor": "trend_alpha",
                "raw_value": 1.2,
                "normalized_value": 0.8,
                "direction": 1.0,
                "base_weight": 0.2,
                "policy_weight": 0.2,
                "contribution_score": 0.16,
            },
            {
                "factor": "noise_factor",
                "raw_value": -0.6,
                "normalized_value": -0.7,
                "direction": -1.0,
                "base_weight": 0.35,
                "policy_weight": 0.35,
                "contribution_score": -0.245,
            },
        ],
    )
    assert entry_decision_id
    ledger.log_order_event(
        event_type="submitted",
        decision_id=entry_decision_id,
        trade_id="101",
        order_id="101",
        broker_order_id="101",
        price=3300.0,
        volume=100.0,
        status="submitted",
        details={"direction": 1},
        event_ts=900_000.0,
    )
    ledger.log_order_event(
        event_type="filled",
        decision_id=entry_decision_id,
        trade_id="101",
        order_id="101",
        broker_order_id="101",
        price=3300.5,
        volume=100.0,
        status="filled",
        details={"direction": 1},
        event_ts=900_001.0,
    )
    ledger.log_position_event(
        position_id="101",
        trade_id="101",
        symbol="XAUUSD+",
        event_type="opened",
        net_volume=100.0,
        avg_price=3300.5,
        details={"direction": 1},
        event_ts=900_002.0,
    )
    ledger.log_position_event(
        position_id="101",
        trade_id="101",
        symbol="XAUUSD+",
        event_type="closed",
        net_volume=0.0,
        avg_price=3280.0,
        realized_pnl=-120.0,
        details={"close_reason": "broker_close"},
        event_ts=1_000_000.0,
    )
    app_id = gov.log_application(
        scope_type="factor",
        scope_key="noise_factor",
        action="downweight",
        bias_multiplier=0.8,
        old_weight=0.35,
        new_weight=0.28,
        suggestion_ids=["s_noise"],
        cycle_ts=1.0,
        details={"reason": "pre-existing governance action"},
    )

    review = reviewer.review_closed_trade(
        position_id="101",
        pnl=-120.0,
        close_price=3280.0,
        close_ts=1_000_000.0,
        contributions={"trend_alpha": 10.0, "noise_factor": -90.0},
        exit_decision_id="dec_close_1",
        real_pnl={"net": -120.0, "commission": 4.0},
    )
    experience = builder.build_from_review(review)
    suggestion = suggester.suggest_from_experience(experience)

    assert review["outcome_label"] == "bad_loss"
    assert review["review_json"]["holding_seconds"] == pytest.approx(100_000.0)
    assert review["review_json"]["mfe"] == pytest.approx(0.0)
    assert review["review_json"]["mae"] == pytest.approx(120.0)
    assert review["review_json"]["holding_efficiency"] >= 0.0
    assert experience["decision_context_json"]["holding_minutes"] == pytest.approx(100_000.0 / 60.0)
    assert "overweight_noise_factor" in review["failure_tags"]
    assert experience["recommended_action"] == "downweight"
    assert suggestion is None

    assert len(_rows(db_path, "SELECT * FROM decision_ledger")) == 1
    assert len(_rows(db_path, "SELECT * FROM decision_factor_snapshot")) == 2
    assert len(_rows(db_path, "SELECT * FROM trade_outcome_review")) == 1
    assert len(_rows(db_path, "SELECT * FROM factor_contribution_review")) == 2
    assert len(_rows(db_path, "SELECT * FROM experience_memory")) == 1
    assert len(_rows(db_path, "SELECT * FROM policy_suggestion")) == 0

    provider = LearningFeatureProvider(db_path)
    sample = provider.build_trade_features("101")
    assert sample["schema_version"] == "learning_sample.v1"
    assert sample["quality"]["model_ready"] is True
    assert sample["target"]["outcome_label"] == "bad_loss"
    assert sample["target"]["recommended_action"] == "downweight"
    assert sample["decision"]["decision_id"] == entry_decision_id
    assert sample["decision"]["factor_count"] == 2
    assert sample["explainability"]["top_factors"][0]["factor"] == "noise_factor"
    assert sample["factor_outcomes"][0]["factor"] == "noise_factor"
    assert sample["factor_outcomes"][0]["outcome_contribution"]["net_contribution"] == -90.0
    assert sample["factor_outcomes"][0]["outcome_contribution"]["attribution_label"] == "confirmed"
    assert sample["factor_outcomes"][0]["outcome_contribution"]["outcome_role"] == "harmful"
    assert sample["factor_outcomes"][1]["outcome_contribution"]["outcome_role"] == "helpful"
    assert sample["attribution_alignment"]["labels"]["confirmed"] == 2
    assert sample["attribution_alignment"]["most_harmful_factors"][0]["factor"] == "noise_factor"
    assert sample["experience"]["trade_id"] == "101"
    assert sample["execution_trace"]["summary"]["order_event_count"] == 2
    assert sample["execution_trace"]["summary"]["position_event_count"] == 2
    assert sample["execution_trace"]["summary"]["order_statuses"]["filled"] == 1
    assert sample["execution_trace"]["summary"]["position_event_types"]["closed"] == 1
    assert sample["llm_context"]["prompt_card"].startswith("trade 101 | bad_loss")
    assert any("harmful_factors=noise_factor" in item for item in sample["llm_context"]["evidence_bullets"])
    assert any("execution_trace orders=2" in item for item in sample["llm_context"]["evidence_bullets"])
    assert sample["explainability"]["evidence_bullets"] == sample["llm_context"]["evidence_bullets"]
    assert sample["application_context"][0]["application_id"] == app_id
    assert sample["application_context"][0]["scope_key"] == "noise_factor"

    ready = provider.build_training_samples(model_ready_only=True)
    assert len(ready) == 1


def test_feature_provider_exports_explainable_skip_decision_samples(tmp_path):
    db_path = str(tmp_path / "state.db")
    ledger = DecisionLedger(db_path)

    decision_id = ledger.log_decision(
        event_type="skip",
        symbol="XAUUSD+",
        timeframe="M5",
        action_score=0.71,
        action_reason="VaR limit exceeded",
        action_json={
            "direction": 1,
            "score": 0.71,
            "gate_passed": False,
            "gate_reason": "VaR limit exceeded",
            "skip_stage": "risk_var_gate",
        },
        factor_snapshots=[
            {
                "factor": "trend_alpha",
                "raw_value": 1.4,
                "normalized_value": 0.9,
                "direction": 1.0,
                "policy_weight": 0.3,
                "contribution_score": 0.27,
            }
        ],
    )
    ledger.log_order_event(
        event_type="order_failed",
        decision_id=decision_id,
        price=3310.0,
        volume=100.0,
        status="failed",
        details={"error_code": "REJECTED", "comment": "insufficient margin"},
        event_ts=1_234_567.0,
    )

    provider = LearningFeatureProvider(db_path)
    sample = provider.build_decision_sample(decision_id)

    assert sample["schema_version"] == "decision_sample.v1"
    assert sample["quality"]["model_ready"] is True
    assert sample["target"]["skipped"] is True
    assert sample["target"]["skip_stage"] == "risk_var_gate"
    assert sample["target"]["gate_reason"] == "VaR limit exceeded"
    assert sample["decision"]["temporal_context"]["timeframe"] == "M5"
    assert "session_label" in sample["decision"]["temporal_context"]
    assert sample["llm_context"]["label_summary"]["skipped"] is True
    assert any("gate_passed=False" in item for item in sample["llm_context"]["evidence_bullets"])
    assert sample["explainability"]["top_factors"][0]["factor"] == "trend_alpha"

    items = provider.build_decision_samples(event_types=["skip"], model_ready_only=True)
    assert [item["sample_id"] for item in items] == [f"decision:{decision_id}"]


def test_feature_provider_exports_execution_failure_decision_samples(tmp_path):
    db_path = str(tmp_path / "state.db")
    ledger = DecisionLedger(db_path)

    decision_id = ledger.log_decision(
        event_type="order_failed",
        symbol="XAUUSD+",
        timeframe="M5",
        action_score=0.82,
        action_reason="REJECTED insufficient margin",
        action_json={
            "direction": 1,
            "score": 0.82,
            "gate_passed": True,
            "gate_reason": "passed",
            "skip_stage": "broker_order_failed",
            "error_code": "REJECTED",
        },
        factor_snapshots=[
            {
                "factor": "margin_pressure_factor",
                "raw_value": 2.0,
                "normalized_value": 0.9,
                "direction": 1.0,
                "policy_weight": 0.4,
                "contribution_score": 0.36,
            }
        ],
    )
    ledger.log_order_event(
        event_type="order_failed",
        decision_id=decision_id,
        price=3310.0,
        volume=100.0,
        status="failed",
        details={"error_code": "REJECTED", "comment": "insufficient margin"},
        event_ts=1_234_567.0,
    )

    provider = LearningFeatureProvider(db_path)
    sample = provider.build_decision_sample(decision_id)

    assert sample["schema_version"] == "decision_sample.v1"
    assert sample["quality"]["model_ready"] is True
    assert sample["target"]["executed"] is False
    assert sample["target"]["skipped"] is True
    assert sample["target"]["failed_execution"] is True
    assert sample["target"]["skip_stage"] == "broker_order_failed"
    assert sample["execution_trace"]["summary"]["has_failed_order"] is True
    assert sample["execution_trace"]["order_events"][0]["status"] == "failed"
    assert sample["llm_context"]["label_summary"]["failed_execution"] is True
    assert any("execution_failed" in item for item in sample["llm_context"]["evidence_bullets"])
    assert sample["explainability"]["top_factors"][0]["factor"] == "margin_pressure_factor"

    items = provider.build_decision_samples(event_types=["order_failed"], model_ready_only=True)
    assert [item["sample_id"] for item in items] == [f"decision:{decision_id}"]


def test_dataset_builder_persists_trade_and_decision_jsonl(tmp_path):
    db_path = str(tmp_path / "state.db")
    out_dir = tmp_path / "datasets"
    ledger = DecisionLedger(db_path)
    reviewer = TradeReviewer(db_path)
    builder = ExperienceBuilder(db_path)

    decision_id = ledger.log_decision(
        event_type="open",
        symbol="XAUUSD+",
        timeframe="M5",
        trade_id="404",
        position_id="404",
        action_score=0.77,
        action_reason="executed",
        action_json={"direction": 1, "gate_passed": True, "gate_reason": "passed"},
        factor_snapshots=[
            {
                "factor": "dataset_factor",
                "normalized_value": 0.7,
                "policy_weight": 0.4,
                "contribution_score": 0.28,
            }
        ],
    )
    review = reviewer.review_closed_trade(
        position_id="404",
        pnl=80.0,
        close_price=3340.0,
        close_ts=1_300_000.0,
        contributions={"dataset_factor": 80.0},
        real_pnl={"net": 80.0},
    )
    builder.build_from_review(review)

    export = LearningDatasetBuilder(db_path, out_dir).build_snapshot(
        name="unit_dataset",
        trade_limit=10,
        decision_limit=10,
        model_ready_only=True,
        min_ready_trades=1,
        min_ready_decisions=1,
    )

    assert export["dataset_id"] == "unit_dataset"
    assert export["schemas"]["trade"] == "learning_sample.v1"
    assert export["schemas"]["decision"] == "decision_sample.v1"
    assert export["quality"]["trade"]["model_ready"] == 1
    assert export["quality"]["decision"]["model_ready"] == 1
    assert export["readiness"]["ready"] is True
    assert export["readiness"]["level"] == "ready"
    assert export["readiness"]["schema_issue_count"] == 0
    assert export["readiness"]["thresholds"]["min_ready_trades"] == 1

    trade_path = out_dir / "unit_dataset" / "trade_samples.jsonl"
    decision_path = out_dir / "unit_dataset" / "decision_samples.jsonl"
    manifest_path = out_dir / "unit_dataset" / "manifest.json"
    assert trade_path.exists()
    assert decision_path.exists()
    assert manifest_path.exists()

    trade_item = json.loads(trade_path.read_text(encoding="utf-8").splitlines()[0])
    decision_item = json.loads(decision_path.read_text(encoding="utf-8").splitlines()[0])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert trade_item["sample_id"] == "trade:404"
    assert trade_item["factor_outcomes"][0]["factor"] == "dataset_factor"
    assert trade_item["factor_outcomes"][0]["outcome_contribution"]["net_contribution"] == 80.0
    assert decision_item["sample_id"] == f"decision:{decision_id}"
    assert manifest["files"]["trade_samples"]["count"] == 1
    assert manifest["files"]["decision_samples"]["count"] == 1
    assert manifest["readiness"]["ready"] is True
    assert "factor_outcomes" in manifest["contracts"]["trade"]["features"]
    assert "execution_trace" in manifest["contracts"]["trade"]["features"]
    assert "llm_context" in manifest["contracts"]["trade"]["features"]
    assert "factor contribution review" in manifest["contracts"]["trade"]["quality_gate"]

    validation = LearningDatasetValidator().validate(out_dir / "unit_dataset")
    assert validation["valid"] is True
    assert validation["readiness"]["ready"] is True
    assert validation["files"]["trade_samples"]["count"] == 1

    with trade_path.open("a", encoding="utf-8") as f:
        f.write("{broken json\n")
    broken = LearningDatasetValidator().validate(out_dir / "unit_dataset")
    assert broken["valid"] is False
    assert any(item["issue"] == "sha256_mismatch" for item in broken["issues"])
    assert any(item["issue"] == "invalid_json" for item in broken["issues"])


def test_dataset_summary_adapter_builds_safe_model_card(tmp_path):
    db_path = str(tmp_path / "state.db")
    out_dir = tmp_path / "datasets"
    artifact_dir = tmp_path / "artifacts"
    ledger = DecisionLedger(db_path)
    reviewer = TradeReviewer(db_path)
    builder = ExperienceBuilder(db_path)

    decision_id = ledger.log_decision(
        event_type="open",
        symbol="XAUUSD+",
        timeframe="M5",
        trade_id="707",
        position_id="707",
        action_score=0.88,
        action_reason="executed",
        action_json={"direction": 1, "gate_passed": True, "gate_reason": "passed"},
        factor_snapshots=[
            {
                "factor": "adapter_factor",
                "normalized_value": 0.8,
                "policy_weight": 0.5,
                "contribution_score": 0.40,
            }
        ],
    )
    review = reviewer.review_closed_trade(
        position_id="707",
        pnl=95.0,
        close_price=3360.0,
        close_ts=1_600_000.0,
        contributions={"adapter_factor": 95.0},
        real_pnl={"net": 95.0},
    )
    builder.build_from_review(review)
    export = LearningDatasetBuilder(db_path, out_dir).build_snapshot(
        name="adapter_dataset",
        trade_limit=10,
        decision_limit=10,
        model_ready_only=True,
        min_ready_trades=1,
        min_ready_decisions=1,
    )

    adapter = DatasetSummaryAdapter(artifact_dir)
    registry_db = str(tmp_path / "experiments.db")
    result = adapter.fit(export["dataset_ref"], register=True, registry_db_path=registry_db)

    assert result["ok"] is True
    assert result["model_card"]["capabilities"]["live_trading"] is False
    assert result["model_card"]["label_distribution"]["trade_outcome"]["good_win"] == 1
    assert result["model_card"]["label_distribution"]["decision_event"]["open"] == 1
    assert (artifact_dir / "adapter_dataset_model_card.json").exists()
    assert result["registry_version"]["model_type"] == "dataset_summary_adapter"
    assert result["registry_version"]["metrics"]["safe_for_live_trading"] is False
    assert result["registry_version"]["params"]["safe_for_live_trading"] is False
    registered = ModelRegistry(db_path=registry_db).list_versions("dataset_summary_adapter")
    assert registered[0].artifact_path == result["artifact_path"]

    api_artifact_dir = tmp_path / "api_artifacts"
    api_registry_db = str(tmp_path / "api_experiments.db")
    api_result = build_learning_dataset_model_card(
        None,
        DatasetModelCardRequest(
            dataset_ref=export["dataset_ref"],
            artifact_dir=str(api_artifact_dir),
            register_model=True,
            registry_db_path=api_registry_db,
        ),
    )
    assert api_result["ok"] is True
    assert api_result["model_card"]["capabilities"]["live_trading"] is False
    assert api_result["registry_version"]["metrics"]["safe_for_live_trading"] is False
    assert (api_artifact_dir / "adapter_dataset_model_card.json").exists()

    trade_item = json.loads((out_dir / "adapter_dataset" / "trade_samples.jsonl").read_text(encoding="utf-8").splitlines()[0])
    prediction = adapter.predict(trade_item)
    explanation = adapter.explain(trade_item, prediction)

    assert prediction["safe_for_live_trading"] is False
    assert prediction["prediction_type"] == "review_hint"
    assert explanation["safe_for_live_trading"] is False
    assert explanation["evidence_bullets"]
    assert decision_id


def test_learning_statistical_trainer_builds_explainable_offline_artifact(tmp_path):
    db_path = str(tmp_path / "state.db")
    out_dir = tmp_path / "datasets"
    artifact_dir = tmp_path / "trained_artifacts"
    ledger = DecisionLedger(db_path)
    reviewer = TradeReviewer(db_path)
    builder = ExperienceBuilder(db_path)

    outcomes = [
        ("801", 120.0, 120.0, 0.70),
        ("802", 90.0, 90.0, 0.55),
        ("803", -110.0, -110.0, -0.60),
        ("804", -80.0, -80.0, -0.45),
    ]
    for trade_id, pnl, contribution, signal in outcomes:
        ledger.log_decision(
            event_type="open",
            symbol="XAUUSD+",
            timeframe="M5",
            trade_id=trade_id,
            position_id=trade_id,
            action_score=abs(signal),
            action_reason="executed",
            action_json={"direction": 1, "gate_passed": True, "gate_reason": "passed"},
            factor_snapshots=[
                {
                    "factor": "stat_factor",
                    "normalized_value": signal,
                    "policy_weight": 0.5,
                    "contribution_score": signal,
                }
            ],
        )
        review = reviewer.review_closed_trade(
            position_id=trade_id,
            pnl=pnl,
            close_price=3360.0,
            close_ts=1_600_000.0 + int(trade_id),
            contributions={"stat_factor": contribution},
            real_pnl={"net": pnl},
        )
        builder.build_from_review(review)

    export = LearningDatasetBuilder(db_path, out_dir).build_snapshot(
        name="trainer_dataset",
        trade_limit=10,
        decision_limit=10,
        model_ready_only=True,
        min_ready_trades=4,
        min_ready_decisions=4,
    )
    trainer = LearningStatisticalTrainer(artifact_dir)
    result = trainer.train(
        export["dataset_ref"],
        holdout_ratio=0.25,
        min_samples=4,
        register=True,
        registry_db_path=str(tmp_path / "trainer_registry.db"),
    )

    assert result["ok"] is True
    assert result["model_type"] == "learning_statistical_baseline"
    assert result["metrics"]["sample_count"] == 4
    assert result["metrics"]["feature_count"] > 0
    assert result["promotion"]["eligible_for_live"] is False
    assert result["registry_version"]["metrics"]["safe_for_live_trading"] is False
    assert (artifact_dir / "trainer_dataset_learning_statistical_baseline.json").exists()
    assert any(item["feature"].startswith("factor:stat_factor") for item in result["explainability"]["top_weights"])

    gate = ModelPromotionGate().evaluate(
        artifact_path=result["artifact_path"],
        min_samples=4,
        min_holdout_samples=1,
        min_oos_acc=0.0,
        min_features=1,
    )
    assert gate["ok"] is True
    assert gate["decision"] == "shadow_candidate"
    assert gate["capabilities"]["live_trading"] is False
    assert gate["action"] == "queue_shadow_validation"

    strict_gate = ModelPromotionGate().evaluate(
        artifact_path=result["artifact_path"],
        min_samples=10,
        min_holdout_samples=5,
        min_oos_acc=0.99,
        min_features=1,
    )
    assert strict_gate["ok"] is False
    assert strict_gate["decision"] == "needs_more_data"
    assert {issue["code"] for issue in strict_gate["issues"]} >= {
        "insufficient_samples",
        "insufficient_holdout_samples",
    }

    api_artifact_dir = tmp_path / "api_trained_artifacts"
    api_registry_db = str(tmp_path / "api_trainer_registry.db")
    api_result = train_learning_dataset(
        None,
        DatasetTrainRequest(
            dataset_ref=export["dataset_ref"],
            artifact_dir=str(api_artifact_dir),
            holdout_ratio=0.25,
            min_samples=4,
            register_model=True,
            registry_db_path=api_registry_db,
        ),
    )
    assert api_result["ok"] is True
    assert api_result["promotion"]["eligible_for_live"] is False
    assert api_result["registry_version"]["model_type"] == "learning_statistical_baseline"
    assert (api_artifact_dir / "trainer_dataset_learning_statistical_baseline.json").exists()

    api_gate = evaluate_learning_model_promotion(
        None,
        ModelPromotionGateRequest(
            version=api_result["registry_version"]["version"],
            registry_db_path=api_registry_db,
            min_samples=4,
            min_holdout_samples=1,
            min_oos_acc=0.0,
        ),
    )
    assert api_gate["ok"] is True
    assert api_gate["decision"] == "shadow_candidate"
    assert api_gate["registry_version"]["version"] == api_result["registry_version"]["version"]

    queue = ModelShadowQueue(api_registry_db)
    queued = queue.queue_from_gate(gate_result=api_gate, note="unit test queue")
    assert queued["ok"] is True
    candidate = queued["candidate"]
    assert candidate["status"] == "queued"
    assert candidate["gate_decision"] == "shadow_candidate"
    assert candidate["gate"]["capabilities"]["live_trading"] is False
    assert queue.list_candidates(status="queued")[0]["candidate_id"] == candidate["candidate_id"]

    duplicate = queue.queue_from_gate(gate_result=api_gate, note="unit test duplicate")
    assert duplicate["ok"] is True
    assert duplicate["candidate"]["candidate_id"] == candidate["candidate_id"]
    assert len(queue.list_candidates()) == 1

    status_update = queue.update_status(candidate["candidate_id"], "running", "shadow runner picked up")
    assert status_update["ok"] is True
    assert status_update["candidate"]["status"] == "running"

    api_queue = queue_learning_model_shadow_candidate(
        None,
        ModelShadowQueueRequest(
            version=api_result["registry_version"]["version"],
            registry_db_path=api_registry_db,
            min_samples=4,
            min_holdout_samples=1,
            min_oos_acc=0.0,
            note="api queue",
        ),
    )
    assert api_queue["ok"] is True
    assert api_queue["candidate"]["candidate_id"] == candidate["candidate_id"]
    assert api_queue["risk_verdict"]["allowed"] is True
    assert api_queue["risk_verdict"]["required_mode"] == "shadow"

    listed = list_learning_model_shadow_candidates(
        None,
        status=None,
        model_type=None,
        limit=10,
        registry_db_path=api_registry_db,
    )
    assert listed["count"] == 1
    assert listed["items"][0]["gate"]["decision"] == "shadow_candidate"

    api_status = update_learning_model_shadow_candidate(
        None,
        ModelShadowStatusRequest(
            candidate_id=candidate["candidate_id"],
            status="passed",
            note="shadow validation completed",
            registry_db_path=api_registry_db,
        ),
    )
    assert api_status["ok"] is True
    assert api_status["candidate"]["status"] == "passed"

    queued_again = queue.queue_from_gate(gate_result=api_gate, note="queue for shadow runner")
    runner = ModelShadowRunner(
        registry_db_path=api_registry_db,
        report_dir=tmp_path / "shadow_reports",
    )
    shadow_result = runner.run_candidate(
        queued_again["candidate"]["candidate_id"],
        min_shadow_samples=4,
        min_shadow_accuracy=0.0,
    )
    assert shadow_result["ok"] is True
    assert shadow_result["passed"] is True
    assert shadow_result["report"]["metrics"]["sample_count"] == 4
    assert shadow_result["report"]["capabilities"]["live_trading"] is False
    assert shadow_result["report"]["explainability"]["sample_explanations"][0]["top_terms"]
    assert queue.get_candidate(queued_again["candidate"]["candidate_id"])["status"] == "passed"

    queue.queue_from_gate(gate_result=api_gate, note="queue for api runner")
    api_run = run_learning_model_shadow_validation(
        None,
        ModelShadowRunRequest(
            registry_db_path=api_registry_db,
            report_dir=str(tmp_path / "api_shadow_reports"),
            min_shadow_samples=4,
            min_shadow_accuracy=0.0,
        ),
    )
    assert api_run["ok"] is True
    assert api_run["report"]["decision"] == "passed"

    canary = ModelCanaryReviewer(api_registry_db)
    canary_review = canary.review_candidate(
        queued_again["candidate"]["candidate_id"],
        report_path=shadow_result["report"]["report_path"],
        min_shadow_samples=4,
        min_shadow_accuracy=0.0,
        min_positive_rate=0.0,
        max_positive_rate=1.0,
        note="unit canary review",
    )
    assert canary_review["ok"] is True
    assert canary_review["decision"] == "canary_ready"
    assert canary_review["capabilities"]["live_trading"] is False
    assert canary_review["candidate"]["status"] == "canary_ready"
    assert canary.list_reviews(candidate_id=queued_again["candidate"]["candidate_id"])[0]["decision"] == "canary_ready"

    queue.queue_from_gate(gate_result=api_gate, note="queue for api canary")
    api_shadow = runner.run_candidate(
        queued_again["candidate"]["candidate_id"],
        min_shadow_samples=4,
        min_shadow_accuracy=0.0,
    )
    assert api_shadow["ok"] is True
    api_canary = review_learning_model_canary(
        None,
        ModelCanaryReviewRequest(
            candidate_id=queued_again["candidate"]["candidate_id"],
            registry_db_path=api_registry_db,
            report_path=api_shadow["report"]["report_path"],
            min_shadow_samples=4,
            min_shadow_accuracy=0.0,
            min_positive_rate=0.0,
            max_positive_rate=1.0,
            note="api canary review",
        ),
    )
    assert api_canary["ok"] is True
    assert api_canary["decision"] == "canary_ready"
    assert api_canary["risk_verdict"]["allowed"] is True
    assert api_canary["risk_verdict"]["required_mode"] == "canary"
    listed_canary = list_learning_model_canary_reviews(
        None,
        candidate_id=queued_again["candidate"]["candidate_id"],
        limit=10,
        registry_db_path=api_registry_db,
    )
    assert listed_canary["count"] >= 1
    assert listed_canary["items"][0]["metrics"]["sample_count"] == 4

    sample_item = json.loads((out_dir / "trainer_dataset" / "trade_samples.jsonl").read_text(encoding="utf-8").splitlines()[0])
    contract = ModelInferenceContract(api_registry_db)
    advisory = contract.score(
        candidate_id=queued_again["candidate"]["candidate_id"],
        sample=sample_item,
        mode="advisory",
    )
    assert advisory["ok"] is True
    assert advisory["capabilities"]["live_trading"] is False
    assert advisory["advice"] == "review_only"
    assert advisory["guardrails"]
    assert advisory["audit"]["candidate_id"] == queued_again["candidate"]["candidate_id"]

    live_advisory = score_learning_model_inference(
        None,
        ModelInferenceRequest(
            candidate_id=queued_again["candidate"]["candidate_id"],
            registry_db_path=api_registry_db,
            factor_signals={"stat_factor": 0.8},
            factor_values={"stat_factor": 0.8},
            composite_score=0.7,
        ),
    )
    assert live_advisory["ok"] is True
    assert live_advisory["capabilities"]["advisory_only"] is True
    assert live_advisory["explainability"]["top_terms"]
    audits = list_learning_model_inference_audits(
        None,
        candidate_id=queued_again["candidate"]["candidate_id"],
        limit=10,
        registry_db_path=api_registry_db,
    )
    assert audits["count"] >= 2

    queue.update_status(queued_again["candidate"]["candidate_id"], "canary_ready", "restore for controlled canary trial")
    executor = ModelCanaryExecutor(api_registry_db)
    trial = executor.run_trial(
        candidate_id=queued_again["candidate"]["candidate_id"],
        samples=[sample_item],
        contexts=[
            {
                "factor_signals": {"stat_factor": 0.8},
                "factor_values": {"stat_factor": 0.8},
                "composite_score": 0.7,
            }
        ],
        min_items=2,
        min_success_rate=1.0,
        min_decision_coverage=1.0,
        note="unit controlled canary",
    )
    assert trial["ok"] is True
    assert trial["passed"] is True
    assert trial["candidate"]["status"] == "canary_passed"
    assert trial["capabilities"]["live_trading"] is False
    assert executor.list_trials(candidate_id=queued_again["candidate"]["candidate_id"])[0]["status"] == "canary_passed"

    queue.update_status(queued_again["candidate"]["candidate_id"], "canary_ready", "restore for api controlled canary")
    api_trial = run_learning_model_canary_trial(
        None,
        ModelCanaryTrialRequest(
            candidate_id=queued_again["candidate"]["candidate_id"],
            registry_db_path=api_registry_db,
            contexts=[
                {
                    "factor_signals": {"stat_factor": 0.8},
                    "factor_values": {"stat_factor": 0.8},
                    "composite_score": 0.7,
                }
            ],
            min_items=1,
            min_success_rate=1.0,
            min_decision_coverage=1.0,
            note="api controlled canary",
        ),
    )
    assert api_trial["ok"] is True
    assert api_trial["trial"]["status"] == "canary_passed"
    assert api_trial["risk_verdict"]["allowed"] is True
    assert api_trial["risk_verdict"]["required_mode"] == "canary"
    listed_trials = list_learning_model_canary_trials(
        None,
        candidate_id=queued_again["candidate"]["candidate_id"],
        limit=10,
        registry_db_path=api_registry_db,
    )
    assert listed_trials["count"] >= 2
    assert listed_trials["items"][0]["details"]["guardrails"]

    queue.update_status(queued_again["candidate"]["candidate_id"], "passed", "downgrade for contract guard test")
    rejected_advisory = contract.score(
        candidate_id=queued_again["candidate"]["candidate_id"],
        factor_signals={"stat_factor": 0.8},
    )
    assert rejected_advisory["ok"] is False
    assert "canary_ready" in rejected_advisory["error"]

    pipeline_registry_db = str(tmp_path / "pipeline_registry.db")
    pipeline = LearningModelPipeline(
        registry_db_path=pipeline_registry_db,
        artifact_dir=tmp_path / "pipeline_artifacts",
        shadow_report_dir=tmp_path / "pipeline_shadow_reports",
    )
    pipeline_result = pipeline.run(
        dataset_ref=export["dataset_ref"],
        min_train_samples=4,
        min_gate_samples=4,
        min_gate_holdout_samples=1,
        min_shadow_samples=4,
        min_trial_items=1,
    )
    assert pipeline_result["ok"] is True
    assert pipeline_result["stage"] == "complete"
    assert pipeline_result["trial"]["trial"]["status"] == "canary_passed"
    assert pipeline_result["capabilities"]["live_trading"] is False

    api_pipeline = run_learning_model_pipeline(
        None,
        ModelPipelineRunRequest(
            dataset_ref=export["dataset_ref"],
            registry_db_path=str(tmp_path / "api_pipeline_registry.db"),
            artifact_dir=str(tmp_path / "api_pipeline_artifacts"),
            shadow_report_dir=str(tmp_path / "api_pipeline_shadow_reports"),
            min_train_samples=4,
            min_gate_samples=4,
            min_gate_holdout_samples=1,
            min_shadow_samples=4,
            min_trial_items=1,
        ),
    )
    assert api_pipeline["ok"] is True
    assert api_pipeline["stage"] == "complete"


def test_dataset_readiness_validates_contract_and_thresholds(tmp_path):
    db_path = str(tmp_path / "state.db")
    ledger = DecisionLedger(db_path)
    reviewer = TradeReviewer(db_path)
    builder = ExperienceBuilder(db_path)

    decision_id = ledger.log_decision(
        event_type="open",
        symbol="XAUUSD+",
        timeframe="M5",
        trade_id="505",
        position_id="505",
        action_score=0.64,
        action_reason="executed",
        action_json={"direction": 1, "gate_passed": True, "gate_reason": "passed"},
        factor_snapshots=[
            {
                "factor": "readiness_factor",
                "normalized_value": 0.6,
                "policy_weight": 0.5,
                "contribution_score": 0.30,
            }
        ],
    )
    review = reviewer.review_closed_trade(
        position_id="505",
        pnl=60.0,
        close_price=3350.0,
        close_ts=1_400_000.0,
        contributions={"readiness_factor": 60.0},
        real_pnl={"net": 60.0},
    )
    builder.build_from_review(review)

    report = LearningDatasetReadiness(db_path).analyze(
        trade_limit=10,
        decision_limit=10,
        min_ready_trades=1,
        min_ready_decisions=1,
    )

    assert report["ready"] is True
    assert report["level"] == "ready"
    assert report["quality"]["trade"]["model_ready"] == 1
    assert report["quality"]["decision"]["model_ready"] == 1
    assert report["schema_issue_count"] == 0
    assert "factor_outcomes" in report["contracts"]["trade_required_fields"]
    assert "llm_context" in report["contracts"]["trade_required_fields"]
    assert decision_id


def test_dataset_readiness_reports_missing_model_data(tmp_path):
    db_path = str(tmp_path / "state.db")
    ledger = DecisionLedger(db_path)
    reviewer = TradeReviewer(db_path)

    ledger.log_decision(
        event_type="open",
        symbol="XAUUSD+",
        timeframe="M5",
        trade_id="606",
        position_id="606",
        action_score=0.45,
        action_reason="executed",
        action_json={"direction": 1, "gate_passed": True, "gate_reason": "passed"},
        factor_snapshots=[
            {
                "factor": "missing_experience_factor",
                "normalized_value": 0.4,
                "policy_weight": 0.2,
                "contribution_score": 0.08,
            }
        ],
    )
    reviewer.review_closed_trade(
        position_id="606",
        pnl=-10.0,
        close_price=3300.0,
        close_ts=1_500_000.0,
        contributions={"missing_experience_factor": -10.0},
        real_pnl={"net": -10.0},
    )

    report = LearningDatasetReadiness(db_path).analyze(
        trade_limit=10,
        decision_limit=10,
        min_ready_trades=1,
        min_ready_decisions=1,
    )

    assert report["ready"] is False
    assert report["level"] == "warming_up"
    assert report["quality"]["trade"]["model_ready"] == 0
    assert report["quality"]["trade"]["missing"]["has_experience"] == 1
    assert report["blockers"][0]["code"] == "insufficient_model_ready_trades"


def test_policy_suggester_downweights_after_repeated_bad_losses(tmp_path):
    db_path = str(tmp_path / "state.db")
    suggester = PolicySuggester(db_path)

    actions = []
    for idx in range(3):
        suggestion = suggester.suggest_from_experience(
            {
                "experience_id": f"exp_{idx}",
                "primary_factor": "fragile_factor",
                "outcome_label": "bad_loss",
                "reward_score": -0.8,
                "failure_tags": ["bad_loss", "regime_mismatch"],
            }
        )
        actions.append(suggestion["action"] if suggestion else None)

    assert actions[-1] == "downweight"
    stats = _rows(
        db_path,
        "SELECT * FROM experience_pattern_stats WHERE scope_type='factor' AND scope_key='fragile_factor'",
    )[0]
    assert int(stats["sample_count"]) == 3
    assert int(stats["bad_loss_count"]) == 3
    assert float(stats["avg_reward"]) < 0


def test_policy_suggester_skips_watch_and_promotes_fast_positive_factor(tmp_path):
    db_path = str(tmp_path / "state.db")
    suggester = PolicySuggester(db_path)

    weak = suggester.suggest_from_experience(
        {
            "experience_id": "exp_watch",
            "primary_factor": "slow_factor",
            "outcome_label": "lucky_win",
            "reward_score": 0.12,
            "failure_tags": [],
        }
    )
    assert weak is None
    assert len(_rows(db_path, "SELECT * FROM policy_suggestion")) == 0

    result = None
    for idx, reward in enumerate((0.45, 0.55, 0.32, 0.40), start=1):
        result = suggester.suggest_from_experience(
            {
                "experience_id": f"exp_fast_{idx}",
                "primary_factor": "fast_factor",
                "outcome_label": "good_win",
                "reward_score": reward,
                "failure_tags": [],
            }
        )

    assert result is not None
    assert result["action"] == "boost_small"
    rows = _rows(
        db_path,
        "SELECT * FROM policy_suggestion WHERE scope_key='fast_factor'",
    )
    assert len(rows) == 1


def test_rule_learning_pipeline_deweights_recovery_replay_samples(tmp_path):
    db_path = str(tmp_path / "state.db")
    ledger = DecisionLedger(db_path)
    reviewer = TradeReviewer(db_path)
    builder = ExperienceBuilder(db_path)

    entry_decision_id = ledger.log_decision(
        event_type="open",
        symbol="XAUUSD+",
        timeframe="M5",
        trade_id="202",
        position_id="202",
        action_score=0.68,
        action_reason="executed",
        action_json={"price": 3310.0},
        factor_snapshots=[
            {
                "factor": "dsl_auto_factor",
                "raw_value": 1.0,
                "normalized_value": 0.6,
                "direction": 1.0,
                "base_weight": 0.25,
                "policy_weight": 0.25,
                "contribution_score": 0.15,
            }
        ],
    )
    assert entry_decision_id

    review = reviewer.review_closed_trade(
        position_id="202",
        pnl=42.0,
        close_price=3322.0,
        close_ts=1_100_000.0,
        contributions={"dsl_auto_factor": 42.0},
        exit_decision_id="dec_close_202",
        real_pnl={"net": 42.0},
        close_reason="restart_replay",
        context_integrity="partial",
    )
    experience = builder.build_from_review(review)

    assert "partial_context" in experience["failure_tags"]
    assert "restart_replay" in experience["failure_tags"]
    assert experience["recommended_action"] == "watch"
    assert float(experience["reward_score"]) < 0.3
    assert float(experience["evidence_strength"]) < 0.2


def test_governance_run_returns_risk_verdict(monkeypatch):
    class _FakeGovernor:
        def list_suggestions(self, limit=500, status=None):
            return []

        def review_pending(self):
            return {"approved": 0, "rejected": 0, "unchanged": 0}

        def reconcile_active(self):
            return {"rolled_back": 0, "kept": 0}

        def reconcile_application_effects(self):
            return {"rolled_back": 0, "reinforced": 0}

    import backend.api.learning as learning_api

    monkeypatch.setattr(learning_api, "RuleEvolutionGovernor", _FakeGovernor)

    result = run_governance(None)

    assert result["weights_synced"] is False
    assert result["risk_verdict"]["allowed"] is True
    assert result["risk_verdict"]["audit_payload"]["action"] == "update_weight"


def test_feature_provider_marks_incomplete_samples_not_model_ready(tmp_path):
    db_path = str(tmp_path / "state.db")
    ledger = DecisionLedger(db_path)
    reviewer = TradeReviewer(db_path)

    ledger.log_decision(
        event_type="open",
        symbol="XAUUSD+",
        timeframe="M5",
        trade_id="303",
        position_id="303",
        action_score=0.25,
        factor_snapshots=[
            {
                "factor": "thin_factor",
                "normalized_value": 0.2,
                "policy_weight": 0.1,
                "contribution_score": 0.02,
            }
        ],
    )
    review = reviewer.review_closed_trade(
        position_id="303",
        pnl=12.0,
        close_price=3330.0,
        close_ts=1_200_000.0,
        contributions={"thin_factor": 12.0},
        real_pnl={"net": 12.0},
    )
    assert review["accepted"] is True

    provider = LearningFeatureProvider(db_path)
    sample = provider.build_trade_features("303")

    assert sample["quality"]["model_ready"] is False
    assert "has_experience" in sample["quality"]["missing"]
    assert "has_factor_contribution_review" not in sample["quality"]["missing"]
    assert sample["decision"]["factor_evidence"][0]["factor"] == "thin_factor"
    assert sample["factor_outcomes"][0]["outcome_contribution"]["net_contribution"] == 12.0
    assert provider.build_training_samples(model_ready_only=True) == []
