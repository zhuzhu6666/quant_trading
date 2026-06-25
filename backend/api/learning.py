from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from backend.core.auth import RequireUser
from research.features import (
    LearningDatasetBuilder,
    LearningDatasetReadiness,
    LearningDatasetValidator,
    LearningFeatureProvider,
)
from research.learning.governor import RuleEvolutionGovernor
from research.model_adapter import DatasetSummaryAdapter
from research.offline_trainer import LearningStatisticalTrainer
from research.model_promotion import ModelPromotionGate
from research.model_shadow_queue import ModelShadowQueue
from research.model_shadow_runner import ModelShadowRunner
from research.model_canary import ModelCanaryReviewer
from research.model_canary_executor import ModelCanaryExecutor
from research.model_inference_contract import ModelInferenceContract
from research.model_pipeline import LearningModelPipeline
from risk.policy_service import RiskPolicyService

router = APIRouter(prefix="/api/learning", tags=["learning"])


def _parse_review_row(row) -> dict:
    item = dict(row)
    try:
        item["failure_tags"] = json.loads(item.pop("failure_tags_json") or "[]")
    except Exception:
        item["failure_tags"] = []
    try:
        item["review"] = json.loads(item.pop("review_json") or "{}")
    except Exception:
        item["review"] = {}
    return item


def _is_visible_review(item: dict) -> bool:
    review = item.get("review") or {}
    real_pnl = review.get("real_pnl") or {}
    close_reason = str(review.get("close_reason") or "")
    if isinstance(real_pnl, dict) and real_pnl.get("net") is not None:
        return True
    if close_reason in {"broker_close", "restart_replay", "emergency_close"}:
        return False
    return True


def _risk_verdict(action: str, context: dict | None = None) -> dict:
    return RiskPolicyService.shared().evaluate(action, context or {}).to_dict()


def _blocked_by_risk(verdict: dict) -> dict:
    return {
        "ok": False,
        "blocked": True,
        "error": verdict.get("reason", "risk_policy_block"),
        "risk_verdict": verdict,
    }


class ReviewRequest(BaseModel):
    suggestion_id: str
    status: str
    note: str = ""


class DatasetExportRequest(BaseModel):
    name: str | None = None
    trade_limit: int = 1000
    decision_limit: int = 5000
    model_ready_only: bool = False
    decision_event_types: list[str] | None = None
    min_ready_trades: int = 50
    min_ready_decisions: int = 200


class DatasetValidateRequest(BaseModel):
    dataset_ref: str


class DatasetModelCardRequest(BaseModel):
    dataset_ref: str
    artifact_dir: str | None = None
    register_model: bool = False
    registry_db_path: str | None = None
    symbol: str = "XAUUSD+"
    timeframe: str = "M5"


class DatasetTrainRequest(BaseModel):
    dataset_ref: str
    artifact_dir: str | None = None
    holdout_ratio: float = 0.25
    min_samples: int = 4
    min_feature_count: int = 1
    register_model: bool = False
    registry_db_path: str | None = None
    symbol: str = "XAUUSD+"
    timeframe: str = "M5"


class ModelPromotionGateRequest(BaseModel):
    model_type: str = "learning_statistical_baseline"
    artifact_path: str | None = None
    version: int | None = None
    registry_db_path: str | None = None
    symbol: str = "XAUUSD+"
    timeframe: str = "M5"
    min_samples: int = 20
    min_holdout_samples: int = 5
    min_oos_acc: float = 0.52
    min_features: int = 1
    require_snapshot_ready: bool = True


class ModelShadowQueueRequest(ModelPromotionGateRequest):
    note: str = ""


class ModelShadowStatusRequest(BaseModel):
    candidate_id: str
    status: str
    note: str = ""
    registry_db_path: str | None = None


class ModelShadowRunRequest(BaseModel):
    candidate_id: str | None = None
    registry_db_path: str | None = None
    report_dir: str | None = None
    dataset_ref: str | None = None
    min_shadow_samples: int = 20
    min_shadow_accuracy: float = 0.52


class ModelCanaryReviewRequest(BaseModel):
    candidate_id: str
    registry_db_path: str | None = None
    report_path: str | None = None
    min_shadow_samples: int = 20
    min_shadow_accuracy: float = 0.55
    min_positive_rate: float = 0.05
    max_positive_rate: float = 0.95
    note: str = ""


class ModelInferenceRequest(BaseModel):
    candidate_id: str
    registry_db_path: str | None = None
    sample: dict | None = None
    factor_signals: dict[str, float | None] | None = None
    factor_values: dict[str, float | None] | None = None
    composite_score: float | None = None
    mode: str = "advisory"


class ModelCanaryTrialRequest(BaseModel):
    candidate_id: str
    registry_db_path: str | None = None
    samples: list[dict] | None = None
    contexts: list[dict] | None = None
    min_items: int = 1
    min_success_rate: float = 1.0
    min_decision_coverage: float = 0.0
    note: str = ""


class ModelPipelineRunRequest(BaseModel):
    dataset_ref: str
    registry_db_path: str | None = None
    artifact_dir: str | None = None
    shadow_report_dir: str | None = None
    symbol: str = "XAUUSD+"
    timeframe: str = "M5"
    holdout_ratio: float = 0.25
    min_train_samples: int = 4
    min_feature_count: int = 1
    min_gate_samples: int = 4
    min_gate_holdout_samples: int = 1
    min_gate_oos_acc: float = 0.0
    min_shadow_samples: int = 4
    min_shadow_accuracy: float = 0.0
    min_canary_positive_rate: float = 0.0
    max_canary_positive_rate: float = 1.0
    min_trial_items: int = 1
    min_trial_success_rate: float = 1.0
    min_trial_coverage: float = 1.0


@router.get("/suggestions")
def get_suggestions(
    _user: RequireUser,
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    gov = RuleEvolutionGovernor()
    return {"items": gov.list_suggestions(status=status, limit=limit)}


@router.post("/review")
def review_suggestion(_user: RequireUser, req: ReviewRequest) -> dict:
    gov = RuleEvolutionGovernor()
    try:
        ok = gov.set_status(req.suggestion_id, req.status, req.note)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail="suggestion not found")
    return {"ok": True, "suggestion_id": req.suggestion_id, "status": req.status}


@router.post("/govern/run")
def run_governance(_user: RequireUser) -> dict:
    gov = RuleEvolutionGovernor()
    before = gov.list_suggestions(limit=500)
    before_summary = {
        "proposed": sum(1 for item in before if item.get("status") == "proposed"),
        "approved": sum(1 for item in before if item.get("status") == "approved"),
        "rejected": sum(1 for item in before if item.get("status") == "rejected"),
        "rolled_back": sum(1 for item in before if item.get("status") == "rolled_back"),
    }
    review_result = gov.review_pending()
    reconcile_result = gov.reconcile_active()
    effect_result = gov.reconcile_application_effects()
    after = gov.list_suggestions(limit=500)
    after_summary = {
        "proposed": sum(1 for item in after if item.get("status") == "proposed"),
        "approved": sum(1 for item in after if item.get("status") == "approved"),
        "rejected": sum(1 for item in after if item.get("status") == "rejected"),
        "rolled_back": sum(1 for item in after if item.get("status") == "rolled_back"),
    }
    auto_actions = (
        int(review_result.get("approved", 0))
        + int(review_result.get("rejected", 0))
        + int(reconcile_result.get("rolled_back", 0))
        + int(effect_result.get("rolled_back", 0))
        + int(effect_result.get("reinforced", 0))
    )
    weights_synced = False
    if after_summary["approved"] > 0 or auto_actions > 0:
        try:
            from backend.runtime.evolution_orchestrator import _update_weights
            weights_synced = bool(_update_weights())
        except Exception:
            weights_synced = False
    message = (
        f"本轮治理自动处理 {auto_actions} 条建议："
        f"批准 {review_result.get('approved', 0)}，"
        f"拒绝 {review_result.get('rejected', 0)}，"
        f"回滚 {reconcile_result.get('rolled_back', 0) + effect_result.get('rolled_back', 0)}，"
        f"增强 {effect_result.get('reinforced', 0)}。"
    )
    if weights_synced:
        message += " 已同步最新权重。"
    return {
        "review_pending": review_result,
        "reconcile_active": reconcile_result,
        "reconcile_application_effects": effect_result,
        "before": before_summary,
        "after": after_summary,
        "message": message,
        "auto_actions": auto_actions,
        "weights_synced": weights_synced,
    }


@router.get("/summary")
def get_learning_summary(_user: RequireUser) -> dict:
    from backend.core.db import get_state_conn

    conn = get_state_conn()
    try:
        suggestions = conn.execute(
            """
            SELECT status, COUNT(*) AS c
            FROM policy_suggestion
            GROUP BY status
            """
        ).fetchall()
        apps = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM learning_application_log
            """
        ).fetchone()
        review_rows = conn.execute(
            """
            SELECT review_id, trade_id, position_id, entry_decision_id, exit_decision_id,
                   entry_quality, hold_quality, exit_quality, regime_fit_score,
                   execution_quality, pnl, mae, mfe, outcome_label, failure_tags_json,
                   summary_text, review_json, created_at
            FROM trade_outcome_review
            ORDER BY created_at DESC
            """
        ).fetchone()
        visible_reviews = []
        if review_rows:
            rows = conn.execute(
                """
                SELECT review_id, trade_id, position_id, entry_decision_id, exit_decision_id,
                       entry_quality, hold_quality, exit_quality, regime_fit_score,
                       execution_quality, pnl, mae, mfe, outcome_label, failure_tags_json,
                       summary_text, review_json, created_at
                FROM trade_outcome_review
                ORDER BY created_at DESC
                """
            ).fetchall()
            visible_reviews = [
                item for item in (_parse_review_row(row) for row in rows)
                if _is_visible_review(item)
            ]
        review_counts: dict[str, int] = {}
        for item in visible_reviews:
            key = str(item.get("outcome_label") or "")
            review_counts[key] = review_counts.get(key, 0) + 1
        last_review = visible_reviews[0] if visible_reviews else None
        return {
            "suggestions": {str(r["status"]): int(r["c"]) for r in suggestions},
            "reviews": review_counts,
            "applications": int((apps["c"] if apps else 0) or 0),
            "latest_review": {
                "review_id": last_review["review_id"],
                "trade_id": last_review["trade_id"],
                "position_id": last_review["position_id"],
                "outcome_label": last_review["outcome_label"],
                "pnl": last_review["pnl"],
                "summary_text": last_review["summary_text"],
                "created_at": last_review["created_at"],
            } if last_review else None,
        }
    finally:
        conn.close()


@router.get("/reviews")
def get_reviews(
    _user: RequireUser,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    from backend.core.db import get_state_conn

    conn = get_state_conn()
    try:
        rows = conn.execute(
            """
            SELECT review_id, trade_id, position_id, entry_decision_id, exit_decision_id,
                   entry_quality, hold_quality, exit_quality, regime_fit_score,
                   execution_quality, pnl, mae, mfe, outcome_label, failure_tags_json,
                   summary_text, review_json, created_at
            FROM trade_outcome_review
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        items = [
            item for item in (_parse_review_row(row) for row in rows)
            if _is_visible_review(item)
        ]
        return {"items": items}
    finally:
        conn.close()


@router.get("/applications")
def get_applications(
    _user: RequireUser,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    from backend.core.db import get_state_conn

    conn = get_state_conn()
    try:
        rows = conn.execute(
            """
            SELECT l.application_id, l.cycle_ts, l.scope_type, l.scope_key, l.action, l.bias_multiplier,
                   l.old_weight, l.new_weight, l.suggestion_ids_json, l.status, l.details_json, l.created_at,
                   e.observed_trade_count, e.baseline_trade_count, e.post_avg_reward, e.baseline_avg_reward,
                   e.delta_avg_reward, e.post_win_rate, e.baseline_win_rate, e.last_review_at
            FROM learning_application_log l
            LEFT JOIN learning_application_effect e
              ON e.application_id = l.application_id
            ORDER BY l.created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            try:
                item["suggestion_ids"] = json.loads(item.pop("suggestion_ids_json") or "[]")
            except Exception:
                item["suggestion_ids"] = []
            try:
                item["details"] = json.loads(item.pop("details_json") or "{}")
            except Exception:
                item["details"] = {}
            items.append(item)
        return {"items": items}
    finally:
        conn.close()


@router.get("/lifecycle")
def get_lifecycle(
    _user: RequireUser,
    limit: int = Query(default=60, ge=1, le=500),
) -> dict:
    from backend.core.db import get_state_conn

    conn = get_state_conn()
    try:
        rows = conn.execute(
            """
            SELECT id, timestamp, event, factor, source, description, score, status, reason
            FROM lifecycle_events
            ORDER BY timestamp DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            items.append(
                {
                    "id": item.get("id"),
                    "ts": float(item.get("timestamp") or 0.0),
                    "event": str(item.get("event") or ""),
                    "factor": str(item.get("factor") or ""),
                    "source": str(item.get("source") or ""),
                    "description": str(item.get("description") or ""),
                    "score": float(item.get("score") or 0.0),
                    "status": str(item.get("status") or ""),
                    "reason": str(item.get("reason") or item.get("description") or ""),
                    "metrics": {},
                    "kind": "factor_lifecycle",
                }
            )
        return {"items": items}
    finally:
        conn.close()


@router.get("/dataset")
def get_learning_dataset(
    _user: RequireUser,
    limit: int = Query(default=100, ge=1, le=500),
    model_ready_only: bool = Query(default=False),
) -> dict:
    provider = LearningFeatureProvider()
    samples = provider.build_training_samples(
        limit=limit,
        model_ready_only=model_ready_only,
    )
    quality_counts = {
        "model_ready": sum(1 for item in samples if item.get("quality", {}).get("model_ready")),
        "needs_attention": sum(1 for item in samples if not item.get("quality", {}).get("model_ready")),
    }
    return {
        "schema_version": "learning_sample.v1",
        "count": len(samples),
        "quality": quality_counts,
        "items": samples,
    }


@router.get("/decision-dataset")
def get_learning_decision_dataset(
    _user: RequireUser,
    limit: int = Query(default=100, ge=1, le=500),
    event_type: list[str] | None = Query(default=None),
    model_ready_only: bool = Query(default=False),
) -> dict:
    provider = LearningFeatureProvider()
    samples = provider.build_decision_samples(
        limit=limit,
        event_types=event_type,
        model_ready_only=model_ready_only,
    )
    quality_counts = {
        "model_ready": sum(1 for item in samples if item.get("quality", {}).get("model_ready")),
        "needs_attention": sum(1 for item in samples if not item.get("quality", {}).get("model_ready")),
    }
    return {
        "schema_version": "decision_sample.v1",
        "count": len(samples),
        "quality": quality_counts,
        "items": samples,
    }


@router.post("/dataset/export")
def export_learning_dataset(_user: RequireUser, req: DatasetExportRequest) -> dict:
    trade_limit = max(1, min(int(req.trade_limit or 1000), 10000))
    decision_limit = max(1, min(int(req.decision_limit or 5000), 50000))
    builder = LearningDatasetBuilder()
    manifest = builder.build_snapshot(
        name=req.name,
        trade_limit=trade_limit,
        decision_limit=decision_limit,
        model_ready_only=bool(req.model_ready_only),
        decision_event_types=req.decision_event_types,
        min_ready_trades=max(0, min(int(req.min_ready_trades or 0), trade_limit)),
        min_ready_decisions=max(0, min(int(req.min_ready_decisions or 0), decision_limit)),
    )
    return {"ok": True, "manifest": manifest}


@router.get("/dataset/readiness")
def get_learning_dataset_readiness(
    _user: RequireUser,
    trade_limit: int = Query(default=1000, ge=1, le=10000),
    decision_limit: int = Query(default=5000, ge=1, le=50000),
    min_ready_trades: int = Query(default=50, ge=0, le=10000),
    min_ready_decisions: int = Query(default=200, ge=0, le=50000),
) -> dict:
    readiness = LearningDatasetReadiness()
    return readiness.analyze(
        trade_limit=trade_limit,
        decision_limit=decision_limit,
        min_ready_trades=min_ready_trades,
        min_ready_decisions=min_ready_decisions,
    )


@router.post("/dataset/validate")
def validate_learning_dataset(_user: RequireUser, req: DatasetValidateRequest) -> dict:
    if not req.dataset_ref:
        raise HTTPException(status_code=400, detail="dataset_ref is required")
    return LearningDatasetValidator().validate(req.dataset_ref)


@router.post("/dataset/model-card")
def build_learning_dataset_model_card(_user: RequireUser, req: DatasetModelCardRequest) -> dict:
    if not req.dataset_ref:
        raise HTTPException(status_code=400, detail="dataset_ref is required")
    adapter = DatasetSummaryAdapter(req.artifact_dir)
    return adapter.fit(
        req.dataset_ref,
        register=bool(req.register_model),
        registry_db_path=req.registry_db_path,
        symbol=req.symbol,
        timeframe=req.timeframe,
    )


@router.post("/dataset/train")
def train_learning_dataset(_user: RequireUser, req: DatasetTrainRequest) -> dict:
    if not req.dataset_ref:
        raise HTTPException(status_code=400, detail="dataset_ref is required")
    trainer = LearningStatisticalTrainer(req.artifact_dir)
    return trainer.train(
        req.dataset_ref,
        holdout_ratio=max(0.0, min(float(req.holdout_ratio), 0.8)),
        min_samples=max(1, int(req.min_samples)),
        min_feature_count=max(1, int(req.min_feature_count)),
        register=bool(req.register_model),
        registry_db_path=req.registry_db_path,
        symbol=req.symbol,
        timeframe=req.timeframe,
    )


@router.post("/model/promotion-gate")
def evaluate_learning_model_promotion(_user: RequireUser, req: ModelPromotionGateRequest) -> dict:
    return ModelPromotionGate().evaluate(
        model_type=req.model_type,
        artifact_path=req.artifact_path,
        version=req.version,
        registry_db_path=req.registry_db_path,
        symbol=req.symbol,
        timeframe=req.timeframe,
        min_samples=max(1, int(req.min_samples)),
        min_holdout_samples=max(1, int(req.min_holdout_samples)),
        min_oos_acc=float(req.min_oos_acc),
        min_features=max(1, int(req.min_features)),
        require_snapshot_ready=bool(req.require_snapshot_ready),
    )


@router.post("/model/shadow-queue")
def queue_learning_model_shadow_candidate(_user: RequireUser, req: ModelShadowQueueRequest) -> dict:
    verdict = _risk_verdict(
        "start_shadow_model",
        {
            "model_type": req.model_type,
            "symbol": req.symbol,
            "timeframe": req.timeframe,
            "capabilities": {"live_trading": False},
        },
    )
    if not verdict.get("allowed", False):
        return _blocked_by_risk(verdict)
    result = ModelShadowQueue(req.registry_db_path).queue(
        model_type=req.model_type,
        artifact_path=req.artifact_path,
        version=req.version,
        registry_db_path=req.registry_db_path,
        symbol=req.symbol,
        timeframe=req.timeframe,
        min_samples=max(1, int(req.min_samples)),
        min_holdout_samples=max(1, int(req.min_holdout_samples)),
        min_oos_acc=float(req.min_oos_acc),
        min_features=max(1, int(req.min_features)),
        require_snapshot_ready=bool(req.require_snapshot_ready),
        note=req.note,
    )
    result["risk_verdict"] = verdict
    return result


@router.get("/model/shadow-queue")
def list_learning_model_shadow_candidates(
    _user: RequireUser,
    status: str | None = Query(default=None),
    model_type: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    registry_db_path: str | None = Query(default=None),
) -> dict:
    items = ModelShadowQueue(registry_db_path).list_candidates(
        status=status,
        model_type=model_type,
        limit=limit,
    )
    return {"items": items, "count": len(items)}


@router.post("/model/shadow-queue/status")
def update_learning_model_shadow_candidate(_user: RequireUser, req: ModelShadowStatusRequest) -> dict:
    if not req.candidate_id:
        raise HTTPException(status_code=400, detail="candidate_id is required")
    return ModelShadowQueue(req.registry_db_path).update_status(req.candidate_id, req.status, req.note)


@router.post("/model/shadow-run")
def run_learning_model_shadow_validation(_user: RequireUser, req: ModelShadowRunRequest) -> dict:
    runner = ModelShadowRunner(
        registry_db_path=req.registry_db_path,
        report_dir=req.report_dir,
    )
    if req.candidate_id:
        return runner.run_candidate(
            req.candidate_id,
            dataset_ref=req.dataset_ref,
            min_shadow_samples=max(1, int(req.min_shadow_samples)),
            min_shadow_accuracy=float(req.min_shadow_accuracy),
        )
    return runner.run_next(
        min_shadow_samples=max(1, int(req.min_shadow_samples)),
        min_shadow_accuracy=float(req.min_shadow_accuracy),
    )


@router.post("/model/canary-review")
def review_learning_model_canary(_user: RequireUser, req: ModelCanaryReviewRequest) -> dict:
    if not req.candidate_id:
        raise HTTPException(status_code=400, detail="candidate_id is required")
    candidate = ModelShadowQueue(req.registry_db_path).get_candidate(req.candidate_id)
    verdict = _risk_verdict(
        "start_canary_model",
        {
            "candidate_id": req.candidate_id,
            "candidate_status": str((candidate or {}).get("status") or ""),
            "allowed_statuses": ["passed"],
            "capabilities": {"live_trading": False},
        },
    )
    if not verdict.get("allowed", False):
        return _blocked_by_risk(verdict)
    result = ModelCanaryReviewer(req.registry_db_path).review_candidate(
        req.candidate_id,
        report_path=req.report_path,
        min_shadow_samples=max(1, int(req.min_shadow_samples)),
        min_shadow_accuracy=float(req.min_shadow_accuracy),
        min_positive_rate=float(req.min_positive_rate),
        max_positive_rate=float(req.max_positive_rate),
        note=req.note,
    )
    result["risk_verdict"] = verdict
    return result


@router.get("/model/canary-review")
def list_learning_model_canary_reviews(
    _user: RequireUser,
    candidate_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    registry_db_path: str | None = Query(default=None),
) -> dict:
    items = ModelCanaryReviewer(registry_db_path).list_reviews(
        candidate_id=candidate_id,
        limit=limit,
    )
    return {"items": items, "count": len(items)}


@router.post("/model/inference")
def score_learning_model_inference(_user: RequireUser, req: ModelInferenceRequest) -> dict:
    if not req.candidate_id:
        raise HTTPException(status_code=400, detail="candidate_id is required")
    return ModelInferenceContract(req.registry_db_path).score(
        candidate_id=req.candidate_id,
        sample=req.sample,
        factor_signals=req.factor_signals,
        factor_values=req.factor_values,
        composite_score=req.composite_score,
        mode=req.mode,
    )


@router.get("/model/inference")
def list_learning_model_inference_audits(
    _user: RequireUser,
    candidate_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    registry_db_path: str | None = Query(default=None),
) -> dict:
    items = ModelInferenceContract(registry_db_path).list_audits(
        candidate_id=candidate_id,
        limit=limit,
    )
    return {"items": items, "count": len(items)}


@router.post("/model/canary-trial")
def run_learning_model_canary_trial(_user: RequireUser, req: ModelCanaryTrialRequest) -> dict:
    if not req.candidate_id:
        raise HTTPException(status_code=400, detail="candidate_id is required")
    candidate = ModelShadowQueue(req.registry_db_path).get_candidate(req.candidate_id)
    verdict = _risk_verdict(
        "start_canary_model",
        {
            "candidate_id": req.candidate_id,
            "candidate_status": str((candidate or {}).get("status") or ""),
            "allowed_statuses": ["canary_ready"],
            "capabilities": {"live_trading": False},
        },
    )
    if not verdict.get("allowed", False):
        return _blocked_by_risk(verdict)
    result = ModelCanaryExecutor(req.registry_db_path).run_trial(
        candidate_id=req.candidate_id,
        samples=req.samples,
        contexts=req.contexts,
        min_items=max(1, int(req.min_items)),
        min_success_rate=float(req.min_success_rate),
        min_decision_coverage=float(req.min_decision_coverage),
        note=req.note,
    )
    result["risk_verdict"] = verdict
    return result


@router.get("/model/canary-trial")
def list_learning_model_canary_trials(
    _user: RequireUser,
    candidate_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    registry_db_path: str | None = Query(default=None),
) -> dict:
    items = ModelCanaryExecutor(registry_db_path).list_trials(
        candidate_id=candidate_id,
        limit=limit,
    )
    return {"items": items, "count": len(items)}


@router.post("/model/pipeline/run")
def run_learning_model_pipeline(_user: RequireUser, req: ModelPipelineRunRequest) -> dict:
    if not req.dataset_ref:
        raise HTTPException(status_code=400, detail="dataset_ref is required")
    return LearningModelPipeline(
        registry_db_path=req.registry_db_path,
        artifact_dir=req.artifact_dir,
        shadow_report_dir=req.shadow_report_dir,
    ).run(
        dataset_ref=req.dataset_ref,
        symbol=req.symbol,
        timeframe=req.timeframe,
        holdout_ratio=float(req.holdout_ratio),
        min_train_samples=max(1, int(req.min_train_samples)),
        min_feature_count=max(1, int(req.min_feature_count)),
        min_gate_samples=max(1, int(req.min_gate_samples)),
        min_gate_holdout_samples=max(1, int(req.min_gate_holdout_samples)),
        min_gate_oos_acc=float(req.min_gate_oos_acc),
        min_shadow_samples=max(1, int(req.min_shadow_samples)),
        min_shadow_accuracy=float(req.min_shadow_accuracy),
        min_canary_positive_rate=float(req.min_canary_positive_rate),
        max_canary_positive_rate=float(req.max_canary_positive_rate),
        min_trial_items=max(1, int(req.min_trial_items)),
        min_trial_success_rate=float(req.min_trial_success_rate),
        min_trial_coverage=float(req.min_trial_coverage),
    )
