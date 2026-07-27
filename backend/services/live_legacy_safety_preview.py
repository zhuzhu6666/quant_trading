"""Read-only preview of the legacy position-protection arbitration.

This module intentionally duplicates the legacy stage ordering instead of
calling the V2 planner.  The two outputs can therefore be compared before any
broker mutation.  Stateful reads are dependency-injected through the same
read-only adapter contract; no broker object is accepted here.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from backend.services.live_safety_planner import (
    SafetyPlan,
    SafetyPlannerRuntime,
    safety_candidate,
)


def _pid(position: Mapping[str, Any]) -> int:
    try:
        return int(position.get("position_id") or position.get("ticket") or 0)
    except (TypeError, ValueError):
        return 0


def _number(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _legacy_entry_repair(
    position: Mapping[str, Any],
    plan: Mapping[str, Any],
    *,
    now_ts: float,
    cooldown_seconds: float,
):
    if str(plan.get("schema_version") or "") != "entry_protection_plan.v1":
        return None
    target_sl = _number(plan.get("target_stop_loss"))
    target_tp = _number(plan.get("target_take_profit"))
    if target_sl <= 0 and target_tp <= 0:
        return None
    last_attempt = _number(plan.get("last_attempt_ts"))
    if last_attempt > 0 and now_ts - last_attempt < cooldown_seconds:
        return None
    direction = int(plan.get("direction") or position.get("direction") or 0)
    current_sl = _number(
        position.get("sl") or position.get("stop_loss") or position.get("stopLoss")
    )
    current_tp = _number(
        position.get("tp") or position.get("take_profit") or position.get("takeProfit")
    )
    missing_sl = bool(
        target_sl > 0
        and (
            current_sl <= 0
            or (direction > 0 and target_sl > current_sl + 0.01)
            or (direction < 0 and target_sl < current_sl - 0.01)
        )
    )
    missing_tp = bool(target_tp > 0 and current_tp <= 0)
    if not missing_sl and not missing_tp:
        return None
    return safety_candidate(
        action="repair_entry_protection",
        position_id=_pid(position),
        source="entry_protection_repair",
        controls={
            "target_stop_loss": round(target_sl, 2) if target_sl > 0 else 0.0,
            "target_take_profit": round(target_tp, 2) if target_tp > 0 else 0.0,
            "close_reason": "entry_protection_repair",
            "protection_mode": "entry_sltp_repair",
        },
    )

def preview_legacy_safety_candidates(
    *,
    positions: Sequence[Mapping[str, Any]],
    cfg: Any,
    account: Mapping[str, Any],
    current_price: float,
    atr_price: float,
    runtime: SafetyPlannerRuntime,
    entry_repair_cooldown_seconds: float = 20.0,
    planned_at: float,
) -> SafetyPlan:
    """Reproduce legacy arbitration without executing or persisting actions."""

    now_ts = float(planned_at)
    rows = [dict(item or {}) for item in positions]
    selected = []
    arbitration = []
    handled: set[int] = set()

    for position in rows:
        position_id = _pid(position)
        if position_id <= 0:
            continue
        context = dict(runtime.build_timeout_context(position, cfg, now_ts) or {})
        held = _number(context.get("holding_seconds"))
        limit = _number(context.get("max_holding_seconds"))
        if limit <= 0 or held < limit:
            continue
        item = safety_candidate(
            action="timeout",
            position_id=position_id,
            source="holding_timeout",
            controls={"close_reason": "holding_timeout"},
        )
        selected.append(item)
        handled.add(position_id)
        arbitration.append(
            {"fingerprint": item.fingerprint, "decision": "selected", "priority": 10}
        )

    for position in rows:
        position_id = _pid(position)
        if position_id <= 0 or position_id in handled:
            continue
        item = _legacy_entry_repair(
            position,
            dict(runtime.load_entry_protection_plan(position_id) or {}),
            now_ts=now_ts,
            cooldown_seconds=float(entry_repair_cooldown_seconds),
        )
        if item is None:
            continue
        selected.append(item)
        handled.add(position_id)
        arbitration.append(
            {"fingerprint": item.fingerprint, "decision": "selected", "priority": 20}
        )

    for position in rows:
        position_id = _pid(position)
        if position_id <= 0 or position_id in handled:
            continue
        verdict = dict(
            runtime.evaluate_supervisor(position, rows, cfg, account, now_ts) or {}
        )
        if runtime.normalize_supervisor_action is not None:
            verdict = dict(
                runtime.normalize_supervisor_action(position, verdict) or verdict
            )
        action = str(verdict.get("action") or "hold").strip().lower()
        if action not in {"close", "reduce", "tighten"}:
            continue
        item = safety_candidate(
            action=action,
            position_id=position_id,
            source=f"supervisor_{action}",
            controls=dict(verdict.get("recommended_controls") or {}),
        )
        selected.append(item)
        handled.add(position_id)
        arbitration.append(
            {"fingerprint": item.fingerprint, "decision": "selected", "priority": 30}
        )

    if float(atr_price or 0.0) > 0:
        conviction = float(runtime.composite_conviction() or 0.0)
        for position in rows:
            position_id = _pid(position)
            if position_id <= 0:
                continue
            update = dict(
                runtime.build_trailing_update(
                    position,
                    dict(runtime.trailing_state(position_id) or {}),
                    float(current_price or 0.0),
                    float(atr_price or 0.0),
                    conviction,
                )
                or {}
            )
            payload = update.get("candidate")
            if not isinstance(payload, Mapping):
                continue
            item = safety_candidate(
                action="trailing",
                position_id=position_id,
                source="legacy_awe_trailing",
                controls=dict(payload.get("controls") or {}),
            )
            if position_id in handled:
                arbitration.append(
                    {"fingerprint": item.fingerprint, "decision": "superseded", "priority": 50}
                )
                continue
            selected.append(item)
            handled.add(position_id)
            arbitration.append(
                {"fingerprint": item.fingerprint, "decision": "selected", "priority": 50}
            )

    return SafetyPlan(
        candidates=tuple(selected),
        arbitration=tuple(arbitration),
        planned_at=now_ts,
    )
