"""Generation-owned safety and startup orchestration for the live loop.

The functions here contain domain ordering while ``live_service`` supplies
process-local state callbacks.  No broker mutation thread is created here;
all work remains serial in the owning live-loop generation.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from loguru import logger

from backend.services.live_reconciliation import (
    fresh_observation_timestamp,
    reconcile_value,
)
from backend.services.live_safety_plane import SafetyCandidate


def _position_component_fact_payload(result: Any, component: str) -> dict[str, Any]:
    direct = reconcile_value(result, f"{component}_component", None)
    if direct is None:
        components = reconcile_value(result, "components", None)
        if isinstance(components, dict):
            direct = components.get(component)
        elif components is not None and hasattr(components, "get"):
            direct = components.get(component)
    if direct is None:
        return {}
    if is_dataclass(direct):
        return asdict(direct)
    if isinstance(direct, dict):
        return dict(direct)
    return {
        "state": str(reconcile_value(direct, "state", "") or ""),
        "source": str(reconcile_value(direct, "source", "") or ""),
        "observed_at": float(reconcile_value(direct, "observed_at", 0.0) or 0.0),
        "reason_code": str(reconcile_value(direct, "reason_code", "") or ""),
    }


def _explicit_position_component_state(
    result: Any,
    positions: list[dict[str, Any]],
    component: str,
) -> str:
    fact = _position_component_fact_payload(result, component)
    state = str(fact.get("state") or "").strip().lower()
    if state:
        return state
    keys = (
        ("pnl_state", "unrealized_pnl_state")
        if component == "pnl"
        else ("current_price_state", "price_state")
    )
    states = {
        str(position.get(key) or "").strip().lower()
        for position in positions
        for key in keys
        if position.get(key) not in (None, "")
    }
    if not states:
        return ""
    return "known" if states == {"known"} else "unknown"


# A fresh Safety cycle can be complete while a component fact still prevents
# opening additional risk.  Keep this list deliberately small and explicit:
# any blocker not listed here remains a cycle/authority failure and therefore
# keeps the startup barrier closed.
_SAFETY_ADMISSION_ONLY_BLOCKERS = frozenset(
    {
        "broker_position_price_unknown",
        "broker_position_pnl_unknown",
        "factor_warmup",
        "initial_safety_cycle",
        "market_session_blocks_open",
        "session_circuit_breaker",
        "safety_not_accepting_new_risk",
    }
)
_SAFETY_CYCLE_COMPLETION_STATUSES = frozenset(
    {
        # LiveSafetyPlane.run_cycle emits these statuses for a healthy normal
        # cadence: full enforce, heartbeat-only, and migration off/shadow.
        "completed",
        "heartbeat",
        "off",
        "shadow",
    }
)
# ``force_full_cycle`` suppresses heartbeat-only cadence, but the existing
# rollout modes still return ``off``/``shadow`` for a completed full cycle
# (LiveSafetyPlane.run_cycle); they are valid completion statuses, unlike an
# unknown/new status.  Enforce mode returns ``completed``.
_SAFETY_STARTUP_COMPLETION_STATUSES = frozenset({"completed", "off", "shadow"})


def _build_safety_cycle_contract(
    *,
    reconciliation_state: Any,
    status: Any,
    blockers: Any,
    completion_statuses: Any = _SAFETY_CYCLE_COMPLETION_STATUSES,
) -> dict[str, Any]:
    """Separate completion of Safety facts from new-risk admission.

    ``blockers`` is intentionally not treated as a boolean startup verdict.
    Only the explicit admission-only set is allowed to coexist with a
    completed initial cycle.  Unknown blocker names fail closed so adding a
    new Safety failure cannot accidentally open the startup barrier.
    """

    normalized_state = str(reconciliation_state or "unknown").strip().lower()
    normalized_status = str(status or "unknown").strip().lower()
    allowed_statuses = frozenset(
        str(item).strip().lower()
        for item in (completion_statuses or ())
        if str(item or "").strip()
    )
    normalized_blockers = sorted(
        {
            str(item).strip()
            for item in (blockers or ())
            if str(item or "").strip()
        }
    )
    admission_blockers = sorted(
        item for item in normalized_blockers
        if item in _SAFETY_ADMISSION_ONLY_BLOCKERS
    )
    cycle_blockers = sorted(
        item for item in normalized_blockers
        if item not in _SAFETY_ADMISSION_ONLY_BLOCKERS
    )
    failure_reasons: list[str] = []
    if normalized_state != "fresh":
        failure_reasons.append(f"reconciliation_{normalized_state}")
    if normalized_status not in allowed_statuses:
        failure_reasons.append(f"status_{normalized_status}")
    failure_reasons.extend(cycle_blockers)
    return {
        "schema_version": "safety_cycle_contract.v1",
        "status": "complete" if not failure_reasons else "failed",
        "reconciliation_state": normalized_state,
        "cycle_blockers": cycle_blockers,
        "admission_blockers": admission_blockers,
        "failure_reasons": sorted(set(failure_reasons)),
        "admission_only": bool(admission_blockers) and not bool(failure_reasons),
    }


@dataclass(frozen=True)
class LiveSafetyCycleRuntime:
    get_safety_plane: Callable[[str], Any]
    explicit_position_reconcile: Callable[[Any], Any]
    publish_fresh_positions: Callable[..., list[dict[str, Any]]]
    get_live_state: Callable[..., Any]
    update_live_state: Callable[..., None]
    runtime_config: Callable[[], Any]
    safety_reference_price: Callable[[Any, list[dict[str, Any]]], float]
    factor_pipeline: dict[str, Any]
    plan_safety_candidates: Callable[..., Any]
    execute_safety_candidate: Callable[..., dict[str, Any]]
    run_position_protection_cycle: Callable[..., Any]
    persist_safety_fail_closed: Callable[..., dict[str, Any]]
    controller: Any
    record_shadow_observation: Callable[[Mapping[str, Any]], Any] | None = None


def run_live_safety_cycle(
    *,
    bridge: Any,
    broker: str,
    tick: int,
    log,
    runtime: LiveSafetyCycleRuntime,
    generation_id: str = "",
    reconcile_result: Any | None = None,
    force_full_cycle: bool = False,
) -> dict[str, Any]:
    """Run broker snapshot and protection before any alpha work."""

    plane = runtime.get_safety_plane(generation_id)
    result = (
        reconcile_result
        if reconcile_result is not None
        else runtime.explicit_position_reconcile(bridge)
    )
    positions = runtime.publish_fresh_positions(result, broker=broker)
    plane_reconcile_result = result
    if not positions and str(reconcile_value(result, "status", "failed") or "failed") != "fresh":
        positions = list(runtime.get_live_state("positions", [], clone=True) or [])
        if positions:
            plane_reconcile_result = {
                "status": str(reconcile_value(result, "status", "failed") or "failed"),
                "success": False,
                "positions": positions,
                "reconcile_id": str(reconcile_value(result, "reconcile_id", "") or ""),
                "observed_at": float(reconcile_value(result, "observed_at", 0.0) or 0.0),
                "error_code": str(reconcile_value(result, "error_code", "") or ""),
                "error_message": str(reconcile_value(result, "error_message", "") or ""),
            }
    try:
        unknown_count = int(bridge.unresolved_execution_intent_count()) if bridge is not None else 1
        unknown_status_error = False
    except Exception as exc:
        logger.warning("[live] unresolved execution status unavailable: %s", exc)
        unknown_count = 1
        unknown_status_error = True

    due = force_full_cycle or plane.full_cycle_due(
        has_positions=bool(positions),
        unknown_execution_count=unknown_count,
    )
    supervisor_cycle_executed = False
    execution_payload: dict[str, Any] = {"ok": True, "status": "no_positions"}
    cfg: Any | None = None
    account: dict[str, Any] = {}
    current_price = 0.0
    atr_ratio = 0.0
    atr_price = 0.0
    planning_now = datetime.now(timezone.utc).timestamp()
    plan_payload: dict[str, Any] = {
        "candidates": [],
        "arbitration": [],
        "planned_at": planning_now,
    }
    planner_error = ""

    if due and (positions or plane.mode in {"shadow", "enforce"}):
        try:
            cfg = runtime.runtime_config()
            account = runtime.get_live_state("account", {}, clone=True) or {}
            current_price = (
                runtime.safety_reference_price(bridge, positions)
                if positions
                else 0.0
            )
            factor_values = dict(runtime.factor_pipeline.get("last_factor_values") or {})
            atr_ratio = float(factor_values.get("atr_ratio", 0.0) or 0.0)
            atr_price = atr_ratio * current_price if current_price > 0 else 0.0
            if plane.mode in {"shadow", "enforce"}:
                plan = runtime.plan_safety_candidates(
                    bridge=bridge,
                    positions=positions,
                    cfg=cfg,
                    account=account,
                    current_price=current_price,
                    atr_price=atr_price,
                    planned_at=planning_now,
                )
                plan_payload = (
                    dict(plan.to_dict())
                    if hasattr(plan, "to_dict")
                    else dict(plan or {})
                )
        except Exception as exc:
            planner_error = f"{type(exc).__name__}: {exc}"
            logger.warning("[live] independent safety planner failed: {}", planner_error)

    try:
        candidate_preview = [
            item if isinstance(item, SafetyCandidate) else SafetyCandidate(**dict(item))
            for item in list(plan_payload.get("candidates") or [])
        ]
    except Exception as exc:
        candidate_preview = []
        planner_error = planner_error or f"{type(exc).__name__}: {exc}"

    def candidates(_raw_positions) -> list[SafetyCandidate]:
        return list(candidate_preview)

    def execute(candidate: SafetyCandidate) -> dict[str, Any]:
        if cfg is None:
            return {"ok": False, "status": "runtime_config_unavailable"}
        try:
            return dict(
                runtime.execute_safety_candidate(
                    candidate,
                    bridge=bridge,
                    positions=positions,
                    cfg=cfg,
                    account=account,
                    pipeline=runtime.factor_pipeline,
                    current_price=current_price,
                    atr_price=atr_price,
                    tick=tick,
                    log=log,
                    decision_ts=planning_now,
                )
                or {}
            )
        except Exception as exc:
            return {
                "ok": False,
                "status": "exception",
                "error": f"{type(exc).__name__}: {exc}",
            }

    def execute_supervisor_cycle() -> dict[str, Any]:
        nonlocal supervisor_cycle_executed, execution_payload
        if supervisor_cycle_executed:
            return execution_payload
        supervisor_cycle_executed = True
        try:
            effective_cfg = cfg if cfg is not None else runtime.runtime_config()
            effective_account = account or runtime.get_live_state(
                "account", {}, clone=True
            ) or {}
            effective_price = current_price or runtime.safety_reference_price(
                bridge, positions
            )
            effective_atr = atr_price
            if effective_atr <= 0 and effective_price > 0:
                values = dict(runtime.factor_pipeline.get("last_factor_values") or {})
                effective_atr = float(values.get("atr_ratio", 0.0) or 0.0) * effective_price
            result_payload = runtime.run_position_protection_cycle(
                bridge,
                positions,
                cfg=effective_cfg,
                acct=effective_account,
                pipeline=runtime.factor_pipeline,
                current_price=effective_price,
                atr_price=effective_atr,
                tick=tick,
                log=log,
                decision_ts=planning_now,
            )
            execution_payload = {
                "ok": True,
                "status": "completed",
                "result": result_payload,
            }
        except Exception as exc:
            execution_payload = {
                "ok": False,
                "status": "exception",
                "error": f"{type(exc).__name__}: {exc}",
            }
        return execution_payload

    supervisor_first_pid = int(
        positions[0].get("position_id") or positions[0].get("ticket") or 0
    ) if positions else 0
    cycle = plane.run_cycle(
        reconcile_result=plane_reconcile_result,
        unknown_execution_count=unknown_count,
        candidate_provider=candidates,
        executor=execute,
        # The supervisor planner/executor is the only live candidate source;
        # old AWE rows remain audit data and cannot authorize a broker action.
        force_full_cycle=force_full_cycle,
    )

    # The planner/executor is the only candidate authority.  The serial
    # protection cycle below is retained as the off/shadow runtime entrypoint
    # for the same supervisor executor; it is not an AWE fallback stream.
    forced_shadow = bool(
        plane.mode == "enforce" and getattr(plane, "forced_shadow", False)
    )
    fallback_persistence: dict[str, Any] = {}
    fallback_persistence_error = ""
    if due and forced_shadow:
        fallback_blockers = set(cycle.blockers) | {"safety_v2_forced_shadow"}
        if planner_error:
            fallback_blockers.add("safety_candidate_planner_failed")
        try:
            fallback_persistence = dict(
                runtime.persist_safety_fail_closed(
                    blockers=sorted(fallback_blockers),
                    source="safety_v2_forced_shadow",
                    error="; ".join(
                        item for item in (planner_error,) if item
                    ),
                )
                or {}
            )
        except Exception as exc:
            # Persistence failures are themselves fail-closed, but may never
            # suppress close/reduce/tighten on an existing broker position.
            fallback_persistence_error = f"{type(exc).__name__}: {exc}"
    if (
        due
        and positions
        and supervisor_first_pid > 0
        and (plane.mode in {"off", "shadow"} or forced_shadow)
    ):
        execute_supervisor_cycle()

    payload = cycle.to_dict()
    payload["planner"] = {
        **plan_payload,
        "ok": not bool(planner_error),
        "error": planner_error,
        "broker_mutation": False,
    }
    supervisor_result = dict(execution_payload.get("result") or {})
    payload["supervisor_arbitration"] = list(
        supervisor_result.get("safety_arbitration") or []
    )
    # Both scheduling modes below invoke the same governed supervisor
    # executor.  ``off``/``shadow`` only describe whether the cycle is allowed
    # to mutate the broker; they do not create a second safety authority.
    payload["supervisor_executor_authoritative"] = True
    payload["supervisor_execution_path"] = (
        "governed_supervisor_executor"
        if supervisor_cycle_executed
        else "safety_candidate_executor"
    )
    payload["forced_shadow_persistence"] = {
        "ok": bool(fallback_persistence) and not fallback_persistence_error,
        "result": fallback_persistence,
        "error": fallback_persistence_error,
    }
    payload["protection"] = (
        execution_payload if supervisor_cycle_executed else {
            "ok": not any(not bool(item.get("ok")) for item in payload.get("executed", [])),
            "status": "v2_enforced" if payload.get("executed") else "not_due",
        }
    )
    if supervisor_cycle_executed and not bool(execution_payload.get("ok")):
        payload["blockers"] = sorted(
            set(payload.get("blockers", [])) | {"safety_protection_cycle_failed"}
        )
        payload["accepting_new_risk"] = False
    if forced_shadow:
        payload["blockers"] = sorted(
            set(payload.get("blockers", [])) | {"safety_v2_forced_shadow"}
        )
        payload["accepting_new_risk"] = False
    if fallback_persistence_error:
        payload["blockers"] = sorted(
            set(payload.get("blockers", []))
            | {"safety_forced_shadow_persistence_failed"}
        )
        payload["accepting_new_risk"] = False
    if planner_error and plane.mode in {"shadow", "enforce"}:
        payload["blockers"] = sorted(
            set(payload.get("blockers", [])) | {"safety_candidate_planner_failed"}
        )
        payload["accepting_new_risk"] = False
    if positions and any(
        int(item.get("position_id") or item.get("ticket") or 0) <= 0
        for item in positions
    ):
        payload["blockers"] = sorted(
            set(payload.get("blockers", [])) | {"broker_position_identity_missing"}
        )
        payload["accepting_new_risk"] = False
    if unknown_status_error:
        payload["blockers"] = sorted(
            set(payload.get("blockers", [])) | {"unknown_execution_status_unavailable"}
        )
        payload["accepting_new_risk"] = False
    component_facts = {
        component: _position_component_fact_payload(result, component)
        for component in ("identity", "protection", "price", "pnl")
    }
    payload["position_components"] = {
        name: fact for name, fact in component_facts.items() if fact
    }
    if positions:
        price_state = _explicit_position_component_state(result, positions, "price")
        pnl_state = _explicit_position_component_state(result, positions, "pnl")
        component_blockers: set[str] = set()
        if price_state and price_state != "known":
            component_blockers.add("broker_position_price_unknown")
        if pnl_state and pnl_state != "known":
            component_blockers.add("broker_position_pnl_unknown")
        if component_blockers:
            payload["blockers"] = sorted(
                set(payload.get("blockers", [])) | component_blockers
            )
            # Existing-position protection already ran (or remains eligible
            # to run) above.  Only admission of additional risk closes here.
            payload["accepting_new_risk"] = False
    if (
        str(payload.get("mode") or "") == "shadow"
        and str(payload.get("status") or "") != "heartbeat"
        and runtime.record_shadow_observation is not None
    ):
        try:
            runtime.record_shadow_observation(payload)
        except Exception as exc:
            # Observation evidence cannot rewrite a completed broker action.
            # Missing evidence simply prevents the later enforce gate.
            logger.error("[live] safety shadow observation unavailable: %s", exc)
    payload["safety_cycle"] = _build_safety_cycle_contract(
        reconciliation_state=payload.get("reconciliation_state"),
        status=payload.get("status"),
        blockers=payload.get("blockers"),
        completion_statuses=(
            _SAFETY_STARTUP_COMPLETION_STATUSES
            if force_full_cycle
            else _SAFETY_CYCLE_COMPLETION_STATUSES
        ),
    )
    if payload["safety_cycle"]["status"] != "complete":
        payload["accepting_new_risk"] = False
    runtime.update_live_state(
        safety_plane=payload,
        accepting_new_risk=bool(payload.get("accepting_new_risk", False)),
    )
    if generation_id:
        try:
            runtime.controller.heartbeat(generation_id, "safety")
            runtime.controller.update_runtime_health(
                generation_id,
                blockers=tuple(payload.get("blockers", [])),
            )
            runtime.update_live_state(
                accepting_new_risk=runtime.controller.accepting_new_risk(generation_id)
            )
        except RuntimeError as exc:
            logger.warning("[live] safety heartbeat ownership mismatch: %s", exc)
    return payload


@dataclass(frozen=True)
class StartupBarrierRuntime:
    controller: Any
    update_live_state: Callable[..., None]
    get_live_state: Callable[..., Any]
    explicit_position_reconcile: Callable[[Any], Any]
    publish_fresh_positions: Callable[..., list[dict[str, Any]]]
    run_safety_cycle: Callable[..., dict[str, Any]]
    restore_session_state: Callable[..., bool]
    bootstrap_position_recovery: Callable[..., bool]
    factor_pipeline: dict[str, Any]
    strategy_name: str


def _complete_barrier_step(runtime: StartupBarrierRuntime, generation_id: str, step: str) -> None:
    status = runtime.controller.status()
    if not bool((status.get("startup_barrier") or {}).get(step)):
        runtime.controller.complete_barrier_step(generation_id, step)


def _require_fresh_reconcile(
    value: Any,
    *,
    unavailable_error: str,
    timestamp_prefix: str,
) -> float:
    """Validate freshness again at the startup authority boundary.

    The normal caller already passes the explicit reconcile wrappers, but the
    startup barrier is the final gate that authorizes new risk.  It must not
    trust a payload merely because an account/positions value is present, and
    a missing observation timestamp is never equivalent to a fresh snapshot.
    """

    if value is None or str(reconcile_value(value, "status", "failed") or "failed") != "fresh":
        raise RuntimeError(unavailable_error)
    observed_at = float(reconcile_value(value, "observed_at", 0.0) or 0.0)
    if not fresh_observation_timestamp(observed_at):
        suffix = "timestamp_unknown" if observed_at <= 0.0 else "stale"
        raise RuntimeError(f"{timestamp_prefix}_{suffix}")
    return observed_at


def attempt_generation_startup_barrier(
    *,
    generation_id: str,
    bridge: Any,
    broker: str,
    tick: int,
    log,
    account_reconcile: Any,
    positions_reconcile: Any,
    safety_result: dict[str, Any],
    runtime: StartupBarrierRuntime,
) -> bool:
    """Complete the ordered fail-closed startup barrier one attempt at a time."""

    try:
        if bridge is None or not bool(getattr(bridge, "is_connected", False)):
            raise RuntimeError("broker_not_ready")
        _complete_barrier_step(runtime, generation_id, "broker_ready")

        account_observed_at = _require_fresh_reconcile(
            account_reconcile,
            unavailable_error="fresh_account_unavailable",
            timestamp_prefix="fresh_account",
        )
        account = reconcile_value(account_reconcile, "account", None)
        if account is None:
            raise RuntimeError("fresh_account_missing")
        account_payload = asdict(account) if is_dataclass(account) else dict(account)
        account_payload.update({"ok": True, "broker": broker})
        runtime.update_live_state(
            account=account_payload,
            account_reconciled=dict(account_payload),
            account_updated_at=account_observed_at,
            account_reconcile_id=str(
                reconcile_value(account_reconcile, "reconcile_id", "") or ""
            ),
            account_reconcile_failed_at=None,
            account_reconcile_error=None,
        )
        _complete_barrier_step(runtime, generation_id, "fresh_account")

        _require_fresh_reconcile(
            positions_reconcile,
            unavailable_error="fresh_positions_unavailable",
            timestamp_prefix="fresh_positions",
        )
        positions = runtime.publish_fresh_positions(positions_reconcile, broker=broker)
        _complete_barrier_step(runtime, generation_id, "fresh_positions")

        if not hasattr(bridge, "recover_execution_intents"):
            raise RuntimeError("execution_recovery_contract_missing")
        recovery_status = dict(bridge.recover_execution_intents() or {})
        if (
            not bool(recovery_status.get("ready"))
            or int(recovery_status.get("unresolved_count") or 0) != 0
        ):
            raise RuntimeError("unknown_execution_unresolved")
        runtime.update_live_state(execution_recovery=recovery_status)
        _complete_barrier_step(runtime, generation_id, "unknown_execution_recovered")

        # A delayed broker receipt may materialize a position while recovery
        # resolves an intent.  Reconcile again before deriving session state,
        # attaching recovery metadata, or declaring the startup safety cycle
        # complete.
        positions_reconcile = runtime.explicit_position_reconcile(bridge)
        _require_fresh_reconcile(
            positions_reconcile,
            unavailable_error="post_recovery_positions_unavailable",
            timestamp_prefix="post_recovery_positions",
        )
        positions = runtime.publish_fresh_positions(positions_reconcile, broker=broker)

        open_position_ids = {
            int(item.get("position_id") or item.get("ticket") or 0)
            for item in positions
            if int(item.get("position_id") or item.get("ticket") or 0) > 0
        }
        # Resolve broker-missing recovery rows before session authority is
        # projected.  A disappeared position without a concrete close deal is
        # not a zero-PnL trade and must keep the startup barrier closed.  The
        # public barrier step remains ordered after session restore; this
        # preflight only establishes the deal/recovery facts it consumes.
        if not runtime.bootstrap_position_recovery(
            bridge,
            broker=broker,
            strategy_name=runtime.strategy_name,
            log=log,
        ):
            raise RuntimeError("position_recovery_close_deal_unavailable")
        # Bootstrap performs broker/deal recovery work and may span multiple
        # RPCs.  Do not reuse the pre-bootstrap open-ID set: a position can
        # open/close while recovery is attaching.  Session classification and
        # the initial safety cycle share this final fresh snapshot.
        positions_reconcile = runtime.explicit_position_reconcile(bridge)
        _require_fresh_reconcile(
            positions_reconcile,
            unavailable_error="post_bootstrap_positions_unavailable",
            timestamp_prefix="post_bootstrap_positions",
        )
        positions = runtime.publish_fresh_positions(positions_reconcile, broker=broker)
        open_position_ids = {
            int(item.get("position_id") or item.get("ticket") or 0)
            for item in positions
            if int(item.get("position_id") or item.get("ticket") or 0) > 0
        }
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if not runtime.restore_session_state(
            today_str,
            broker_open_position_ids=open_position_ids,
        ) or str(runtime.get_live_state("session_state_status", "")) != "available":
            raise RuntimeError("authoritative_session_restore_unavailable")
        _complete_barrier_step(runtime, generation_id, "session_restored")

        _complete_barrier_step(runtime, generation_id, "recovery_attached")

        safety_result = runtime.run_safety_cycle(
            bridge=bridge,
            broker=broker,
            tick=tick,
            log=log,
            generation_id=generation_id,
            reconcile_result=positions_reconcile,
            force_full_cycle=True,
        )
        safety_contract = dict(safety_result.get("safety_cycle") or {})
        if not safety_contract:
            raise RuntimeError("initial_safety_contract_unavailable")
        if str(safety_contract.get("reconciliation_state") or "") != "fresh":
            raise RuntimeError("initial_safety_reconcile_unavailable")
        if str(safety_contract.get("status") or "") != "complete":
            reasons = ",".join(
                str(item)
                for item in (safety_contract.get("failure_reasons") or ())
            ) or "unknown"
            raise RuntimeError(f"initial_safety_cycle_failed:{reasons}")
        _complete_barrier_step(runtime, generation_id, "initial_safety_cycle")
        admission_blockers = tuple(
            str(item)
            for item in (safety_contract.get("admission_blockers") or ())
            if str(item or "").strip()
        )
        if admission_blockers:
            # The cycle is complete, but this generation must remain
            # admission-blocked until the normal same-owner runtime gate
            # publishes a fresh blocker set.  Do not report a transient
            # ready/accepting edge between these two facts.
            runtime.controller.update_runtime_health(
                generation_id,
                blockers=admission_blockers,
            )
            runtime.update_live_state(
                accepting_new_risk=False,
                startup_safety_admission_blockers=list(admission_blockers),
            )

        engine = runtime.factor_pipeline.get("engine")
        if engine is None or not bool(getattr(engine, "is_warm", False)):
            raise RuntimeError("factor_warmup_incomplete")
        runtime.controller.bind_component(generation_id, "factor_pipeline")
        _complete_barrier_step(runtime, generation_id, "factor_warmup")
    except Exception as exc:
        runtime.controller.mark_degraded(generation_id, str(exc))
        runtime.update_live_state(
            accepting_new_risk=False,
            startup_blocker=str(exc),
        )
        log(f"tick {tick}: startup barrier waiting ({exc})")
        return False

    ready = runtime.controller.accepting_new_risk(generation_id)
    runtime.update_live_state(
        accepting_new_risk=ready,
        startup_blocker="",
    )
    return ready
