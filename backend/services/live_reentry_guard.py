"""Supervisor and retrospective same-direction re-entry guards."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from backend.services.canonical_v2_reader import iter_review_rows


@dataclass(frozen=True)
class ReentryGuardRuntime:
    blocks: dict[str, dict[str, Any]]
    blocks_lock: Any
    reentry_key: Any
    build_block_payload: Any
    block_view: Any
    direction_from_position: Any
    position_symbol: Any
    payload_get: Any
    cooldown_seconds: Any
    build_pending_payload: Any
    state_connection_factory: Any
    warning: Any
    now: Any


def remember_supervisor_reentry_block(
    *,
    position: Any,
    action: str,
    reason: str,
    cfg: Any,
    runtime: ReentryGuardRuntime,
    current_price: float = 0.0,
    tick: int = 0,
) -> None:
    direction = runtime.direction_from_position(position)
    if direction == 0:
        return
    symbol = runtime.position_symbol(position)
    cooldown_seconds = runtime.cooldown_seconds(cfg)
    if cooldown_seconds <= 0:
        return
    now_ts = float(runtime.now())
    try:
        position_id = int(
            runtime.payload_get(position, "position_id", 0)
            or runtime.payload_get(position, "ticket", 0)
            or 0
        )
    except Exception:
        position_id = 0
    payload = runtime.build_block_payload(
        symbol=symbol,
        direction=direction,
        position_id=position_id,
        action=action,
        reason=reason,
        started_at=now_ts,
        cooldown_seconds=cooldown_seconds,
        current_price=float(
            current_price
            or runtime.payload_get(position, "current_price", 0.0)
            or 0.0
        ),
        tick=tick,
    )
    with runtime.blocks_lock:
        runtime.blocks[runtime.reentry_key(symbol, direction)] = payload


def active_supervisor_reentry_block(
    *,
    symbol: str,
    direction: int,
    runtime: ReentryGuardRuntime,
) -> dict[str, Any] | None:
    if int(direction or 0) == 0:
        return None
    key = runtime.reentry_key(symbol, direction)
    now_ts = float(runtime.now())
    with runtime.blocks_lock:
        block = dict(runtime.blocks.get(key) or {})
        view = runtime.block_view(block, now_ts=now_ts)
        if view is None:
            runtime.blocks.pop(key, None)
            return None
    return view


def recent_review_reentry_block(
    *,
    symbol: str,
    direction: int,
    runtime: ReentryGuardRuntime,
    now_ts: float | None = None,
) -> dict[str, Any] | None:
    """Block two consecutive same-direction conflicting-thesis losses."""

    if int(direction or 0) == 0:
        return None
    now = float(now_ts or runtime.now())
    try:
        conn = runtime.state_connection_factory(read_only=True)
        try:
            rows = iter_review_rows(conn, limit=0)
            cutoff = float(now - 3 * 3600.0)
            rows = [
                row for row in rows
                if float(row.get("created_at") or 0.0) >= cutoff
            ]
            rows.sort(key=lambda row: float(row.get("created_at") or 0.0), reverse=True)
            rows = rows[:12]
            rows = [
                {
                    **dict(row),
                    "_review_payload": (
                        dict(row).get("review_json")
                        if isinstance(dict(row).get("review_json"), dict)
                        else _json_object(dict(row).get("review_json"))
                    ),
                }
                for row in rows
            ]
        finally:
            conn.close()
    except Exception as exc:
        runtime.warning(
            "[live] recent review reentry evidence unavailable: {}",
            exc,
        )
        return None

    matched: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        review = item.pop("_review_payload", {})
        if not isinstance(review, dict):
            review = {}
        if int(review.get("direction") or 0) != int(direction):
            continue
        tags = _json_list(item.get("failure_tags_json"))
        tag_set = {str(tag) for tag in tags}
        is_failed_thesis = (
            str(item.get("outcome_label") or "") == "bad_loss"
            and bool(
                tag_set.intersection(
                    {
                        "factor_conflict",
                        "conflicting_factor_entry",
                        "conflict_entry_loss",
                        "thesis_broken",
                        "regime_mismatch",
                    }
                )
            )
        )
        if not is_failed_thesis:
            break
        matched.append(item)
        if len(matched) >= 2:
            break
    if len(matched) < 2:
        return None

    latest_close = float(matched[0].get("created_at") or 0.0)
    expires_at = latest_close + 3600.0
    if expires_at <= now:
        return None
    return {
        "schema_version": "retrospective_reentry_block.v1",
        "active": True,
        "source": "canonical_v2.trade_review",
        "action": "block_same_direction_reentry",
        "reason": "repeated_conflicting_thesis_loss",
        "symbol": str(symbol or ""),
        "direction": int(direction),
        "position_id": str(matched[0].get("position_id") or ""),
        "review_ids": [
            str(item.get("review_id") or "") for item in matched
        ],
        "started_at": latest_close,
        "expires_at": expires_at,
        "remaining_seconds": round(expires_at - now, 3),
    }


def pending_supervisor_reentry_block_from_positions(
    positions: list[Any],
    *,
    symbol: str,
    direction: int,
    cfg: Any,
    runtime: ReentryGuardRuntime,
) -> dict[str, Any] | None:
    if int(direction or 0) == 0:
        return None
    allow_reduce_block = bool(
        getattr(cfg, "risk_supervisor_reentry_block_reduce", True)
    )
    for position in positions or []:
        pos_direction = runtime.direction_from_position(position)
        if pos_direction != int(direction):
            continue
        if runtime.position_symbol(position, symbol) != runtime.position_symbol(
            {"symbol": symbol}
        ):
            continue
        supervisor = runtime.payload_get(position, "supervisor", {}) or {}
        action = str(
            (
                supervisor.get("action")
                if hasattr(supervisor, "get")
                else ""
            )
            or runtime.payload_get(position, "supervisor_action", "")
            or ""
        ).lower()
        reason = str(
            (
                supervisor.get("summary_reason")
                if hasattr(supervisor, "get")
                else ""
            )
            or runtime.payload_get(position, "supervisor_reason", "")
            or ""
        )
        evidence = (
            supervisor.get("evidence")
            if hasattr(supervisor, "get")
            else {}
        ) or {}
        thesis_status = str(
            evidence.get("thesis_status")
            or runtime.payload_get(position, "thesis_status", "")
            or ""
        ).lower()
        should_block = (
            action == "close"
            or (allow_reduce_block and action == "reduce")
            or thesis_status == "broken"
        )
        if not should_block:
            continue
        try:
            position_id = int(
                runtime.payload_get(position, "position_id", 0)
                or runtime.payload_get(position, "ticket", 0)
                or 0
            )
        except Exception:
            position_id = 0
        return runtime.build_pending_payload(
            symbol=runtime.position_symbol(position, symbol),
            direction=direction,
            position_id=position_id,
            action=action,
            reason=reason,
            thesis_status=thesis_status,
            remaining_seconds=runtime.cooldown_seconds(cfg),
        )
    return None


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "{}")
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "[]")
        except Exception:
            return []
        return list(parsed) if isinstance(parsed, list) else []
    return []
