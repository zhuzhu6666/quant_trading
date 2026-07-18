"""Position-supervision orchestration outside the live-service façade.

The runtime object makes every stateful or broker-facing dependency explicit.
This keeps the serial mutation order testable while preserving the live
service's compatibility entrypoint and monkeypatch boundaries.
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
    floor_api_volume_to_step: Any
    should_full_close_untradeable_reduce: Any
    build_close_position_risk_context: Any
    remember_close_reason: Any
    remember_close_verdict: Any
    capture_partial_close_session_cursor: Any
    sync_partial_close_session_fact: Any


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
    record_partial_close_execution: Any = None,
    decision_ts: float | None = None,
    candidate_recorder: Any = None,
    planned_verdicts: dict[int, dict[str, Any]] | None = None,
) -> set[int]:
    """Evaluate and serially dispatch risk-reducing supervisor actions."""

    handled: set[int] = set()
    skipped = set(skip_position_ids or set())
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
            continue

        action = str(verdict.get("action") or "hold")
        if action == "hold":
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
            continue

        controls = verdict.get("recommended_controls") or {}
        if action in {"close", "reduce", "tighten"} and candidate_recorder is not None:
            try:
                candidate_recorder(
                    runtime.make_candidate(
                        action=action,
                        position_id=position_id,
                        source=f"supervisor_{action}",
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
        if runtime.recently_applied(position_id, action):
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
                if not runtime.noop_fingerprint_seen(position_id, fingerprint):
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
                    floor_api_volume_to_step=runtime.floor_api_volume_to_step,
                    should_full_close_untradeable_reduce=(
                        runtime.should_full_close_untradeable_reduce
                    ),
                    build_close_position_risk_context=(
                        runtime.build_close_position_risk_context
                    ),
                    risk_policy_evaluate=runtime.evaluate_risk_policy,
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
