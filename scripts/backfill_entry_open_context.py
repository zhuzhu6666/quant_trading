#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.core.db import get_state_pg_conn
from backend.services.autonomous_learning import materialize_autonomous_learning_samples
from backend.services.failure_taxonomy import build_failure_taxonomy
from research.learning.experience_builder import ExperienceBuilder


SCHEMA_VERSION = "entry_open_context_backfill.v1"


def _loads(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except Exception:
        return default


def _dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, default=str)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value if value not in (None, "") else default)
    except Exception:
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value if value not in (None, "") else default)
    except Exception:
        return int(default)


def _symbol_key(value: Any) -> str:
    return str(value or "XAUUSD").replace("+", "").upper()


def _direction_from_action(action: dict[str, Any], action_score: float) -> int:
    direction = _safe_int(action.get("direction"), 0)
    if direction:
        return 1 if direction > 0 else -1
    if action_score > 0:
        return 1
    if action_score < 0:
        return -1
    return 0


def _review_close_ts(review_row: Any) -> float:
    review = _loads(review_row["review_json"], {})
    return _safe_float(review.get("close_ts"), _safe_float(review_row["created_at"]))


def _entry_cluster_for_open(open_row: dict[str, Any], prior_active: list[dict[str, Any]]) -> dict[str, Any]:
    direction = int(open_row["direction"])
    symbol = _symbol_key(open_row["symbol"])
    opened_at = _safe_float(open_row["created_at"], _safe_float(open_row["decision_ts"]))
    same = [
        item for item in prior_active
        if _symbol_key(item.get("symbol")) == symbol and int(item.get("direction") or 0) == direction
    ]
    opposite = [
        item for item in prior_active
        if _symbol_key(item.get("symbol")) == symbol and int(item.get("direction") or 0) == -direction
    ]
    same_ages = [
        max(0.0, opened_at - _safe_float(item.get("opened_at")))
        for item in same
        if _safe_float(item.get("opened_at")) > 0
    ]
    same_volume = sum(_safe_float(item.get("api_volume")) for item in same)
    opposite_volume = sum(_safe_float(item.get("api_volume")) for item in opposite)
    new_volume = _safe_float(open_row.get("api_volume"))
    return {
        "schema_version": "entry_cluster_context.v1",
        "backfilled_by": SCHEMA_VERSION,
        "symbol": str(open_row["symbol"] or ""),
        "direction": direction,
        "open_position_count_before": len(prior_active),
        "open_position_count_after": len(prior_active) + 1,
        "same_direction_open_count_before": len(same),
        "same_direction_open_count_after": len(same) + 1,
        "opposite_direction_open_count_before": len(opposite),
        "same_direction_api_volume_before": same_volume,
        "same_direction_api_volume_after": same_volume + new_volume,
        "opposite_direction_api_volume_before": opposite_volume,
        "seconds_since_last_same_direction_open": min(same_ages) if same_ages else 0.0,
        "recent_same_direction_entries": {
            "5m": sum(1 for age in same_ages if age <= 300.0),
            "15m": sum(1 for age in same_ages if age <= 900.0),
            "30m": sum(1 for age in same_ages if age <= 1800.0),
        },
        "same_direction_position_ids": [str(item.get("position_id") or "") for item in same if item.get("position_id")],
        "new_position_id": _safe_int(open_row.get("position_id")),
        "position_slot_index": len(same) + 1,
        "is_pyramid": bool(same),
        "pyramid_depth": len(same),
    }


def _merge_open_action(action: dict[str, Any], cluster: dict[str, Any], *, force: bool) -> tuple[dict[str, Any], bool]:
    changed = False
    if force or not isinstance(action.get("entry_cluster"), dict) or not action.get("entry_cluster"):
        action["entry_cluster"] = cluster
        changed = True
    if force or "same_direction_open_count" not in action:
        action["same_direction_open_count"] = cluster["same_direction_open_count_before"]
        changed = True
    if force or "recent_same_direction_entries" not in action:
        action["recent_same_direction_entries"] = cluster["recent_same_direction_entries"]
        changed = True
    if force or not isinstance(action.get("portfolio_exposure"), dict) or not action.get("portfolio_exposure"):
        action["portfolio_exposure"] = {
            "schema_version": "portfolio_exposure_context.v1",
            "backfilled_by": SCHEMA_VERSION,
            "open_position_count_before": cluster["open_position_count_before"],
            "open_position_count_after": cluster["open_position_count_after"],
            "same_direction_open_count_before": cluster["same_direction_open_count_before"],
            "same_direction_open_count_after": cluster["same_direction_open_count_after"],
            "same_direction_api_volume_before": cluster["same_direction_api_volume_before"],
            "same_direction_api_volume_after": cluster["same_direction_api_volume_after"],
        }
        changed = True
    if force or not isinstance(action.get("data_quality_context"), dict) or not action.get("data_quality_context"):
        action["data_quality_context"] = {
            "schema_version": "entry_data_quality_context.v1",
            "backfilled_by": SCHEMA_VERSION,
            "historical_backfill": True,
            "unavailable_fields": ["quote_fresh", "quote_age_seconds", "spread", "bid", "ask", "l2_context"],
        }
        changed = True
    if changed:
        action["entry_context_backfill"] = {
            "schema_version": SCHEMA_VERSION,
            "backfilled_at": time.time(),
            "source_tables": ["decision_ledger", "trade_outcome_review"],
        }
    return action, changed


def _update_review_from_entry(conn: Any, review_row: Any, entry_action: dict[str, Any], *, force: bool) -> bool:
    review = _loads(review_row["review_json"], {})
    if not isinstance(review, dict):
        review = {}
    failure_tags = _loads(review_row["failure_tags_json"], [])
    if not isinstance(failure_tags, list):
        failure_tags = []
    changed = False
    copy_keys = [
        "same_direction_open_count",
        "recent_same_direction_entries",
        "entry_cluster",
        "portfolio_exposure",
        "market_micro_context",
        "spread",
        "bar_context",
        "event_context",
        "event_sizing",
        "execution_context",
        "data_quality_context",
        "decision_quality_context",
        "direction",
    ]
    for key in copy_keys:
        if key not in entry_action:
            continue
        target_key = "event_context" if key == "event_sizing" else key
        if force or target_key not in review or review.get(target_key) in (None, "", {}, []):
            review[target_key] = entry_action[key]
            changed = True

    pnl = _safe_float(review_row["pnl"])
    same_count = _safe_int(review.get("same_direction_open_count"))
    if pnl <= 0 and same_count >= 2 and "entry_cluster_risk" not in failure_tags:
        failure_tags.append("entry_cluster_risk")
        changed = True

    taxonomy = build_failure_taxonomy({**review, "pnl": pnl})
    if force or review.get("primary_responsibility") != taxonomy["primary_responsibility"]:
        review["primary_responsibility"] = taxonomy["primary_responsibility"]
        changed = True
    labels = list(dict.fromkeys(list(review.get("responsibility_labels", []) or []) + taxonomy["responsibility_labels"]))
    if force or labels != list(review.get("responsibility_labels", []) or []):
        review["responsibility_labels"] = labels
        changed = True
    for label in taxonomy["responsibility_labels"]:
        if label not in failure_tags:
            failure_tags.append(label)
            changed = True
    review["failure_taxonomy"] = taxonomy

    if not changed:
        return False
    conn.execute(
        """
        UPDATE state_v1.trade_outcome_review
        SET review_json=%s, failure_tags_json=%s
        WHERE review_id=%s
        """,
        (_dumps(review), _dumps(failure_tags), str(review_row["review_id"] or "")),
    )
    return True


def run_backfill(*, limit: int, force: bool, dry_run: bool, materialize: bool) -> dict[str, Any]:
    conn = get_state_pg_conn(read_only=False)
    updated_decisions = 0
    updated_reviews = 0
    rebuilt_experiences = 0
    try:
        reviews = conn.execute(
            """
            SELECT *
            FROM state_v1.trade_outcome_review
            ORDER BY created_at ASC
            """
        ).fetchall()
        review_by_entry = {str(row["entry_decision_id"] or ""): row for row in reviews if str(row["entry_decision_id"] or "")}
        review_by_position = {str(row["position_id"] or ""): row for row in reviews if str(row["position_id"] or "")}
        opens = conn.execute(
            """
            SELECT *
            FROM state_v1.decision_ledger
            WHERE event_type='open'
            ORDER BY created_at ASC, decision_ts ASC
            LIMIT %s
            """,
            (max(1, int(limit)),),
        ).fetchall()
        active: list[dict[str, Any]] = []
        entry_actions: dict[str, dict[str, Any]] = {}
        for row in opens:
            action = _loads(row["action_json"], {})
            if not isinstance(action, dict):
                action = {}
            created_at = _safe_float(row["created_at"], _safe_float(row["decision_ts"]))
            active = [item for item in active if _safe_float(item.get("close_ts"), 0.0) <= 0 or _safe_float(item.get("close_ts")) > created_at]
            open_item = {
                "decision_id": str(row["decision_id"] or ""),
                "position_id": str(row["position_id"] or ""),
                "symbol": str(row["symbol"] or ""),
                "created_at": created_at,
                "decision_ts": _safe_float(row["decision_ts"], created_at),
                "direction": _direction_from_action(action, _safe_float(row["action_score"])),
                "api_volume": _safe_float(action.get("volume"), _safe_float(action.get("requested_volume"))),
            }
            cluster = _entry_cluster_for_open(open_item, active)
            action, changed = _merge_open_action(action, cluster, force=force)
            entry_actions[open_item["decision_id"]] = action
            if changed:
                updated_decisions += 1
                if not dry_run:
                    conn.execute(
                        """
                        UPDATE state_v1.decision_ledger
                        SET action_json=%s
                        WHERE decision_id=%s
                        """,
                        (_dumps(action), open_item["decision_id"]),
                    )
            review = review_by_entry.get(open_item["decision_id"]) or review_by_position.get(open_item["position_id"])
            close_ts = _review_close_ts(review) if review is not None else 0.0
            active.append({**open_item, "opened_at": created_at, "close_ts": close_ts})

        for review in reviews:
            entry_id = str(review["entry_decision_id"] or "")
            action = entry_actions.get(entry_id)
            if not action:
                continue
            if _update_review_from_entry(conn, review, action, force=force):
                updated_reviews += 1

        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    finally:
        conn.close()

    if not dry_run and updated_reviews > 0:
        builder = ExperienceBuilder()
        conn = get_state_pg_conn(read_only=True)
        try:
            rows = conn.execute(
                """
                SELECT *
                FROM state_v1.trade_outcome_review
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (max(1, int(limit)),),
            ).fetchall()
            for row in rows:
                review_json = _loads(row["review_json"], {})
                review = {
                    "accepted": True,
                    "review_id": str(row["review_id"] or ""),
                    "trade_id": str(row["trade_id"] or ""),
                    "position_id": str(row["position_id"] or ""),
                    "regime_id": str(review_json.get("regime_id") or ""),
                    "outcome_label": str(row["outcome_label"] or ""),
                    "pnl": _safe_float(row["pnl"]),
                    "failure_tags": _loads(row["failure_tags_json"], []),
                    "summary_text": str(row["summary_text"] or ""),
                    "review_json": review_json,
                }
                builder.build_from_review(review)
                rebuilt_experiences += 1
        finally:
            conn.close()

    materialized = {}
    if materialize and not dry_run:
        materialized = materialize_autonomous_learning_samples(limit=max(500, int(limit)))

    return {
        "schema_version": SCHEMA_VERSION,
        "dry_run": dry_run,
        "force": force,
        "limit": int(limit),
        "updated_decisions": updated_decisions,
        "updated_reviews": updated_reviews,
        "rebuilt_experiences": rebuilt_experiences,
        "materialized": materialized,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill entry-open context into PG state_v1 learning tables.")
    parser.add_argument("--limit", type=int, default=10000)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-materialize", action="store_true")
    args = parser.parse_args()
    result = run_backfill(
        limit=args.limit,
        force=bool(args.force),
        dry_run=bool(args.dry_run),
        materialize=not bool(args.no_materialize),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
