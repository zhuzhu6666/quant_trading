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
    log_evaluation: Any = None


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
                "[live] supervisor evaluation unavailable for pos {}; other safety stages continue: {}",
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
        # Bar-level evaluation ledger: one lean row per position per closed
        # M5 bar regardless of outcome; action traces remain full-fidelity.
        if runtime.log_evaluation is not None:
            try:
                runtime.log_evaluation(
                    position_id=position_id,
                    verdict=verdict,
                    tick=tick,
                    decision_ts=cycle_ts,
                )
            except Exception as exc:
                runtime.record_aux_failure(
                    "supervisor_evaluation_record_failed",
                    position_id=position_id,
                    action="position_supervisor",
                    error=exc,
                )
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
                    "[live] supervisor tighten preflight unavailable for pos {}: {}",
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
                "[live] supervisor action {} failed for pos {}: {}",
                action,
                position_id,
                exc,
            )
    return handled


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
    build_position_supervisor_context_inputs as _lifecycle_build_position_supervisor_context_inputs,
    build_protection_superseded_trace_fields as _lifecycle_build_protection_superseded_trace_fields,
    build_supervisor_close_context_inputs as _lifecycle_build_supervisor_close_context_inputs,
    build_supervisor_decision_ledger_payload as _lifecycle_build_supervisor_decision_ledger_payload,
    build_supervisor_position_event_payload as _lifecycle_build_supervisor_position_event_payload,
    build_supervisor_risk_context_payload as _lifecycle_build_supervisor_risk_context_payload,
    build_supervisor_state_upsert_payload as _lifecycle_build_supervisor_state_upsert_payload,
    build_supervisor_tighten_sl_plan as _lifecycle_build_supervisor_tighten_sl_plan,
    build_supervisor_tighten_sl_plan_inputs as _lifecycle_build_supervisor_tighten_sl_plan_inputs,
    build_supervisor_trace_ledger_payload as _lifecycle_build_supervisor_trace_ledger_payload,
    build_target_tp_extension_inputs as _lifecycle_build_target_tp_extension_inputs,
    enrich_positions_with_lifecycle_metrics as _lifecycle_enrich_positions_with_lifecycle_metrics,
    holding_timeout_is_expired as _lifecycle_holding_timeout_is_expired,
    supervisor_noop_fingerprint_seen as _lifecycle_supervisor_noop_fingerprint_seen,
    supervisor_recently_applied_from_meta as _lifecycle_supervisor_recently_applied_from_meta,
    supervisor_reentry_cooldown_seconds as _lifecycle_supervisor_reentry_cooldown_seconds,
    target_tp_is_extension as _lifecycle_target_tp_is_extension,
)
from backend.services.live_position_protection_cycle import ProtectionCandidate
from backend.services.position_supervisor import is_hard_supervisor_action
from backend.services.position_supervisor_governance import POSITION_SUPERVISOR_SELECTION_PROJECTION_KEY, select_position_supervisor_binding
from backend.services.position_supervisor_templates import build_legacy_position_supervisor_binding, build_position_supervisor_binding, get_position_supervisor_template, verify_position_supervisor_binding
from typing import Mapping
from dataclasses import asdict

# moved from live_service (2026-09-12 structural repair)

def supervisor_reentry_cooldown_seconds(cfg) -> float:
    return _lifecycle_supervisor_reentry_cooldown_seconds(
        cooldown_bars=getattr(cfg, "risk_supervisor_reentry_cooldown_bars", 3),
        timeframe=str(getattr(cfg, "timeframe", "M5") or "M5"),
        timeframe_seconds=_live_service()._timeframe_seconds,
    )


def _position_supervisor_selection_key(
    *,
    cfg: Any,
    composite: Any,
) -> dict[str, str]:
    quality = _live_service()._decision_quality_context(composite)
    current_regime = str(
        getattr(composite, "regime_id", "")
        or quality.get("regime_id")
        or _live_service()._current_regime_hint()
        or "unknown"
    )
    symbol = str(
        getattr(composite, "symbol", "")
        or _live_service().live_state_get("symbol", "")
        or "XAUUSD+"
    )
    timeframe = str(getattr(cfg, "timeframe", "") or "M5")
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "entry_regime": current_regime,
        "current_regime": current_regime,
    }


def _static_position_supervisor_binding(
    *,
    cfg: Any,
    composite: Any,
    reason: str,
) -> dict[str, Any]:
    template_id = str(
        getattr(cfg, "position_supervisor_template_id", "")
        or "position_supervisor:default.v1"
    )
    template = get_position_supervisor_template(template_id)
    source = (
        "static_baseline"
        if template_id == "position_supervisor:default.v1"
        else "governed_global_baseline"
    )
    return build_position_supervisor_binding(
        template,
        binding_source=source,
        selection_status="bound",
        selection_key=_position_supervisor_selection_key(
            cfg=cfg,
            composite=composite,
        ),
        evidence_refs={"reason": str(reason or "static_baseline")},
    )


def select_position_supervisor_binding_for_open(
    *,
    cfg: Any,
    composite: Any,
) -> dict[str, Any]:
    """Select once before an open, without creating a broker-side mutation."""

    mode = str(
        getattr(cfg, "position_supervisor_auto_selection_mode", "off") or "off"
    ).strip().lower()
    static_binding = _static_position_supervisor_binding(
        cfg=cfg,
        composite=composite,
        reason=(
            "selection_disabled"
            if mode == "off"
            else "selection_mode_not_executable"
        ),
    )
    if mode not in {"shadow", "demo_execute"}:
        static_binding["evidence_refs"]["selection_status"] = "no_change"
        static_binding["evidence_refs"]["selection_reason"] = (
            "selection_disabled"
        )
        return static_binding
    if mode == "demo_execute" and not _live_service().bounded_demo_mode_active(cfg):
        static_binding["evidence_refs"]["selection_reason"] = "bounded_demo_required"
        return static_binding
    try:
        projection = live_close_settlement.runtime_kv_get(POSITION_SUPERVISOR_SELECTION_PROJECTION_KEY, {})
        selection = select_position_supervisor_binding(
            projection if isinstance(projection, dict) else {},
            **_position_supervisor_selection_key(cfg=cfg, composite=composite),
            current_binding=None,
            max_age_seconds=float(
                getattr(cfg, "position_supervisor_selection_max_age_seconds", 900.0)
                or 900.0
            ),
        )
    except Exception as exc:
        static_binding["evidence_refs"]["selection_reason"] = (
            f"selection_projection_unavailable:{type(exc).__name__}"
        )
        return static_binding
    selected = dict(selection.get("binding") or {})
    if mode == "shadow":
        static_binding["evidence_refs"]["shadow_selection"] = {
            "reason": str(selection.get("reason") or ""),
            "ok": bool(selection.get("ok")),
            "selection_event_id": str(selection.get("selection_event_id") or ""),
            "template_id": str(selected.get("template_id") or ""),
            "template_hash": str(selected.get("template_hash") or ""),
        }
        return static_binding
    if (
        not selection.get("ok")
        or not selected
        or str(selection.get("reason") or "")
        != "selected_highest_positive_effect"
    ):
        static_binding["evidence_refs"]["selection_reason"] = str(
            selection.get("reason") or "selection_not_available"
        )
        return static_binding
    selected_check = verify_position_supervisor_binding(selected)
    if not selected_check.get("valid"):
        static_binding["evidence_refs"]["selection_reason"] = (
            "selected_binding_invalid"
        )
        return static_binding
    selected["evidence_refs"] = {
        **dict(selected.get("evidence_refs") or {}),
        "selection_event_id": str(selection.get("selection_event_id") or ""),
    }
    return selected


def _position_supervisor_policy_for_position(
    *,
    cfg: Any,
    supervisor_state: dict[str, Any],
    position: dict[str, Any],
    position_metrics: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve one position's snapshot and its fail-closed policy state."""

    plan = dict(supervisor_state.get("entry_protection_plan") or {})
    raw_binding = plan.get("supervisor_binding")
    symbol = str(position.get("symbol") or "XAUUSD+")
    selection_key = {
        "symbol": symbol,
        "timeframe": str(getattr(cfg, "timeframe", "") or "M5"),
        "entry_regime": str(
            position_metrics.get("entry_regime")
            or supervisor_state.get("entry_regime")
            or ""
        ),
        "current_regime": str(
            position_metrics.get("current_regime")
            or supervisor_state.get("current_regime")
            or ""
        ),
    }
    if isinstance(raw_binding, dict):
        checked = verify_position_supervisor_binding(raw_binding)
        if checked.get("valid"):
            binding = dict(checked.get("binding") or raw_binding)
            template = dict(checked.get("template") or {})
            return template, {
                "schema_version": "position_supervisor_policy.v1",
                "binding_state": "bound",
                "binding_reason": "binding_verified",
                "binding_source": str(binding.get("binding_source") or ""),
                "template_id": str(binding.get("template_id") or ""),
                "template_version": str(binding.get("template_version") or ""),
                "template_hash": str(binding.get("template_hash") or ""),
                "selection_event_id": str(
                    dict(binding.get("evidence_refs") or {}).get(
                        "selection_event_id", ""
                    )
                ),
                "posterior_fingerprint": str(
                    binding.get("posterior_fingerprint") or ""
                ),
                "selection_key": dict(binding.get("selection_key") or selection_key),
                "binding": binding,
            }
        if checked.get("state") == "legacy":
            template_id = str(
                raw_binding.get("template_id")
                or getattr(cfg, "position_supervisor_template_id", "")
                or "position_supervisor:default.v1"
            )
            return get_position_supervisor_template(template_id), {
                "schema_version": "position_supervisor_policy.v1",
                "binding_state": "legacy",
                "binding_reason": "legacy_global_fallback",
                "binding_source": "legacy_global_fallback",
                "template_id": template_id,
                "template_version": "",
                "template_hash": "",
                "selection_event_id": "",
                "posterior_fingerprint": "",
                "selection_key": selection_key,
                "binding": raw_binding,
            }
        # The template is deliberately not taken from a corrupted snapshot.
        # Hard-risk evaluation still uses the safe built-in baseline, while
        # the runtime evaluator blocks discretionary actions below.
        return get_position_supervisor_template("position_supervisor:default.v1"), {
            "schema_version": "position_supervisor_policy.v1",
            "binding_state": "invalid",
            "binding_reason": str(
                checked.get("reason") or "binding_unverified"
            ),
            "binding_source": str(raw_binding.get("binding_source") or ""),
            "template_id": str(raw_binding.get("template_id") or ""),
            "template_version": str(raw_binding.get("template_version") or ""),
            "template_hash": str(raw_binding.get("template_hash") or ""),
            "selection_event_id": "",
            "posterior_fingerprint": "",
            "selection_key": selection_key,
            "binding": raw_binding,
        }
    template_id = str(
        getattr(cfg, "position_supervisor_template_id", "")
        or "position_supervisor:default.v1"
    )
    legacy = build_legacy_position_supervisor_binding(
        template_id,
        selection_key=selection_key,
    )
    return get_position_supervisor_template(template_id), {
        "schema_version": "position_supervisor_policy.v1",
        "binding_state": "legacy",
        "binding_reason": "legacy_global_fallback",
        "binding_source": "legacy_global_fallback",
        "template_id": template_id,
        "template_version": "",
        "template_hash": "",
        "selection_event_id": "",
        "posterior_fingerprint": "",
        "selection_key": selection_key,
        "binding": legacy,
    }


def _position_supervisor_switch_state_payload(
    state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    current = dict(state or {})
    try:
        switch_count = max(0, int(current.get("switch_count") or 0))
    except (TypeError, ValueError):
        switch_count = 0
    try:
        stable_bars = max(0, int(current.get("stable_bars") or 0))
    except (TypeError, ValueError):
        stable_bars = 0
    try:
        last_switch_bar_number = int(current.get("last_switch_bar_number"))
    except (TypeError, ValueError):
        last_switch_bar_number = -1
    return {
        "schema_version": "position_supervisor_switch.v1",
        "candidate_regime": str(current.get("candidate_regime") or ""),
        "candidate_bar_key": str(current.get("candidate_bar_key") or ""),
        "last_seen_bar_key": str(current.get("last_seen_bar_key") or ""),
        "stable_bars": stable_bars,
        "switch_count": switch_count,
        "last_switch_bar_number": last_switch_bar_number,
        "last_switch_bar_key": str(current.get("last_switch_bar_key") or ""),
        "last_switch_ts": float(current.get("last_switch_ts") or 0.0),
        "last_selection_bar_key": str(current.get("last_selection_bar_key") or ""),
        "last_selection_reason": str(current.get("last_selection_reason") or ""),
    }


def _position_supervisor_switch_block_reason(
    *,
    cfg: Any,
    context: Mapping[str, Any],
    verdict: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> str:
    """Return a conservative reason when a soft policy switch is unsafe."""

    selection_mode = str(
        getattr(cfg, "position_supervisor_auto_selection_mode", "off") or "off"
    ).strip().lower()
    if selection_mode not in {"shadow", "demo_execute"}:
        return "selection_mode_not_demo_execute"
    if selection_mode == "demo_execute" and not _live_service().bounded_demo_mode_active(cfg):
        return "bounded_demo_required"
    if str(policy.get("binding_state") or "").strip().lower() != "bound":
        return "position_binding_not_verified"
    binding = policy.get("binding")
    if not isinstance(binding, Mapping) or not verify_position_supervisor_binding(binding).get("valid"):
        return "position_binding_not_verified"
    evidence = dict(verdict.get("evidence") or {})
    action = str(
        verdict.get("requested_action")
        or verdict.get("action")
        or "hold"
    ).strip().lower()
    if action != "hold":
        return "active_supervisor_action_has_priority"
    if is_hard_supervisor_action(
        action=action,
        summary_reason=str(verdict.get("summary_reason") or ""),
        evidence=evidence,
    ):
        return "hard_risk_has_priority"
    if bool(evidence.get("hard_risk_active")):
        return "hard_risk_has_priority"
    blocked_tags = {
        "hard_risk_active",
        "holding_timeout_exceeded",
        "near_stop_loss",
        "thesis_broken",
        "regime_shift_detected",
    }
    if blocked_tags.intersection(str(item) for item in evidence.get("trigger_tags") or []):
        return "risk_reduction_has_priority"
    position = dict(context.get("position") or {})
    required_states = {
        "price": str(
            position.get("current_price_state") or position.get("price_state") or ""
        ).strip().lower(),
        "pnl": str(
            position.get("pnl_state") or position.get("unrealized_pnl_state") or ""
        ).strip().lower(),
        "path_metrics": str(
            position.get("position_path_metrics_state") or ""
        ).strip().lower(),
    }
    if any(state != "known" for state in required_states.values()):
        return "position_management_facts_unknown"
    market = dict(context.get("market") or {})
    if str(market.get("market_context_state") or "unknown").strip().lower() != "known":
        return "market_context_unknown"
    if not bool(evidence.get("market_dimensions_known")):
        return "market_dimensions_unknown"
    temporal = dict(context.get("temporal_context") or {})
    if not str(
        evidence.get("closed_bar_key")
        or temporal.get("closed_bar_key")
        or temporal.get("closed_bar_ts")
        or ""
    ):
        return "closed_bar_unknown"
    execution_recovery = _live_service().live_state_get("execution_recovery", {}, clone=True) or {}
    if not bool(execution_recovery.get("ready")):
        return "execution_recovery_not_ready"
    try:
        unresolved_count = int(execution_recovery.get("unresolved_count"))
    except (TypeError, ValueError):
        return "execution_recovery_unknown"
    if unresolved_count != 0:
        return "unresolved_execution_intent"
    safety = _live_service().live_state_get("safety_plane", {}, clone=True) or {}
    if str(safety.get("reconciliation_state") or "unknown").strip().lower() != "fresh":
        return "positions_reconciliation_not_fresh"
    if list(safety.get("blockers") or []):
        return "safety_blocker_present"
    if bool(_live_service().live_state_get("safety_cycle_active", False)):
        return "safety_cycle_in_progress"
    return ""


def maybe_switch_position_supervisor_binding(
    *,
    position: dict[str, Any],
    cfg: Any,
    context: Mapping[str, Any],
    verdict: dict[str, Any],
    now_ts: float,
) -> dict[str, Any]:
    """Switch one bound position only after a stable, safe regime boundary."""

    policy = context.get("position_supervisor_policy")
    if not isinstance(policy, Mapping):
        return verdict
    block_reason = _position_supervisor_switch_block_reason(
        cfg=cfg,
        context=context,
        verdict=verdict,
        policy=policy,
    )
    if block_reason:
        return verdict
    evidence = dict(verdict.get("evidence") or {})
    current_regime = str(
        evidence.get("current_regime")
        or (context.get("market") or {}).get("regime_id")
        or (context.get("risk") or {}).get("current_regime")
        or ""
    ).strip()
    if not current_regime or current_regime.lower() in {"unknown", "none", "unavailable"}:
        return verdict
    binding = dict(policy.get("binding") or {})
    selection_key = dict(binding.get("selection_key") or {})
    previous_regime = str(selection_key.get("current_regime") or "").strip()
    if not previous_regime or previous_regime.lower() in {"unknown", "none", "unavailable"}:
        return verdict
    if current_regime == previous_regime:
        return verdict

    closed_bar_key = str(
        evidence.get("closed_bar_key")
        or (context.get("temporal_context") or {}).get("closed_bar_key")
        or ""
    )
    try:
        bar_number = int(evidence.get("completed_bars_after_entry"))
    except (TypeError, ValueError):
        return verdict
    if not closed_bar_key or bar_number < 0:
        return verdict
    position_id = int(position.get("position_id") or position.get("ticket") or 0)
    if position_id <= 0:
        return verdict

    row = _live_service()._load_recovery_row_for_risk_reduction(
        position_id,
        operation="position_supervisor_binding_switch",
    )
    meta = copy.deepcopy(dict((row or {}).get("recovery_meta") or {}))
    state = _position_supervisor_switch_state_payload(
        meta.get("supervisor_switch_state")
    )
    if state["candidate_regime"] != current_regime:
        state["candidate_regime"] = current_regime
        state["candidate_bar_key"] = closed_bar_key
        state["stable_bars"] = 1
    elif state["last_seen_bar_key"] != closed_bar_key:
        state["stable_bars"] = int(state["stable_bars"] or 0) + 1
        state["candidate_bar_key"] = closed_bar_key
    state["last_seen_bar_key"] = closed_bar_key
    min_stable_bars = max(
        1,
        int(getattr(cfg, "position_supervisor_switch_min_stable_bars", 2) or 2),
    )
    plan = dict(meta.get("entry_protection_plan") or {})
    persisted_binding = plan.get("supervisor_binding")
    if not isinstance(persisted_binding, Mapping):
        return verdict
    persisted_check = verify_position_supervisor_binding(persisted_binding)
    if not persisted_check.get("valid") or str(
        persisted_binding.get("template_hash") or ""
    ) != str(binding.get("template_hash") or ""):
        return verdict
    state_changed = state != _position_supervisor_switch_state_payload(
        meta.get("supervisor_switch_state")
    )

    def persist_state() -> None:
        if state_changed:
            live_close_settlement.merge_recovery_position_meta(
                position_id,
                {"supervisor_switch_state": state},
            )

    if int(state["stable_bars"] or 0) < min_stable_bars:
        persist_state()
        return verdict
    if state["last_selection_bar_key"] == closed_bar_key:
        persist_state()
        return verdict
    max_switches = max(
        0,
        int(getattr(cfg, "position_supervisor_max_switches_per_position", 2) or 0),
    )
    if max_switches <= 0 or int(state["switch_count"] or 0) >= max_switches:
        state["last_selection_bar_key"] = closed_bar_key
        state["last_selection_reason"] = "max_switches_reached"
        live_close_settlement.merge_recovery_position_meta(position_id, {"supervisor_switch_state": state})
        return verdict
    cooldown_bars = max(
        0,
        int(getattr(cfg, "position_supervisor_switch_cooldown_bars", 3) or 0),
    )
    if (
        int(state["last_switch_bar_number"] or -1) >= 0
        and bar_number - int(state["last_switch_bar_number"]) < cooldown_bars
    ):
        state["last_selection_bar_key"] = closed_bar_key
        state["last_selection_reason"] = "switch_cooldown"
        live_close_settlement.merge_recovery_position_meta(position_id, {"supervisor_switch_state": state})
        return verdict

    try:
        projection = live_close_settlement.runtime_kv_get(POSITION_SUPERVISOR_SELECTION_PROJECTION_KEY, {})
        selection = select_position_supervisor_binding(
            projection if isinstance(projection, dict) else {},
            symbol=str(position.get("symbol") or "XAUUSD+"),
            timeframe=str(getattr(cfg, "timeframe", "M5") or "M5"),
            entry_regime=str(selection_key.get("entry_regime") or ""),
            current_regime=current_regime,
            current_binding=binding,
            now_ts=now_ts,
            max_age_seconds=float(
                getattr(cfg, "position_supervisor_selection_max_age_seconds", 900.0)
                or 900.0
            ),
        )
    except Exception as exc:
        selection = {
            "ok": False,
            "changed": False,
            "reason": f"selection_projection_unavailable:{type(exc).__name__}",
        }
    state["last_selection_bar_key"] = closed_bar_key
    state["last_selection_reason"] = str(selection.get("reason") or "selection_not_available")
    selected = dict(selection.get("selected_binding") or selection.get("binding") or {})
    selected_check = verify_position_supervisor_binding(selected)
    selection_event_id = str(selection.get("selection_event_id") or "")
    selection_mode = str(
        getattr(cfg, "position_supervisor_auto_selection_mode", "off") or "off"
    ).strip().lower()
    if selection_mode == "shadow":
        if not _live_service()._LEDGER:
            state["last_selection_reason"] = "supervisor_trace_sink_unavailable"
            live_close_settlement.merge_recovery_position_meta(
                position_id,
                {"supervisor_switch_state": state},
            )
            return verdict
        shadow_verdict = copy.deepcopy(verdict)
        shadow_evidence = dict(shadow_verdict.get("evidence") or {})
        shadow_evidence.update(
            {
                "selection_event_id": selection_event_id,
                "position_supervisor_selection": {
                    "schema_version": "position_supervisor_selection_observation.v1",
                    "ok": bool(selection.get("ok")),
                    "changed": bool(selection.get("changed")),
                    "reason": str(selection.get("reason") or "selection_not_available"),
                    "selected_template_id": str(selected.get("template_id") or ""),
                    "selected_template_version": str(
                        selected.get("template_version") or ""
                    ),
                    "selected_template_hash": str(selected.get("template_hash") or ""),
                    "selection_event_id": selection_event_id,
                },
            }
        )
        if selected_check.get("valid"):
            shadow_evidence["position_supervisor_selection"]["selected_binding"] = selected
        shadow_verdict["evidence"] = shadow_evidence
        shadow_trace_id = log_supervisor_trace(
            position=position,
            verdict=shadow_verdict,
            cfg=cfg,
            tick=int(evidence.get("tick") or 0),
            stage="selection_shadow",
            outcome="shadow",
            execution_status="shadow_only",
            execution_reason=str(
                selection.get("reason") or "selection_not_available"
            ),
            execution={
                "policy_switch_status": "shadow",
                "selection_event_id": selection_event_id,
                "selected_binding": selected if selected_check.get("valid") else {},
                "broker_action_attempted": False,
                "is_real_execution": False,
                "no_change_reason": str(selection.get("reason") or ""),
            },
            acct=_live_service().live_state_get("account", {}, clone=True) or {},
        )
        state["last_selection_reason"] = (
            f"shadow:{selection.get('reason') or 'selection_not_available'}"
            if shadow_trace_id
            else "supervisor_trace_persist_failed"
        )
        live_close_settlement.merge_recovery_position_meta(
            position_id,
            {"supervisor_switch_state": state},
        )
        return verdict
    if (
        not selection.get("ok")
        or not selection.get("changed")
        or not selection_event_id
        or not selected_check.get("valid")
        or str(selected.get("template_hash") or "")
        == str(binding.get("template_hash") or "")
    ):
        live_close_settlement.merge_recovery_position_meta(position_id, {"supervisor_switch_state": state})
        return verdict

    if not _live_service()._LEDGER:
        state["last_selection_reason"] = "supervisor_trace_sink_unavailable"
        live_close_settlement.merge_recovery_position_meta(position_id, {"supervisor_switch_state": state})
        return verdict

    new_template = dict(selected_check.get("template") or {})
    switch_ts = float(now_ts or time.time())
    old_binding = dict(persisted_binding)
    previous_history = [
        dict(item)
        for item in list(meta.get("position_supervisor_binding_history") or [])
        if isinstance(item, Mapping)
    ]
    history = [old_binding, *previous_history][:3]
    next_plan = dict(plan)
    next_plan.update(
        {
            "supervisor_binding": selected,
            "supervisor_binding_previous": old_binding,
            "supervisor_binding_switched_at": switch_ts,
        }
    )
    state["switch_count"] = int(state["switch_count"] or 0) + 1
    state["last_switch_bar_number"] = bar_number
    state["last_switch_bar_key"] = closed_bar_key
    state["last_switch_ts"] = switch_ts
    state["last_selection_reason"] = "selected_highest_positive_effect"
    next_meta = {
        "entry_protection_plan": next_plan,
        "position_supervisor_binding_history": history,
        "supervisor_switch_state": state,
        "position_supervisor_last_switch": {
            "selection_event_id": selection_event_id,
            "previous_template_id": str(old_binding.get("template_id") or ""),
            "previous_template_hash": str(old_binding.get("template_hash") or ""),
            "template_id": str(selected.get("template_id") or ""),
            "template_hash": str(selected.get("template_hash") or ""),
            "switched_at": switch_ts,
            "regime": current_regime,
        },
    }
    live_close_settlement.merge_recovery_position_meta(position_id, next_meta)

    switch_evidence = dict(evidence)
    switch_evidence.update(
        {
            "position_supervisor_binding": selected,
            "previous_position_supervisor_binding": old_binding,
            "position_supervisor_binding_state": "bound",
            "binding_source": str(selected.get("binding_source") or ""),
            "supervisor_template_hash": str(selected.get("template_hash") or ""),
            "selection_event_id": selection_event_id,
            "current_regime": current_regime,
            "policy_switch_status": "applied",
        }
    )
    switch_verdict = copy.deepcopy(verdict)
    switch_verdict["supervisor_template"] = new_template
    switch_verdict["evidence"] = switch_evidence
    switch_verdict["position_supervisor_policy"] = {
        **dict(policy),
        "binding_state": "bound",
        "binding_reason": "policy_switch_applied",
        "binding_source": str(selected.get("binding_source") or ""),
        "template_id": str(selected.get("template_id") or ""),
        "template_version": str(selected.get("template_version") or ""),
        "template_hash": str(selected.get("template_hash") or ""),
        "selection_event_id": selection_event_id,
        "binding": selected,
    }
    trace_id = log_supervisor_trace(
        position=position,
        verdict=switch_verdict,
        cfg=cfg,
        tick=int(evidence.get("tick") or 0),
        stage="policy_switch",
        outcome="applied",
        execution_status="no_op",
        execution_reason="position_supervisor_binding_switched",
        execution={
            "policy_switch_status": "applied",
            "selection_event_id": selection_event_id,
            "previous_binding": old_binding,
            "binding": selected,
            "broker_action_attempted": False,
            "is_real_execution": False,
            "no_change_reason": "",
        },
        acct=_live_service().live_state_get("account", {}, clone=True) or {},
    )
    if not trace_id:
        # Do not leave a binding that cannot be proven by a trace.  Restore the
        # previous object and all switch-owned metadata with a CAS.  Do not
        # overwrite an unrelated concurrent recovery update.
        latest_row = live_close_settlement.load_recovery_position_row(position_id)
        latest_meta = copy.deepcopy(dict((latest_row or {}).get("recovery_meta") or {}))
        restored_meta = dict(latest_meta)
        for key in (
            "entry_protection_plan",
            "position_supervisor_binding_history",
            "supervisor_switch_state",
            "position_supervisor_last_switch",
        ):
            if key in meta:
                restored_meta[key] = copy.deepcopy(meta[key])
            else:
                restored_meta.pop(key, None)
        restored = _live_service()._replace_recovery_position_meta(
            position_id,
            restored_meta,
            expected_meta=latest_meta,
        )
        if not restored:
            _live_service().logger.warning(
                "position supervisor binding rollback CAS failed position_id={}",
                position_id,
            )
        return verdict
    switch_evidence.pop("policy_switch_status", None)
    verdict["evidence"] = switch_evidence
    verdict["position_supervisor_policy"] = switch_verdict["position_supervisor_policy"]
    verdict["supervisor_template"] = new_template
    return verdict


def build_position_supervisor_context(
    position: dict[str, Any],
    *,
    cfg=None,
    acct: dict | None = None,
    now_ts: float | None = None,
    positions: list[Any] | None = None,
    broker_schedule: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now_ts = float(now_ts or time.time())
    temporal_context = _live_service()._build_close_position_risk_context(
        position_id=int(position.get("position_id") or position.get("ticket") or 0),
        close_reason="position_supervisor",
        mode="supervisor",
        symbol=str(position.get("symbol") or "XAUUSD+"),
        position=position,
        cfg=cfg,
        decision_ts=now_ts,
        broker_schedule=broker_schedule,
    )
    position_metrics = _live_service()._position_path_metrics_for_position(position, cfg=cfg, now_ts=now_ts, persist=False)
    supervisor_row = _live_service()._load_recovery_row_for_risk_reduction(
        int(position.get("position_id") or position.get("ticket") or 0),
        operation="position_supervisor_context",
    )
    supervisor_state = dict((supervisor_row or {}).get("recovery_meta") or {})
    supervisor_template, supervisor_policy = _position_supervisor_policy_for_position(
        cfg=cfg,
        supervisor_state=supervisor_state,
        position=position,
        position_metrics=position_metrics,
    )
    context_inputs = _lifecycle_build_position_supervisor_context_inputs(
        position=position,
        cfg=cfg,
        positions=positions,
        account=acct,
        entry_decision_id=_live_service()._lookup_entry_decision_for_risk_reduction(
            int(position.get("position_id") or position.get("ticket") or 0),
            operation="position_supervisor_context",
        ),
        risk_snapshot=_live_service().live_state_get("risk", {}, clone=True) or {},
        total_api_volume=_live_service()._tracked_total_api_volume(positions or []),
        market_context=_live_service().live_state_get("last_composite", {}, clone=True) or {},
        supervisor_state=supervisor_state,
        loop_running=bool(_live_service().live_state_get("loop_running", True)),
        position_supervisor_template=supervisor_template,
        position_supervisor_policy=supervisor_policy,
    )
    return _lifecycle_build_position_supervisor_context_payload(
        **context_inputs,
        temporal_context=temporal_context,
        position_metrics=position_metrics,
    )


def enrich_positions_with_path_metrics(
    pos_list: list[Any],
    *,
    cfg=None,
    now_ts: float | None = None,
    persist: bool = False,
    broker: str = "",
    strategy_name: str = "",
) -> list[dict]:
    now_ts = float(now_ts or time.time())
    return _lifecycle_enrich_positions_with_lifecycle_metrics(
        pos_list,
        cfg=cfg,
        now_ts=now_ts,
        persist=persist,
        broker=broker,
        strategy_name=strategy_name,
        coerce_positions=_live_service()._coerce_live_positions,
        apply_unrealized_pnl_fields_fn=_live_service()._apply_unrealized_pnl_fields,
        holding_summary_for_position=_live_service()._holding_summary_for_position,
        position_path_metrics_for_position=_live_service()._position_path_metrics_for_position,
        evaluate_position_supervisor_for_position=_live_service()._evaluate_position_supervisor_for_position,
    )


def supervisor_risk_context(
    position: dict[str, Any],
    verdict: dict[str, Any],
    *,
    cfg=None,
    mode: str = "live",
    broker_schedule: dict[str, Any] | None = None,
) -> dict[str, Any]:
    close_inputs = _lifecycle_build_supervisor_close_context_inputs(
        position=position,
        verdict=verdict,
        mode=mode,
        broker="ctrader",
    )
    close_context = _live_service()._build_close_position_risk_context(
        **close_inputs,
        cfg=cfg,
        broker_schedule=broker_schedule,
    )
    return _lifecycle_build_supervisor_risk_context_payload(
        close_context=close_context,
        position=position,
        verdict=verdict,
    )


def remember_supervisor_state(
    position: dict[str, Any],
    verdict: dict[str, Any],
    *,
    action_applied: str = "",
    broker: str = "ctrader",
    strategy_name: str = "",
) -> None:
    pid = int(position.get("position_id") or position.get("ticket") or 0)
    row = _live_service()._load_recovery_row_for_risk_reduction(pid, operation="remember_supervisor_state")
    try:
        live_close_settlement.upsert_recovery_position_state(
            position,
            **_lifecycle_build_supervisor_state_upsert_payload(
                recovery_row=row,
                verdict=verdict,
                broker=broker,
                strategy_name=strategy_name,
                loop_strategy_name=_live_service()._current_loop_strategy_name(),
                default_context_integrity=_live_service()._RECOVERY_CONTEXT_PARTIAL,
                action_applied=action_applied,
                applied_ts=time.time() if action_applied else 0.0,
            ),
        )
    except Exception as exc:
        live_close_settlement.record_risk_reduction_aux_failure(
            "risk_reduction_state_persist_failed",
            position_id=pid,
            action="remember_supervisor_state",
            error=exc,
        )


def supervisor_recently_applied(position_id: int, action: str, cooldown_seconds: float = 300.0) -> bool:
    row = _live_service()._load_recovery_row_for_risk_reduction(
        position_id,
        operation="supervisor_cooldown",
    )
    meta = dict((row or {}).get("recovery_meta") or {})
    return _lifecycle_supervisor_recently_applied_from_meta(
        recovery_meta=meta,
        action=action,
        now_ts=time.time(),
        cooldown_seconds=cooldown_seconds,
    )


def supervisor_noop_fingerprint_seen(position_id: int, fingerprint: str) -> bool:
    row = _live_service()._load_recovery_row_for_risk_reduction(
        position_id,
        operation="supervisor_noop_fingerprint",
    )
    return _lifecycle_supervisor_noop_fingerprint_seen(
        recovery_meta=dict((row or {}).get("recovery_meta") or {}),
        fingerprint=fingerprint,
    )


def supervisor_adaptive_duplicate_seen(
    position_id: int,
    verdict: dict[str, Any],
) -> bool:
    """Suppress repeated discretionary recommendations in one bar/episode."""

    evidence = dict(verdict.get("evidence") or {})
    closed_bar_key = str(evidence.get("closed_bar_key") or "")
    trigger_key = "|".join(
        sorted({str(item) for item in evidence.get("trigger_tags") or [] if str(item)})
    )
    if not closed_bar_key or not trigger_key:
        return False
    if is_hard_supervisor_action(
        action=str(verdict.get("requested_action") or verdict.get("action") or ""),
        summary_reason=str(verdict.get("summary_reason") or ""),
        evidence=evidence,
    ):
        return False
    fingerprint = str(verdict.get("action_fingerprint") or "")
    if not fingerprint:
        return False
    row = _live_service()._load_recovery_row_for_risk_reduction(
        int(position_id or 0),
        operation="supervisor_adaptive_duplicate",
    )
    meta = dict((row or {}).get("recovery_meta") or {})
    return bool(
        str(meta.get("supervisor_last_adaptive_closed_bar_key") or "")
        == closed_bar_key
        and str(meta.get("supervisor_last_adaptive_trigger_key") or "")
        == trigger_key
        and str(meta.get("supervisor_last_adaptive_fingerprint") or "")
        == fingerprint
        and str(meta.get("supervisor_posture") or "")
        == str(evidence.get("supervisor_posture") or "")
    )


def remember_supervisor_noop(position: dict[str, Any], verdict: dict[str, Any], *, fingerprint: str, reason: str) -> None:
    pid = int(position.get("position_id") or position.get("ticket") or 0)
    remember_supervisor_state(
        position,
        verdict,
        broker="ctrader",
        strategy_name=_live_service()._current_loop_strategy_name(),
    )
    live_close_settlement.merge_recovery_position_meta(
        pid,
        {
            "last_supervisor_noop_fingerprint": str(fingerprint or ""),
            "last_supervisor_noop_reason": str(reason or ""),
            "last_supervisor_noop_ts": time.time(),
        },
    )


def log_supervisor_decision(
    *,
    position: dict[str, Any],
    verdict: dict[str, Any],
    risk_verdict: dict[str, Any] | None,
    acct: dict | None,
    cfg,
    event_type: str,
    tick: int,
) -> str:
    if not _live_service()._LEDGER:
        return ""
    try:
        return _live_service()._LEDGER.log_decision(
            **_lifecycle_build_supervisor_decision_ledger_payload(
                position=position,
                verdict=verdict,
                risk_state=live_close_settlement.risk_state_with_verdict_dict(risk_verdict or {}),
                risk_verdict=risk_verdict,
                account=acct,
                cfg=cfg,
                event_type=event_type,
                tick=tick,
                session_pnl=_live_service().live_state_get("session_pnl", 0.0),
                fallback_decision_ts=time.time(),
            )
        )
    except Exception as exc:
        _live_service().logger.warning("[live] supervisor ledger failed for pos {}: {}", position.get("position_id"), exc)
        return ""


def log_protection_candidate_superseded(
    candidate: ProtectionCandidate,
    *,
    cfg,
    tick: int,
    reason: str,
    acct: dict | None = None,
) -> None:
    if not candidate.position:
        return
    trace_fields = _lifecycle_build_protection_superseded_trace_fields(
        candidate_payload=asdict(candidate),
        risk_action=candidate.risk_action,
        reason=reason,
    )
    log_supervisor_trace(
        position=candidate.position,
        verdict=_live_service()._candidate_verdict(candidate),
        cfg=cfg,
        tick=tick,
        **trace_fields,
        acct=acct,
    )


def _supervisor_tighten_sl_plan(position: dict[str, Any], target_sl: float, quote: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        from config.runtime_config import shared as _runtime_cfg

        cfg = _runtime_cfg()
        policy = {
            "min_stop_distance_points": getattr(cfg, "supervisor_min_stop_distance_points", 0.20),
            "stop_safety_buffer_ratio": getattr(cfg, "supervisor_stop_safety_buffer_ratio", 0.00008),
            "min_tighten_delta_points": getattr(cfg, "supervisor_min_tighten_delta_points", 0.01),
            "quote_max_age_seconds": getattr(cfg, "supervisor_quote_max_age_seconds", 10.0),
        }
    except Exception:
        policy = {}
    return _lifecycle_build_supervisor_tighten_sl_plan(
        **_lifecycle_build_supervisor_tighten_sl_plan_inputs(
            position=position,
            target_sl=target_sl,
            quote=quote,
            policy=policy,
        ),
    )


def _target_tp_is_extension(position: dict[str, Any], target_tp: float) -> bool:
    return _lifecycle_target_tp_is_extension(
        **_lifecycle_build_target_tp_extension_inputs(
            position=position,
            target_tp=target_tp,
        ),
    )


def log_supervisor_position_event(
    *,
    position: dict[str, Any],
    event_type: str,
    details: dict[str, Any],
    realized_pnl: float = 0.0,
) -> None:
    if not _live_service()._LEDGER:
        return
    try:
        _live_service()._LEDGER.log_position_event(
            **_lifecycle_build_supervisor_position_event_payload(
                position=position,
                event_type=event_type,
                details=details,
                realized_pnl=realized_pnl,
            )
        )
    except Exception as exc:
        _live_service().logger.debug("[live] supervisor position event {} failed for pos {}: {}", event_type, position.get("position_id"), exc)


def log_supervisor_trace(
    *,
    position: dict[str, Any],
    verdict: dict[str, Any],
    cfg,
    tick: int,
    stage: str,
    outcome: str,
    decision_id: str = "",
    risk_action: str = "",
    risk_verdict: dict[str, Any] | None = None,
    execution_status: str = "",
    execution_reason: str = "",
    execution: dict[str, Any] | None = None,
    acct: dict | None = None,
) -> str:
    if not _live_service()._LEDGER:
        return ""
    try:
        return _live_service()._LEDGER.log_position_supervisor_trace(
            **_lifecycle_build_supervisor_trace_ledger_payload(
                position=position,
                verdict=verdict,
                cfg=cfg,
                tick=tick,
                stage=stage,
                outcome=outcome,
                decision_id=decision_id,
                risk_action=risk_action,
                risk_verdict=risk_verdict,
                execution_status=execution_status,
                execution_reason=execution_reason,
                execution=execution,
                account=acct,
                fallback_event_ts=time.time(),
            )
        )
    except Exception as exc:
        _live_service().logger.opt(exception=True).warning(
            "[live] supervisor trace failed for pos {}: {}",
            position.get("position_id"),
            exc,
        )
        return ""


def log_supervisor_evaluation(
    *,
    position_id: int,
    verdict: dict[str, Any],
    tick: int,
    decision_ts: float | None = None,
) -> str:
    """Bar-deduplicated lean evaluation ledger row (see ledger method)."""
    if not _live_service()._LEDGER:
        return ""
    try:
        evidence = dict((verdict or {}).get("evidence") or {})
        bar_key = str(evidence.get("closed_bar_key") or "")
        if not bar_key:
            return ""
        pid = int(position_id or 0)
        if _live_service()._supervisor_evaluation_bars.get(pid) == bar_key:
            return ""
        _live_service()._supervisor_evaluation_bars[pid] = bar_key
        return _live_service()._LEDGER.log_position_supervisor_evaluation(
            position_id=str(pid),
            event_ts=float(decision_ts if decision_ts is not None else time.time()),
            verdict=verdict,
        )
    except Exception as exc:
        _live_service().logger.opt(exception=True).warning(
            "[live] supervisor evaluation ledger failed for pos {}: {}",
            position_id,
            exc,
        )
        return ""


def delegate_timeout_supervisor_close(
    *,
    position: dict[str, Any],
    verdict: dict[str, Any],
    cfg: Any,
    tick: int,
    acct: dict[str, Any],
    broker_schedule: dict[str, Any] | None = None,
) -> bool:
    pid = int(position.get("position_id") or position.get("ticket") or 0)
    timeout_context = _live_service()._build_close_position_risk_context(
        position_id=pid,
        close_reason="holding_timeout",
        mode="live",
        broker="ctrader",
        symbol=str(position.get("symbol") or "XAUUSD+"),
        position=position,
        cfg=cfg,
        broker_schedule=broker_schedule,
    )
    timeout_limit_seconds = float(timeout_context.get("max_holding_seconds", 0.0) or 0.0)
    log_supervisor_trace(
        position=position,
        verdict=verdict,
        cfg=cfg,
        tick=tick,
        stage="timeout_delegated",
        outcome="skipped",
        execution_status="delegated",
        execution_reason="main_timeout_path",
        execution={"timeout_context": timeout_context},
        acct=acct,
    )
    return bool(
        timeout_limit_seconds > 0
        and _lifecycle_holding_timeout_is_expired(timeout_context)
    )
