"""Pure, broker-mutation-free candidate planning for the live safety plane.

The planner deliberately receives every stateful dependency through
``SafetyPlannerRuntime``.  It may read recovery/config/model projections through
those callbacks, but this module has no broker API and cannot submit or amend an
order.  The governed supervisor executor remains the only live mutation
authority; historical AWE adapters are replay/audit inputs only.
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
        # The canonical broker reconcile must publish an explicit component
        # state.  Missing state is unknown, never an implicit clean fact.
        state = _position_component_state(position, normalized)
        if state != "known":
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
    """Normalize an active protection candidate without importing live_service."""

    source = str(getattr(candidate, "source", "") or "")
    if source == "entry_protection_repair":
        action = "repair_entry_protection"
    elif source == "legacy_awe_trailing":
        raise ValueError("retired_legacy_trailing_candidate")
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
    # Historical replay/audit adapters only.  The live wiring intentionally
    # leaves these unset so retired AWE candidates cannot enter a new cycle.
    build_trailing_update: Callable[
        [Mapping[str, Any], Mapping[str, Any] | None, float, float, float],
        Mapping[str, Any],
    ] | None = None
    trailing_state: Callable[[int], Mapping[str, Any] | None] | None = None
    composite_conviction: Callable[[], float] | None = None
    normalize_supervisor_action: Callable[
        [Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]
    ] | None = None
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
    """Plan timeout > entry repair > governed supervisor, without mutation."""

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
        holding_state = str(context.get("holding_seconds_state") or "known")
        market_budget = dict(context.get("market_time_budget") or {})
        if market_budget:
            timeout_expired = holding_state != "known"
            if not timeout_expired:
                timeout_expired = (
                    bool(market_budget.get("timeout_on_market_time"))
                    if "timeout_on_market_time" in market_budget
                    else _float(market_budget.get("market_open_holding_seconds")) >= limit
                )
        else:
            timeout_expired = held >= limit
        if limit > 0 and timeout_expired:
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
        if runtime.normalize_supervisor_action is not None:
            verdict = dict(
                runtime.normalize_supervisor_action(position, verdict) or verdict
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

    return SafetyPlan(
        candidates=tuple(selected),
        arbitration=tuple(arbitration),
        planned_at=now_ts,
    )


_ls_module = None


def _live_service():
    """Lazy handle to the live loop module (import-order-safe)."""
    global _ls_module
    if _ls_module is None:
        from backend.services import live_service as _module
        _ls_module = _module
    return _ls_module


from backend.services import live_close_settlement
from backend.services.live_position_lifecycle import (
    build_position_supervisor_context_payload as _lifecycle_build_position_supervisor_context_payload,
    build_close_position_risk_context_payload as _lifecycle_build_close_position_risk_context_payload,
    build_position_supervisor_context_inputs as _lifecycle_build_position_supervisor_context_inputs,
)
from backend.services.live_supervision_actions import (
    normalize_supervisor_reduce_verdict as _normalize_supervisor_reduce_verdict,
    plan_supervisor_reduce_action as _plan_supervisor_reduce_action,
)
from backend.services.supervisor_payload_contract import (
    compact_supervisor_mapping as _lifecycle_compact_supervisor_mapping,
)

# moved from live_service (2026-09-12 structural repair)

def safety_reference_price(bridge: Any, positions: list[dict[str, Any]]) -> float:
    try:
        quote = bridge.get_spot_quote() if bridge is not None and hasattr(bridge, "get_spot_quote") else {}
        if quote:
            _live_service().live_state_update(spot_quote=quote)
        if _live_service()._quote_is_fresh(quote):
            price = float(quote.get("mid") or 0.0)
            if price > 0:
                return price
    except Exception:
        pass
    for position in positions:
        for field in ("current_price", "price_current", "entry_price", "price_open", "open_price"):
            try:
                price = float(position.get(field) or 0.0)
            except (TypeError, ValueError):
                price = 0.0
            if price > 0:
                return price
    return float(_live_service().get_latest_price() or 0.0)


def live_safety_planner_runtime(bridge: Any) -> SafetyPlannerRuntime:
    """Build read-only adapters shared by two independent planning algorithms."""

    broker_schedule = _live_service()._broker_schedule_from_bridge(bridge)

    def build_timeout_context(position, effective_cfg, now_ts):
        pid = int(position.get("position_id") or position.get("ticket") or 0)
        timeframe = str(getattr(effective_cfg, "timeframe", "M5") or "M5")
        temporal = _live_service()._temporal_context_for_trade(
            decision_ts=float(now_ts),
            timeframe=timeframe,
        )
        return _lifecycle_build_close_position_risk_context_payload(
            position_id=pid,
            close_reason="holding_timeout",
            mode="live",
            broker="ctrader",
            symbol=str(position.get("symbol") or "XAUUSD+"),
            entry_ts=float(_live_service()._position_open_timestamp(position) or 0.0),
            entry_ts_source="broker_position",
            temporal_context=temporal,
            max_holding_bars=int(
                getattr(effective_cfg, "risk_max_holding_bars", 0) or 0
            ),
            broker_schedule=broker_schedule,
        )

    def load_entry_plan(position_id: int) -> dict[str, Any]:
        try:
            row = live_close_settlement.load_recovery_position_row(int(position_id))
        except Exception:
            return {}
        meta = dict((row or {}).get("recovery_meta") or {})
        return dict(meta.get("entry_protection_plan") or {})

    def evaluate_supervisor_read_only(position, all_positions, effective_cfg, acct, now_ts):
        existing = position.get("supervisor")
        if isinstance(existing, dict) and existing.get("action"):
            return _lifecycle_compact_supervisor_mapping(
                existing,
                nested_keys=frozenset({"evidence", "recommended_controls", "execution"}),
            )
        timeout_context = build_timeout_context(position, effective_cfg, now_ts)
        planner_position = dict(position)
        planner_position["max_holding_seconds"] = float(
            timeout_context.get("max_holding_seconds", 0.0) or 0.0
        )
        planner_position["holding_timeout_ratio"] = float(
            timeout_context.get("holding_timeout_ratio", 0.0) or 0.0
        )
        metric_names = {
            "mfe",
            "mae",
            "giveback_ratio",
            "profit_capture_ratio",
            "time_in_profit",
            "time_in_profit_seconds",
            "holding_efficiency",
            "time_decay_score",
            "thesis_status",
            "regime_shift",
            "entry_regime",
            "current_regime",
        }
        metrics = {
            name: planner_position[name]
            for name in metric_names
            if name in planner_position
        }
        context_inputs = _lifecycle_build_position_supervisor_context_inputs(
            position=planner_position,
            cfg=effective_cfg,
            positions=list(all_positions),
            account=dict(acct or {}),
            entry_decision_id="",
            risk_snapshot=_live_service().live_state_get("risk", {}, clone=True) or {},
            total_api_volume=_live_service()._tracked_total_api_volume(list(all_positions)),
            market_context=_live_service().live_state_get("last_composite", {}, clone=True) or {},
            supervisor_state=dict(
                (
                    _live_service()._load_recovery_row_for_risk_reduction(
                        int(position.get("position_id") or position.get("ticket") or 0),
                        operation="position_supervisor_safety_planner_context",
                    )
                    or {}
                ).get("recovery_meta")
                or {}
            ),
            loop_running=bool(_live_service().live_state_get("loop_running", True)),
        )
        context = _lifecycle_build_position_supervisor_context_payload(
            **context_inputs,
            temporal_context=timeout_context,
            position_metrics=metrics,
        )
        return _live_service().evaluate_position_supervisor(context)

    def normalize_supervisor_action(position, verdict):
        payload = dict(verdict or {})
        if str(payload.get("action") or "").strip().lower() != "reduce":
            return payload
        controls = dict(payload.get("recommended_controls") or {})
        execution_plan = _plan_supervisor_reduce_action(
            bridge=bridge,
            position=dict(position or {}),
            verdict=payload,
            controls=controls,
            floor_api_volume_to_step=_live_service()._floor_api_volume_to_step,
            should_full_close_untradeable_reduce=(
                _live_service()._should_full_close_untradeable_reduce
            ),
        )
        return _normalize_supervisor_reduce_verdict(payload, execution_plan)

    return SafetyPlannerRuntime(
        build_timeout_context=build_timeout_context,
        load_entry_protection_plan=load_entry_plan,
        evaluate_supervisor=evaluate_supervisor_read_only,
        normalize_supervisor_action=normalize_supervisor_action,
    )
