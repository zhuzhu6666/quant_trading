"""Pure, broker-mutation-free candidate planning for the live safety plane.

The planner deliberately receives every stateful dependency through
``SafetyPlannerRuntime``.  It may read recovery/config/model projections through
those callbacks, but this module has no broker API and cannot submit or amend an
order.  Legacy execution remains authoritative while the safety plane is in
shadow mode.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from backend.services.live_safety_plane import SafetyCandidate


_CONTROL_FIELDS = (
    "target_stop_loss",
    "target_take_profit",
    "reduce_fraction",
    "close_reason",
    "protection_mode",
)


def _position_id(position: Mapping[str, Any]) -> int:
    try:
        return int(position.get("position_id") or position.get("ticket") or 0)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _position_component_state(position: Mapping[str, Any], component: str) -> str:
    keys = (
        ("pnl_state", "unrealized_pnl_state")
        if component == "pnl"
        else ("current_price_state", "price_state")
        if component == "price"
        else (f"{component}_state",)
    )
    for key in keys:
        value = position.get(key)
        if value not in (None, ""):
            return str(value).strip().lower()
    return ""


def _missing_components(
    position: Mapping[str, Any],
    required: Sequence[str],
) -> tuple[str, ...]:
    missing: list[str] = []
    for component in required:
        normalized = str(component or "").strip().lower()
        if normalized not in {"price", "pnl"}:
            continue
        # Legacy producers without state remain compatible.  Explicit state
        # is fail-closed and only ``known`` can satisfy a metric dependency.
        state = _position_component_state(position, normalized)
        if state not in {"", "known"}:
            missing.append(normalized)
    return tuple(dict.fromkeys(missing))


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(value[key])
            for key in sorted(value, key=lambda item: str(item))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, float):
        return round(value, 8)
    return value


def normalized_controls(controls: Mapping[str, Any] | None) -> dict[str, Any]:
    """Keep only broker-relevant, deterministic comparison fields."""

    raw = dict(controls or {})
    result: dict[str, Any] = {}
    for name in _CONTROL_FIELDS:
        if name not in raw:
            continue
        value = raw[name]
        if name in {"target_stop_loss", "target_take_profit", "reduce_fraction"}:
            value = round(_float(value), 8)
        else:
            value = str(value or "")
        result[name] = value
    return result


def safety_candidate(
    *,
    action: str,
    position_id: int,
    source: str,
    controls: Mapping[str, Any] | None = None,
) -> SafetyCandidate:
    """Build the canonical candidate used by both planners and recorders."""

    normalized_action = str(action or "").strip().lower()
    normalized_source = str(source or normalized_action).strip().lower()
    normalized = normalized_controls(controls)
    identity = {
        "action": normalized_action,
        "position_id": int(position_id or 0),
        "source": normalized_source,
        "controls": _canonical_value(normalized),
    }
    fingerprint = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
            "utf-8"
        )
    ).hexdigest()
    return SafetyCandidate(
        action=normalized_action,
        position_id=int(position_id or 0),
        reason=normalized_source,
        controls=normalized,
        fingerprint=fingerprint,
    )


def protection_candidate_to_safety(candidate: Any) -> SafetyCandidate:
    """Normalize a legacy ``ProtectionCandidate`` without importing live_service."""

    source = str(getattr(candidate, "source", "") or "")
    if source == "entry_protection_repair":
        action = "repair_entry_protection"
    elif source == "legacy_awe_trailing":
        action = "trailing"
    else:
        action = str(getattr(candidate, "action", "") or "")
    return safety_candidate(
        action=action,
        position_id=int(getattr(candidate, "position_id", 0) or 0),
        source=source or str(getattr(candidate, "reason", "") or action),
        controls=dict(getattr(candidate, "controls", {}) or {}),
    )


@dataclass(frozen=True)
class SafetyPlannerRuntime:
    """Read-only adapters supplied by the live-service wiring layer."""

    build_timeout_context: Callable[[Mapping[str, Any], Any, float], Mapping[str, Any]]
    load_entry_protection_plan: Callable[[int], Mapping[str, Any]]
    evaluate_supervisor: Callable[
        [Mapping[str, Any], Sequence[Mapping[str, Any]], Any, Mapping[str, Any], float],
        Mapping[str, Any],
    ]
    build_trailing_update: Callable[
        [Mapping[str, Any], Mapping[str, Any] | None, float, float, float],
        Mapping[str, Any],
    ]
    trailing_state: Callable[[int], Mapping[str, Any] | None]
    composite_conviction: Callable[[], float]
    clock: Callable[[], float] = time.time


@dataclass(frozen=True)
class SafetyPlan:
    candidates: tuple[SafetyCandidate, ...]
    arbitration: tuple[Mapping[str, Any], ...]
    planned_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidates": [candidate.__dict__.copy() for candidate in self.candidates],
            "arbitration": [dict(item) for item in self.arbitration],
            "planned_at": self.planned_at,
        }


def _entry_repair_candidate(
    *,
    position: Mapping[str, Any],
    plan: Mapping[str, Any],
    now_ts: float,
    cooldown_seconds: float,
) -> SafetyCandidate | None:
    if str(plan.get("schema_version") or "") != "entry_protection_plan.v1":
        return None
    target_sl = _float(plan.get("target_stop_loss"))
    target_tp = _float(plan.get("target_take_profit"))
    if target_sl <= 0 and target_tp <= 0:
        return None
    last_attempt_ts = _float(plan.get("last_attempt_ts"))
    if last_attempt_ts > 0 and now_ts - last_attempt_ts < float(cooldown_seconds):
        return None
    direction = int(plan.get("direction") or position.get("direction") or 0)
    current_sl = _float(
        position.get("sl") or position.get("stop_loss") or position.get("stopLoss")
    )
    current_tp = _float(
        position.get("tp") or position.get("take_profit") or position.get("takeProfit")
    )
    needs_sl = bool(
        target_sl > 0
        and (
            current_sl <= 0
            or (direction > 0 and target_sl > current_sl + 0.01)
            or (direction < 0 and target_sl < current_sl - 0.01)
        )
    )
    needs_tp = bool(target_tp > 0 and current_tp <= 0)
    if not needs_sl and not needs_tp:
        return None
    return safety_candidate(
        action="repair_entry_protection",
        position_id=_position_id(position),
        source="entry_protection_repair",
        controls={
            "target_stop_loss": round(target_sl, 2) if target_sl > 0 else 0.0,
            "target_take_profit": round(target_tp, 2) if target_tp > 0 else 0.0,
            "close_reason": "entry_protection_repair",
            "protection_mode": "entry_sltp_repair",
        },
    )


def plan_live_safety_candidates(
    *,
    positions: Sequence[Mapping[str, Any]],
    cfg: Any,
    account: Mapping[str, Any],
    current_price: float,
    atr_price: float,
    runtime: SafetyPlannerRuntime,
    entry_repair_cooldown_seconds: float = 20.0,
    planned_at: float | None = None,
) -> SafetyPlan:
    """Plan timeout > entry repair > supervisor > trailing, without mutation."""

    now_ts = float(planned_at if planned_at is not None else runtime.clock())
    normalized_positions = [dict(position or {}) for position in positions]
    selected: list[SafetyCandidate] = []
    arbitration: list[Mapping[str, Any]] = []
    protected: set[int] = set()

    # 1. Holding timeout is the highest-priority close path.
    for position in normalized_positions:
        pid = _position_id(position)
        if pid <= 0:
            continue
        context = dict(runtime.build_timeout_context(position, cfg, now_ts) or {})
        limit = _float(context.get("max_holding_seconds"))
        held = _float(context.get("holding_seconds"))
        if limit > 0 and held >= limit:
            candidate = safety_candidate(
                action="timeout",
                position_id=pid,
                source="holding_timeout",
                controls={"close_reason": "holding_timeout"},
            )
            selected.append(candidate)
            protected.add(pid)
            arbitration.append(
                {"fingerprint": candidate.fingerprint, "decision": "selected", "priority": 10}
            )

    # 2. Missing entry protection is repaired before discretionary supervision.
    for position in normalized_positions:
        pid = _position_id(position)
        if pid <= 0 or pid in protected:
            continue
        plan = dict(runtime.load_entry_protection_plan(pid) or {})
        candidate = _entry_repair_candidate(
            position=position,
            plan=plan,
            now_ts=now_ts,
            cooldown_seconds=entry_repair_cooldown_seconds,
        )
        if candidate is None:
            continue
        selected.append(candidate)
        protected.add(pid)
        arbitration.append(
            {"fingerprint": candidate.fingerprint, "decision": "selected", "priority": 20}
        )

    # 3. The independent supervisor evaluation may close, reduce or tighten.
    for position in normalized_positions:
        pid = _position_id(position)
        if pid <= 0 or pid in protected:
            continue
        verdict = dict(
            runtime.evaluate_supervisor(position, normalized_positions, cfg, account, now_ts)
            or {}
        )
        action = str(verdict.get("action") or "hold").strip().lower()
        if action not in {"close", "reduce", "tighten"}:
            continue
        required_components = list(verdict.get("required_components") or [])
        if action == "tighten" and "price" not in required_components:
            required_components.append("price")
        missing_components = _missing_components(position, required_components)
        if missing_components:
            arbitration.append(
                {
                    "position_id": pid,
                    "decision": "blocked_component_unknown",
                    "priority": 30,
                    "action": action,
                    "missing_components": list(missing_components),
                }
            )
            continue
        source = f"supervisor_{action}"
        candidate = safety_candidate(
            action=action,
            position_id=pid,
            source=source,
            controls=dict(verdict.get("recommended_controls") or {}),
        )
        selected.append(candidate)
        protected.add(pid)
        arbitration.append(
            {"fingerprint": candidate.fingerprint, "decision": "selected", "priority": 30}
        )

    # 4. AWE trailing is last and cannot override any prior protection action.
    if float(atr_price or 0.0) > 0:
        conviction = float(runtime.composite_conviction() or 0.0)
        for position in normalized_positions:
            pid = _position_id(position)
            if pid <= 0:
                continue
            missing_components = _missing_components(position, ("price",))
            if missing_components:
                arbitration.append(
                    {
                        "position_id": pid,
                        "decision": "blocked_component_unknown",
                        "priority": 50,
                        "action": "trailing",
                        "missing_components": list(missing_components),
                    }
                )
                continue
            update = dict(
                runtime.build_trailing_update(
                    position,
                    dict(runtime.trailing_state(pid) or {}),
                    float(current_price or 0.0),
                    float(atr_price or 0.0),
                    conviction,
                )
                or {}
            )
            payload = update.get("candidate")
            if not isinstance(payload, Mapping):
                continue
            candidate = safety_candidate(
                action="trailing",
                position_id=pid,
                source="legacy_awe_trailing",
                controls=dict(payload.get("controls") or {}),
            )
            if pid in protected:
                arbitration.append(
                    {
                        "fingerprint": candidate.fingerprint,
                        "decision": "superseded",
                        "priority": 50,
                    }
                )
                continue
            selected.append(candidate)
            protected.add(pid)
            arbitration.append(
                {"fingerprint": candidate.fingerprint, "decision": "selected", "priority": 50}
            )

    return SafetyPlan(
        candidates=tuple(selected),
        arbitration=tuple(arbitration),
        planned_at=now_ts,
    )
