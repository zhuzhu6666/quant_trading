from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from backend.core.auth import RequireUser
from research.learning.governor import RuleEvolutionGovernor

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


class ReviewRequest(BaseModel):
    suggestion_id: str
    status: str
    note: str = ""


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
