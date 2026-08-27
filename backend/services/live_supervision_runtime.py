"""Position-supervision orchestration outside the live-service façade.

The runtime object makes every stateful or broker-facing dependency explicit.
This keeps the serial mutation order testable while preserving the live
service's public entrypoint and its explicit dependency boundaries.
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LiveSupervisionRuntime:
    logger: Any
    strategy_name: str
    ledger: Any
    evaluate_position: Any
    record_aux_failure: Any
    log_trace: Any
    make_candidate: Any
    recently_applied: Any
    delegate_timeout_close: Any
    build_tighten_execution_plan: Any
    build_action_fingerprint: Any
    noop_fingerprint_seen: Any
    remember_noop: Any
    risk_action_for_action: Any
    build_risk_evaluation_inputs: Any
    supervisor_risk_context: Any
    live_state_get: Any
    evaluate_risk_policy: Any
    log_decision: Any
    remember_state: Any
    execute_tighten: Any
    execute_reduce: Any
    execute_close: Any
    build_tighten_result_payloads: Any
    log_position_event: Any
    remember_reentry_block: Any
    track_local_sl_tp: Any
    result_is_position_not_found: Any
    retire_broker_missing_position: Any
    reconcile_positions: Any
    verify_protection_projection: Any
    publish_fresh_positions: Any
    persist_safety_fail_closed: Any
    plan_reduce: Any
    normalize_reduce: Any
    remember_close_reason: Any
    remember_close_verdict: Any
    capture_partial_close_session_cursor: Any
    sync_partial_close_session_fact: Any
    adaptive_duplicate_seen: Any = None


def _is_hard_verdict(verdict: dict[str, Any]) -> bool:
    from backend.services.position_supervisor import is_hard_supervisor_action

    return is_hard_supervisor_action(
        action=str(
            verdict.get("requested_action")
            or verdict.get("action")
            or ""
        ),
        summary_reason=str(verdict.get("summary_reason") or ""),
        evidence=dict(verdict.get("evidence") or {}),
    )


def _enforce_unverified_binding_hold(
    verdict: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    """Keep hard-risk actions available while blocking unverifiable discretion."""

    policy = context.get("position_supervisor_policy")
    if not isinstance(policy, dict):
        return verdict
    binding_state = str(policy.get("binding_state") or "").strip().lower()
    if binding_state not in {"invalid", "unknown"} or _is_hard_verdict(verdict):
        return verdict
    original_requested = str(
        verdict.get("requested_action") or verdict.get("action") or "hold"
    )
    evidence = dict(verdict.get("evidence") or {})
    tags = list(evidence.get("trigger_tags") or [])
    if "binding_unverified" not in tags:
        tags.append("binding_unverified")
    evidence.update(
        {
            "position_supervisor_binding_state": binding_state,
            "binding_fail_closed": True,
            "binding_fail_closed_reason": str(
                policy.get("binding_reason") or "binding_unverified"
            ),
            "position_supervisor_requested_action": original_requested,
            "trigger_tags": tags,
        }
    )
    verdict.update(
        {
            "action": "hold",
            "recommended_action": "hold",
            "effective_action": "hold",
            "summary_reason": "position_supervisor_binding_unverified",
            "recommended_controls": {},
            "protection_candidates": [],
            "requires_risk_verdict": False,
            "evidence": evidence,
        }
    )
    return verdict


@dataclass(frozen=True)
class PositionSupervisorEvaluationRuntime:
    build_context: Any
    evaluate_rule: Any
    get_quality_advisor: Any
    set_quality_advisor: Any
    quality_advisor_factory: Any
    model_influence_service: Any
    build_model_tighten_controls: Any
    load_recovery_row: Any
    upsert_recovery_position: Any
    build_state_upsert_payload: Any
    loop_strategy_name: str
    default_context_integrity: str
    record_aux_failure: Any
    after_persist: Any = None


@dataclass(frozen=True)
class PositionPathMetricsRuntime:
    position_id: Any
    holding_summary: Any
    load_recovery_row: Any
    lookup_entry_context: Any
    build_inputs: Any
    current_regime_hint: Any
    position_unrealized_pnl: Any
    now: Any
    loop_strategy_name: str
    default_context_integrity: str
    build_update: Any
    normalize_path_state: Any
    update_path_metrics: Any
    upsert_recovery_position: Any
    record_aux_failure: Any


def position_path_metrics_for_position(
    position: Any,
    *,
    runtime: PositionPathMetricsRuntime,
    cfg: Any = None,
    now_ts: float | None = None,
    persist: bool = False,
    broker: str = "",
    strategy_name: str = "",
) -> dict[str, Any]:
    """Compute path metrics and make cumulative state explicit when persistence fails."""

    position_id = runtime.position_id(position)
    if position_id <= 0:
        return {}

    holding = runtime.holding_summary(position, cfg=cfg, now_ts=now_ts)
    recovery_row = runtime.load_recovery_row(
        position_id,
        operation="position_path_metrics",
    )
    entry_context = runtime.lookup_entry_context(
        position_id,
        operation="position_path_metrics",
    )
    inputs = runtime.build_inputs(
        position=position,
        recovery_row=recovery_row,
        entry_context=entry_context,
        holding_summary=holding,
        current_regime=runtime.current_regime_hint(),
        current_pnl=runtime.position_unrealized_pnl(position),
        now_ts=float(now_ts or runtime.now()),
        broker=broker,
        strategy_name=strategy_name,
        loop_strategy_name=runtime.loop_strategy_name,
        default_context_integrity=runtime.default_context_integrity,
    )
    path_update = runtime.build_update(
        recovery_meta=inputs["recovery_meta"],
        entry_context=inputs["entry_context"],
        current_pnl=inputs["current_pnl"],
        now_ts=inputs["now_ts"],
        holding_seconds=inputs["holding_seconds"],
        max_holding_seconds=inputs["max_holding_seconds"],
        current_regime=inputs["current_regime"],
        normalize_path_state_fn=runtime.normalize_path_state,
        update_position_path_metrics_fn=runtime.update_path_metrics,
    )

    if persist:
        defaults = inputs["upsert_defaults"]
        try:
            runtime.upsert_recovery_position(
                position,
                broker=defaults["broker"],
                strategy_name=defaults["strategy_name"],
                status=defaults["status"],
                context_integrity=defaults["context_integrity"],
                meta=path_update["next_meta"],
            )
        except Exception as exc:
            runtime.record_aux_failure(
                "risk_reduction_state_persist_failed",
                position_id=position_id,
                action="position_path_metrics",
                error=exc,
            )
            return {
                **path_update["result"],
                "position_path_metrics_state": "unknown",
                "position_path_metrics_reason_code": "position_path_persist_failed",
            }
    return path_update["result"]


def evaluate_position_supervisor_for_position(
    position: dict[str, Any],
    *,
    runtime: PositionSupervisorEvaluationRuntime,
    cfg: Any = None,
    account: dict[str, Any] | None = None,
    now_ts: float | None = None,
    positions: list[Any] | None = None,
    persist: bool = False,
    broker: str = "",
    strategy_name: str = "",
) -> dict[str, Any]:
    """Build, augment and optionally persist one supervisor verdict."""

    context = runtime.build_context(
        position,
        cfg=cfg,
        acct=account,
        now_ts=now_ts,
        positions=positions,
    )
    verdict = runtime.evaluate_rule(context)
    component_states = {
        "price": str(
            position.get("current_price_state")
            or position.get("price_state")
            or ""
        )
        .strip()
        .lower(),
        "pnl": str(
            position.get("pnl_state")
            or position.get("unrealized_pnl_state")
            or ""
        )
        .strip()
        .lower(),
        "path_metrics": str(
            position.get("position_path_metrics_state") or ""
        )
        .strip()
        .lower(),
    }
    unavailable_components = sorted(
        name
        for name, state in component_states.items()
        if state and state != "known"
    )
    if unavailable_components:
        advisory = {
            "ok": False,
            "error": "position_component_unknown",
            "unavailable_components": unavailable_components,
            "component_states": component_states,
        }
    else:
        try:
            advisor = runtime.get_quality_advisor()
            if advisor is None:
                advisor = runtime.quality_advisor_factory()
                runtime.set_quality_advisor(advisor)
            position_policy = runtime.model_influence_service().active_policy(
                "position_quality_lightgbm",
                cfg,
            )
            advisory = advisor.score_position_context(
                context,
                artifact_path=(
                    str((position_policy or {}).get("artifact_path") or "")
                    or None
                ),
            )
        except Exception as exc:
            advisory = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    verdict["position_quality_advisory"] = advisory
    evidence = dict(verdict.get("evidence") or {})
    evidence["position_quality_advisory"] = advisory
    # Older/custom rule evaluators may not emit the lifecycle gate.  The live
    # agent must fail closed for model-originated reduce/tighten actions until
    # the rule has explicitly confirmed the management evidence window.
    evidence.setdefault("model_action_boundary_ready", False)
    verdict["evidence"] = evidence
    if (
        advisory.get("ok")
        and float(advisory.get("exit_risk_score") or 0.0) >= 0.65
    ):
        verdict["model_review_priority"] = "high"
    rule_action = str(verdict.get("action") or "hold").strip().lower()
    rule_evidence = dict(verdict.get("evidence") or {})
    rule_posture = str(
        rule_evidence.get("supervisor_posture") or "unknown_observe"
    ).strip().lower()
    rule_controls = copy.deepcopy(verdict.get("recommended_controls") or {})
    try:
        verdict = runtime.model_influence_service().fuse_position(
            verdict=verdict,
            advisory=advisory,
            position_id=str(
                position.get("position_id") or position.get("ticket") or ""
            ),
            cfg=cfg,
            tighten_controls=runtime.build_model_tighten_controls(context),
        )
        if (
            rule_action == "hold"
            and rule_posture in {
                "trend_hold",
                "unknown_observe",
                "transition_confirming",
            }
            and str(verdict.get("action") or "hold").strip().lower() != "hold"
        ):
            model_payload = dict(verdict.get("model_influence") or {})
            model_payload.update(
                {
                    "applied": False,
                    "stage": "shadow",
                    "reason": "posture_boundary_blocked_model_action",
                    "posture": rule_posture,
                }
            )
            verdict["action"] = "hold"
            verdict["requested_action"] = "hold"
            verdict["recommended_action"] = "hold"
            verdict["effective_action"] = "hold"
            verdict["recommended_controls"] = rule_controls
            verdict["model_influence"] = model_payload
    except Exception as exc:
        verdict["model_influence"] = {
            "schema_version": "model_influence_result.v1",
            "model_type": "position_quality_lightgbm",
            "stage": "shadow",
            "applied": False,
            "reason": f"model_influence_unavailable:{type(exc).__name__}",
        }
    verdict = _enforce_unverified_binding_hold(verdict, context)
    if persist:
        position_id = int(
            position.get("position_id") or position.get("ticket") or 0
        )
        row = runtime.load_recovery_row(
            position_id,
            operation="position_supervisor_evaluation",
        )
        try:
            runtime.upsert_recovery_position(
                position,
                **runtime.build_state_upsert_payload(
                    recovery_row=row,
                    verdict=verdict,
                    broker=broker,
                    strategy_name=strategy_name,
                    loop_strategy_name=runtime.loop_strategy_name,
                    default_context_integrity=runtime.default_context_integrity,
                ),
            )
        except Exception as exc:
            runtime.record_aux_failure(
                "risk_reduction_state_persist_failed",
                position_id=position_id,
                action="position_supervisor_evaluation",
                error=exc,
            )
        if runtime.after_persist is not None:
            verdict = runtime.after_persist(context=context, verdict=verdict)
    return verdict


def run_position_supervision(
    bridge: Any,
    positions: list[Any],
    *,
    cfg: Any,
    account: dict[str, Any],
    tick: int,
    log: Any,
    runtime: LiveSupervisionRuntime,
    skip_position_ids: set[int] | None = None,
    preaudited_skip_position_ids: set[int] | None = None,
    record_partial_close_execution: Any = None,
    decision_ts: float | None = None,
    candidate_recorder: Any = None,
    planned_verdicts: dict[int, dict[str, Any]] | None = None,
) -> set[int]:
    """Evaluate and serially dispatch risk-reducing supervisor actions."""

    handled: set[int] = set()
    skipped = set(skip_position_ids or set())
    preaudited_skips = set(preaudited_skip_position_ids or set())
    cycle_ts = float(decision_ts if decision_ts is not None else time.time())
    if not positions or bridge is None:
        return handled

    for raw in positions:
        position = dict(raw)
        position_id = int(
            position.get("position_id") or position.get("ticket") or 0
        )
        if position_id <= 0:
            continue
        if position_id in skipped:
            handled.add(position_id)
            if position_id not in preaudited_skips:
                runtime.log_trace(
                    position=position,
                    verdict={
                        "action": "hold",
                        "requested_action": "hold",
                        "recommended_action": "hold",
                        "effective_action": "hold",
                        "summary_reason": "position_already_handled_by_higher_priority_stage",
                        "execution_class": "superseded",
                        "decision_ts": cycle_ts,
                    },
                    cfg=cfg,
                    tick=tick,
                    stage="supervisor_superseded",
                    outcome="superseded",
                    execution_status="superseded",
                    execution_reason="position_already_handled_by_higher_priority_stage",
                    execution={
                        "superseded": True,
                        "reason_code": "position_already_handled_by_higher_priority_stage",
                    },
                    acct=account,
                )
            continue
        try:
            if planned_verdicts is not None and position_id in planned_verdicts:
                verdict = copy.deepcopy(planned_verdicts[position_id])
            else:
                verdict = runtime.evaluate_position(
                    position,
                    cfg=cfg,
                    acct=account,
                    now_ts=cycle_ts,
                    positions=positions,
                    persist=True,
                    broker="ctrader",
                    strategy_name=runtime.strategy_name,
                )
        except Exception as exc:
            runtime.record_aux_failure(
                "position_supervisor_evaluation_failed",
                position_id=position_id,
                action="position_supervisor",
                error=exc,
            )
            runtime.logger.warning(
                "[live] supervisor evaluation unavailable for pos %s; other safety stages continue: %s",
                position_id,
                exc,
            )
            runtime.log_trace(
                position=position,
                verdict={
                    "action": "hold",
                    "requested_action": "hold",
                    "recommended_action": "hold",
                    "effective_action": "hold",
                    "summary_reason": "position_supervisor_evaluation_failed",
                    "decision_ts": cycle_ts,
                },
                cfg=cfg,
                tick=tick,
                stage="evaluation_failed",
                outcome="failed",
                execution_status="failed",
                execution_reason="position_supervisor_evaluation_failed",
                execution={"error": f"{type(exc).__name__}: {exc}"},
                acct=account,
            )
            continue

        action = str(verdict.get("action") or "hold").strip().lower()
        requested_action = str(
            verdict.get("requested_action")
            or verdict.get("recommended_action")
            or action
        ).strip().lower()
        verdict["requested_action"] = requested_action
        verdict["recommended_action"] = requested_action
        verdict.setdefault("effective_action", action)
        if action == "hold":
            evidence = dict(verdict.get("evidence") or {})
            trigger_key = "|".join(
                sorted({str(item) for item in evidence.get("trigger_tags") or [] if str(item)})
            )
            # A healthy hold is still a decision event, but the live loop may
            # evaluate it on every tick.  Use only conclusion/evidence-level
            # fields for the persisted fingerprint; volatile quote/PnL fields
            # must not turn one unchanged conclusion into one row per tick.
            hold_signature = ":".join(
                (
                    str(verdict.get("summary_reason") or "position_healthy"),
                    str(evidence.get("supervisor_posture") or ""),
                    str(evidence.get("closed_bar_key") or ""),
                    trigger_key,
                    str(evidence.get("thesis_status") or ""),
                    str(evidence.get("regime_shift") or ""),
                    str(bool(evidence.get("thesis_break_confirmed"))),
                    str(bool(evidence.get("management_evidence_ready"))),
                )
            )
            hold_fingerprint = runtime.build_action_fingerprint(
                position_id=position_id,
                action=f"hold:{hold_signature}",
                direction=int(position.get("direction", 0) or 0),
                controls=dict(verdict.get("recommended_controls") or {}),
            )
            verdict["action_fingerprint"] = hold_fingerprint
            if not runtime.noop_fingerprint_seen(position_id, hold_fingerprint):
                runtime.log_trace(
                    position=position,
                    verdict=verdict,
                    cfg=cfg,
                    tick=tick,
                    stage="evaluated",
                    outcome="hold",
                    execution_status="not_required",
                    acct=account,
                )
                runtime.remember_noop(
                    position,
                    verdict,
                    fingerprint=hold_fingerprint,
                    reason="hold_evaluated",
                )
            continue

        controls = dict(verdict.get("recommended_controls") or {})
        reduce_execution_plan: dict[str, Any] = {}
        if action == "reduce":
            reduce_execution_plan = dict(
                runtime.plan_reduce(
                    bridge=bridge,
                    position=position,
                    verdict=verdict,
                    controls=controls,
                )
                or {}
            )
            # Make broker executability part of the agent's canonical action
            # before any trace, candidate, risk-policy, or state consumer sees
            # the verdict.  A requested reduce is not an effective reduce
            # when the broker cannot trade that volume.
            verdict = runtime.normalize_reduce(verdict, reduce_execution_plan)
            action = str(verdict.get("action") or "hold").strip().lower()
            controls = dict(verdict.get("recommended_controls") or {})
            effective_action = str(
                reduce_execution_plan.get("effective_action") or "hold"
            ).strip().lower()
            if effective_action == "hold":
                handled.add(position_id)
                verdict["action_fingerprint"] = runtime.build_action_fingerprint(
                    position_id=position_id,
                    action="reduce_untradeable",
                    direction=int(position.get("direction", 0) or 0),
                    controls=controls,
                )
                if candidate_recorder is not None:
                    try:
                        candidate_recorder(
                            runtime.make_candidate(
                                action=requested_action,
                                position_id=position_id,
                                source=f"supervisor_{requested_action}",
                                controls=controls,
                            )
                        )
                    except Exception as exc:
                        runtime.record_aux_failure(
                            "safety_candidate_record_failed",
                            position_id=position_id,
                            action=requested_action,
                            error=exc,
                        )
                fingerprint = runtime.build_action_fingerprint(
                    position_id=position_id,
                    action="reduce_untradeable",
                    direction=int(position.get("direction", 0) or 0),
                    controls=controls,
                )
                noop_seen = runtime.noop_fingerprint_seen(position_id, fingerprint)
                if not noop_seen:
                    runtime.log_trace(
                        position=position,
                        verdict=verdict,
                        cfg=cfg,
                        tick=tick,
                        stage="no_op_suppressed",
                        outcome="skipped",
                        execution_status="no_op",
                        execution_reason=str(
                            reduce_execution_plan.get("reason")
                            or "reduce_not_tradeable"
                        ),
                        execution={
                            **reduce_execution_plan,
                            "action_fingerprint": fingerprint,
                            "requested_action": "reduce",
                            "applied_controls": controls,
                            "duplicate_audit": False,
                        },
                        acct=account,
                    )
                    runtime.remember_noop(
                        position,
                        verdict,
                        fingerprint=fingerprint,
                        reason=str(
                            reduce_execution_plan.get("reason")
                            or "reduce_not_tradeable"
                        ),
                    )
                continue

        verdict["effective_action"] = action
        if action in {"close", "reduce", "tighten"} and candidate_recorder is not None:
            try:
                candidate_recorder(
                    runtime.make_candidate(
                        action=requested_action or action,
                        position_id=position_id,
                        source=f"supervisor_{requested_action or action}",
                        controls=controls,
                    )
                )
            except Exception as exc:
                runtime.record_aux_failure(
                    "safety_candidate_record_failed",
                    position_id=position_id,
                    action=action,
                    error=exc,
                )

        handled.add(position_id)
        action_fingerprint = runtime.build_action_fingerprint(
            position_id=position_id,
            action=action,
            direction=int(position.get("direction", 0) or 0),
            controls=controls,
        )
        verdict["action_fingerprint"] = action_fingerprint
        hard_action = _is_hard_verdict(verdict)
        if (
            not hard_action
            and runtime.adaptive_duplicate_seen is not None
            and runtime.adaptive_duplicate_seen(position_id, verdict)
        ):
            continue
        if hard_action and runtime.recently_applied(position_id, action):
            runtime.log_trace(
                position=position,
                verdict=verdict,
                cfg=cfg,
                tick=tick,
                stage="cooldown_skipped",
                outcome="skipped",
                execution_status="cooldown",
                execution_reason="recently_applied_same_action",
                acct=account,
            )
            continue
        if (
            action == "close"
            and str(verdict.get("summary_reason") or "")
            == "holding_timeout_exceeded"
        ):
            runtime.delegate_timeout_close(
                position=position,
                verdict=verdict,
                cfg=cfg,
                tick=tick,
                acct=account,
            )
            continue

        if action == "tighten":
            stop_policy = {
                "quote_max_age_seconds": getattr(
                    cfg, "supervisor_quote_max_age_seconds", 10.0
                ),
                "min_stop_distance_points": getattr(
                    cfg, "supervisor_min_stop_distance_points", 0.20
                ),
                "stop_safety_buffer_ratio": getattr(
                    cfg, "supervisor_stop_safety_buffer_ratio", 0.00008
                ),
                "min_tighten_delta_points": getattr(
                    cfg, "supervisor_min_tighten_delta_points", 0.01
                ),
                "precision": int(position.get("digits", 2) or 2),
                "require_side_quote": True,
            }
            try:
                quote = (
                    bridge.get_spot_quote()
                    if hasattr(bridge, "get_spot_quote")
                    else {}
                )
                preflight = runtime.build_tighten_execution_plan(
                    position=position,
                    controls=controls,
                    quote=quote,
                    policy=stop_policy,
                )
            except Exception as exc:
                runtime.logger.debug(
                    "[live] supervisor tighten preflight unavailable for pos %s: %s",
                    position_id,
                    exc,
                )
                preflight = {}
            sl_plan = preflight.get("sl_plan") or {}
            noop_reasons = {
                "not_tightening_long_stop_loss",
                "not_tightening_short_stop_loss",
                "stop_loss_delta_too_small",
            }
            if (
                not sl_plan.get("allowed")
                and str(sl_plan.get("reason") or "") in noop_reasons
            ):
                fingerprint = runtime.build_action_fingerprint(
                    position_id=position_id,
                    action=action,
                    direction=int(position.get("direction", 0) or 0),
                    controls=controls,
                )
                noop_seen = runtime.noop_fingerprint_seen(position_id, fingerprint)
                if not noop_seen:
                    runtime.log_trace(
                        position=position,
                        verdict=verdict,
                        cfg=cfg,
                        tick=tick,
                        stage="no_op_suppressed",
                        outcome="skipped",
                        execution_status="no_op",
                        execution_reason="target_already_applied",
                        execution={
                            "action_fingerprint": fingerprint,
                            "sl_plan": sl_plan,
                            "applied_controls": controls,
                            "duplicate_audit": False,
                        },
                        acct=account,
                    )
                    runtime.remember_noop(
                        position,
                        verdict,
                        fingerprint=fingerprint,
                        reason=str(sl_plan.get("reason") or ""),
                    )
                continue

        risk_action = runtime.risk_action_for_action(action)
        if not risk_action:
            runtime.log_trace(
                position=position,
                verdict=verdict,
                cfg=cfg,
                tick=tick,
                stage="invalid_action",
                outcome="skipped",
                execution_status="invalid_action",
                execution_reason=action,
                acct=account,
            )
            continue
        risk_inputs = runtime.build_risk_evaluation_inputs(
            action=action,
            risk_context=runtime.supervisor_risk_context(
                position,
                verdict,
                cfg=cfg,
            ),
            loop_running=bool(runtime.live_state_get("loop_running", True)),
            bridge_connected=bool(getattr(bridge, "is_connected", False)),
        )
        risk_context = risk_inputs.get("risk_context") or {}
        risk_verdict = runtime.evaluate_risk_policy(
            risk_action,
            risk_context,
        ).to_dict()
        decision_id = runtime.log_decision(
            position=position,
            verdict=verdict,
            risk_verdict=risk_verdict,
            acct=account,
            cfg=cfg,
            event_type=f"supervisor_{action}",
            tick=tick,
        )
        if not risk_verdict.get("allowed", False):
            runtime.log_trace(
                position=position,
                verdict=verdict,
                cfg=cfg,
                tick=tick,
                stage="risk_rejected",
                outcome="blocked",
                decision_id=decision_id,
                risk_action=risk_action,
                risk_verdict=risk_verdict,
                execution_status="blocked",
                execution_reason=str(risk_verdict.get("reason") or ""),
                acct=account,
            )
            runtime.remember_state(
                position,
                verdict,
                broker="ctrader",
                strategy_name=runtime.strategy_name,
            )
            continue

        try:
            common = {
                "bridge": bridge,
                "position": position,
                "verdict": verdict,
                "risk_action": risk_action,
                "risk_verdict": risk_verdict,
                "decision_id": decision_id,
                "cfg": cfg,
                "tick": tick,
                "acct": account,
                "controls": controls,
                "log": log,
                "broker": "ctrader",
                "strategy_name": runtime.strategy_name,
            }
            if action == "tighten":
                runtime.execute_tighten(
                    **common,
                    build_tighten_execution_plan=runtime.build_tighten_execution_plan,
                    build_tighten_result_payloads=runtime.build_tighten_result_payloads,
                    log_supervisor_position_event=runtime.log_position_event,
                    log_supervisor_trace=runtime.log_trace,
                    remember_supervisor_state=runtime.remember_state,
                    remember_supervisor_reentry_block=runtime.remember_reentry_block,
                    track_local_sl_tp=runtime.track_local_sl_tp,
                    result_is_position_not_found=runtime.result_is_position_not_found,
                    retire_broker_missing_position=runtime.retire_broker_missing_position,
                    record_aux_failure=runtime.record_aux_failure,
                    reconcile_positions=runtime.reconcile_positions,
                    verify_protection_projection=runtime.verify_protection_projection,
                    publish_fresh_positions=runtime.publish_fresh_positions,
                    persist_safety_fail_closed=runtime.persist_safety_fail_closed,
                )
            elif action == "reduce":
                runtime.execute_reduce(
                    **common,
                    ledger=runtime.ledger,
                    execution_plan=reduce_execution_plan,
                    log_supervisor_trace=runtime.log_trace,
                    remember_supervisor_state=runtime.remember_state,
                    remember_supervisor_reentry_block=runtime.remember_reentry_block,
                    remember_close_reason=runtime.remember_close_reason,
                    remember_close_verdict=runtime.remember_close_verdict,
                    result_is_position_not_found=runtime.result_is_position_not_found,
                    retire_broker_missing_position=runtime.retire_broker_missing_position,
                    record_partial_close_execution=record_partial_close_execution,
                    capture_partial_close_session_cursor=(
                        runtime.capture_partial_close_session_cursor
                    ),
                    sync_partial_close_session_fact=(
                        runtime.sync_partial_close_session_fact
                    ),
                    record_aux_failure=runtime.record_aux_failure,
                    reconcile_positions=runtime.reconcile_positions,
                    publish_fresh_positions=runtime.publish_fresh_positions,
                )
            elif action == "close":
                runtime.execute_close(
                    **common,
                    log_supervisor_trace=runtime.log_trace,
                    remember_supervisor_state=runtime.remember_state,
                    remember_supervisor_reentry_block=runtime.remember_reentry_block,
                    remember_close_reason=runtime.remember_close_reason,
                    remember_close_verdict=runtime.remember_close_verdict,
                    result_is_position_not_found=runtime.result_is_position_not_found,
                    retire_broker_missing_position=runtime.retire_broker_missing_position,
                    record_aux_failure=runtime.record_aux_failure,
                    reconcile_positions=runtime.reconcile_positions,
                    publish_fresh_positions=runtime.publish_fresh_positions,
                )
        except Exception as exc:
            runtime.log_trace(
                position=position,
                verdict=verdict,
                cfg=cfg,
                tick=tick,
                stage="exception",
                outcome="failed",
                decision_id=decision_id,
                risk_action=risk_action,
                risk_verdict=risk_verdict,
                execution_status="exception",
                execution_reason=str(exc),
                execution={"applied_controls": controls},
                acct=account,
            )
            runtime.logger.debug(
                "[live] supervisor action %s failed for pos %s: %s",
                action,
                position_id,
                exc,
            )
    return handled
