"""Risk API endpoints: summary, VaR, Kelly, stress test, concentration."""
import json
import re
import sqlite3

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Any

from backend.core.auth import RequireUser
from backend.core.db import get_state_conn
from backend.risk import VaRCalculator, KellyCriterion, StressTest, ConcentrationChecker
from backend.services.parameter_templates import ParameterTemplateService
from backend.services.review_contract import normalize_trade_review_contract

router = APIRouter(prefix="/api/risk", tags=["risk"])

# Module-level singletons
_var_calc = VaRCalculator(confidence=0.95)
_kelly = KellyCriterion()
_stress = StressTest()
_conc = ConcentrationChecker(max_single_weight=0.40, max_sector_weight=0.60)
_CANDIDATE_ID_RE = re.compile(r"(ptrc_[0-9a-f]{16})")


def _loads_json(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _get_system_health_report():
    try:
        from monitor.system_health import shared as _system_health_shared

        return _system_health_shared().get_last_report()
    except Exception:
        return None


def _runtime_risk_policy() -> dict[str, bool]:
    try:
        from config.runtime_config import shared as _runtime_cfg

        cfg = _runtime_cfg()
        return {
            "require_l2_depth": bool(getattr(cfg, "risk_require_l2_depth", False)),
            "block_on_disk_critical": bool(getattr(cfg, "risk_block_on_disk_critical", True)),
        }
    except Exception:
        return {
            "require_l2_depth": False,
            "block_on_disk_critical": True,
        }


def _recent_policy_verdicts(limit: int = 50) -> dict[str, Any]:
    limit = max(1, min(int(limit or 50), 200))
    conn = get_state_conn()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT decision_id, event_type, symbol, timeframe, decision_ts,
                   action_reason, action_json, risk_state_json
            FROM decision_ledger
            WHERE risk_state_json LIKE '%policy_verdict%'
               OR action_json LIKE '%risk_verdict%'
            ORDER BY decision_ts DESC, created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()

    items: list[dict[str, Any]] = []
    counts: dict[str, int] = {"allowed": 0, "blocked": 0}
    by_reason: dict[str, int] = {}
    by_action: dict[str, int] = {}

    for row in rows:
        risk_state = _loads_json(row["risk_state_json"], {})
        action_json = _loads_json(row["action_json"], {})
        if not isinstance(risk_state, dict):
            risk_state = {}
        if not isinstance(action_json, dict):
            action_json = {}
        verdict = risk_state.get("policy_verdict") or action_json.get("risk_verdict") or {}
        allowed = bool(verdict.get("allowed", False))
        reason = str(verdict.get("reason") or row["action_reason"] or "unknown")
        action = str((verdict.get("audit_payload") or {}).get("action") or action_json.get("skip_stage") or row["event_type"])
        counts["allowed" if allowed else "blocked"] += 1
        by_reason[reason] = by_reason.get(reason, 0) + 1
        by_action[action] = by_action.get(action, 0) + 1
        items.append({
            "decision_id": row["decision_id"],
            "event_type": row["event_type"],
            "symbol": row["symbol"],
            "timeframe": row["timeframe"],
            "decision_ts": row["decision_ts"],
            "allowed": allowed,
            "reason": reason,
            "action": action,
            "risk_verdict": verdict,
        })

    return {
        "limit": limit,
        "total": len(items),
        "counts": counts,
        "by_reason": by_reason,
        "by_action": by_action,
        "items": items,
    }


def _parse_review_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["failure_tags"] = _loads_json(item.pop("failure_tags_json", None), [])
    review = _loads_json(item.pop("review_json", None), {})
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


def _safe_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except Exception:
        return None


def _db_path_from_conn(conn: sqlite3.Connection) -> str | None:
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
    except Exception:
        return None


def _latest_symbol_context(conn: sqlite3.Connection, *, position_id: str, trade_id: str) -> dict[str, str]:
    if not position_id and not trade_id:
        return {"symbol": "", "timeframe": ""}
    try:
        row = conn.execute(
            """
            SELECT symbol, timeframe
            FROM decision_ledger
            WHERE position_id = ? OR trade_id = ?
            ORDER BY decision_ts DESC, created_at DESC
            LIMIT 1
            """,
            (position_id, trade_id),
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    if not row:
        return {"symbol": "", "timeframe": ""}
    return {
        "symbol": str(row["symbol"] or ""),
        "timeframe": str(row["timeframe"] or ""),
    }


def _top_factor_hint_for_review(conn: sqlite3.Connection, review_id: str) -> dict[str, Any]:
    if not review_id:
        return {}
    try:
        rows = conn.execute(
            """
            SELECT factor, net_contribution, notes
            FROM factor_contribution_review
            WHERE review_id = ?
            ORDER BY ABS(net_contribution) DESC, id ASC
            LIMIT 5
            """,
            (review_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    items: list[dict[str, Any]] = []
    for row in rows:
        payload = _loads_json(str(row["notes"] or ""), {})
        items.append(
            {
                "factor": str(row["factor"] or ""),
                "net_contribution": float(row["net_contribution"] or 0.0),
                "primary_responsibility": str(payload.get("primary_responsibility") or ""),
                "responsibility_labels": list(payload.get("responsibility_labels") or []),
            }
        )
    if not items:
        return {}
    parameter_items = [
        item for item in items
        if item["primary_responsibility"] == "parameter"
        or "factor_logic_ok_but_param_suspect" in item["responsibility_labels"]
    ]
    return parameter_items[0] if parameter_items else items[0]
    if row is None:
        return None
    try:
        return str(row[2] or "")
    except Exception:
        return None


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


def _humanize_approval_path(value: str) -> str:
    key = str(value or "").lower()
    if key == "offline_validation_then_gray_release":
        return "先离线验证再灰度发布"
    if key == "governed_apply_switch":
        return "经治理审批后受控切换"
    if key == "governor_review_then_live_switch":
        return "经 governor 审批后在线切换"
    return "按治理链继续推进"


def _parameter_governance_stage_snapshot(
    *,
    candidate: dict[str, Any] | None = None,
    recommendation: dict[str, Any] | None = None,
) -> dict[str, str]:
    candidate = candidate or {}
    recommendation = recommendation or {}
    candidate_id = str(candidate.get("candidate_id") or "")
    candidate_status = str(candidate.get("status") or "").lower()
    if candidate_id:
        if candidate_status == "pending_review":
            return {
                "stage_label": "待审候选",
                "next_step_label": "进入候选审核",
                "next_step_summary": "下一步先人工审核离线证据；只有审核通过后，才允许继续灰度发布到运行态。",
                "entry_type": "candidate",
            }
        if candidate_status == "approved":
            return {
                "stage_label": "等待发布",
                "next_step_label": "执行灰度发布",
                "next_step_summary": "下一步把候选模板切到运行态，并继续观察后验 reward、胜率和是否需要回滚。",
                "entry_type": "candidate",
            }
        if candidate_status == "deployed":
            return {
                "stage_label": "发布观察",
                "next_step_label": "观察发布效果",
                "next_step_summary": "下一步持续盯后验表现，确认是否要强化当前模板，或者因为效果恶化而回滚。",
                "entry_type": "candidate",
            }
        if candidate_status == "rolled_back":
            return {
                "stage_label": "已回滚",
                "next_step_label": "回到离线复核",
                "next_step_summary": "下一步复核这次回滚的原因，再决定是否要重新离线验证或改用别的模板。",
                "entry_type": "candidate",
            }
        if candidate_status == "rejected":
            return {
                "stage_label": "已拒绝",
                "next_step_label": "保留证据继续观察",
                "next_step_summary": "下一步保留这次离线证据，等待更多样本后再决定是否重新发起模板候选。",
                "entry_type": "candidate",
            }
    recommendation_id = str(recommendation.get("recommendation_id") or "")
    if recommendation_id:
        scope = str(((recommendation.get("boundary") or {}).get("recommended_scope") or "")).lower()
        if scope == "offline_deep":
            return {
                "stage_label": "离线深调",
                "next_step_label": "创建离线验证",
                "next_step_summary": "下一步先发起离线验证；验证通过后再登记灰度候选，并进入人工审核与发布链。",
                "entry_type": "recommendation",
            }
        return {
            "stage_label": "在线轻调",
            "next_step_label": "生成治理建议",
            "next_step_summary": "下一步把推荐转成正式治理建议，走 governor 审批后即可受控切换运行态模板。",
            "entry_type": "recommendation",
        }
    return {
        "stage_label": "",
        "next_step_label": "",
        "next_step_summary": "",
        "entry_type": "",
    }


def _parameter_governance_target_type(entry_type: str) -> str:
    key = str(entry_type or "").lower()
    if key == "candidate":
        return "模板候选"
    if key == "recommendation":
        return "参数推荐"
    if key == "suggestion":
        return "治理建议"
    if key == "parameter_lifecycle":
        return "治理轨迹"
    return ""


def _parameter_governance_action_label(entry_type: str, stage_label: str) -> str:
    normalized_type = str(entry_type or "").lower()
    normalized_stage = str(stage_label or "")
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


def _parameter_governance_priority_snapshot(
    *,
    entry_type: str = "",
    stage_label: str = "",
    has_governance_factor: bool = False,
) -> dict[str, Any]:
    normalized_type = str(entry_type or "").lower()
    normalized_stage = str(stage_label or "")
    if normalized_type == "candidate" and normalized_stage == "待审候选":
        return {
            "score": 100,
            "label": "优先审核",
            "summary": "这条样本已经形成候选，优先把人工审核处理掉。",
        }
    if normalized_type == "candidate" and normalized_stage == "等待发布":
        return {
            "score": 90,
            "label": "优先发布",
            "summary": "这条样本对应的候选已经批准，下一步应推进灰度发布。",
        }
    if normalized_type == "recommendation" and normalized_stage == "离线深调":
        return {
            "score": 80,
            "label": "优先验证",
            "summary": "这条样本已收敛到离线深调入口，当前应尽快做离线验证。",
        }
    if normalized_type == "recommendation" and normalized_stage == "在线轻调":
        return {
            "score": 70,
            "label": "优先审建议",
            "summary": "这条样本已满足在线轻调边界，可继续生成或审批治理建议。",
        }
    if normalized_type == "candidate" and normalized_stage == "发布观察":
        return {
            "score": 60,
            "label": "优先观察",
            "summary": "这条样本对应模板已经上线，当前重点是观察效果与回滚信号。",
        }
    if normalized_type == "candidate" and normalized_stage == "已回滚":
        return {
            "score": 50,
            "label": "优先复核",
            "summary": "这条样本对应治理链已经回滚，当前应回到离线复核。",
        }
    if has_governance_factor:
        return {
            "score": 40,
            "label": "继续收敛",
            "summary": "这条样本已经露出参数问题线索，但还没有形成更具体的治理对象。",
        }
    return {
        "score": 0,
        "label": "",
        "summary": "",
    }


def _parameter_governance_jump_snapshot(
    *,
    latest_candidate: dict[str, Any] | None = None,
    recommendation: dict[str, Any] | None = None,
    suggestion: dict[str, Any] | None = None,
    lifecycle_event: dict[str, Any] | None = None,
    stage_label: str = "",
    target_type: str = "",
    action_label: str = "",
) -> dict[str, Any]:
    candidate = latest_candidate or {}
    recommendation = recommendation or {}
    suggestion = suggestion or {}
    lifecycle_event = lifecycle_event or {}
    candidate_id = str(candidate.get("candidate_id") or "")
    recommendation_id = str(((candidate.get("trace") or {}).get("recommendation_id") or "")) or str(recommendation.get("recommendation_id") or "")
    suggestion_id = str(suggestion.get("suggestion_id") or "")
    lifecycle_event_id = str(lifecycle_event.get("id") or "")
    candidate_status = str(candidate.get("status") or "").lower()
    suggestion_status = str(suggestion.get("status") or "").lower()
    recommendation_scope = str(((recommendation.get("boundary") or {}).get("recommended_scope") or "")).lower()

    if candidate_id:
        return {
            "type": "offline_candidate",
            "type_label": target_type or "模板候选",
            "button_text": action_label or _parameter_governance_action_label("candidate", stage_label),
            "summary": (
                f"当前最该处理的是候选 {candidate_id} 的人工审核。"
                if candidate_status == "pending_review"
                else f"当前最该处理的是候选 {candidate_id} 的灰度发布。"
                if candidate_status == "approved"
                else f"当前治理链已落到候选 {candidate_id}，应继续围绕候选状态推进。"
            ),
            "candidate_id": candidate_id,
            "recommendation_id": recommendation_id,
            "suggestion_id": suggestion_id,
            "lifecycle_event_id": lifecycle_event_id,
        }
    if suggestion_id:
        return {
            "type": "suggestion",
            "type_label": "治理建议",
            "button_text": (
                "去审批治理建议"
                if suggestion_status == "proposed"
                else "看已批建议"
                if suggestion_status == "approved"
                else "看回滚建议"
                if suggestion_status == "rolled_back"
                else "看治理建议"
            ),
            "summary": (
                f"推荐 {recommendation_id or '--'} 已生成建议 {suggestion_id}，当前正等待审批。"
                if suggestion_status == "proposed"
                else f"推荐 {recommendation_id or '--'} 已生成建议 {suggestion_id}，可继续查看其后续状态。"
            ),
            "candidate_id": candidate_id,
            "recommendation_id": recommendation_id,
            "suggestion_id": suggestion_id,
            "lifecycle_event_id": lifecycle_event_id,
        }
    if lifecycle_event_id:
        return {
            "type": "parameter_lifecycle",
            "type_label": "治理轨迹",
            "button_text": "看治理轨迹",
            "summary": f"推荐 {recommendation_id or '--'} 已进入治理轨迹 {lifecycle_event_id}，可回看链路推进情况。",
            "candidate_id": candidate_id,
            "recommendation_id": recommendation_id,
            "suggestion_id": suggestion_id,
            "lifecycle_event_id": lifecycle_event_id,
        }
    if recommendation_id:
        return {
            "type": "template_recommendation",
            "type_label": target_type or "参数推荐",
            "button_text": action_label or ("看离线推荐" if recommendation_scope == "offline_deep" else "看在线推荐"),
            "summary": (
                f"当前还停在推荐 {recommendation_id}，下一步应先发起离线验证。"
                if recommendation_scope == "offline_deep"
                else f"当前还停在推荐 {recommendation_id}，下一步应先生成正式治理建议。"
            ),
            "candidate_id": candidate_id,
            "recommendation_id": recommendation_id,
            "suggestion_id": suggestion_id,
            "lifecycle_event_id": lifecycle_event_id,
        }
    return {
        "type": "",
        "type_label": "",
        "button_text": "",
        "summary": "",
        "candidate_id": "",
        "recommendation_id": "",
        "suggestion_id": "",
        "lifecycle_event_id": "",
    }


def _parameter_governance_todo_queue_snapshot(
    *,
    latest_candidate: dict[str, Any] | None = None,
    recommendation: dict[str, Any] | None = None,
    suggestion: dict[str, Any] | None = None,
    lifecycle_event: dict[str, Any] | None = None,
    stage_label: str = "",
    next_step_summary: str = "",
    priority_label: str = "",
    jump: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    candidate = latest_candidate or {}
    recommendation = recommendation or {}
    suggestion = suggestion or {}
    lifecycle_event = lifecycle_event or {}
    primary_jump = jump or {}
    tasks: list[dict[str, Any]] = []

    def push_task(task: dict[str, Any] | None) -> None:
      if not task or not str(task.get("type") or ""):
          return
      target_id = str(task.get("target_id") or "")
      for existing in tasks:
          if str(existing.get("type") or "") == str(task.get("type") or "") and str(existing.get("target_id") or "") == target_id:
              return
      tasks.append(task)

    candidate_id = str(candidate.get("candidate_id") or "")
    candidate_status = str(candidate.get("status") or "").lower()
    recommendation_id = str(((candidate.get("trace") or {}).get("recommendation_id") or "")) or str(recommendation.get("recommendation_id") or "")
    recommendation_scope = str(((recommendation.get("boundary") or {}).get("recommended_scope") or "")).lower()
    suggestion_id = str(suggestion.get("suggestion_id") or "")
    suggestion_status = str(suggestion.get("status") or "").lower()
    lifecycle_event_id = str(lifecycle_event.get("id") or "")

    if candidate_id:
        priority = _parameter_governance_priority_snapshot(entry_type="candidate", stage_label=stage_label)
        push_task({
            "type": "offline_candidate",
            "type_label": "模板候选",
            "target_id": candidate_id,
            "title": f"{candidate_id} · {stage_label or _humanize_template_candidate_status(candidate.get('status') or '')}",
            "reason": (
                "离线证据已经收敛，当前卡点就是人工审核。"
                if candidate_status == "pending_review"
                else "候选已经通过审核，当前最关键的是推进灰度发布。"
                if candidate_status == "approved"
                else "模板已经进入运行态，当前重点转成观察效果与回滚信号。"
                if candidate_status == "deployed"
                else "候选已经回滚，当前应先回到离线复核，而不是继续上线。"
                if candidate_status == "rolled_back"
                else "当前治理链已经落到候选层，继续围绕候选状态推进。"
            ),
            "button_text": _parameter_governance_action_label("candidate", stage_label),
            "priority_label": str(priority["label"]),
            "summary": (
                f"先处理候选 {candidate_id} 的审核，避免治理链停在“已验证但未批准”。"
                if candidate_status == "pending_review"
                else f"先处理候选 {candidate_id} 的发布，把治理动作真正切到运行态。"
                if candidate_status == "approved"
                else f"候选 {candidate_id} 已经是当前最具体的治理对象。"
            ),
            "candidate_id": candidate_id,
            "recommendation_id": recommendation_id,
            "priority_score": int(priority["score"]),
        })

    if suggestion_id:
        suggestion_score = 75 if suggestion_status == "proposed" else 55 if suggestion_status == "approved" else 35
        push_task({
            "type": "suggestion",
            "type_label": "治理建议",
            "target_id": suggestion_id,
            "title": f"{suggestion_id or '--'} · {'待审批' if suggestion_status == 'proposed' else '已批准' if suggestion_status == 'approved' else '已回滚' if suggestion_status == 'rolled_back' else '建议处理中'}",
            "reason": "推荐已经生成正式治理建议，当前卡点是 governor 审批。" if suggestion_status == "proposed" else "推荐已经沉淀为 suggestion，可继续沿建议状态回看治理链。",
            "button_text": "去审建议" if suggestion_status == "proposed" else "看已批建议" if suggestion_status == "approved" else "看治理建议",
            "summary": f"推荐 {recommendation_id} 已经物化成 suggestion {suggestion_id or '--'}。" if recommendation_id else f"当前建议对象为 {suggestion_id or '--'}。",
            "suggestion_id": suggestion_id,
            "recommendation_id": recommendation_id,
            "priority_score": suggestion_score,
        })

    if recommendation_id:
        recommendation_stage = "离线深调" if recommendation_scope == "offline_deep" else "在线轻调"
        priority = _parameter_governance_priority_snapshot(entry_type="recommendation", stage_label=recommendation_stage)
        push_task({
            "type": "template_recommendation",
            "type_label": "参数推荐",
            "target_id": recommendation_id,
            "title": f"{recommendation_id} · {recommendation_stage}",
            "reason": "这条推荐当前只能先走离线验证，不能直接切线上。" if recommendation_scope == "offline_deep" else "这条推荐已经满足在线轻调边界，可以继续进入 suggestion 审批链。",
            "button_text": _parameter_governance_action_label("recommendation", recommendation_stage),
            "priority_label": str(priority["label"]),
            "summary": f"推荐 {recommendation_id} 还停在离线验证入口。" if recommendation_scope == "offline_deep" else f"推荐 {recommendation_id} 已可继续生成治理建议。",
            "recommendation_id": recommendation_id,
            "priority_score": int(priority["score"]),
        })

    if lifecycle_event_id:
        push_task({
            "type": "parameter_lifecycle",
            "type_label": "治理轨迹",
            "target_id": lifecycle_event_id,
            "title": f"{lifecycle_event_id or '--'} · 生命周期",
            "reason": "这条轨迹适合回看 recommendation -> candidate -> release 的完整推进链。",
            "button_text": "看治理轨迹",
            "summary": "需要核对历史推进脉络时，优先回到 lifecycle 事件。",
            "lifecycle_event_id": lifecycle_event_id,
            "priority_score": 10,
        })

    if not tasks:
        return None
    tasks.sort(key=lambda item: (-int(item.get("priority_score") or 0), str(item.get("target_id") or "")))
    primary_type = str(primary_jump.get("type") or "")
    primary_target_id = (
        str(primary_jump.get("candidate_id") or "")
        or str(primary_jump.get("suggestion_id") or "")
        or str(primary_jump.get("recommendation_id") or "")
        or str(primary_jump.get("lifecycle_event_id") or "")
    )
    primary_task = None
    if primary_type:
        for task in tasks:
            if str(task.get("type") or "") == primary_type and (
                not primary_target_id or str(task.get("target_id") or "") == primary_target_id
            ):
                primary_task = task
                break
    if not primary_task:
        primary_task = tasks[0]
    secondary_tasks = [
        task for task in tasks
        if not (
            str(task.get("type") or "") == str(primary_task.get("type") or "")
            and str(task.get("target_id") or "") == str(primary_task.get("target_id") or "")
        )
    ]
    return {
        "primary_task": primary_task,
        "secondary_tasks": secondary_tasks,
        "queue_summary": f"当前主推进动作：{next_step_summary}" if next_step_summary else "当前已识别出可继续推进的参数治理对象。",
        "queue_hint": f"除主任务外，当前还可回看 {len(secondary_tasks)} 个关联治理对象。" if secondary_tasks else "当前没有更多并行治理对象，先把主任务处理完。",
        "priority_label": priority_label,
    }


def _parameter_governance_timeline_context_snapshot(
    *,
    factor_id: str = "",
    stage_label: str = "",
    stage_summary: str = "",
    next_step_summary: str = "",
    jump: dict[str, Any] | None = None,
    todo_queue: dict[str, Any] | None = None,
) -> dict[str, Any]:
    jump = jump or {}
    todo_queue = todo_queue or {}
    jump_type = str(jump.get("type") or "")
    jump_type_label = str(jump.get("type_label") or "")
    actions: list[dict[str, Any]] = []

    def push_action(action: dict[str, Any] | None) -> None:
        if not action or not str(action.get("type") or ""):
            return
        normalized = {
            "type": str(action.get("type") or ""),
            "type_label": str(action.get("type_label") or ""),
            "button_text": str(action.get("button_text") or ""),
            "summary": str(action.get("summary") or ""),
            "factor_id": str(action.get("factor_id") or factor_id or ""),
            "source": str(action.get("source") or "trade_trace_timeline"),
            "candidate_id": str(action.get("candidate_id") or ""),
            "recommendation_id": str(action.get("recommendation_id") or ""),
            "suggestion_id": str(action.get("suggestion_id") or ""),
            "lifecycle_event_id": str(action.get("lifecycle_event_id") or ""),
        }
        target_id = (
            normalized["candidate_id"]
            or normalized["suggestion_id"]
            or normalized["recommendation_id"]
            or normalized["lifecycle_event_id"]
        )
        for existing in actions:
            existing_target = (
                existing["candidate_id"]
                or existing["suggestion_id"]
                or existing["recommendation_id"]
                or existing["lifecycle_event_id"]
            )
            if existing["type"] == normalized["type"] and existing_target == target_id:
                return
        actions.append(normalized)

    push_action(jump)
    primary_task = dict(todo_queue.get("primary_task") or {})
    if primary_task:
        push_action(primary_task)
    for task in todo_queue.get("secondary_tasks") or []:
        push_action(dict(task or {}))
    return {
        "stage_tag": str(stage_label or ""),
        "stage_summary": str(stage_summary or next_step_summary or ""),
        "review_stage_tag": str(stage_label or "参数问题已入治理"),
        "review_stage_summary": str(stage_summary or next_step_summary or "这条复盘已经能直接接到后续参数治理链。"),
        "governance_jump_type": jump_type,
        "governance_jump_type_label": jump_type_label,
        "governance_jump_button_text": str(jump.get("button_text") or ""),
        "governance_jump_summary": str(jump.get("summary") or ""),
        "review_jump_button_text": (
            "按复盘去审建议"
            if jump_type == "suggestion"
            else "按复盘看候选"
            if jump_type == "offline_candidate"
            else "按复盘继续治理"
            if jump_type
            else ""
        ),
        "review_jump_summary": (
            f"这条复盘已经把问题收敛到参数治理，当前建议直接转去{jump_type_label or '治理对象'}继续处理。"
            if jump_type
            else ""
        ),
        "governance_actions": actions,
        "candidate_id": str(jump.get("candidate_id") or ""),
        "recommendation_id": str(jump.get("recommendation_id") or ""),
        "suggestion_id": str(jump.get("suggestion_id") or ""),
        "lifecycle_event_id": str(jump.get("lifecycle_event_id") or ""),
    }


def _parameter_governance_timeline_filter_context_snapshot() -> dict[str, Any]:
    return {
        "focus_filters": {
            "all": {
                "label": "全部",
                "summary_template": "当前证据链共 {count} 个事件。",
                "empty_summary": "当前还没有可展示的时间线事件。",
            },
            "governance": {
                "label": "治理相关",
                "summary_template": "优先关注 {count} 个治理/复盘事件，先判断是否要走 recommendation、suggestion 或 candidate 链路。",
                "empty_summary": "当前还没有治理或复盘事件，先看执行与决策证据。",
            },
            "decision": {
                "label": "决策监督",
                "summary_template": "这里收敛了 {count} 个开仓、监督或风控裁决事件。",
                "empty_summary": "当前没有额外的决策或监督事件。",
            },
            "execution": {
                "label": "执行落地",
                "summary_template": "这里收敛了 {count} 个仓位、订单或恢复事件。",
                "empty_summary": "当前没有额外的执行落地事件。",
            },
        },
        "governance_stage_filters": {
            "all": {
                "label": "全部治理态",
                "summary_template": "当前治理相关时间线共 {count} 个事件。",
                "empty_summary": "当前还没有治理相关时间线事件。",
            },
            "online_light": {
                "label": "在线轻调",
                "summary": "当前可以继续生成建议并走受控审批切换。",
            },
            "offline_deep": {
                "label": "离线深调",
                "summary": "当前不能直接上线，必须先走离线验证。",
            },
            "pending_review": {
                "label": "待审候选",
                "summary": "离线证据已经形成，当前重点是人工审核。",
            },
            "approved": {
                "label": "等待发布",
                "summary": "候选已通过审核，下一步应推进灰度发布。",
            },
            "deployed": {
                "label": "发布观察",
                "summary": "模板已进运行态，当前重点是观察效果与回滚信号。",
            },
            "rolled_back": {
                "label": "已回滚",
                "summary": "这条参数治理链已经回滚，当前应回到离线复核。",
            },
        },
    }


def _parameter_governance_entry_context_snapshot(
    *,
    stage_label: str = "",
    stage_summary: str = "",
    next_step_label: str = "",
    next_step_summary: str = "",
    entry_type: str = "",
    target_type: str = "",
    action_label: str = "",
) -> dict[str, Any]:
    return {
        "entry_type": str(entry_type or ""),
        "entry_label": str(target_type or ""),
        "action_label": str(action_label or ""),
        "stage_label": str(stage_label or ""),
        "stage_summary": str(stage_summary or ""),
        "next_step_label": str(next_step_label or ""),
        "next_step_summary": str(next_step_summary or ""),
    }


def _parameter_governance_quick_actions_snapshot(
    *,
    factor_id: str = "",
    latest_candidate: dict[str, Any] | None = None,
    recommendation: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    candidate = latest_candidate or {}
    recommendation = recommendation or {}
    actions: list[dict[str, Any]] = []
    recommendation_id = str(((candidate.get("trace") or {}).get("recommendation_id") or "")) or str(recommendation.get("recommendation_id") or "")
    candidate_id = str(candidate.get("candidate_id") or "")
    if recommendation_id:
        actions.append({
            "type": "template_recommendation",
            "label": "查看治理建议",
            "button_tone": "primary",
            "summary": f"回到推荐 {recommendation_id} 查看原始治理建议与边界结论。",
            "factor_id": str(factor_id or ""),
            "recommendation_id": recommendation_id,
            "candidate_id": candidate_id,
        })
    if candidate_id:
        actions.append({
            "type": "offline_candidate",
            "label": "查看模板候选",
            "button_tone": "secondary",
            "summary": f"回到候选 {candidate_id} 查看离线验证、审核与发布状态。",
            "factor_id": str(factor_id or ""),
            "recommendation_id": recommendation_id,
            "candidate_id": candidate_id,
        })
    return actions


def _parameter_governance_overview_snapshot(
    *,
    ops_summary: str = "",
    stage_label: str = "",
    stage_summary: str = "",
    next_step_label: str = "",
    next_step_summary: str = "",
    entry_type: str = "",
    target_type: str = "",
    action_label: str = "",
    priority_label: str = "",
    priority_summary: str = "",
    latest_candidate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    candidate = latest_candidate or {}
    candidate_trace = dict(candidate.get("trace") or {})
    latest_candidate_id = str(candidate.get("candidate_id") or "")
    latest_candidate_status_text = _humanize_template_candidate_status(candidate.get("status") or "") if candidate else ""
    latest_candidate_trace_text = (
        f"来源推荐 {candidate_trace.get('recommendation_id')} · "
        f"{_humanize_template_responsibility(((candidate_trace.get('responsibility') or {}).get('primary_responsibility') or ''))}"
        if candidate_trace.get("recommendation_id")
        else ""
    )
    overview_ops_summary = str(ops_summary or "")
    overview_stage_label = str(stage_label or ("参数问题待收敛" if overview_ops_summary else ""))
    overview_stage_summary = str(
        stage_summary
        or ("这笔交易已经暴露出参数问题线索，但还没有形成可执行的模板推荐或候选。" if overview_ops_summary else "")
    )
    overview_next_step_label = str(next_step_label or ("继续收敛证据" if overview_ops_summary else ""))
    overview_next_step_summary = str(
        next_step_summary
        or ("下一步继续积累参数可疑证据，等待推荐或离线候选正式出现。" if overview_ops_summary else "")
    )
    return {
        "ops_summary": overview_ops_summary,
        "stage_label": overview_stage_label,
        "stage_summary": overview_stage_summary,
        "next_step_label": overview_next_step_label,
        "next_step_summary": overview_next_step_summary,
        "entry_type": str(entry_type or ""),
        "entry_label": str(target_type or ""),
        "entry_hint_text": f"建议入口：{target_type}" if target_type else "",
        "target_type": str(target_type or ""),
        "action_label": str(action_label or ""),
        "priority_label": str(priority_label or ""),
        "priority_summary": str(priority_summary or ""),
        "latest_candidate_id": latest_candidate_id,
        "latest_candidate_status_text": latest_candidate_status_text,
        "latest_candidate_trace_text": latest_candidate_trace_text,
        "latest_candidate_summary_text": (
            f"最新模板候选 {latest_candidate_id} · {latest_candidate_status_text}"
            if latest_candidate_id and latest_candidate_status_text
            else ""
        ),
        "show_stage_card": bool(
            overview_stage_label
            or overview_stage_summary
            or overview_next_step_summary
            or target_type
            or action_label
            or priority_label
        ),
    }


def _latest_template_candidate_for_factor(conn: sqlite3.Connection, factor_id: str) -> dict[str, Any]:
    if not factor_id:
        return {}
    try:
        row = conn.execute(
            """
            SELECT candidate_id, factor_id, template_id, regime_key, status,
                   validation_summary_json, validation_report_path, created_at, updated_at
            FROM parameter_template_release_candidate
            WHERE factor_id=?
            ORDER BY updated_at DESC, created_at DESC
            LIMIT 1
            """,
            (factor_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return {}
    if not row:
        return {}
    validation_summary = _loads_json(row["validation_summary_json"], {})
    recommendation_source = dict(validation_summary.get("recommendation_source") or {})
    trace = {
        "source": str(recommendation_source.get("source") or ""),
        "recommendation_id": str(recommendation_source.get("recommendation_id") or ""),
        "reason": str(recommendation_source.get("reason") or ""),
        "responsibility": dict(recommendation_source.get("responsibility") or {}),
        "approval_path": str(recommendation_source.get("approval_path") or ""),
    } if recommendation_source else {}
    return {
        "candidate_id": str(row["candidate_id"] or ""),
        "factor_id": str(row["factor_id"] or ""),
        "template_id": str(row["template_id"] or ""),
        "regime_key": str(row["regime_key"] or ""),
        "status": str(row["status"] or ""),
        "validation_summary": validation_summary,
        "validation_report_path": str(row["validation_report_path"] or ""),
        "created_at": float(row["created_at"] or 0.0),
        "updated_at": float(row["updated_at"] or 0.0),
        "trace": trace,
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


def _build_trade_trace_parameter_governance(
    conn: sqlite3.Connection,
    *,
    factor_contributions: list[dict[str, Any]],
) -> dict[str, Any]:
    suspected = [
        item for item in factor_contributions
        if str(item.get("primary_responsibility") or "") == "parameter"
        or "factor_logic_ok_but_param_suspect" in (item.get("responsibility_labels") or [])
    ]
    if not suspected:
        return {
            "timeline_filter_context": _parameter_governance_timeline_filter_context_snapshot(),
            "overview": {
                "ops_summary": "当前这笔交易还没有进入参数治理链。",
                "stage_label": "未进入治理链",
                "stage_summary": "",
                "next_step_label": "",
                "next_step_summary": "",
                "entry_type": "",
                "entry_label": "",
                "entry_hint_text": "",
                "target_type": "",
                "action_label": "",
                "priority_label": "",
                "priority_summary": "",
                "latest_candidate_id": "",
                "latest_candidate_status_text": "",
                "latest_candidate_trace_text": "",
                "latest_candidate_summary_text": "",
                "show_stage_card": False,
            },
        }
    suspected.sort(key=lambda item: abs(float(item.get("net_contribution") or 0.0)), reverse=True)
    anchor = suspected[0]
    factor_id = str(anchor.get("factor") or "")
    recommendation = None
    db_path = _db_path_from_conn(conn)
    try:
        recommendation = ParameterTemplateService(db_path).list_recommendations(
            factor_id=factor_id,
            limit=1,
        )[0]
    except Exception:
        recommendation = None
    latest_candidate = _latest_template_candidate_for_factor(conn, factor_id)
    latest_trace = dict(latest_candidate.get("trace") or {})
    suggestion = _latest_template_suggestion_for_recommendation(
        conn,
        str(
            (latest_trace.get("recommendation_id") or "")
            or ((recommendation or {}).get("recommendation_id") or "")
        ),
    )
    lifecycle_event = _latest_parameter_template_lifecycle_for_recommendation(
        conn,
        factor_id=factor_id,
        recommendation_id=str(
            (latest_trace.get("recommendation_id") or "")
            or ((recommendation or {}).get("recommendation_id") or "")
        ),
        candidate_id=str(latest_candidate.get("candidate_id") or ""),
    )
    governance_stage = _parameter_governance_stage_snapshot(
        candidate=latest_candidate,
        recommendation=recommendation,
    )
    priority = _parameter_governance_priority_snapshot(
        entry_type=str(governance_stage.get("entry_type") or ""),
        stage_label=str(governance_stage.get("stage_label") or ""),
        has_governance_factor=bool(factor_id),
    )
    target_type = _parameter_governance_target_type(str(governance_stage.get("entry_type") or ""))
    action_label = _parameter_governance_action_label(
        str(governance_stage.get("entry_type") or ""),
        str(governance_stage.get("stage_label") or ""),
    )
    governance_jump = _parameter_governance_jump_snapshot(
        latest_candidate=latest_candidate,
        recommendation=recommendation,
        suggestion=suggestion,
        lifecycle_event=lifecycle_event,
        stage_label=str(governance_stage.get("stage_label") or ""),
        target_type=target_type,
        action_label=action_label,
    )
    governance_todo_queue = _parameter_governance_todo_queue_snapshot(
        latest_candidate=latest_candidate,
        recommendation=recommendation,
        suggestion=suggestion,
        lifecycle_event=lifecycle_event,
        stage_label=str(governance_stage.get("stage_label") or ""),
        next_step_summary=str(governance_stage.get("next_step_summary") or ""),
        priority_label=str(priority["label"]),
        jump=governance_jump,
    )
    timeline_context = _parameter_governance_timeline_context_snapshot(
        factor_id=factor_id,
        stage_label=str(governance_stage.get("stage_label") or ""),
        stage_summary=str(priority["summary"] or ""),
        next_step_summary=str(governance_stage.get("next_step_summary") or ""),
        jump=governance_jump,
        todo_queue=governance_todo_queue,
    )
    timeline_filter_context = _parameter_governance_timeline_filter_context_snapshot()
    entry_context = _parameter_governance_entry_context_snapshot(
        stage_label=str(governance_stage.get("stage_label") or ""),
        stage_summary=str(priority["summary"] or ""),
        next_step_label=str(governance_stage.get("next_step_label") or ""),
        next_step_summary=str(governance_stage.get("next_step_summary") or ""),
        entry_type=str(governance_stage.get("entry_type") or ""),
        target_type=target_type,
        action_label=action_label,
    )
    quick_actions = _parameter_governance_quick_actions_snapshot(
        factor_id=factor_id,
        latest_candidate=latest_candidate,
        recommendation=recommendation,
    )
    responsibility_text = _humanize_template_responsibility(
        str(anchor.get("primary_responsibility") or "")
    )
    labels = list(anchor.get("responsibility_labels") or [])
    if latest_candidate:
        candidate_status = _humanize_template_candidate_status(latest_candidate.get("status") or "")
        if latest_trace.get("recommendation_id"):
            ops_summary = (
                f"这笔交易当前最值得关注的参数治理对象是 {factor_id}。"
                f"最近候选 {latest_candidate.get('candidate_id')} 当前{candidate_status}，"
                f"来源推荐 {latest_trace.get('recommendation_id')} "
                f"({responsibility_text}，{_humanize_approval_path(latest_trace.get('approval_path') or '')})。"
            )
        else:
            ops_summary = (
                f"这笔交易当前最值得关注的参数治理对象是 {factor_id}。"
                f"最近候选 {latest_candidate.get('candidate_id')} 当前{candidate_status}。"
            )
    elif recommendation:
        boundary_scope = str(((recommendation.get("boundary") or {}).get("recommended_scope") or "")).lower()
        boundary_text = "离线深调" if boundary_scope == "offline_deep" else "在线轻调"
        ops_summary = (
            f"这笔交易对 {factor_id} 的归因更像参数问题。"
            f"当前建议切到 {recommendation.get('target_template_version') or recommendation.get('target_template_id') or '--'}，"
            f"并按 {boundary_text} 路径推进。"
        )
    else:
        ops_summary = (
            f"这笔交易对 {factor_id} 的归因更像参数问题，"
            "但当前还没有形成可执行的模板推荐。"
        )
    overview = _parameter_governance_overview_snapshot(
        ops_summary=ops_summary,
        stage_label=str(governance_stage.get("stage_label") or ""),
        stage_summary=str(priority["summary"] or ""),
        next_step_label=str(governance_stage.get("next_step_label") or ""),
        next_step_summary=str(governance_stage.get("next_step_summary") or ""),
        entry_type=str(governance_stage.get("entry_type") or ""),
        target_type=target_type,
        action_label=action_label,
        priority_label=str(priority["label"]),
        priority_summary=str(priority["summary"]),
        latest_candidate=latest_candidate,
    )
    return {
        "factor_id": factor_id,
        "primary_responsibility": str(anchor.get("primary_responsibility") or ""),
        "responsibility_text": responsibility_text,
        "responsibility_labels": labels,
        "net_contribution": float(anchor.get("net_contribution") or 0.0),
        "factor_role": str(anchor.get("factor_role") or ""),
        "recommendation": recommendation,
        "latest_candidate": latest_candidate or None,
        "suggestion": suggestion or None,
        "lifecycle_event": lifecycle_event or None,
        "governance_jump": governance_jump,
        "governance_todo_queue": governance_todo_queue,
        "timeline_context": timeline_context,
        "timeline_filter_context": timeline_filter_context,
        "entry_context": entry_context,
        "quick_actions": quick_actions,
        "overview": overview,
        "stage_label": str(governance_stage.get("stage_label") or ""),
        "stage_summary": str(governance_stage.get("next_step_summary") or ""),
        "next_step_label": str(governance_stage.get("next_step_label") or ""),
        "next_step_summary": str(governance_stage.get("next_step_summary") or ""),
        "entry_type": str(governance_stage.get("entry_type") or ""),
        "target_type": target_type,
        "action_label": action_label,
        "priority_score": int(priority["score"]),
        "priority_label": str(priority["label"]),
        "priority_summary": str(priority["summary"]),
        "ops_summary": ops_summary,
    }


def _recent_trade_trace_index(limit: int = 20) -> dict[str, Any]:
    limit = max(1, min(int(limit or 20), 100))
    conn = get_state_conn()
    conn.row_factory = sqlite3.Row
    try:
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
        except sqlite3.OperationalError:
            rows = []
        items: list[dict[str, Any]] = []
        for row in rows:
            parsed = _parse_review_row(row)
            position_id = str(parsed.get("position_id") or "")
            trade_id = str(parsed.get("trade_id") or "")
            context = _latest_symbol_context(conn, position_id=position_id, trade_id=trade_id)
            factor_hint = _top_factor_hint_for_review(conn, str(parsed.get("review_id") or ""))
            parameter_factor = ""
            parameter_candidate_status = ""
            parameter_candidate_id = ""
            parameter_recommendation_id = ""
            parameter_governance_stage = ""
            parameter_governance_stage_summary = ""
            parameter_governance_next_step = ""
            parameter_governance_entry_type = ""
            parameter_governance_target_type = ""
            parameter_governance_entry_hint_text = ""
            parameter_governance_action_label = ""
            parameter_governance_priority_score = 0
            parameter_governance_priority_label = ""
            parameter_governance_priority_summary = ""
            if factor_hint and (
                str(factor_hint.get("primary_responsibility") or "") == "parameter"
                or "factor_logic_ok_but_param_suspect" in (factor_hint.get("responsibility_labels") or [])
            ):
                parameter_factor = str(factor_hint.get("factor") or "")
                candidate = _latest_template_candidate_for_factor(conn, parameter_factor)
                recommendation = None
                db_path = _db_path_from_conn(conn)
                try:
                    recommendation = ParameterTemplateService(db_path).list_recommendations(
                        factor_id=parameter_factor,
                        limit=1,
                    )[0]
                except Exception:
                    recommendation = None
                parameter_candidate_status = str(candidate.get("status") or "")
                parameter_candidate_id = str(candidate.get("candidate_id") or "")
                parameter_recommendation_id = str(((candidate.get("trace") or {}).get("recommendation_id") or ""))
                if not parameter_recommendation_id and recommendation:
                    parameter_recommendation_id = str(recommendation.get("recommendation_id") or "")
                governance_stage = _parameter_governance_stage_snapshot(
                    candidate=candidate,
                    recommendation=recommendation,
                )
                parameter_governance_stage = governance_stage["stage_label"]
                parameter_governance_next_step = governance_stage["next_step_summary"]
                parameter_governance_entry_type = governance_stage["entry_type"]
                parameter_governance_stage_summary = (
                    parameter_governance_next_step
                    if parameter_governance_stage
                    else ""
                )
                parameter_governance_target_type = _parameter_governance_target_type(
                    parameter_governance_entry_type
                )
                parameter_governance_entry_hint_text = (
                    f"建议先看{parameter_governance_target_type}"
                    if parameter_governance_target_type
                    else ""
                )
                parameter_governance_action_label = _parameter_governance_action_label(
                    parameter_governance_entry_type,
                    parameter_governance_stage,
                )
                priority = _parameter_governance_priority_snapshot(
                    entry_type=parameter_governance_entry_type,
                    stage_label=parameter_governance_stage,
                    has_governance_factor=bool(parameter_factor),
                )
                parameter_governance_priority_score = int(priority["score"])
                parameter_governance_priority_label = str(priority["label"])
                parameter_governance_priority_summary = str(priority["summary"])
            items.append(
                {
                    "review_id": str(parsed.get("review_id") or ""),
                    "position_id": position_id,
                    "trade_id": trade_id,
                    "entry_decision_id": str(parsed.get("entry_decision_id") or ""),
                    "exit_decision_id": str(parsed.get("exit_decision_id") or ""),
                    "symbol": context["symbol"],
                    "timeframe": context["timeframe"],
                    "outcome_label": str(parsed.get("outcome_label") or ""),
                    "summary_text": str(parsed.get("summary_text") or ""),
                    "close_reason": str((parsed.get("review") or {}).get("close_reason") or ""),
                    "primary_responsibility": str(parsed.get("primary_responsibility") or ""),
                    "responsibility_labels": list(parsed.get("responsibility_labels") or []),
                    "parameter_governance_factor": parameter_factor,
                    "parameter_candidate_status": parameter_candidate_status,
                    "parameter_candidate_id": parameter_candidate_id,
                    "parameter_recommendation_id": parameter_recommendation_id,
                    "parameter_governance_stage": parameter_governance_stage,
                    "parameter_governance_stage_summary": parameter_governance_stage_summary,
                    "parameter_governance_next_step": parameter_governance_next_step,
                    "parameter_governance_entry_type": parameter_governance_entry_type,
                    "parameter_governance_target_type": parameter_governance_target_type,
                    "parameter_governance_entry_hint_text": parameter_governance_entry_hint_text,
                    "parameter_governance_action_label": parameter_governance_action_label,
                    "parameter_governance_priority_score": parameter_governance_priority_score,
                    "parameter_governance_priority_label": parameter_governance_priority_label,
                    "parameter_governance_priority_summary": parameter_governance_priority_summary,
                    "created_at": float(parsed.get("created_at") or 0.0),
                }
            )
        return {"items": items, "count": len(items), "limit": limit}
    finally:
        conn.close()


def _trade_trace(position_id: str | None = None, decision_id: str | None = None) -> dict[str, Any]:
    resolved_position_id = str(position_id or "").strip()
    resolved_decision_id = str(decision_id or "").strip()
    if not resolved_position_id and not resolved_decision_id:
        raise ValueError("position_id or decision_id is required")

    conn = get_state_conn()
    conn.row_factory = sqlite3.Row
    try:
        anchor = None
        if resolved_decision_id:
            anchor = conn.execute(
                """
                SELECT decision_id, trade_id, position_id, event_type, symbol, timeframe, decision_ts
                FROM decision_ledger
                WHERE decision_id = ?
                LIMIT 1
                """,
                (resolved_decision_id,),
            ).fetchone()
            if anchor and not resolved_position_id:
                resolved_position_id = str(anchor["position_id"] or anchor["trade_id"] or "").strip()

        ledger_rows = []
        if resolved_position_id:
            ledger_rows = conn.execute(
                """
                SELECT decision_id, trade_id, position_id, event_type, symbol, timeframe, decision_ts,
                       regime_id, regime_confidence, portfolio_state_json, risk_state_json,
                       policy_version, factor_set_version, action_score, action_reason, action_json, created_at
                FROM decision_ledger
                WHERE position_id = ? OR trade_id = ?
                ORDER BY decision_ts ASC, created_at ASC
                """,
                (resolved_position_id, resolved_position_id),
            ).fetchall()
        elif anchor:
            ledger_rows = [anchor]

        if not anchor and ledger_rows:
            anchor = ledger_rows[0]
        if not anchor and resolved_decision_id:
            raise LookupError(f"decision_id not found: {resolved_decision_id}")

        trade_id = ""
        symbol = ""
        timeframe = ""
        for row in ledger_rows:
            trade_id = trade_id or str(row["trade_id"] or "")
            symbol = symbol or str(row["symbol"] or "")
            timeframe = timeframe or str(row["timeframe"] or "")

        position_events = []
        recovery_state = None
        pos_int = _safe_int(resolved_position_id)
        if pos_int is not None:
            position_events = conn.execute(
                """
                SELECT event_id, position_id, trade_id, symbol, event_type, event_ts,
                       net_volume, avg_price, unrealized_pnl, realized_pnl, details_json
                FROM position_lifecycle_event
                WHERE position_id = ?
                ORDER BY event_ts ASC, event_id ASC
                """,
                (str(pos_int),),
            ).fetchall()
            recovery_state = conn.execute(
                """
                SELECT position_id, broker, symbol, direction, open_price, volume, first_seen_at,
                       last_seen_at, status, strategy_name, entry_decision_id, context_integrity,
                       recovery_meta_json, closed_at, close_reason, close_pnl
                FROM recovery_position_state
                WHERE position_id = ?
                LIMIT 1
                """,
                (pos_int,),
            ).fetchone()

        order_events = []
        if trade_id:
            order_events = conn.execute(
                """
                SELECT event_id, decision_id, trade_id, order_id, broker_order_id, event_type,
                       event_ts, price, volume, status, details_json
                FROM order_lifecycle_event
                WHERE trade_id = ?
                ORDER BY event_ts ASC, event_id ASC
                """,
                (trade_id,),
            ).fetchall()

        review_row = None
        if resolved_position_id:
            review_row = conn.execute(
                """
                SELECT review_id, trade_id, position_id, entry_decision_id, exit_decision_id,
                       entry_quality, hold_quality, exit_quality, regime_fit_score,
                       execution_quality, pnl, mae, mfe, outcome_label, failure_tags_json,
                       summary_text, review_json, created_at
                FROM trade_outcome_review
                WHERE position_id = ? OR trade_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (resolved_position_id, resolved_position_id),
            ).fetchone()
        if review_row is None and resolved_decision_id:
            review_row = conn.execute(
                """
                SELECT review_id, trade_id, position_id, entry_decision_id, exit_decision_id,
                       entry_quality, hold_quality, exit_quality, regime_fit_score,
                       execution_quality, pnl, mae, mfe, outcome_label, failure_tags_json,
                       summary_text, review_json, created_at
                FROM trade_outcome_review
                WHERE entry_decision_id = ? OR exit_decision_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (resolved_decision_id, resolved_decision_id),
            ).fetchone()

        factor_rows = []
        if review_row is not None:
            factor_rows = conn.execute(
                """
                SELECT id, review_id, trade_id, factor, entry_contribution, hold_contribution,
                       exit_contribution, net_contribution, confidence, notes
                FROM factor_contribution_review
                WHERE review_id = ?
                ORDER BY ABS(net_contribution) DESC, id ASC
                """,
                (review_row["review_id"],),
            ).fetchall()

        def _parse_ledger(row: sqlite3.Row) -> dict[str, Any]:
            item = dict(row)
            item["portfolio_state"] = _loads_json(item.pop("portfolio_state_json", None), {})
            item["risk_state"] = _loads_json(item.pop("risk_state_json", None), {})
            item["action"] = _loads_json(item.pop("action_json", None), {})
            return item

        def _parse_event(row: sqlite3.Row) -> dict[str, Any]:
            item = dict(row)
            item["details"] = _loads_json(item.pop("details_json", None), {})
            return item

        review = _parse_review_row(review_row) if review_row is not None else None
        factor_contributions = [dict(row) for row in factor_rows]
        for item in factor_contributions:
            raw_notes = str(item.get("notes") or "")
            note_payload = _loads_json(raw_notes, {}) if raw_notes.startswith("{") else {}
            item["note_payload"] = note_payload
            item["primary_responsibility"] = str(note_payload.get("primary_responsibility") or "")
            item["responsibility_labels"] = list(note_payload.get("responsibility_labels") or [])
            item["factor_role"] = str(note_payload.get("factor_role") or "")
        supervisor_events = []
        latest_supervisor = None
        for row in ledger_rows:
            event_type = str(row["event_type"] or "")
            if event_type.startswith("supervisor_"):
                parsed = _parse_ledger(row)
                supervisor_events.append(parsed)
                latest_supervisor = parsed
        parameter_governance = _build_trade_trace_parameter_governance(
            conn,
            factor_contributions=factor_contributions,
        )
        if not ledger_rows and not position_events and not order_events and review is None and recovery_state is None:
            locator = resolved_position_id or resolved_decision_id
            raise LookupError(f"trade trace not found: {locator}")
        summary = {
            "position_id": resolved_position_id or (str(review["position_id"]) if review else ""),
            "decision_id": resolved_decision_id or (str(anchor["decision_id"]) if anchor else ""),
            "trade_id": trade_id or (str(review["trade_id"]) if review else ""),
            "symbol": symbol or (str(review["review"].get("symbol") or "") if review else ""),
            "timeframe": timeframe,
            "ledger_events": len(ledger_rows),
            "position_events": len(position_events),
            "order_events": len(order_events),
            "has_review": review is not None,
            "factor_count": len(factor_contributions),
            "latest_outcome": str(review["outcome_label"] or "") if review else "",
            "latest_close_reason": str((review.get("review") or {}).get("close_reason") or "") if review else "",
            "supervisor_events": len(supervisor_events),
            "latest_supervisor_action": str((latest_supervisor or {}).get("action", {}).get("supervisor_verdict", {}).get("action") or ""),
            "parameter_governance_factor": str(parameter_governance.get("factor_id") or ""),
        }
        return {
            "summary": summary,
            "anchor": dict(anchor) if anchor is not None else None,
            "decision_ledger": [_parse_ledger(row) for row in ledger_rows],
            "position_supervisor": {
                "latest": latest_supervisor,
                "events": supervisor_events,
            },
            "position_lifecycle": [_parse_event(row) for row in position_events],
            "order_lifecycle": [_parse_event(row) for row in order_events],
            "review": review,
            "factor_contributions": factor_contributions,
            "parameter_governance": parameter_governance or None,
            "recovery_state": {
                **dict(recovery_state),
                "recovery_meta": _loads_json(recovery_state["recovery_meta_json"], {}),
            } if recovery_state is not None else None,
        }
    finally:
        conn.close()


def _system_health_summary() -> dict[str, Any]:
    report = _get_system_health_report()
    if report is None:
        return {
            "overall": "unknown",
            "overall_score": 0.0,
            "critical_components": [],
            "degraded_components": [],
            "blocking_components": [],
            "advisory_critical_components": [],
            "trading_blocked": False,
            "impact_status": "unknown",
            "impact_summary": "还没有拿到运行环境快照，暂时无法判断是否会影响交易。",
            "policy_flags": _runtime_risk_policy(),
            "components": {},
            "errors": [],
        }

    policy_flags = _runtime_risk_policy()
    components = getattr(report, "components", {}) or {}
    component_status = {
        str(name): {
            "status": str(getattr(component, "status", "") or ""),
            "detail": str(getattr(component, "detail", "") or ""),
            "score": float(getattr(component, "score", 0.0) or 0.0),
        }
        for name, component in components.items()
    }
    critical_components = [name for name, item in component_status.items() if item["status"] == "critical"]
    degraded_components = [name for name, item in component_status.items() if item["status"] == "degraded"]

    advisory_only_components = {"tick_data"}
    blocking_components: list[str] = []
    advisory_critical_components: list[str] = []
    for name in critical_components:
        if name in advisory_only_components:
            advisory_critical_components.append(name)
        elif name == "l2_depth" and not policy_flags["require_l2_depth"]:
            advisory_critical_components.append(name)
        elif name == "disk_space" and not policy_flags["block_on_disk_critical"]:
            advisory_critical_components.append(name)
        else:
            blocking_components.append(name)

    trading_blocked = bool(blocking_components)
    if trading_blocked:
        impact_status = "blocked"
        impact_summary = (
            f"当前有 {len(blocking_components)} 个运行风险会直接阻断新开仓："
            + " / ".join(blocking_components)
        )
        if advisory_critical_components or degraded_components:
            advisory_parts = advisory_critical_components + degraded_components
            impact_summary += "；同时还有需要盯住的观察项：" + " / ".join(advisory_parts)
    elif advisory_critical_components or degraded_components:
        impact_status = "observe"
        focus_items = advisory_critical_components or degraded_components
        impact_summary = (
            "当前有运行观察项，但按现有风控配置不会直接阻断交易："
            + " / ".join(focus_items)
        )
        if advisory_critical_components and degraded_components:
            impact_summary += "；一般观察项：" + " / ".join(degraded_components)
    else:
        impact_status = "ok"
        impact_summary = "运行环境目前没有明显风险项，暂时不会额外拖累交易执行。"

    return {
        "overall": str(getattr(report, "overall", "unknown") or "unknown"),
        "overall_score": float(getattr(report, "overall_score", 0.0) or 0.0),
        "critical_components": critical_components,
        "degraded_components": degraded_components,
        "blocking_components": blocking_components,
        "advisory_critical_components": advisory_critical_components,
        "trading_blocked": trading_blocked,
        "impact_status": impact_status,
        "impact_summary": impact_summary,
        "policy_flags": policy_flags,
        "components": component_status,
        "errors": list(getattr(report, "errors", []) or []),
        "ts": float(getattr(report, "ts", 0.0) or 0.0),
    }


class VarRequest(BaseModel):
    equity_series: list[float]


class KellyRequest(BaseModel):
    win_rate: float
    avg_win: float
    avg_loss: float


class StressRequest(BaseModel):
    equity_series: list[float]
    initial_equity: float | None = None


class ConcentrationRequest(BaseModel):
    weights: list[float]


@router.get("/summary")
def get_risk_summary(_user: RequireUser) -> dict[str, Any]:
    """
    获取风控指标概览: VaR, Kelly, stress, concentration.
    """
    var = _var_calc.get_status()
    kelly = _kelly.get_status()
    stress = _stress.get_status()
    conc = _conc.get_status()
    return {
        "var": var,
        "kelly": kelly,
        "stress": stress,
        "concentration": conc,
        "policy": _recent_policy_verdicts(limit=25),
        "system_health": _system_health_summary(),
    }


@router.get("/policy/verdicts")
def get_policy_verdicts(_user: RequireUser, limit: int = 50) -> dict[str, Any]:
    """最近的统一风控裁决，用于 Phase B 风控面板与审计."""
    return _recent_policy_verdicts(limit=limit)


@router.get("/trade-trace")
def get_trade_trace(
    _user: RequireUser,
    position_id: str | None = None,
    decision_id: str | None = None,
) -> dict[str, Any]:
    """按 position_id / decision_id 查询一笔交易的风控、生命周期与复盘证据链。"""
    try:
        return _trade_trace(position_id=position_id, decision_id=decision_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/trade-trace/recent")
def get_recent_trade_traces(_user: RequireUser, limit: int = 20) -> dict[str, Any]:
    return _recent_trade_trace_index(limit=limit)


@router.post("/var")
def calc_var(_user: RequireUser, req: VarRequest) -> dict[str, Any]:
    """
    计算并返回 VaR / CVaR.
    """
    return _var_calc.calculate(req.equity_series)


@router.get("/var")
def get_var_status(_user: RequireUser) -> dict[str, Any]:
    """获取当前 VaR 状态 (无权益数据时返回空结构)。"""
    return _var_calc.get_status()


@router.post("/kelly")
def calc_kelly(_user: RequireUser, req: KellyRequest) -> dict[str, Any]:
    """
    计算 Kelly 最优下注比例。
    """
    return _kelly.calculate(req.win_rate, req.avg_win, req.avg_loss)


@router.get("/kelly")
def get_kelly_status(_user: RequireUser) -> dict[str, Any]:
    """获取 Kelly 状态概览 (无数据时)。"""
    return _kelly.get_status()


@router.post("/stress/run")
def run_stress(_user: RequireUser, req: StressRequest) -> dict[str, Any]:
    """
    运行压力测试场景。
    """
    return _stress.run(req.equity_series, req.initial_equity)


@router.get("/stress")
def get_stress_status(_user: RequireUser) -> dict[str, Any]:
    """获取压力测试状态 (无数据时)。"""
    return _stress.get_status()


@router.post("/concentration")
def check_concentration(
    _user: RequireUser,
    weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    """
    检查因子/仓位集中度。
    weights: {因子名: 权重百分比}
    """
    return _conc.check(weights)


@router.get("/concentration")
def get_concentration_status(_user: RequireUser) -> dict[str, Any]:
    """获取集中度状态 (无数据时)。"""
    return _conc.get_status()
