from __future__ import annotations

from copy import deepcopy
import json
import os
import sqlite3
import threading
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
import re

from backend.core.auth import RequireUser
from backend.core.db import STATE_DB, connect_sqlite
from backend.jobs import get_job_manager
from backend.services.factor_cards import FactorCardService
from backend.services.parameter_templates import ParameterTemplateService
from backend.services.parameter_template_validation import (
    ParameterTemplateValidationService,
    run_parameter_template_offline_validation,
)
from backend.services.position_supervisor_governance import (
    build_position_supervisor_advisories,
    replay_position_supervisor_templates,
)
from backend.services.position_supervisor_templates import list_position_supervisor_templates
from backend.services.review_contract import normalize_trade_review_contract
from backend.services.supervisor_counterfactual import (
    evaluate_counterfactuals,
    list_counterfactuals,
)
from backend.services.autonomous_learning import (
    backfill_position_supervisor_traces,
    list_autonomous_learning_samples,
    mature_position_supervisor_traces,
    run_autonomous_learning_cycle,
)
from backend.services.evolution_ledger import (
    get_evolution_run,
    list_evolution_runs,
    persist_runtime_config_snapshot,
    record_evolution_decision,
    start_evolution_run,
    finish_evolution_run,
)
from backend.services.model_permissions import (
    list_model_permission_audits,
    validate_model_artifact,
)
from backend.services.meta_governance import MetaGovernanceService
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
from research.meta_model_sidecar import MetaModelSidecar
from research.meta_model_lightgbm import MetaModelLightGBMService
from research.llm_advisory import LLMAdvisoryService
from research.factor_governance_lightgbm import FactorGovernanceLightGBMService
from research.position_quality_lightgbm import PositionQualityLightGBMService
from risk.policy_service import RiskPolicyService

router = APIRouter(prefix="/api/learning", tags=["learning"])

_CANDIDATE_ID_RE = re.compile(r"(ptrc_[0-9a-f]{16})")
_LEARNING_CACHE_TTL_SEC = 30.0
_LEARNING_CACHE_LOCK = threading.Lock()
_LEARNING_CACHE: dict[str, tuple[float, Any]] = {}
_LEARNING_COMPUTE_LOCKS: dict[str, threading.Lock] = {}


def _learning_cache_get(key: str) -> Any | None:
    now_ts = time.time()
    with _LEARNING_CACHE_LOCK:
        cached = _LEARNING_CACHE.get(key)
        if not cached:
            return None
        expires_at, payload = cached
        if expires_at <= now_ts:
            _LEARNING_CACHE.pop(key, None)
            return None
        return deepcopy(payload)


def _learning_cache_set(key: str, payload: Any, ttl_sec: float = _LEARNING_CACHE_TTL_SEC) -> Any:
    expires_at = time.time() + max(1.0, float(ttl_sec))
    cloned = deepcopy(payload)
    with _LEARNING_CACHE_LOCK:
        _LEARNING_CACHE[key] = (expires_at, cloned)
    return deepcopy(cloned)


def _learning_cache_invalidate(*prefixes: str) -> None:
    with _LEARNING_CACHE_LOCK:
        if not prefixes:
            _LEARNING_CACHE.clear()
            return
        keys = list(_LEARNING_CACHE.keys())
        for key in keys:
            if any(key.startswith(prefix) for prefix in prefixes):
                _LEARNING_CACHE.pop(key, None)


def _learning_compute_lock(key: str) -> threading.Lock:
    with _LEARNING_CACHE_LOCK:
        lock = _LEARNING_COMPUTE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LEARNING_COMPUTE_LOCKS[key] = lock
        return lock


def _humanize_template_responsibility(value: str) -> str:
    key = str(value or "").lower()
    if key == "exit":
        return "退出问题"
    if key == "timing":
        return "时长问题"
    if key == "regime":
        return "市场切换问题"
    if key == "parameter":
        return "参数问题"
    if key == "thesis":
        return "thesis 失效"
    if key == "holding":
        return "持仓效率问题"
    return "待继续归因"


def _humanize_approval_path(value: str) -> str:
    key = str(value or "").lower()
    if key == "offline_validation_then_gray_release":
        return "先离线验证再灰度发布"
    if key == "offline_replay_then_governed_release":
        return "先离线回放再规则发布"
    if key == "governed_apply_switch":
        return "经治理审批后受控切换"
    if key == "governor_review_then_live_switch":
        return "经审批后在线切换"
    return "按治理链继续推进"


def _humanize_template_candidate_status(value: str) -> str:
    key = str(value or "").lower()
    if key == "pending_review":
        return "待审"
    if key == "approved":
        return "已批准"
    if key == "rejected":
        return "已拒绝"
    if key == "deployed":
        return "已发布"
    if key == "rolled_back":
        return "已回滚"
    return "状态未知"


def _humanize_boundary_scope(value: str) -> str:
    return "离线深调" if str(value or "").lower() == "offline_deep" else "在线轻调"


def _humanize_boundary_reason(value: str) -> str:
    key = str(value or "").lower()
    if key == "fits_runtime_guardrail":
        return "满足当前运行态护栏"
    if key == "factor_not_runtime_tunable":
        return "该因子暂不支持运行时直接改参数"
    if key == "formula_version_changed":
        return "模板公式版本发生变化"
    if key == "factor_family_changed":
        return "模板所属因子家族发生变化"
    if key == "parameter_delta_too_large":
        return "参数跳变幅度超过在线护栏"
    if key == "unsupported_template_role":
        return "模板角色不在在线护栏允许范围内"
    return str(value or "未分类边界原因")


def _build_parameter_template_ops_summary(
    *,
    recommendation_counts: dict[str, int],
    latest_recommendation: dict | None,
    latest_candidate: dict | None,
    latest_candidate_trace: dict | None,
) -> str:
    total = int((recommendation_counts or {}).get("total") or 0)
    online = int((recommendation_counts or {}).get("online_light") or 0)
    offline = int((recommendation_counts or {}).get("offline_deep") or 0)
    candidate = latest_candidate or {}
    trace = latest_candidate_trace or {}
    if candidate:
        status_label = _humanize_template_candidate_status(candidate.get("status") or "")
        factor_id = str(candidate.get("factor_id") or "")
        template_id = str(candidate.get("template_id") or "")
        candidate_id = str(candidate.get("candidate_id") or "")
        if trace.get("recommendation_id"):
            responsibility = _humanize_template_responsibility(
                ((trace.get("responsibility") or {}).get("primary_responsibility") or "")
            )
            approval_path = _humanize_approval_path(trace.get("approval_path") or "")
            return (
                f"参数治理最新进展：候选 {candidate_id} 当前{status_label}，"
                f"目标 {factor_id}/{template_id}，来源推荐 {trace.get('recommendation_id')} "
                f"({responsibility}，{approval_path})。当前推荐共 {total} 条，在线 {online} / 离线 {offline}。"
            )
        return (
            f"参数治理最新进展：候选 {candidate_id} 当前{status_label}，"
            f"目标 {factor_id}/{template_id}。当前推荐共 {total} 条，在线 {online} / 离线 {offline}。"
        )
    if latest_recommendation:
        boundary = (latest_recommendation.get("boundary") or {}).get("recommended_scope") or ""
        boundary_text = "离线深调" if str(boundary).lower() == "offline_deep" else "在线轻调"
        factor_id = str(latest_recommendation.get("factor_id") or "")
        template_version = str(
            latest_recommendation.get("target_template_version")
            or latest_recommendation.get("target_template_id")
            or ""
        )
        return (
            f"参数治理当前有 {total} 条推荐，在线 {online} / 离线 {offline}。"
            f"最新建议指向 {factor_id}/{template_version}，建议按 {boundary_text} 路径推进。"
        )
    return "当前还没有新的参数模板推荐或候选发布动作。"


def _governance_target_type(entry_type: str) -> str:
    key = str(entry_type or "").lower()
    if key == "candidate":
        return "模板候选"
    if key == "recommendation":
        return "参数推荐"
    return ""


def _governance_action_label(entry_type: str, stage_tag: str) -> str:
    normalized_type = str(entry_type or "").lower()
    normalized_stage = str(stage_tag or "")
    if normalized_type == "candidate":
        if normalized_stage == "待审候选":
            return "去审候选"
        if normalized_stage == "等待发布":
            return "去发布"
        if normalized_stage == "发布观察":
            return "看观察"
        if normalized_stage == "已回滚":
            return "看回滚"
        return "看候选"
    if normalized_type == "recommendation":
        if normalized_stage == "在线轻调":
            return "去审建议"
        if normalized_stage == "离线深调":
            return "去做验证"
        return "看建议"
    return "打开治理"


def _governance_priority(entry_type: str, stage_tag: str, has_governance_factor: bool = False) -> dict[str, Any]:
    normalized_type = str(entry_type or "").lower()
    normalized_stage = str(stage_tag or "")
    if normalized_type == "candidate" and normalized_stage == "待审候选":
        return {"score": 100, "label": "优先治理", "summary": "这条治理对象已经形成候选，优先等待系统规则审核推进。"}
    if normalized_type == "candidate" and normalized_stage == "等待发布":
        return {"score": 90, "label": "优先发布", "summary": "这条治理对象已经批准，下一步应推进灰度发布。"}
    if normalized_type == "recommendation" and normalized_stage == "离线深调":
        return {"score": 80, "label": "优先验证", "summary": "这条治理对象当前应先走离线验证，不能直接切线上。"}
    if normalized_type == "recommendation" and normalized_stage == "在线轻调":
        return {"score": 70, "label": "优先审建议", "summary": "这条治理对象已满足在线轻调边界，可继续生成或审批治理建议。"}
    if normalized_type == "candidate" and normalized_stage == "发布观察":
        return {"score": 60, "label": "优先观察", "summary": "这条治理对象已经上线，当前重点是观察效果与回滚信号。"}
    if normalized_type == "candidate" and normalized_stage == "已回滚":
        return {"score": 50, "label": "优先复核", "summary": "这条治理对象已经回滚，当前应先回到离线复核。"}
    if has_governance_factor:
        return {"score": 40, "label": "继续收敛", "summary": "这条对象已经露出参数问题线索，但还没有形成更具体的治理对象。"}
    return {"score": 0, "label": "", "summary": ""}


def _governance_stage_tone(entry_type: str, stage_tag: str) -> str:
    normalized_type = str(entry_type or "").lower()
    normalized_stage = str(stage_tag or "")
    if normalized_type == "candidate":
        if normalized_stage == "待审候选":
            return "warning"
        if normalized_stage in {"等待发布", "发布观察"}:
            return "positive"
        if normalized_stage in {"已回滚", "已拒绝"}:
            return "negative"
        return "neutral"
    if normalized_type == "recommendation":
        if normalized_stage == "离线深调":
            return "warning"
        if normalized_stage == "在线轻调":
            return "positive"
    return "neutral"


def _build_parameter_template_todo(
    *,
    latest_candidate: dict | None,
    latest_recommendation: dict | None,
    latest_candidate_trace: dict | None,
    recommendation_counts: dict[str, int],
) -> dict | None:
    candidate = latest_candidate or {}
    recommendation = latest_recommendation or {}
    trace = latest_candidate_trace or {}
    items: list[dict] = []
    if candidate:
        status = str(candidate.get("status") or "").lower()
        stage_tag = (
            "待审候选" if status == "pending_review"
            else "等待发布" if status == "approved"
            else "发布观察" if status == "deployed"
            else "已回滚" if status == "rolled_back"
            else "已拒绝" if status == "rejected"
            else "候选处理中"
        )
        priority = _governance_priority("candidate", stage_tag, has_governance_factor=bool(candidate.get("factor_id")))
        items.append({
            "factor_id": str(candidate.get("factor_id") or ""),
            "title": f"{candidate.get('factor_id') or '--'} · {candidate.get('template_id') or '--'}",
            "entry_type": "candidate",
            "target_type": _governance_target_type("candidate"),
            "stage_tag": stage_tag,
            "stage_tone": _governance_stage_tone("candidate", stage_tag),
            "action_label": _governance_action_label("candidate", stage_tag),
            "candidate_id": str(candidate.get("candidate_id") or ""),
            "recommendation_id": str(trace.get("recommendation_id") or ""),
            "priority_score": int(priority["score"]),
            "priority_label": str(priority["label"]),
            "priority_summary": str(priority["summary"]),
            "summary": (
                "下一步等待系统规则审核，通过后才允许灰度发布。" if status == "pending_review"
                else "下一步执行灰度发布，并继续观察后验效果。" if status == "approved"
                else "下一步观察发布后的 reward 和胜率表现。" if status == "deployed"
                else "下一步复核回滚原因，再决定是否重新离线调参。" if status == "rolled_back"
                else "下一步保留证据观察，等待更多样本后再发起候选。" if status == "rejected"
                else "下一步继续积累更多治理证据。"
            ),
            "created_at": float(candidate.get("created_at") or 0.0),
        })
    if recommendation:
        scope = str(((recommendation.get("boundary") or {}).get("recommended_scope") or "")).lower()
        stage_tag = "离线深调" if scope == "offline_deep" else "在线轻调"
        priority = _governance_priority("recommendation", stage_tag, has_governance_factor=bool(recommendation.get("factor_id")))
        items.append({
            "factor_id": str(recommendation.get("factor_id") or ""),
            "title": f"{recommendation.get('factor_id') or '--'} · {stage_tag}",
            "entry_type": "recommendation",
            "target_type": _governance_target_type("recommendation"),
            "stage_tag": stage_tag,
            "stage_tone": _governance_stage_tone("recommendation", stage_tag),
            "action_label": _governance_action_label("recommendation", stage_tag),
            "candidate_id": "",
            "recommendation_id": str(recommendation.get("recommendation_id") or ""),
            "priority_score": int(priority["score"]),
            "priority_label": str(priority["label"]),
            "priority_summary": str(priority["summary"]),
            "summary": (
                "下一步先做离线验证，验证通过后再登记灰度候选。" if scope == "offline_deep"
                else "下一步生成治理建议，走 governor 审批后受控切到运行态。"
            ),
            "created_at": float(recommendation.get("created_at") or 0.0),
        })
    if not items:
        return None
    items.sort(key=lambda item: (-int(item.get("priority_score") or 0), -float(item.get("created_at") or 0.0)))
    primary = items[0]
    return {
        **primary,
        "queue_hint": (
            f"除此之外还有 {len(items) - 1} 条参数治理对象可继续处理。"
            if len(items) > 1
            else f"当前推荐共 {int((recommendation_counts or {}).get('total') or 0)} 条，先把这条主待办处理完。"
        ),
    }


def _build_parameter_template_overview(
    *,
    suggestion_counts: dict[str, int],
    first_pending_candidate: dict | None,
    first_online_recommendation: dict | None,
    first_offline_recommendation: dict | None,
) -> dict[str, Any]:
    pending_candidate = first_pending_candidate or {}
    online_recommendation = first_online_recommendation or {}
    offline_recommendation = first_offline_recommendation or {}
    proposed = int((suggestion_counts or {}).get("proposed") or 0)
    approved = int((suggestion_counts or {}).get("approved") or 0)

    if pending_candidate:
        headline = {
            "label": "待审候选",
            "tone": "warning",
            "summary": f"{pending_candidate.get('factor_id') or '--'} 已形成模板候选，当前优先等待系统规则审核。",
        }
    elif online_recommendation:
        headline = {
            "label": "在线轻调",
            "tone": "warning",
            "summary": f"{online_recommendation.get('factor_id') or '--'} 已可直接进入建议审批链。",
        }
    elif offline_recommendation:
        headline = {
            "label": "离线深调",
            "tone": "warning",
            "summary": f"{offline_recommendation.get('factor_id') or '--'} 当前必须先做离线验证。",
        }
    elif proposed > 0:
        headline = {
            "label": "待审核经验",
            "tone": "warning",
            "summary": "当前有经验建议待审核，参数治理对象仍在持续收敛。",
        }
    elif approved > 0:
        headline = {
            "label": "已形成可用经验",
            "tone": "positive",
            "summary": "已有可用经验进入下一轮应用观察，当前没有新的参数治理待办。",
        }
    else:
        headline = {
            "label": "学习观察中",
            "tone": "neutral",
            "summary": "当前还没有新的参数治理对象，继续观察样本与证据沉淀。",
        }

    pending_candidate_hint = None
    if pending_candidate:
        governance = _parameter_template_candidate_governance_snapshot(pending_candidate)
        pending_candidate_hint = {
            "factor_id": str(pending_candidate.get("factor_id") or ""),
            "candidate_id": str(pending_candidate.get("candidate_id") or ""),
            "title": f"{pending_candidate.get('factor_id') or '--'} · {pending_candidate.get('template_id') or '--'}",
            "stage_tag": str(governance.get("stage_label") or ""),
            "action_label": str(governance.get("action_label") or ""),
            "summary": str(governance.get("next_step_summary") or ""),
        }

    online_light_hint = None
    if online_recommendation:
        governance = _parameter_template_recommendation_governance_snapshot(online_recommendation)
        online_light_hint = {
            "factor_id": str(online_recommendation.get("factor_id") or ""),
            "recommendation_id": str(online_recommendation.get("recommendation_id") or ""),
            "title": f"{online_recommendation.get('factor_id') or '--'} · 在线轻调",
            "stage_tag": str(governance.get("stage_label") or ""),
            "action_label": str(governance.get("action_label") or ""),
            "summary": str(governance.get("next_step_summary") or ""),
        }

    offline_deep_hint = None
    if offline_recommendation:
        governance = _parameter_template_recommendation_governance_snapshot(offline_recommendation)
        offline_deep_hint = {
            "factor_id": str(offline_recommendation.get("factor_id") or ""),
            "recommendation_id": str(offline_recommendation.get("recommendation_id") or ""),
            "title": f"{offline_recommendation.get('factor_id') or '--'} · 离线深调",
            "stage_tag": str(governance.get("stage_label") or ""),
            "action_label": str(governance.get("action_label") or ""),
            "summary": str(governance.get("next_step_summary") or ""),
        }

    return {
        "headline": headline,
        "pending_candidate_hint": pending_candidate_hint,
        "online_light_hint": online_light_hint,
        "offline_deep_hint": offline_deep_hint,
    }


def _build_parameter_template_empty_states() -> dict[str, str]:
    return {
        "offline_candidates": "还没有参数模板候选",
        "lifecycle": "还没有参数治理轨迹",
        "recommendations": "还没有参数模板建议",
    }


def _build_parameter_template_task_cards(
    *,
    candidate_counts: dict[str, int],
    recommendation_counts: dict[str, int],
    lifecycle_count: int,
    parameter_template_todo: dict[str, Any] | None,
    parameter_template_overview: dict[str, Any],
) -> list[dict[str, str]]:
    headline = dict((parameter_template_overview or {}).get("headline") or {})
    total_candidates = sum(int(value or 0) for value in candidate_counts.values())
    total_recommendations = int(recommendation_counts.get("total") or 0)
    if parameter_template_todo:
        template_note = (
            f"{parameter_template_todo.get('stage_tag') or '治理推进中'} · "
            f"{parameter_template_todo.get('action_label') or '继续推进'}"
        )
        template_tone = str(parameter_template_todo.get("stage_tone") or "warning")
    else:
        template_note = (
            f"{total_candidates} 条离线候选"
            if total_candidates > 0
            else "尚未形成离线模板候选"
        )
        template_tone = str(headline.get("tone") or "neutral")
    return [
        {
            "id": "template",
            "index": "6",
            "title": "模板候选",
            "note": template_note,
            "tone": template_tone,
        },
        {
            "id": "template-lifecycle",
            "index": "7",
            "title": "治理轨迹",
            "note": (
                f"{lifecycle_count} 条模板治理事件"
                if lifecycle_count > 0
                else "尚未形成模板治理事件"
            ),
            "tone": "positive" if lifecycle_count > 0 else "neutral",
        },
        {
            "id": "template-reco",
            "index": "8",
            "title": "参数模板建议",
            "note": (
                f"{total_recommendations} 条证据驱动推荐"
                if total_recommendations > 0
                else "尚未识别新的参数模板推荐"
            ),
            "tone": str(headline.get("tone") or "neutral"),
        },
    ]


def _parameter_template_candidate_governance_snapshot(item: dict | None) -> dict[str, Any]:
    candidate = item or {}
    status = str(candidate.get("status") or "").lower()
    validation_summary = dict(candidate.get("validation_summary") or {})
    recommendation = dict(validation_summary.get("recommendation_source") or {})
    review = dict(validation_summary.get("review") or {})
    deployment = dict(validation_summary.get("deployment") or {})
    rollback = dict(validation_summary.get("rollback") or {})
    stage_tag = (
        "待审候选" if status == "pending_review"
        else "等待发布" if status == "approved"
        else "发布观察" if status == "deployed"
        else "已回滚" if status == "rolled_back"
        else "已拒绝" if status == "rejected"
        else "候选处理中"
    )
    priority = _governance_priority(
        "candidate",
        stage_tag,
        has_governance_factor=bool(candidate.get("factor_id")),
    )
    action_buttons: list[dict[str, Any]] = []
    if status == "pending_review":
        action_buttons = [
            {"key": "approve", "label": "批准候选", "tone": "primary", "disabled": False},
            {"key": "reject", "label": "拒绝候选", "tone": "secondary", "disabled": False},
        ]
    elif status == "approved":
        action_buttons = [
            {"key": "release", "label": "执行灰度发布", "tone": "primary", "disabled": False},
            {"key": "reject", "label": "改判拒绝", "tone": "secondary", "disabled": False},
        ]
    elif status == "deployed":
        action_buttons = [
            {"key": "rollback", "label": "执行回滚", "tone": "secondary", "disabled": False},
        ]
    elif status in {"rolled_back", "rejected"}:
        action_buttons = [
            {"key": "observe", "label": "当前仅观察", "tone": "secondary", "disabled": True},
        ]
    recommendation_id = str(recommendation.get("recommendation_id") or "")
    responsibility = _humanize_template_responsibility(
        ((recommendation.get("responsibility") or {}).get("primary_responsibility") or "")
    )
    approval_path = str(recommendation.get("approval_path") or "")
    candidate_avg_ic = float(validation_summary.get("candidate_avg_ic") or 0.0)
    baseline_avg_ic = float(validation_summary.get("baseline_avg_ic") or 0.0)
    delta_ic = candidate_avg_ic - baseline_avg_ic
    return {
        "entry_type": "candidate",
        "target_type": _governance_target_type("candidate"),
        "source_summary": f"来源推荐 {recommendation_id} · {responsibility}" if recommendation_id else "",
        "approval_path_text": _humanize_approval_path(approval_path) if approval_path else "",
        "evidence_display": f"Walk-forward IC {candidate_avg_ic:.3f}，基线 {baseline_avg_ic:.3f}，Δ {delta_ic:+.3f}",
        "status_label": stage_tag,
        "stage_label": stage_tag,
        "stage_tone": _governance_stage_tone("candidate", stage_tag),
        "stage_summary": (
            "离线证据已形成候选，当前优先等待系统规则审核。"
            if status == "pending_review"
            else "离线证据已审核通过，当前等待灰度发布。"
            if status == "approved"
            else "候选模板已经进入运行态，当前重点是观察后验效果。"
            if status == "deployed"
            else "候选模板已经回滚，当前应优先复核失败原因。"
            if status == "rolled_back"
            else "候选模板已被拒绝，当前先保留证据继续观察。"
            if status == "rejected"
            else "候选模板正在治理链中推进。"
        ),
        "next_step_label": (
            "等待规则审核" if status == "pending_review"
            else "等待灰度发布" if status == "approved"
            else "观察发布效果" if status == "deployed"
            else "复核回滚原因" if status == "rolled_back"
            else "保留证据观察" if status == "rejected"
            else "继续观察"
        ),
        "next_step_summary": (
            "下一步由系统规则审核离线证据；通过后才允许进入灰度发布。"
            if status == "pending_review"
            else "下一步执行灰度发布，把候选模板切到运行态，并继续观察后验效果。"
            if status == "approved"
            else "下一步继续盯后验 reward 和胜率，确认是否需要强化或回滚。"
            if status == "deployed"
            else "下一步复核为什么回滚，并判断是否需要回到离线验证重新调参。"
            if status == "rolled_back"
            else "下一步保留这次离线证据，等待更多样本后再决定是否重新发起候选。"
            if status == "rejected"
            else "下一步继续积累证据，等待治理链出现更明确动作。"
        ),
        "action_label": _governance_action_label("candidate", stage_tag),
        "action_buttons": action_buttons,
        "review_display": (
            f"{_humanize_template_candidate_status(review.get('status') or '')} · {review.get('note') or '已治理处理'}"
            if review.get("status")
            else "等待系统规则审核"
        ),
        "deployment_display": (
            f"已发布，旧模板 {deployment.get('old_template_id') or '--'}"
            if deployment.get("status")
            else "尚未发布"
        ),
        "rollback_display": (
            f"已回滚到 {rollback.get('restored_template_id') or '--'}"
            if rollback.get("status")
            else ""
        ),
        "priority_score": int(priority["score"]),
        "priority_label": str(priority["label"]),
        "priority_summary": str(priority["summary"]),
    }


def _parameter_template_recommendation_governance_snapshot(item: dict | None) -> dict[str, Any]:
    recommendation = item or {}
    scope = str(((recommendation.get("boundary") or {}).get("recommended_scope") or "")).lower()
    stage_tag = "离线深调" if scope == "offline_deep" else "在线轻调"
    priority = _governance_priority(
        "recommendation",
        stage_tag,
        has_governance_factor=bool(recommendation.get("factor_id")),
    )
    return {
        "entry_type": "recommendation",
        "target_type": _governance_target_type("recommendation"),
        "status_label": stage_tag,
        "stage_label": stage_tag,
        "stage_tone": _governance_stage_tone("recommendation", stage_tag),
        "stage_summary": (
            "这条推荐当前不允许直接切线上，应先做离线验证。"
            if scope == "offline_deep"
            else "这条推荐已满足在线轻调边界，可继续推进治理建议与审批。"
        ),
        "next_step_label": "先做离线验证" if scope == "offline_deep" else "生成治理建议",
        "next_step_summary": (
            "下一步先创建离线验证，验证通过后再登记灰度候选并进入系统规则审核。"
            if scope == "offline_deep"
            else "下一步把推荐转成正式治理建议，再走 governor 审批后受控切换运行态模板。"
        ),
        "action_label": _governance_action_label("recommendation", stage_tag),
        "action_button_text": "创建离线验证" if scope == "offline_deep" else "生成治理建议",
        "action_summary": (
            "先离线验证再入灰度候选。"
            if scope == "offline_deep"
            else "可直接进入受控 suggestion -> apply-switch 链路。"
        ),
        "followup_hint": (
            "若这条推荐已经生成过离线验证，下一步应转去看对应候选或生命周期事件。"
            if scope == "offline_deep"
            else "若这条推荐已经生成过治理建议，下一步应转去看 suggestion 审批或后续模板候选。"
        ),
        "priority_score": int(priority["score"]),
        "priority_label": str(priority["label"]),
        "priority_summary": str(priority["summary"]),
    }


def _parameter_template_lifecycle_governance_snapshot(
    item: dict | None,
    candidate_trace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event = str((item or {}).get("event") or "").lower()
    event_stage = event
    for prefix in ("parameter_template_candidate_", "parameter_template_"):
        if event_stage.startswith(prefix):
            event_stage = event_stage[len(prefix):]
            break
    trace = candidate_trace or {}
    candidate_id = str(trace.get("candidate_id") or "")
    recommendation_id = str(trace.get("recommendation_id") or "")
    jump_type = "offline_candidate" if candidate_id else "template_recommendation" if recommendation_id else ""
    target_type = "模板候选" if candidate_id else "参数推荐" if recommendation_id else ""
    target_id = candidate_id or recommendation_id
    responsibility = _humanize_template_responsibility(
        ((trace.get("responsibility") or {}).get("primary_responsibility") or "")
    )
    approval_path = str(trace.get("approval_path") or "")
    source_summary = f"来源推荐 {recommendation_id} · {responsibility}" if recommendation_id else ""
    approval_path_text = _humanize_approval_path(approval_path) if approval_path else ""
    trace_display = {
        "source_summary": source_summary,
        "approval_path_text": approval_path_text,
    }
    button_text = ""
    if jump_type == "offline_candidate":
        button_text = "查看对应候选"
    elif jump_type == "template_recommendation":
        button_text = "查看来源推荐"
    if event_stage == "registered":
        return {
            **trace_display,
            "status_label": "待审候选",
            "stage_label": "待审候选",
            "stage_tone": _governance_stage_tone("candidate", "待审候选"),
            "stage_summary": "这条治理轨迹已登记候选，当前应进入系统规则审核。",
            "next_step_label": "等待规则审核",
            "next_step_summary": "下一步应进入系统规则审核，决定这条候选是否可以继续推进到发布。",
            "action_label": "去审候选",
            "target_type": target_type,
            "target_id": target_id,
            "jump_type": jump_type,
            "button_text": button_text or "去审候选",
            "candidate_id": candidate_id,
            "recommendation_id": recommendation_id,
        }
    if event_stage == "reviewed":
        return {
            **trace_display,
            "status_label": "等待发布",
            "stage_label": "等待发布",
            "stage_tone": _governance_stage_tone("candidate", "等待发布"),
            "stage_summary": "这条治理轨迹已经完成审核，当前进入发布或终止决策。",
            "next_step_label": "准备发布或终止",
            "next_step_summary": "下一步根据审核结论执行发布，或在证据不足时终止这条治理链。",
            "action_label": "去发布",
            "target_type": target_type,
            "target_id": target_id,
            "jump_type": jump_type,
            "button_text": button_text or "去发布",
            "candidate_id": candidate_id,
            "recommendation_id": recommendation_id,
        }
    if event_stage == "deployed":
        return {
            **trace_display,
            "status_label": "发布观察",
            "stage_label": "发布观察",
            "stage_tone": _governance_stage_tone("candidate", "发布观察"),
            "stage_summary": "这条治理轨迹已经完成发布，当前重点是观察运行态效果。",
            "next_step_label": "继续观察效果",
            "next_step_summary": "下一步盯运行态效果，确认模板发布后是否真的带来改进。",
            "action_label": "看观察",
            "target_type": target_type,
            "target_id": target_id,
            "jump_type": jump_type,
            "button_text": button_text or "看观察",
            "candidate_id": candidate_id,
            "recommendation_id": recommendation_id,
        }
    if event_stage == "rolled_back":
        return {
            **trace_display,
            "status_label": "已回滚",
            "stage_label": "已回滚",
            "stage_tone": _governance_stage_tone("candidate", "已回滚"),
            "stage_summary": "这条治理轨迹已经回滚，当前应回到离线复核。",
            "next_step_label": "回到离线复核",
            "next_step_summary": "下一步回到离线证据层复核失败原因，再决定是否重新生成候选。",
            "action_label": "看回滚",
            "target_type": target_type,
            "target_id": target_id,
            "jump_type": jump_type,
            "button_text": button_text or "看回滚",
            "candidate_id": candidate_id,
            "recommendation_id": recommendation_id,
        }
    return {
        **trace_display,
        "status_label": "",
        "stage_label": "",
        "stage_tone": "neutral",
        "stage_summary": "",
        "next_step_label": "继续观察",
        "next_step_summary": "下一步继续积累证据，等治理链上出现更明确的动作条件。",
        "action_label": "",
        "target_type": target_type,
        "target_id": target_id,
        "jump_type": jump_type,
        "button_text": button_text,
        "candidate_id": candidate_id,
        "recommendation_id": recommendation_id,
    }


def _parameter_template_suggestion_progress_snapshot(
    conn: sqlite3.Connection,
    suggestion: dict[str, Any] | None,
) -> dict[str, Any]:
    item = suggestion or {}
    evidence = dict(item.get("evidence") or {})
    evidence_context = dict(evidence.get("evidence_context") or {})
    recommendation_id = str(evidence_context.get("recommendation_id") or "")
    if not recommendation_id:
        return {
            "state_label": "",
            "state_summary": "",
            "target_type": "",
            "target_id": "",
            "button_text": "",
        }
    factor_id = str(evidence_context.get("factor_id") or "")
    candidate = {}
    if factor_id:
        try:
            candidates = ParameterTemplateValidationService().list_release_candidates(
                factor_id=factor_id,
                limit=20,
            )
        except Exception:
            candidates = []
        for candidate_item in candidates:
            trace = dict(((candidate_item.get("validation_summary") or {}).get("recommendation_source") or {}))
            if str(trace.get("recommendation_id") or "") == recommendation_id:
                candidate = candidate_item
                break
    if candidate:
        return {
            "state_label": "已进入模板候选",
            "state_summary": f"这条建议来自推荐 {recommendation_id}，后续已形成候选 {candidate.get('candidate_id') or '--'}。",
            "target_type": "candidate",
            "target_id": str(candidate.get("candidate_id") or ""),
            "button_text": "查看后续候选",
        }
    recommendation = None
    try:
        recommendation = ParameterTemplateService().list_recommendations(
            factor_id=factor_id or None,
            limit=50,
        )
        recommendation = next(
            (entry for entry in recommendation if str(entry.get("recommendation_id") or "") == recommendation_id),
            None,
        )
    except Exception:
        recommendation = None
    if recommendation:
        return {
            "state_label": "来自参数推荐",
            "state_summary": f"这条建议由参数模板推荐 {recommendation_id} materialize 而来，可回看原始推荐证据。",
            "target_type": "recommendation",
            "target_id": recommendation_id,
            "button_text": "回到来源推荐",
        }
    return {
        "state_label": "来自参数推荐",
        "state_summary": f"这条建议由参数模板推荐 {recommendation_id} 生成，但当前还没拿到对应对象详情。",
        "target_type": "",
        "target_id": "",
        "button_text": "",
    }


def _parameter_template_suggestion_display_snapshot(suggestion: dict[str, Any] | None) -> dict[str, Any]:
    item = suggestion or {}
    evidence = dict(item.get("evidence") or {})
    boundary = dict(evidence.get("boundary") or {})
    scope = str(boundary.get("recommended_scope") or "").lower()
    boundary_scope_label = _humanize_boundary_scope(scope)
    boundary_reason_text = "；".join(
        _humanize_boundary_reason(reason) for reason in list(boundary.get("reasons") or []) if reason
    ) or "满足当前运行态护栏"
    approval_path_text = (
        "这条建议不能直接切运行态，需要先走离线验证、灰度候选和发布链路。"
        if scope == "offline_deep"
        else "这条建议可以在现有 governor 审批后，直接进入受控 apply-switch。"
    )
    impact_text = (
        "当前先进入审批与验证链，不会直接改动运行参数。"
        if scope == "offline_deep"
        else "审批通过后可直接切换运行态模板，并同步到 runtime config。"
    )
    evidence_text = (
        f"边界判定：{boundary_scope_label}，{boundary_reason_text}。"
        if boundary_reason_text
        else f"边界判定：{boundary_scope_label}。"
    )
    return {
        "boundary_scope_label": boundary_scope_label,
        "boundary_reason_text": boundary_reason_text,
        "approval_path_text": approval_path_text,
        "impact_text": impact_text,
        "evidence_text": evidence_text,
    }


def _parameter_template_recommendation_progress_snapshot(
    *,
    recommendation_id: str,
    candidate: dict[str, Any] | None = None,
    suggestion: dict[str, Any] | None = None,
    lifecycle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    candidate = candidate or {}
    suggestion = suggestion or {}
    lifecycle = lifecycle or {}
    if candidate:
        return {
            "state_label": "已进入离线候选",
            "state_summary": f"这条推荐已经生成候选 {candidate.get('candidate_id') or '--'}，当前状态 {_humanize_template_candidate_status(candidate.get('status') or '')}。",
            "state_done": True,
            "target_type": "candidate",
            "target_id": str(candidate.get("candidate_id") or ""),
            "button_text": "查看对应候选",
        }
    if suggestion:
        return {
            "state_label": "已生成治理建议",
            "state_summary": f"这条推荐已经生成建议 {suggestion.get('suggestion_id') or '--'}，当前状态 {suggestion.get('status') or '--'}。",
            "state_done": True,
            "target_type": "suggestion",
            "target_id": str(suggestion.get("suggestion_id") or ""),
            "button_text": "查看对应建议",
        }
    if lifecycle:
        return {
            "state_label": "已进入治理轨迹",
            "state_summary": f"这条推荐已经写入治理轨迹事件 {lifecycle.get('event') or '--'}。",
            "state_done": True,
            "target_type": "lifecycle",
            "target_id": str(lifecycle.get("id") or ""),
            "button_text": "查看治理轨迹",
        }
    return {
        "state_label": "",
        "state_summary": "",
        "state_done": False,
        "target_type": "",
        "target_id": "",
        "button_text": "",
    }


def _offline_candidate_action_result_display(
    *,
    action: str,
    status: str = "",
    blocked: bool = False,
) -> dict[str, str]:
    if blocked:
        return {
            "result_label": "当前动作被阻断",
            "result_summary": "这次候选动作没有执行，需先处理风控或边界原因。",
        }
    key = str(action or "").lower()
    status_key = str(status or "").lower()
    if key == "review" and status_key == "approved":
        return {
            "result_label": "已批准候选",
            "result_summary": "这条离线候选已通过规则审核，下一步可进入灰度发布。",
        }
    if key == "review" and status_key == "rejected":
        return {
            "result_label": "已拒绝候选",
            "result_summary": "这条离线候选已被拒绝，当前保留证据继续观察。",
        }
    if key == "release":
        return {
            "result_label": "已执行发布",
            "result_summary": "这条候选模板已切到运行态，下一步观察后验效果。",
        }
    if key == "rollback":
        return {
            "result_label": "已执行回滚",
            "result_summary": "这条候选模板已回滚，下一步复核回滚原因。",
        }
    return {
        "result_label": "处理完成",
        "result_summary": "候选动作已处理完成。",
    }


def _suggestion_review_result_display(status: str) -> dict[str, str]:
    key = str(status or "").lower()
    if key == "approved":
        return {
            "result_label": "已批准建议",
            "result_summary": "这条治理建议已批准，下一步等待应用或继续观察效果。",
        }
    if key == "rejected":
        return {
            "result_label": "已拒绝建议",
            "result_summary": "这条治理建议已拒绝，当前保留证据继续观察。",
        }
    return {
        "result_label": "建议已处理",
        "result_summary": "这条治理建议的审批状态已更新。",
    }


def _parse_review_row(row) -> dict:
    item = dict(row)
    try:
        item["failure_tags"] = json.loads(item.pop("failure_tags_json") or "[]")
    except Exception:
        item["failure_tags"] = []
    try:
        review = json.loads(item.pop("review_json") or "{}")
    except Exception:
        review = {}
    normalized = normalize_trade_review_contract(
        review,
        entry_quality=item.get("entry_quality"),
        hold_quality=item.get("hold_quality"),
        exit_quality=item.get("exit_quality"),
        regime_fit_score=item.get("regime_fit_score"),
        execution_quality=item.get("execution_quality"),
    )
    item["review"] = normalized
    item["regime_fit"] = normalized["regime_fit"]
    item["thesis_status_at_exit"] = normalized["thesis_status_at_exit"]
    item["regime_shift_at_exit"] = normalized["regime_shift_at_exit"]
    item["profit_capture_ratio"] = normalized["profit_capture_ratio"]
    item["giveback_ratio"] = normalized["giveback_ratio"]
    item["time_in_profit"] = normalized["time_in_profit"]
    item["holding_efficiency"] = normalized["holding_efficiency"]
    taxonomy = normalized.get("failure_taxonomy") or {}
    item["failure_taxonomy"] = taxonomy
    item["primary_responsibility"] = str(
        normalized.get("primary_responsibility")
        or taxonomy.get("primary_responsibility")
        or ""
    )
    item["responsibility_labels"] = list(
        normalized.get("responsibility_labels")
        or taxonomy.get("responsibility_labels")
        or []
    )
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


def _candidate_trace_by_id(conn, candidate_id: str) -> dict:
    if not candidate_id:
        return {}
    row = conn.execute(
        """
        SELECT validation_summary_json
        FROM parameter_template_release_candidate
        WHERE candidate_id=?
        """,
        (candidate_id,),
    ).fetchone()
    if not row:
        return {}
    try:
        summary = json.loads(row["validation_summary_json"] or "{}")
    except Exception:
        summary = {}
    trace = summary.get("recommendation_source") or {}
    if not isinstance(trace, dict) or not trace:
        return {}
    return {
        "candidate_id": str(candidate_id or ""),
        "source": str(trace.get("source") or ""),
        "recommendation_id": str(trace.get("recommendation_id") or ""),
        "reason": str(trace.get("reason") or ""),
        "responsibility": dict(trace.get("responsibility") or {}),
        "approval_path": str(trace.get("approval_path") or ""),
    }


def _trace_locator_from_review_row(row: sqlite3.Row | dict | None) -> dict:
    if not row:
        return {}
    item = dict(row)
    return {
        "review_id": str(item.get("review_id") or ""),
        "trade_id": str(item.get("trade_id") or ""),
        "position_id": str(item.get("position_id") or ""),
        "entry_decision_id": str(item.get("entry_decision_id") or ""),
        "exit_decision_id": str(item.get("exit_decision_id") or ""),
    }


def _latest_template_suggestion_for_recommendation(
    conn: sqlite3.Connection,
    recommendation_id: str,
) -> dict[str, Any]:
    recommendation_key = str(recommendation_id or "").strip()
    if not recommendation_key:
        return {}
    try:
        rows = conn.execute(
            """
            SELECT suggestion_id, scope_type, scope_key, action, confidence, reason,
                   evidence_json, status, reviewed_at, review_note, created_at
            FROM policy_suggestion
            WHERE scope_type='parameter_template'
            ORDER BY created_at DESC
            LIMIT 50
            """
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for row in rows:
        try:
            evidence = json.loads(row["evidence_json"] or "{}")
        except Exception:
            evidence = {}
        evidence_context = dict((evidence or {}).get("evidence_context") or {})
        if str(evidence_context.get("recommendation_id") or "") != recommendation_key:
            continue
        return {
            "suggestion_id": str(row["suggestion_id"] or ""),
            "scope_type": str(row["scope_type"] or ""),
            "scope_key": str(row["scope_key"] or ""),
            "action": str(row["action"] or ""),
            "confidence": float(row["confidence"] or 0.0),
            "reason": str(row["reason"] or ""),
            "status": str(row["status"] or ""),
            "reviewed_at": float(row["reviewed_at"] or 0.0),
            "review_note": str(row["review_note"] or ""),
            "created_at": float(row["created_at"] or 0.0),
            "evidence": evidence,
        }
    return {}


def _latest_parameter_template_lifecycle_for_recommendation(
    conn: sqlite3.Connection,
    *,
    factor_id: str,
    recommendation_id: str,
    candidate_id: str = "",
) -> dict[str, Any]:
    factor_key = str(factor_id or "").strip()
    recommendation_key = str(recommendation_id or "").strip()
    candidate_key = str(candidate_id or "").strip()
    if not factor_key and not recommendation_key and not candidate_key:
        return {}
    try:
        rows = conn.execute(
            """
            SELECT id, timestamp, event, factor, source, description, score, status, reason
            FROM lifecycle_events
            WHERE source='parameter_template' AND factor=?
            ORDER BY timestamp DESC, id DESC
            LIMIT 40
            """,
            (factor_key,),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for row in rows:
        text = f"{row['description'] or ''} {row['reason'] or ''}"
        candidate_trace = {}
        match = _CANDIDATE_ID_RE.search(text)
        if match:
            candidate_trace = _candidate_trace_by_id(conn, match.group(1))
        trace_recommendation_id = str(candidate_trace.get("recommendation_id") or "")
        trace_candidate_id = str(candidate_trace.get("candidate_id") or "")
        if recommendation_key and trace_recommendation_id == recommendation_key:
            return {
                "id": int(row["id"] or 0),
                "ts": float(row["timestamp"] or 0.0),
                "event": str(row["event"] or ""),
                "factor": str(row["factor"] or ""),
                "source": str(row["source"] or ""),
                "description": str(row["description"] or ""),
                "score": float(row["score"] or 0.0),
                "status": str(row["status"] or ""),
                "reason": str(row["reason"] or row["description"] or ""),
                "candidate_trace": candidate_trace,
                "trace_locator": _latest_factor_trace_locator(conn, factor_key),
            }
        if candidate_key and trace_candidate_id == candidate_key:
            return {
                "id": int(row["id"] or 0),
                "ts": float(row["timestamp"] or 0.0),
                "event": str(row["event"] or ""),
                "factor": str(row["factor"] or ""),
                "source": str(row["source"] or ""),
                "description": str(row["description"] or ""),
                "score": float(row["score"] or 0.0),
                "status": str(row["status"] or ""),
                "reason": str(row["reason"] or row["description"] or ""),
                "candidate_trace": candidate_trace,
                "trace_locator": _latest_factor_trace_locator(conn, factor_key),
            }
    return {}


def _latest_factor_trace_locator(conn, factor_id: str) -> dict:
    factor_key = str(factor_id or "").strip()
    if not factor_key:
        return {}
    row = conn.execute(
        """
        SELECT r.review_id, r.trade_id, r.position_id, r.entry_decision_id, r.exit_decision_id, r.created_at
        FROM factor_contribution_review f
        JOIN trade_outcome_review r ON r.review_id = f.review_id
        WHERE f.factor = ?
        ORDER BY r.created_at DESC, f.id DESC
        LIMIT 1
        """,
        (factor_key,),
    ).fetchone()
    return _trace_locator_from_review_row(row)


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


class PositionQualityLightGBMTrainRequest(BaseModel):
    db_path: str | None = None
    artifact_dir: str | None = None
    registry_db_path: str | None = None
    symbol: str = "XAUUSD+"
    timeframe: str = "M5"
    limit: int = 1000
    holdout_ratio: float = 0.25
    min_samples: int = 20
    register_model: bool = True
    run_shadow: bool = True
    shadow_limit: int = 100


class PositionQualityLightGBMShadowRequest(BaseModel):
    db_path: str | None = None
    artifact_dir: str | None = None
    artifact_path: str | None = None
    limit: int = 100
    mode: str = "shadow"


class FactorGovernanceLightGBMTrainRequest(BaseModel):
    db_path: str | None = None
    artifact_dir: str | None = None
    registry_db_path: str | None = None
    symbol: str = "XAUUSD+"
    timeframe: str = "M5"
    limit: int = 2000
    holdout_ratio: float = 0.25
    min_samples: int = 30
    register_model: bool = True
    run_shadow: bool = True
    shadow_limit: int = 200
    materialize_suggestions: bool = False
    min_weakness_score: float = 0.65


class FactorGovernanceLightGBMShadowRequest(BaseModel):
    db_path: str | None = None
    artifact_dir: str | None = None
    artifact_path: str | None = None
    limit: int = 200
    mode: str = "shadow"
    materialize_suggestions: bool = False
    min_weakness_score: float = 0.65


class MetaModelLightGBMTrainRequest(BaseModel):
    db_path: str | None = None
    artifact_dir: str | None = None
    registry_db_path: str | None = None
    symbol: str = "XAUUSD+"
    timeframe: str = "M5"
    limit: int = 2000
    window: int = 12
    horizon: int = 3
    holdout_ratio: float = 0.25
    min_samples: int = 30
    register_model: bool = True
    run_shadow: bool = True
    shadow_limit: int = 200
    materialize_ledger: bool = False


class MetaModelLightGBMShadowRequest(BaseModel):
    db_path: str | None = None
    artifact_dir: str | None = None
    artifact_path: str | None = None
    limit: int = 200
    window: int = 12
    horizon: int = 3
    mode: str = "shadow"
    materialize_ledger: bool = False


class MetaModelLightGBMSnapshotRequest(BaseModel):
    db_path: str | None = None
    limit: int = 200
    include_samples: bool = False
    source: str = "manual"


class MetaModelLightGBMGovernanceRequest(BaseModel):
    db_path: str | None = None
    limit: int = 200
    snapshot: bool = True
    source: str = "manual"


class SupervisorCounterfactualRunRequest(BaseModel):
    db_path: str | None = None
    limit: int = 100
    horizons_minutes: list[int] | None = None
    materialize: bool = True


class AutonomousLearningRunRequest(BaseModel):
    db_path: str | None = None
    sample_limit: int = 500
    recommendation_limit: int = 20
    submit_offline_deep: bool = True


class PositionSupervisorTraceMaterializeRequest(BaseModel):
    db_path: str | None = None
    limit: int = 500


class PositionSupervisorTemplateApplySwitchRequest(BaseModel):
    suggestion_id: str
    note: str = ""


class ModelPermissionValidateRequest(BaseModel):
    artifact_path: str | None = None
    artifact: dict[str, Any] | None = None
    model_type: str | None = None
    db_path: str | None = None
    require_shadow: bool = True


class MetaModelContextRequest(BaseModel):
    db_path: str | None = None
    context: dict[str, Any] | None = None


class MetaModelAdvisoryRunRequest(BaseModel):
    db_path: str | None = None
    context: dict[str, Any] | None = None
    materialize: bool = True


class LLMAdvisoryRunRequest(BaseModel):
    db_path: str | None = None
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    task_type: str = "review_summary"
    target_type: str = ""
    target_id: str = ""
    context: dict[str, Any] = {}
    dry_run: bool = False
    max_tokens: int | None = None
    temperature: float = 0.2


class ParameterTemplateUpsertRequest(BaseModel):
    template: dict
    source: str = "manual"
    activate: bool = False


class ParameterTemplateSuggestSwitchRequest(BaseModel):
    factor_id: str
    template_id: str
    regime_key: str = ""
    note: str = ""


class ParameterTemplateApplySwitchRequest(BaseModel):
    factor_id: str
    template_id: str
    regime_key: str = ""
    suggestion_id: str = ""
    note: str = ""


class ParameterTemplateRecommendationActionRequest(BaseModel):
    recommendation_id: str
    note: str = ""
    symbol: str = "XAUUSD+"
    timeframe: str = "M15"
    risk_per_trade_pct: float | None = None
    enable_circuit: bool = False
    walk_forward_folds: int = 3
    walk_forward_train_bars: int = 180
    walk_forward_test_bars: int = 40
    walk_forward_purge_bars: int = 5
    walk_forward_embargo_bars: int = 5


class ParameterTemplateBoundaryRequest(BaseModel):
    factor_id: str
    template_id: str
    regime_key: str = ""


class ParameterTemplateOfflineValidationRequest(BaseModel):
    factor_id: str
    template_id: str
    regime_key: str = ""
    symbol: str = "XAUUSD+"
    timeframe: str = "M15"
    risk_per_trade_pct: float | None = None
    enable_circuit: bool = False
    walk_forward_folds: int = 3
    walk_forward_train_bars: int = 180
    walk_forward_test_bars: int = 40
    walk_forward_purge_bars: int = 5
    walk_forward_embargo_bars: int = 5
    note: str = ""


class ParameterTemplateOfflineCandidateReviewRequest(BaseModel):
    candidate_id: str
    status: str
    note: str = ""


class ParameterTemplateOfflineCandidateActionRequest(BaseModel):
    candidate_id: str
    note: str = ""


@router.get("/suggestions")
def get_suggestions(
    _user: RequireUser,
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    cache_key = f"suggestions:{status or '*'}:{int(limit)}"
    cached = _learning_cache_get(cache_key)
    if cached is not None:
        return cached
    gov = RuleEvolutionGovernor()
    conn = connect_sqlite(ParameterTemplateService().db_path)
    conn.row_factory = sqlite3.Row
    try:
        items = []
        for raw in gov.list_suggestions(status=status, limit=limit):
            item = dict(raw)
            try:
                evidence = json.loads(item.get("evidence_json") or "{}") if isinstance(item.get("evidence_json"), str) else dict(item.get("evidence") or {})
            except Exception:
                evidence = dict(item.get("evidence") or {})
            item["evidence"] = evidence
            progress = {}
            parameter_template_display = {}
            if "switch_parameter_template" in str(item.get("action") or "").lower():
                progress = _parameter_template_suggestion_progress_snapshot(conn, item)
                parameter_template_display = _parameter_template_suggestion_display_snapshot(item)
            item["progress"] = progress
            item["parameter_template_display"] = parameter_template_display
            items.append(item)
        return _learning_cache_set(cache_key, {"items": items})
    finally:
        conn.close()


@router.post("/review")
def review_suggestion(_user: RequireUser, req: ReviewRequest) -> dict:
    gov = RuleEvolutionGovernor()
    try:
        ok = gov.set_status(req.suggestion_id, req.status, req.note)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail="suggestion not found")
    _learning_cache_invalidate(
        "summary",
        "recommendations:",
        "suggestions:",
        "applications:",
        "reviews:",
        "lifecycle:",
        "offline_candidates:",
    )
    return {
        "ok": True,
        "suggestion_id": req.suggestion_id,
        "status": req.status,
        **_suggestion_review_result_display(req.status),
    }


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
    weight_risk_verdict = _risk_verdict(
        "update_weight",
        {
            "required_mode": "governed",
            "governance": {
                "auto_actions": auto_actions,
                "approved_after": after_summary["approved"],
            },
        },
    )
    if after_summary["approved"] > 0 or auto_actions > 0:
        if weight_risk_verdict.get("allowed", False):
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
    elif (after_summary["approved"] > 0 or auto_actions > 0) and not weight_risk_verdict.get("allowed", False):
        message += f" 权重同步被风控阻断：{weight_risk_verdict.get('reason', 'unknown')}。"
    result_label = f"已处理 {auto_actions} 条" if auto_actions else "没有新动作"
    _learning_cache_invalidate(
        "summary",
        "recommendations:",
        "suggestions:",
        "applications:",
        "reviews:",
        "lifecycle:",
        "offline_candidates:",
    )
    return {
        "review_pending": review_result,
        "reconcile_active": reconcile_result,
        "reconcile_application_effects": effect_result,
        "before": before_summary,
        "after": after_summary,
        "message": message,
        "result_label": result_label,
        "result_summary": message,
        "auto_actions": auto_actions,
        "weights_synced": weights_synced,
        "risk_verdict": weight_risk_verdict,
    }


@router.get("/summary")
def get_learning_summary(_user: RequireUser) -> dict:
    from backend.core.db import STATE_DB, get_state_conn

    cache_key = "summary"
    cached = _learning_cache_get(cache_key)
    if cached is not None:
        return cached
    with _learning_compute_lock(cache_key):
        cached = _learning_cache_get(cache_key)
        if cached is not None:
            return cached

        template_service = ParameterTemplateService(str(STATE_DB))
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
            candidate_rows = conn.execute(
                """
                SELECT candidate_id, factor_id, template_id, regime_key, status,
                       validation_summary_json, validation_report_path, created_at, updated_at
                FROM parameter_template_release_candidate
                ORDER BY created_at DESC
                """
            ).fetchall()
            try:
                lifecycle_count = int(conn.execute(
                    """
                    SELECT COUNT(*) AS c
                    FROM lifecycle_events
                    WHERE source='parameter_template'
                    """
                ).fetchone()["c"] or 0)
            except sqlite3.OperationalError:
                lifecycle_count = 0
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
            candidate_counts: dict[str, int] = {}
            latest_candidate = None
            latest_candidate_trace = None
            if candidate_rows:
                for row in candidate_rows:
                    key = str(row["status"] or "")
                    candidate_counts[key] = candidate_counts.get(key, 0) + 1
                first = candidate_rows[0]
                try:
                    validation_summary = json.loads(first["validation_summary_json"] or "{}")
                except Exception:
                    validation_summary = {}
                latest_candidate = {
                    "candidate_id": str(first["candidate_id"] or ""),
                    "factor_id": str(first["factor_id"] or ""),
                    "template_id": str(first["template_id"] or ""),
                    "regime_key": str(first["regime_key"] or ""),
                    "status": str(first["status"] or ""),
                    "validation_summary": validation_summary,
                    "validation_report_path": str(first["validation_report_path"] or ""),
                    "created_at": float(first["created_at"] or 0.0),
                    "updated_at": float(first["updated_at"] or 0.0),
                }
                recommendation_source = dict(validation_summary.get("recommendation_source") or {})
                if recommendation_source:
                    latest_candidate_trace = {
                        "source": str(recommendation_source.get("source") or ""),
                        "recommendation_id": str(recommendation_source.get("recommendation_id") or ""),
                        "reason": str(recommendation_source.get("reason") or ""),
                        "responsibility": dict(recommendation_source.get("responsibility") or {}),
                        "approval_path": str(recommendation_source.get("approval_path") or ""),
                    }
            recommendations = template_service.list_recommendations(limit=20)
            suggestion_counts = {str(r["status"]): int(r["c"]) for r in suggestions}
            recommendation_counts = {
                "total": len(recommendations),
                "online_light": sum(
                    1 for item in recommendations
                    if str(((item.get("boundary") or {}).get("recommended_scope") or "")) == "online_light"
                ),
                "offline_deep": sum(
                    1 for item in recommendations
                    if str(((item.get("boundary") or {}).get("recommended_scope") or "")) == "offline_deep"
                ),
            }
            latest_recommendation = recommendations[0] if recommendations else None
            first_pending_candidate = next(
                (
                    {
                        "candidate_id": str(row["candidate_id"] or ""),
                        "factor_id": str(row["factor_id"] or ""),
                        "template_id": str(row["template_id"] or ""),
                        "regime_key": str(row["regime_key"] or ""),
                        "status": str(row["status"] or ""),
                        "created_at": float(row["created_at"] or 0.0),
                        "updated_at": float(row["updated_at"] or 0.0),
                    }
                    for row in candidate_rows
                    if str(row["status"] or "").lower() == "pending_review"
                ),
                None,
            )
            first_online_recommendation = next(
                (
                    item for item in recommendations
                    if str(((item.get("boundary") or {}).get("recommended_scope") or "")).lower() == "online_light"
                ),
                None,
            )
            first_offline_recommendation = next(
                (
                    item for item in recommendations
                    if str(((item.get("boundary") or {}).get("recommended_scope") or "")).lower() == "offline_deep"
                ),
                None,
            )
            ops_summary = _build_parameter_template_ops_summary(
                recommendation_counts=recommendation_counts,
                latest_recommendation=latest_recommendation,
                latest_candidate=latest_candidate,
                latest_candidate_trace=latest_candidate_trace,
            )
            parameter_template_todo = _build_parameter_template_todo(
                latest_candidate=latest_candidate,
                latest_recommendation=latest_recommendation,
                latest_candidate_trace=latest_candidate_trace,
                recommendation_counts=recommendation_counts,
            )
            parameter_template_overview = _build_parameter_template_overview(
                suggestion_counts=suggestion_counts,
                first_pending_candidate=first_pending_candidate,
                first_online_recommendation=first_online_recommendation,
                first_offline_recommendation=first_offline_recommendation,
            )
            parameter_template_task_cards = _build_parameter_template_task_cards(
                candidate_counts=candidate_counts,
                recommendation_counts=recommendation_counts,
                lifecycle_count=lifecycle_count,
                parameter_template_todo=parameter_template_todo,
                parameter_template_overview=parameter_template_overview,
            )
            payload = {
                "suggestions": suggestion_counts,
                "reviews": review_counts,
                "applications": int((apps["c"] if apps else 0) or 0),
                "parameter_template_candidates": candidate_counts,
                "parameter_template_recommendations": recommendation_counts,
                "parameter_template_ops_summary": ops_summary,
                "parameter_template_todo": parameter_template_todo,
                "parameter_template_overview": parameter_template_overview,
                "parameter_template_empty_states": _build_parameter_template_empty_states(),
                "parameter_template_task_cards": parameter_template_task_cards,
                "latest_review": {
                    "review_id": last_review["review_id"],
                    "trade_id": last_review["trade_id"],
                    "position_id": last_review["position_id"],
                    "entry_decision_id": last_review["entry_decision_id"],
                    "exit_decision_id": last_review["exit_decision_id"],
                    "outcome_label": last_review["outcome_label"],
                    "pnl": last_review["pnl"],
                    "summary_text": last_review["summary_text"],
                    "created_at": last_review["created_at"],
                    "trace_locator": _trace_locator_from_review_row(last_review),
                } if last_review else None,
                "latest_parameter_template_candidate": latest_candidate,
                "latest_parameter_template_candidate_trace": {
                    **latest_candidate_trace,
                    "trace_locator": _latest_factor_trace_locator(
                        conn,
                        str((latest_candidate or {}).get("factor_id") or ""),
                    ),
                } if latest_candidate_trace else None,
                "latest_parameter_template_recommendation": latest_recommendation,
            }
            return _learning_cache_set(cache_key, payload)
        finally:
            conn.close()


@router.get("/reviews")
def get_reviews(
    _user: RequireUser,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    from backend.core.db import get_state_conn

    cache_key = f"reviews:{int(limit)}"
    cached = _learning_cache_get(cache_key)
    if cached is not None:
        return cached
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
        for item in items:
            item["trace_locator"] = _trace_locator_from_review_row(item)
        return _learning_cache_set(cache_key, {"items": items})
    finally:
        conn.close()


@router.get("/factor-cards")
def get_factor_cards(
    _user: RequireUser,
    limit: int = Query(default=100, ge=1, le=500),
    source: str | None = Query(default=None),
    lifecycle_status: str | None = Query(default=None),
    factor_id: str | None = Query(default=None),
    factor_family: str | None = Query(default=None),
) -> dict:
    source = source if isinstance(source, str) and source else None
    lifecycle_status = (
        lifecycle_status if isinstance(lifecycle_status, str) and lifecycle_status else None
    )
    factor_id = factor_id if isinstance(factor_id, str) and factor_id else None
    factor_family = factor_family if isinstance(factor_family, str) and factor_family else None
    service = FactorCardService()
    return {
        "items": service.list_cards(
            limit=limit,
            source=source,
            lifecycle_status=lifecycle_status,
            factor_id=factor_id,
            factor_family=factor_family,
        )
    }


@router.get("/position-supervisor/templates")
def list_position_supervisor_template_catalog(_user: RequireUser) -> dict:
    return {
        "schema_version": "position_supervisor_template_catalog.v1",
        "items": list_position_supervisor_templates(),
    }


@router.get("/position-supervisor/replay")
def replay_position_supervisor_template_catalog(
    _user: RequireUser,
    day: str = Query(default="2026-06-26"),
    small_abs_pnl: float = Query(default=5.0, ge=0.0, le=100.0),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict:
    cache_key = f"position_supervisor_replay:{day}:{float(small_abs_pnl):.4f}:{int(limit)}"
    cached = _learning_cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        payload = replay_position_supervisor_templates(
            day=day,
            small_abs_pnl=small_abs_pnl,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _learning_cache_set(cache_key, payload)


@router.get("/position-supervisor/advisories")
def list_position_supervisor_advisories(
    _user: RequireUser,
    day: str = Query(default="2026-06-26"),
) -> dict:
    cache_key = f"position_supervisor_advisories:{day}"
    cached = _learning_cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        payload = build_position_supervisor_advisories(day=day, materialize=False)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _learning_cache_set(cache_key, payload)


@router.post("/position-supervisor/advisories/materialize")
def materialize_position_supervisor_advisories(
    _user: RequireUser,
    day: str = Query(default="2026-06-26"),
) -> dict:
    try:
        payload = build_position_supervisor_advisories(day=day, materialize=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _learning_cache_invalidate("position_supervisor_advisories:", "suggestions:", "summary")
    return payload


@router.post("/position-supervisor/templates/apply-switch")
def apply_position_supervisor_template_switch(
    _user: RequireUser,
    req: PositionSupervisorTemplateApplySwitchRequest,
) -> dict:
    suggestion_id = str(req.suggestion_id or "").strip()
    if not suggestion_id:
        raise HTTPException(status_code=400, detail="suggestion_id_required")
    evo_run = start_evolution_run(
        run_type="position_supervisor_template_switch",
        trigger_source="learning_api",
        db_path=STATE_DB,
        summary={"suggestion_id": suggestion_id, "note": req.note},
    )
    conn = connect_sqlite(STATE_DB)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT *
            FROM policy_suggestion
            WHERE suggestion_id=?
            LIMIT 1
            """,
            (suggestion_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="suggestion_not_found")
        scope_type = str(row["scope_type"] or "")
        scope_key = str(row["scope_key"] or "")
        status = str(row["status"] or "")
        if scope_type != "position_supervisor_template":
            raise HTTPException(status_code=400, detail="not_position_supervisor_template_suggestion")
        evidence = json.loads(row["evidence_json"] or "{}")
        valid_templates = {str(item.get("template_id") or "") for item in list_position_supervisor_templates()}
        if scope_key not in valid_templates:
            raise HTTPException(status_code=400, detail="invalid_position_supervisor_template")
        try:
            from config.runtime_config import patch as patch_runtime_config
            from config.runtime_config import shared as runtime_config
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"runtime_config_unavailable: {exc}")
        previous_template_id = str(getattr(runtime_config(), "position_supervisor_template_id", "") or "position_supervisor:default.v1")
        verdict = RiskPolicyService.shared().evaluate(
            "switch_position_supervisor_template",
            {
                "suggestion_id": suggestion_id,
                "suggestion_status": status,
                "target_template_id": scope_key,
                "previous_template_id": previous_template_id,
                "evidence": evidence,
            },
        ).to_dict()
        if not verdict.get("allowed", False):
            payload = {
                "blocked": True,
                "suggestion_id": suggestion_id,
                "target_template_id": scope_key,
                "previous_template_id": previous_template_id,
                "risk_verdict": verdict,
            }
            record_evolution_decision(
                run_id=str(evo_run.get("run_id") or ""),
                decision_type="apply_switch",
                scope_type="position_supervisor_template",
                scope_key=scope_key,
                action="switch_position_supervisor_template",
                status="blocked",
                evidence=evidence,
                risk_verdict=verdict,
                before={"template_id": previous_template_id},
                after={"template_id": scope_key},
                result=payload,
                db_path=STATE_DB,
            )
            finish_evolution_run(str(evo_run.get("run_id") or ""), status="blocked", summary=payload, db_path=STATE_DB)
            return payload
        patch_runtime_config({"position_supervisor_template_id": scope_key})
        snapshot = persist_runtime_config_snapshot(
            runtime_config(),
            source="learning_api.position_supervisor_template_switch",
            db_path=STATE_DB,
            run_id=str(evo_run.get("run_id") or ""),
        )
        now_ts = time.time()
        application_id = f"psv_apply_{int(now_ts)}_{suggestion_id[-8:]}"
        details = {
            "schema_version": "position_supervisor_template_switch.v1",
            "suggestion_id": suggestion_id,
            "previous_template_id": previous_template_id,
            "target_template_id": scope_key,
            "note": req.note,
            "risk_verdict": verdict,
            "evidence": evidence,
            "config_version": int(snapshot.get("config_version") or 0),
            "config_hash": str(snapshot.get("config_hash") or ""),
        }
        conn.execute(
            """
            INSERT OR REPLACE INTO learning_application_log
            (application_id, cycle_ts, scope_type, scope_key, action,
             bias_multiplier, old_weight, new_weight, suggestion_ids_json,
             status, details_json, created_at)
            VALUES (?, ?, 'position_supervisor_template', ?, 'switch_position_supervisor_template',
                    1.0, 0.0, 0.0, ?, 'applied', ?, ?)
            """,
            (
                application_id,
                now_ts,
                scope_key,
                json.dumps([suggestion_id], ensure_ascii=False),
                json.dumps(details, ensure_ascii=False, default=str),
                now_ts,
            ),
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO learning_application_effect
            (application_id, scope_type, scope_key, action, status,
             decision_json, updated_at, created_at)
            VALUES (?, 'position_supervisor_template', ?, 'switch_position_supervisor_template',
                    'observing', ?, ?, COALESCE(
                        (SELECT created_at FROM learning_application_effect WHERE application_id=?),
                        ?
                    ))
            """,
            (
                application_id,
                scope_key,
                json.dumps(details, ensure_ascii=False, default=str),
                now_ts,
                application_id,
                now_ts,
            ),
        )
        conn.execute(
            """
            UPDATE policy_suggestion
            SET status='applied', reviewed_at=CASE WHEN reviewed_at > 0 THEN reviewed_at ELSE ? END,
                review_note=?
            WHERE suggestion_id=?
            """,
            (now_ts, req.note or "applied position supervisor template switch", suggestion_id),
        )
        conn.commit()
        _learning_cache_invalidate("position_supervisor_advisories:", "suggestions:", "summary", "applications:")
        payload = {
            "blocked": False,
            "suggestion_id": suggestion_id,
            "application_id": application_id,
            "previous_template_id": previous_template_id,
            "target_template_id": scope_key,
            "risk_verdict": verdict,
        }
        record_evolution_decision(
            run_id=str(evo_run.get("run_id") or ""),
            decision_type="apply_switch",
            scope_type="position_supervisor_template",
            scope_key=scope_key,
            action="switch_position_supervisor_template",
            status="applied",
            evidence=evidence,
            risk_verdict=verdict,
            before={"template_id": previous_template_id},
            after={"template_id": scope_key},
            result={"suggestion_id": suggestion_id, "application_id": application_id},
            rollback={"previous_template_id": previous_template_id},
            config_version=int(snapshot.get("config_version") or 0),
            config_hash=str(snapshot.get("config_hash") or ""),
            db_path=STATE_DB,
        )
        finish_evolution_run(str(evo_run.get("run_id") or ""), status="completed", summary=payload, db_path=STATE_DB)
        return payload
    except HTTPException:
        raise
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid_suggestion_evidence_json")
    finally:
        conn.close()


@router.post("/position-supervisor/counterfactual/run")
def run_position_supervisor_counterfactual(
    _user: RequireUser,
    req: SupervisorCounterfactualRunRequest,
) -> dict:
    payload = evaluate_counterfactuals(
        db_path=req.db_path or STATE_DB,
        limit=max(1, int(req.limit)),
        horizons_minutes=req.horizons_minutes,
        materialize=bool(req.materialize),
    )
    if req.materialize:
        _learning_cache_invalidate("position_supervisor_counterfactual:")
    return payload


@router.get("/position-supervisor/counterfactual")
def get_position_supervisor_counterfactual(
    _user: RequireUser,
    limit: int = Query(default=100, ge=1, le=1000),
    position_id: str | None = Query(default=None),
    label: str | None = Query(default=None),
    db_path: str | None = Query(default=None),
) -> dict:
    return list_counterfactuals(
        db_path=db_path or STATE_DB,
        limit=limit,
        position_id=position_id,
        label=label,
    )


@router.post("/position-supervisor/traces/backfill")
def backfill_position_supervisor_trace_api(
    _user: RequireUser,
    req: PositionSupervisorTraceMaterializeRequest,
) -> dict:
    payload = backfill_position_supervisor_traces(
        db_path=req.db_path or STATE_DB,
        limit=max(1, int(req.limit)),
    )
    _learning_cache_invalidate("autonomous:samples", "evolution:")
    return payload


@router.post("/position-supervisor/traces/materialize-labels")
def materialize_position_supervisor_trace_labels_api(
    _user: RequireUser,
    req: PositionSupervisorTraceMaterializeRequest,
) -> dict:
    payload = mature_position_supervisor_traces(
        db_path=req.db_path or STATE_DB,
        limit=max(1, int(req.limit)),
    )
    _learning_cache_invalidate("autonomous:samples", "evolution:")
    return payload


@router.get("/evolution/runs")
def get_learning_evolution_runs(
    _user: RequireUser,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    cache_key = f"evolution:runs:{int(limit)}"
    cached = _learning_cache_get(cache_key)
    if cached is not None:
        return cached
    return _learning_cache_set(cache_key, list_evolution_runs(db_path=STATE_DB, limit=limit))


@router.get("/evolution/runs/{run_id}")
def get_learning_evolution_run(_user: RequireUser, run_id: str) -> dict:
    payload = get_evolution_run(run_id, db_path=STATE_DB)
    if not payload:
        raise HTTPException(status_code=404, detail="evolution_run_not_found")
    return payload


@router.post("/autonomous/run")
def run_learning_autonomous_cycle(_user: RequireUser, req: AutonomousLearningRunRequest) -> dict:
    payload = run_autonomous_learning_cycle(
        db_path=req.db_path or STATE_DB,
        sample_limit=max(1, int(req.sample_limit)),
        recommendation_limit=max(1, int(req.recommendation_limit)),
        submit_offline_deep=bool(req.submit_offline_deep),
    )
    _learning_cache_invalidate(
        "summary",
        "recommendations:",
        "suggestions:",
        "applications:",
        "reviews:",
        "lifecycle:",
        "offline_candidates:",
    )
    return payload


@router.get("/autonomous/samples")
def get_learning_autonomous_samples(
    _user: RequireUser,
    limit: int = Query(default=100, ge=1, le=1000),
    sample_type: str | None = Query(default=None),
    label_status: str | None = Query(default=None),
    position_id: str | None = Query(default=None),
    db_path: str | None = Query(default=None),
) -> dict:
    return list_autonomous_learning_samples(
        db_path=db_path or STATE_DB,
        limit=limit,
        sample_type=sample_type,
        label_status=label_status,
        position_id=position_id,
    )


@router.get("/parameter-templates")
def get_parameter_templates(
    _user: RequireUser,
    limit: int = Query(default=200, ge=1, le=500),
    factor_id: str | None = Query(default=None),
    regime: str | None = Query(default=None),
) -> dict:
    factor_id = factor_id if isinstance(factor_id, str) and factor_id else None
    regime = regime if isinstance(regime, str) and regime else None
    service = ParameterTemplateService()
    return {
        "items": service.list_templates(
            factor_id=factor_id,
            regime=regime,
            limit=limit,
        )
    }


@router.get("/parameter-templates/active")
def get_active_parameter_templates(
    _user: RequireUser,
    factor_id: str | None = Query(default=None),
) -> dict:
    factor_id = factor_id if isinstance(factor_id, str) and factor_id else None
    service = ParameterTemplateService()
    return {"items": service.list_active_templates(factor_id=factor_id)}


@router.get("/parameter-templates/recommendations")
def get_parameter_template_recommendations(
    _user: RequireUser,
    factor_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    factor_id = factor_id if isinstance(factor_id, str) and factor_id else None
    cache_key = f"recommendations:{factor_id or '*'}:{int(limit)}"
    cached = _learning_cache_get(cache_key)
    if cached is not None:
        return cached
    with _learning_compute_lock(cache_key):
        cached = _learning_cache_get(cache_key)
        if cached is not None:
            return cached
        service = ParameterTemplateService()
        items = service.list_recommendations(factor_id=factor_id, limit=limit)
        validation_service = ParameterTemplateValidationService(service.db_path)
        conn = connect_sqlite(service.db_path)
        conn.row_factory = sqlite3.Row
        try:
            enriched = []
            for item in items:
                recommendation_id = str(item.get("recommendation_id") or "")
                factor_id = str(item.get("factor_id") or "")
                suggestion = _latest_template_suggestion_for_recommendation(conn, recommendation_id)
                candidate = {}
                candidates = validation_service.list_release_candidates(factor_id=factor_id, limit=20)
                for candidate_item in candidates:
                    trace = dict(((candidate_item.get("validation_summary") or {}).get("recommendation_source") or {}))
                    if str(trace.get("recommendation_id") or "") == recommendation_id:
                        candidate = candidate_item
                        break
                lifecycle = _latest_parameter_template_lifecycle_for_recommendation(
                    conn,
                    factor_id=factor_id,
                    recommendation_id=recommendation_id,
                    candidate_id=str(candidate.get("candidate_id") or ""),
                )
                governance = _parameter_template_recommendation_governance_snapshot(item)
                progress = _parameter_template_recommendation_progress_snapshot(
                    recommendation_id=recommendation_id,
                    candidate=candidate,
                    suggestion=suggestion,
                    lifecycle=lifecycle,
                )
                enriched.append(
                    {
                        **item,
                        "governance": governance,
                        "progress": progress,
                        "suggestion": suggestion,
                        "latest_candidate": candidate,
                        "lifecycle_event": lifecycle,
                        "trace_locator": _latest_factor_trace_locator(
                            conn,
                            factor_id,
                        ),
                    }
                )
            return _learning_cache_set(cache_key, {"items": enriched})
        finally:
            conn.close()


@router.get("/parameter-templates/switch-logs")
def get_parameter_template_switch_logs(
    _user: RequireUser,
    factor_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    factor_id = factor_id if isinstance(factor_id, str) and factor_id else None
    service = ParameterTemplateService()
    return {"items": service.list_switch_logs(factor_id=factor_id, limit=limit)}


@router.post("/parameter-templates/upsert")
def upsert_parameter_template(_user: RequireUser, req: ParameterTemplateUpsertRequest) -> dict:
    service = ParameterTemplateService()
    try:
        item = service.upsert_template(req.template, source=req.source, activate=bool(req.activate))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _learning_cache_invalidate(
        "summary",
        "recommendations:",
        "suggestions:",
        "applications:",
        "reviews:",
        "lifecycle:",
        "offline_candidates:",
    )
    return {
        "ok": True,
        "item": item,
        "result_label": "已保存并激活" if req.activate else "已保存模板",
        "result_summary": (
            "这条参数模板已保存并同步为当前运行态模板。"
            if req.activate
            else "这条参数模板已保存，后续可进入建议、审批或切换链路。"
        ),
    }


@router.post("/parameter-templates/suggest-switch")
def suggest_parameter_template_switch(
    _user: RequireUser,
    req: ParameterTemplateSuggestSwitchRequest,
) -> dict:
    service = ParameterTemplateService()
    try:
        item = service.create_switch_suggestion(
            factor_id=req.factor_id,
            template_id=req.template_id,
            regime_key=req.regime_key,
            note=req.note,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _learning_cache_invalidate(
        "summary",
        "recommendations:",
        "suggestions:",
        "applications:",
        "reviews:",
        "lifecycle:",
        "offline_candidates:",
    )
    return {
        "ok": True,
        "item": item,
        "result_label": "已生成治理建议",
        "result_summary": "这条参数模板切换建议已创建，下一步等待 governor 审批。",
    }


@router.post("/parameter-templates/recommendations/materialize")
def materialize_parameter_template_recommendation(
    _user: RequireUser,
    req: ParameterTemplateRecommendationActionRequest,
) -> dict:
    service = ParameterTemplateService()
    try:
        recommendation = service.get_recommendation(req.recommendation_id)
        if not recommendation:
            raise ValueError(f"recommendation not found: {req.recommendation_id}")
        if str(recommendation.get("recommended_action") or "") == "offline_validate":
            boundary = dict(recommendation.get("boundary") or {})
            params = req.model_dump()
            params.update(
                {
                    "factor_id": str(recommendation.get("factor_id") or ""),
                    "template_id": str(recommendation.get("target_template_id") or ""),
                    "regime_key": str(recommendation.get("regime_key") or ""),
                    "recommended_scope": boundary.get("recommended_scope"),
                    "boundary_reasons": list(boundary.get("reasons") or []),
                    "recommendation_context": {
                        "source": "parameter_template_recommendation",
                        "recommendation_id": req.recommendation_id,
                        "reason": recommendation.get("reason", ""),
                        "responsibility": dict(recommendation.get("responsibility") or {}),
                        "approval_path": recommendation.get("approval_path", ""),
                    },
                }
            )
            mgr = get_job_manager()
            fn = lambda cb: run_parameter_template_offline_validation(params, cb)
            js = mgr.submit("parameter_template_validation", params, fn)
            result = {
                "ok": True,
                "mode": "offline_validate",
                "result_label": "已创建离线验证",
                "result_summary": "这条推荐已进入离线验证作业；验证通过后会登记灰度候选。",
                "recommendation": recommendation,
                "job_id": js.id,
                "status": js.status,
                "boundary": boundary,
            }
        else:
            result = service.create_suggestion_from_recommendation(
                recommendation_id=req.recommendation_id,
                note=req.note,
            )
            result["mode"] = "suggest_switch"
            result["result_label"] = "已生成治理建议"
            result["result_summary"] = "这条推荐已转成正式治理建议，下一步等待 governor 审批。"
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _learning_cache_invalidate(
        "summary",
        "recommendations:",
        "suggestions:",
        "applications:",
        "reviews:",
        "lifecycle:",
        "offline_candidates:",
    )
    return result


@router.post("/parameter-templates/apply-switch")
def apply_parameter_template_switch(
    _user: RequireUser,
    req: ParameterTemplateApplySwitchRequest,
) -> dict:
    service = ParameterTemplateService()
    try:
        result = service.activate_template(
            factor_id=req.factor_id,
            template_id=req.template_id,
            regime_key=req.regime_key,
            suggestion_id=req.suggestion_id,
            note=req.note,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _learning_cache_invalidate(
        "summary",
        "recommendations:",
        "suggestions:",
        "applications:",
        "reviews:",
        "lifecycle:",
        "offline_candidates:",
    )
    if result.get("blocked"):
        return {
            **result,
            **_offline_candidate_action_result_display(action="release", blocked=True),
        }
    return {
        **result,
        **_offline_candidate_action_result_display(action="release"),
    }


@router.post("/parameter-templates/boundary-check")
def assess_parameter_template_boundary(
    _user: RequireUser,
    req: ParameterTemplateBoundaryRequest,
) -> dict:
    service = ParameterTemplateService()
    try:
        item = service.assess_template_change(
            factor_id=req.factor_id,
            target_template_id=req.template_id,
            regime_key=req.regime_key,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "item": item}


@router.post("/parameter-templates/offline-validate")
def submit_parameter_template_offline_validation(
    _user: RequireUser,
    req: ParameterTemplateOfflineValidationRequest,
) -> dict:
    service = ParameterTemplateService()
    try:
        boundary = service.assess_template_change(
            factor_id=req.factor_id,
            target_template_id=req.template_id,
            regime_key=req.regime_key,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if boundary.get("recommended_scope") != "offline_deep":
        return {
            "ok": False,
            "blocked": True,
            "message": "template fits online_light; use governed apply-switch flow instead",
            "boundary": boundary,
        }

    mgr = get_job_manager()
    params = req.model_dump()
    params["recommended_scope"] = boundary.get("recommended_scope")
    params["boundary_reasons"] = list(boundary.get("reasons") or [])
    fn = lambda cb: run_parameter_template_offline_validation(params, cb)
    js = mgr.submit("parameter_template_validation", params, fn)
    return {
        "ok": True,
        "job_id": js.id,
        "status": js.status,
        "boundary": boundary,
    }


@router.get("/parameter-templates/offline-candidates")
def list_parameter_template_offline_candidates(
    _user: RequireUser,
    factor_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    factor_id = factor_id if isinstance(factor_id, str) and factor_id else None
    status = status if isinstance(status, str) and status else None
    cache_key = f"offline_candidates:{factor_id or '*'}:{status or '*'}:{int(limit)}"
    cached = _learning_cache_get(cache_key)
    if cached is not None:
        return cached
    service = ParameterTemplateValidationService()
    items = service.list_release_candidates(
        factor_id=factor_id,
        status=status,
        limit=limit,
    )
    conn = connect_sqlite(service.db_path)
    conn.row_factory = sqlite3.Row
    try:
        enriched = []
        for item in items:
            governance = _parameter_template_candidate_governance_snapshot(item)
            enriched.append(
                {
                    **item,
                    "governance": governance,
                    "trace_locator": _latest_factor_trace_locator(
                        conn,
                        str(item.get("factor_id") or ""),
                    ),
                }
            )
        return _learning_cache_set(cache_key, {"items": enriched})
    finally:
        conn.close()


@router.post("/parameter-templates/offline-candidates/review")
def review_parameter_template_offline_candidate(
    _user: RequireUser,
    req: ParameterTemplateOfflineCandidateReviewRequest,
) -> dict:
    service = ParameterTemplateValidationService()
    try:
        item = service.review_release_candidate(
            candidate_id=req.candidate_id,
            status=req.status,
            note=req.note,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _learning_cache_invalidate(
        "summary",
        "recommendations:",
        "suggestions:",
        "applications:",
        "reviews:",
        "lifecycle:",
        "offline_candidates:",
    )
    return {
        "ok": True,
        "item": item,
        **_offline_candidate_action_result_display(
            action="review",
            status=req.status,
        ),
    }


@router.post("/parameter-templates/offline-candidates/release")
def release_parameter_template_offline_candidate(
    _user: RequireUser,
    req: ParameterTemplateOfflineCandidateActionRequest,
) -> dict:
    service = ParameterTemplateValidationService()
    try:
        result = service.deploy_release_candidate(
            candidate_id=req.candidate_id,
            note=req.note,
        )
    except ValueError as e:
        message = str(e)
        if "candidate template missing/orphan candidate" in message:
            friendly_message = "候选模板已不存在，请重新生成候选"
            raise HTTPException(status_code=400, detail=friendly_message)
        raise HTTPException(status_code=400, detail=message)
    _learning_cache_invalidate(
        "summary",
        "recommendations:",
        "suggestions:",
        "applications:",
        "reviews:",
        "lifecycle:",
        "offline_candidates:",
    )
    if result.get("blocked"):
        return {
            **result,
            **_offline_candidate_action_result_display(action="rollback", blocked=True),
        }
    return {
        **result,
        **_offline_candidate_action_result_display(action="release"),
    }


@router.post("/parameter-templates/offline-candidates/rollback")
def rollback_parameter_template_offline_candidate(
    _user: RequireUser,
    req: ParameterTemplateOfflineCandidateActionRequest,
) -> dict:
    service = ParameterTemplateValidationService()
    try:
        result = service.rollback_release_candidate(
            candidate_id=req.candidate_id,
            note=req.note,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _learning_cache_invalidate(
        "summary",
        "recommendations:",
        "suggestions:",
        "applications:",
        "reviews:",
        "lifecycle:",
        "offline_candidates:",
    )
    if result.get("blocked"):
        return {
            **result,
            **_offline_candidate_action_result_display(action="rollback", blocked=True),
        }
    return {
        **result,
        **_offline_candidate_action_result_display(action="rollback"),
    }


@router.get("/applications")
def get_applications(
    _user: RequireUser,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    from backend.core.db import get_state_conn

    cache_key = f"applications:{int(limit)}"
    cached = _learning_cache_get(cache_key)
    if cached is not None:
        return cached
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
        return _learning_cache_set(cache_key, {"items": items})
    finally:
        conn.close()


@router.get("/lifecycle")
def get_lifecycle(
    _user: RequireUser,
    limit: int = Query(default=60, ge=1, le=500),
) -> dict:
    from backend.core.db import get_state_conn

    cache_key = f"lifecycle:{int(limit)}"
    cached = _learning_cache_get(cache_key)
    if cached is not None:
        return cached
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
            candidate_trace = {}
            if str(item.get("source") or "") == "parameter_template":
                text = f"{item.get('description') or ''} {item.get('reason') or ''}"
                match = _CANDIDATE_ID_RE.search(text)
                if match:
                    candidate_trace = _candidate_trace_by_id(conn, match.group(1))
                if candidate_trace:
                    candidate_trace["trace_locator"] = _latest_factor_trace_locator(
                        conn,
                        str(item.get("factor") or ""),
                    )
            governance = (
                _parameter_template_lifecycle_governance_snapshot(item, candidate_trace)
                if str(item.get("source") or "") == "parameter_template"
                else {}
            )
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
                    "governance": governance,
                    "metrics": {"candidate_trace": candidate_trace} if candidate_trace else {},
                    "kind": "factor_lifecycle",
                }
            )
        return _learning_cache_set(cache_key, {"items": items})
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


@router.post("/model/meta/context")
def build_learning_meta_model_context(_user: RequireUser, req: MetaModelContextRequest) -> dict:
    return MetaModelSidecar(req.db_path or STATE_DB).build_context(req.context)


@router.post("/model/meta/advisory-run")
def run_learning_meta_model_advisory(_user: RequireUser, req: MetaModelAdvisoryRunRequest) -> dict:
    return MetaModelSidecar(req.db_path or STATE_DB).run(
        context=req.context,
        materialize=bool(req.materialize),
    )


@router.get("/model/meta/advisories")
def list_learning_meta_model_advisories(
    _user: RequireUser,
    limit: int = Query(default=50, ge=1, le=500),
    db_path: str | None = Query(default=None),
) -> dict:
    return MetaModelSidecar(db_path or STATE_DB).list_advisories(limit=limit)


@router.post("/model/llm/advisory-run")
def run_learning_llm_advisory(_user: RequireUser, req: LLMAdvisoryRunRequest) -> dict:
    max_output_tokens = max(1, int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "32768") or 32768))
    return LLMAdvisoryService(
        req.db_path or STATE_DB,
        provider=req.provider,
        model=req.model,
        base_url=req.base_url,
    ).run(
        task_type=req.task_type,
        context=req.context,
        target_type=req.target_type,
        target_id=req.target_id,
        dry_run=bool(req.dry_run),
        max_tokens=None if req.max_tokens is None else max(1, min(int(req.max_tokens), max_output_tokens)),
        temperature=max(0.0, min(float(req.temperature), 2.0)),
    )


@router.get("/model/llm/audits")
def list_learning_llm_advisory_audits(
    _user: RequireUser,
    limit: int = Query(default=50, ge=1, le=500),
    task_type: str | None = Query(default=None),
    target_type: str | None = Query(default=None),
    target_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    db_path: str | None = Query(default=None),
) -> dict:
    return LLMAdvisoryService(db_path or STATE_DB).list_audits(
        limit=limit,
        task_type=task_type,
        target_type=target_type,
        target_id=target_id,
        status=status,
    )


@router.post("/model/permissions/validate")
def validate_learning_model_permissions(_user: RequireUser, req: ModelPermissionValidateRequest) -> dict:
    if not req.artifact_path and not req.artifact:
        raise HTTPException(status_code=400, detail="artifact_path or artifact is required")
    target: str | dict[str, Any] = req.artifact_path or req.artifact or {}
    return validate_model_artifact(
        target,
        model_type=req.model_type,
        db_path=req.db_path or STATE_DB,
        context={"operation": "api_validate_model_permissions"},
        require_shadow=bool(req.require_shadow),
    )


@router.get("/model/permissions/audits")
def list_learning_model_permission_audits(
    _user: RequireUser,
    limit: int = Query(default=100, ge=1, le=1000),
    model_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    db_path: str | None = Query(default=None),
) -> dict:
    return list_model_permission_audits(
        db_path=db_path or STATE_DB,
        limit=limit,
        model_type=model_type,
        status=status,
    )


@router.post("/model/position-quality-lightgbm/train")
def train_position_quality_lightgbm(_user: RequireUser, req: PositionQualityLightGBMTrainRequest) -> dict:
    service = PositionQualityLightGBMService(
        db_path=req.db_path or STATE_DB,
        artifact_dir=req.artifact_dir,
    )
    result = service.train(
        limit=max(1, int(req.limit)),
        holdout_ratio=max(0.0, min(float(req.holdout_ratio), 0.8)),
        min_samples=max(1, int(req.min_samples)),
        register=bool(req.register_model),
        registry_db_path=req.registry_db_path,
        symbol=req.symbol,
        timeframe=req.timeframe,
    )
    if result.get("ok") and req.run_shadow:
        result["shadow"] = service.score_samples(
            artifact_path=result.get("artifact_path"),
            limit=max(1, int(req.shadow_limit)),
            mode="shadow_after_train",
        )
    return result


@router.post("/model/position-quality-lightgbm/shadow-run")
def run_position_quality_lightgbm_shadow(_user: RequireUser, req: PositionQualityLightGBMShadowRequest) -> dict:
    service = PositionQualityLightGBMService(
        db_path=req.db_path or STATE_DB,
        artifact_dir=req.artifact_dir,
    )
    return service.score_samples(
        artifact_path=req.artifact_path,
        limit=max(1, int(req.limit)),
        mode=req.mode or "shadow",
    )


@router.get("/model/position-quality-lightgbm/audits")
def list_position_quality_lightgbm_audits(
    _user: RequireUser,
    limit: int = Query(default=100, ge=1, le=1000),
    position_id: str | None = Query(default=None),
    db_path: str | None = Query(default=None),
) -> dict:
    service = PositionQualityLightGBMService(
        db_path=db_path or STATE_DB,
    )
    return service.list_audits(limit=limit, position_id=position_id)


@router.post("/model/factor-governance-lightgbm/train")
def train_factor_governance_lightgbm(_user: RequireUser, req: FactorGovernanceLightGBMTrainRequest) -> dict:
    service = FactorGovernanceLightGBMService(
        db_path=req.db_path or STATE_DB,
        artifact_dir=req.artifact_dir,
    )
    result = service.train(
        limit=max(1, int(req.limit)),
        holdout_ratio=max(0.0, min(float(req.holdout_ratio), 0.8)),
        min_samples=max(1, int(req.min_samples)),
        register=bool(req.register_model),
        registry_db_path=req.registry_db_path,
        symbol=req.symbol,
        timeframe=req.timeframe,
    )
    if result.get("ok") and req.run_shadow:
        result["shadow"] = service.score_samples(
            artifact_path=result.get("artifact_path"),
            limit=max(1, int(req.shadow_limit)),
            mode="shadow_after_train",
            materialize=bool(req.materialize_suggestions),
            min_weakness_score=max(0.0, min(float(req.min_weakness_score), 1.0)),
        )
    return result


@router.post("/model/factor-governance-lightgbm/shadow-run")
def run_factor_governance_lightgbm_shadow(_user: RequireUser, req: FactorGovernanceLightGBMShadowRequest) -> dict:
    service = FactorGovernanceLightGBMService(
        db_path=req.db_path or STATE_DB,
        artifact_dir=req.artifact_dir,
    )
    return service.score_samples(
        artifact_path=req.artifact_path,
        limit=max(1, int(req.limit)),
        mode=req.mode or "shadow",
        materialize=bool(req.materialize_suggestions),
        min_weakness_score=max(0.0, min(float(req.min_weakness_score), 1.0)),
    )


@router.get("/model/factor-governance-lightgbm/audits")
def list_factor_governance_lightgbm_audits(
    _user: RequireUser,
    limit: int = Query(default=100, ge=1, le=1000),
    factor: str | None = Query(default=None),
    db_path: str | None = Query(default=None),
) -> dict:
    service = FactorGovernanceLightGBMService(
        db_path=db_path or STATE_DB,
    )
    return service.list_audits(limit=limit, factor=factor)


@router.get("/model/factor-governance-lightgbm/advisories")
def list_factor_governance_lightgbm_advisories(
    _user: RequireUser,
    limit: int = Query(default=500, ge=1, le=2000),
    factor: str | None = Query(default=None),
    materialize: bool = Query(default=False),
    min_weakness_score: float = Query(default=0.65, ge=0.0, le=1.0),
    db_path: str | None = Query(default=None),
) -> dict:
    service = FactorGovernanceLightGBMService(
        db_path=db_path or STATE_DB,
    )
    audits = service.list_audits(limit=limit, factor=factor)
    payload = service.build_advisories(
        items=audits["items"],
        materialize=bool(materialize),
        min_weakness_score=float(min_weakness_score),
    )
    if materialize:
        _learning_cache_invalidate("suggestions:", "summary")
    return payload


@router.post("/model/meta-lightgbm/train")
def train_meta_model_lightgbm(_user: RequireUser, req: MetaModelLightGBMTrainRequest) -> dict:
    service = MetaModelLightGBMService(
        db_path=req.db_path or STATE_DB,
        artifact_dir=req.artifact_dir,
    )
    result = service.train(
        limit=max(1, int(req.limit)),
        window=max(1, int(req.window)),
        horizon=max(1, int(req.horizon)),
        holdout_ratio=max(0.0, min(float(req.holdout_ratio), 0.8)),
        min_samples=max(1, int(req.min_samples)),
        register=bool(req.register_model),
        registry_db_path=req.registry_db_path,
        symbol=req.symbol,
        timeframe=req.timeframe,
    )
    if result.get("ok") and req.run_shadow:
        result["shadow"] = service.score_samples(
            artifact_path=result.get("artifact_path"),
            limit=max(1, int(req.shadow_limit)),
            window=max(1, int(req.window)),
            horizon=max(1, int(req.horizon)),
            mode="shadow_after_train",
            materialize_ledger=bool(req.materialize_ledger),
        )
    return result


@router.post("/model/meta-lightgbm/shadow-run")
def run_meta_model_lightgbm_shadow(_user: RequireUser, req: MetaModelLightGBMShadowRequest) -> dict:
    service = MetaModelLightGBMService(
        db_path=req.db_path or STATE_DB,
        artifact_dir=req.artifact_dir,
    )
    return service.score_samples(
        artifact_path=req.artifact_path,
        limit=max(1, int(req.limit)),
        window=max(1, int(req.window)),
        horizon=max(1, int(req.horizon)),
        mode=req.mode or "shadow",
        materialize_ledger=bool(req.materialize_ledger),
    )


@router.get("/model/meta-lightgbm/audits")
def list_meta_model_lightgbm_audits(
    _user: RequireUser,
    limit: int = Query(default=100, ge=1, le=1000),
    posture: str | None = Query(default=None),
    db_path: str | None = Query(default=None),
    artifact_dir: str | None = Query(default=None),
) -> dict:
    service = MetaModelLightGBMService(
        db_path=db_path or STATE_DB,
        artifact_dir=artifact_dir,
    )
    return service.list_audits(limit=limit, posture=posture)


@router.get("/model/meta-lightgbm/shadow-report")
def build_meta_model_lightgbm_shadow_report(
    _user: RequireUser,
    limit: int = Query(default=200, ge=1, le=2000),
    posture: str | None = Query(default=None),
    include_samples: bool = Query(default=True),
    db_path: str | None = Query(default=None),
    artifact_dir: str | None = Query(default=None),
) -> dict:
    service = MetaModelLightGBMService(
        db_path=db_path or STATE_DB,
        artifact_dir=artifact_dir,
    )
    return service.build_shadow_report(
        limit=limit,
        posture=posture,
        include_samples=bool(include_samples),
    )


@router.post("/model/meta-lightgbm/shadow-report/snapshot")
def snapshot_meta_model_lightgbm_shadow_report(
    _user: RequireUser,
    req: MetaModelLightGBMSnapshotRequest,
) -> dict:
    report = MetaModelLightGBMService(db_path=req.db_path or STATE_DB).build_shadow_report(
        limit=max(1, int(req.limit)),
        include_samples=bool(req.include_samples),
    )
    return MetaGovernanceService(req.db_path or STATE_DB).create_shadow_report_snapshot(
        report=report,
        limit=max(1, int(req.limit)),
        include_samples=bool(req.include_samples),
        source=req.source or "manual",
    )


@router.get("/model/meta-lightgbm/shadow-report/snapshots")
def list_meta_model_lightgbm_shadow_report_snapshots(
    _user: RequireUser,
    limit: int = Query(default=20, ge=1, le=200),
    db_path: str | None = Query(default=None),
) -> dict:
    return MetaGovernanceService(db_path or STATE_DB).list_shadow_report_snapshots(limit=limit)


@router.post("/model/meta-lightgbm/governance-suggestion")
def materialize_meta_model_lightgbm_governance_suggestion(
    _user: RequireUser,
    req: MetaModelLightGBMGovernanceRequest,
) -> dict:
    report = MetaModelLightGBMService(db_path=req.db_path or STATE_DB).build_shadow_report(
        limit=max(1, int(req.limit)),
        include_samples=False,
    )
    result = MetaGovernanceService(req.db_path or STATE_DB).materialize_meta_governance_suggestion(
        report=report,
        limit=max(1, int(req.limit)),
        snapshot=bool(req.snapshot),
        source=req.source or "manual",
    )
    _learning_cache_invalidate("suggestions:", "summary")
    return result


@router.get("/model/offmarket-high-load/audits")
def list_offmarket_high_load_audits(
    _user: RequireUser,
    limit: int = Query(default=50, ge=1, le=500),
    job_name: str | None = Query(default=None),
) -> dict:
    conn = connect_sqlite(STATE_DB)
    try:
        exists = conn.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='offmarket_high_load_job_audit'
            """
        ).fetchone()
        if not exists:
            return {"items": [], "count": 0}
        clauses = []
        params: list[Any] = []
        if job_name:
            clauses.append("job_name=?")
            params.append(str(job_name))
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        cur = conn.execute(
            f"""
            SELECT *
            FROM offmarket_high_load_job_audit
            {where}
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (*params, int(limit)),
        )
        columns = [str(item[0]) for item in cur.description]
        rows = [dict(zip(columns, row)) for row in cur.fetchall()]
        items = []
        for row in rows:
            payload_raw = row.get("payload_json") or "{}"
            result_raw = row.get("result_json") or "{}"
            items.append(
                {
                    "audit_id": str(row.get("audit_id") or ""),
                    "job_name": str(row.get("job_name") or ""),
                    "status": str(row.get("status") or ""),
                    "session_status": str(row.get("session_status") or ""),
                    "high_load_profile": str(row.get("high_load_profile") or ""),
                    "payload": json.loads(payload_raw or "{}"),
                    "result": json.loads(result_raw or "{}"),
                    "error": str(row.get("error") or ""),
                    "started_at": float(row.get("started_at") or 0.0),
                    "finished_at": float(row.get("finished_at") or 0.0),
                }
            )
        return {"items": items, "count": len(items)}
    finally:
        conn.close()


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

