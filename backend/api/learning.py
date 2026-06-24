from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from backend.core.auth import RequireUser
from research.learning.governor import RuleEvolutionGovernor

router = APIRouter(prefix="/api/learning", tags=["learning"])


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
    return {
        "review_pending": gov.review_pending(),
        "reconcile_active": gov.reconcile_active(),
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
        reviews = conn.execute(
            """
            SELECT outcome_label, COUNT(*) AS c
            FROM trade_outcome_review
            GROUP BY outcome_label
            """
        ).fetchall()
        apps = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM learning_application_log
            """
        ).fetchone()
        last_review = conn.execute(
            """
            SELECT review_id, trade_id, position_id, outcome_label, pnl, summary_text, created_at
            FROM trade_outcome_review
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()
        return {
            "suggestions": {str(r["status"]): int(r["c"]) for r in suggestions},
            "reviews": {str(r["outcome_label"]): int(r["c"]) for r in reviews},
            "applications": int((apps["c"] if apps else 0) or 0),
            "latest_review": dict(last_review) if last_review else None,
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
        items = []
        for row in rows:
            item = dict(row)
            try:
                item["failure_tags"] = json.loads(item.pop("failure_tags_json") or "[]")
            except Exception:
                item["failure_tags"] = []
            try:
                item["review"] = json.loads(item.pop("review_json") or "{}")
            except Exception:
                item["review"] = {}
            items.append(item)
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
            SELECT application_id, cycle_ts, scope_type, scope_key, action, bias_multiplier,
                   old_weight, new_weight, suggestion_ids_json, status, details_json, created_at
            FROM learning_application_log
            ORDER BY created_at DESC
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
