"""Governed position protection orchestration outside the live façade."""



from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class PositionProtectionCycleRuntime:
    enforce_holding_timeout: Callable[..., Any]
    entry_protection_repair_candidates: Callable[..., Any]
    log_candidate_superseded: Callable[..., Any]
    execute_candidate: Callable[..., Any]
    run_position_supervision: Callable[..., Any]
    protection_candidate_to_safety: Callable[[Any], Any]
    build_cycle_result: Callable[..., dict[str, Any]]
    record_aux_failure: Callable[..., Any]
    warning: Callable[..., Any]
    now: Callable[[], float]


def run_position_protection_cycle(
    bridge: Any,
    positions: list[Any],
    *,
    cfg: Any,
    account: dict[str, Any],
    pipeline: dict[str, Any],
    current_price: float,
    atr_price: float,
    tick: int,
    log: Callable[..., Any],
    runtime: PositionProtectionCycleRuntime,
    decision_ts: float | None = None,
) -> dict[str, Any]:
    """Run timeout, entry repair and supervisor in fixed priority.
from __future__ import annotations

    ``legacy_awe_trailing`` is historical evidence only.  It is deliberately
    not collected, arbitrated, written, or executed by the live cycle.
    """

    if not positions or bridge is None or cfg is None:
        return {
            "timeout": [],
            "entry_repair": [],
            "supervisor": [],
            "trailing_applied": [],
            "trailing_superseded": [],
        }

    cycle_ts = float(decision_ts if decision_ts is not None else runtime.now())
    stage_errors: list[dict[str, str]] = []
    selected_candidates: list[Any] = []
    arbitration: list[dict[str, Any]] = []

    def record_selected(candidate: Any, *, priority: int) -> None:
        if any(item.fingerprint == candidate.fingerprint for item in selected_candidates):
            return
        selected_candidates.append(candidate)
        arbitration.append(
            {
                "fingerprint": candidate.fingerprint,
                "decision": "selected",
                "priority": int(priority),
            }
        )

    def record_superseded(candidate: Any, *, priority: int, reason: str) -> None:
        arbitration.append(
            {
                "fingerprint": candidate.fingerprint,
                "decision": "superseded",
                "priority": int(priority),
                "reason": str(reason or ""),
            }
        )

    def record_stage_error(
        stage: str,
        exc: Exception,
        *,
        position_id: int = 0,
    ) -> None:
        runtime.warning(
            "[live] protection stage %s failed%s: %s",
            stage,
            f" for pos {position_id}" if position_id else "",
            exc,
        )
        stage_errors.append(
            {
                "stage": stage,
                "position_id": str(int(position_id or 0)),
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        runtime.record_aux_failure(
            "position_protection_stage_failed",
            position_id=position_id,
            action=stage,
            error=exc,
        )

    try:
        timeout_handled = runtime.enforce_holding_timeout(
            bridge,
            positions,
            cfg=cfg,
            tick=tick,
            log=log,
            decision_ts=cycle_ts,
            candidate_recorder=lambda candidate: record_selected(
                candidate,
                priority=10,
            ),
        )
    except Exception as exc:
        record_stage_error("holding_timeout", exc)
        timeout_handled = set()
    try:
        entry_repair_candidates = runtime.entry_protection_repair_candidates(
            positions,
            current_price=current_price,
            tick=tick,
            decision_ts=cycle_ts,
        )
    except Exception as exc:
        record_stage_error("entry_protection_candidate_collection", exc)
        entry_repair_candidates = []
    entry_repair_applied: set[int] = set()
    for candidate in sorted(entry_repair_candidates, key=lambda item: item.priority):
        if candidate.position_id in timeout_handled:
            runtime.log_candidate_superseded(
                candidate,
                cfg=cfg,
                tick=tick,
                reason="holding_timeout",
                acct=account,
            )
            record_superseded(
                runtime.protection_candidate_to_safety(candidate),
                priority=20,
                reason="holding_timeout",
            )
            continue
        try:
            if runtime.execute_candidate(
                candidate,
                bridge=bridge,
                cfg=cfg,
                tick=tick,
                log=log,
                acct=account,
            ):
                entry_repair_applied.add(candidate.position_id)
                record_selected(
                    runtime.protection_candidate_to_safety(candidate),
                    priority=20,
                )
        except Exception as exc:
            record_stage_error(
                "entry_protection_execution",
                exc,
                position_id=int(candidate.position_id or 0),
            )

    try:
        supervisor_handled = runtime.run_position_supervision(
            bridge,
            positions,
            cfg=cfg,
            acct=account,
            tick=tick,
            log=log,
            skip_position_ids=set(timeout_handled) | set(entry_repair_applied),
            preaudited_skip_position_ids=(
                set(timeout_handled) | set(entry_repair_applied)
            ),
            decision_ts=cycle_ts,
            candidate_recorder=lambda candidate: record_selected(
                candidate,
                priority=30,
            ),
            record_partial_close_execution=(
                getattr(pipeline.get("attribution"), "record_partial_close", None)
            ),
        )
    except Exception as exc:
        record_stage_error("position_supervisor", exc)
        supervisor_handled = set()
    result = runtime.build_cycle_result(
        timeout_handled=set(timeout_handled),
        entry_repair_applied=entry_repair_applied,
        supervisor_handled=set(supervisor_handled),
        trailing_applied=set(),
        trailing_superseded=set(),
    )
    if stage_errors:
        result["stage_errors"] = stage_errors
    selected_candidates.sort(
        key=lambda item: (item.position_id, item.action, item.fingerprint)
    )
    result["safety_candidates"] = [asdict(item) for item in selected_candidates]
    result["safety_arbitration"] = arbitration
    return result


def _live_service():
    from backend.services import live_service as _module
    return _module


from backend.services.live_close_settlement import track_pending_close_ids

# moved from live_service (2026-09-12 structural repair)

@dataclass
class ProtectionCandidate:
    source: str
    action: str
    priority: int
    position_id: int
    risk_action: str
    controls: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    position: dict[str, Any] = field(default_factory=dict)
    config_version: int = 0
    config_hash: str = ""

"""Live close-settlement family: runtime_kv plumbing, session state restore,
deferred-close latches, pending-close bookkeeping, close reason/verdict memory,
recovery sync/replay/retirement, and daily-drawdown evaluation.

Extracted from ``backend.services.live_service`` (2026-09-12 structural repair,
AST-precise move, public names).  The live loop keeps calling these through the
settlement public API; the only back-references are function-local lazy imports
for loop-owned helpers (reconcile publisher, risk-reduction aux recorder,
loss-streak book, bridge risk context).
"""

from backend.services import live_close_settlement
from backend.services.live_position_lifecycle import (
    build_holding_timeout_result_trace_fields as _lifecycle_build_holding_timeout_result_trace_fields,
    build_holding_timeout_verdict_payload as _lifecycle_build_holding_timeout_verdict_payload,
    build_protection_candidate_risk_context_from_candidate as _lifecycle_build_protection_candidate_risk_context_from_candidate,
    build_protection_execution_plan as _lifecycle_build_protection_execution_plan,
    build_protection_execution_result_payloads as _lifecycle_build_protection_execution_result_payloads,
    holding_timeout_is_expired as _lifecycle_holding_timeout_is_expired,
    market_open_seconds_between as _lifecycle_market_open_seconds_between,
    update_entry_protection_plan_payload as _lifecycle_update_entry_protection_plan_payload,
)
from backend.services.live_reconciliation import (
    explicit_position_reconcile as _explicit_position_reconcile,
    verify_position_protection_projection as _verify_position_protection_projection,
)
from backend.services.live_safety_planner import safety_candidate
from loguru import logger
from typing import Any, Mapping
from typing import Any
import time
from backend.services import live_safety_watchdog
from backend.services import live_supervision_runtime
from backend.services.live_state_store import (
    live_state_get,
)


def update_entry_protection_plan_status(
    position_id: int,
    *,
    status: str,
    error: str = "",
    attempted: bool = False,
    applied_sl: float = 0.0,
    applied_tp: float = 0.0,
) -> None:
    row = live_close_settlement.load_recovery_position_row(int(position_id))
    meta = dict((row or {}).get("recovery_meta") or {})
    plan = dict(meta.get("entry_protection_plan") or {})
    if not plan:
        return
    now = time.time()
    plan = _lifecycle_update_entry_protection_plan_payload(
        plan=plan,
        status=status,
        updated_at=now,
        error=error,
        attempted=attempted,
        applied_sl=applied_sl,
        applied_tp=applied_tp,
    )
    meta["entry_protection_plan"] = plan
    live_close_settlement.merge_recovery_position_meta(int(position_id), meta)


def _entry_protection_repair_candidates(
    pos: list,
    *,
    current_price: float,
    tick: int,
    decision_ts: float | None = None,
) -> list[ProtectionCandidate]:
    now_ts = float(decision_ts if decision_ts is not None else time.time())
    candidates: list[ProtectionCandidate] = []
    for p in pos or []:
        if not isinstance(p, dict):
            continue
        try:
            pid = int(p.get("position_id") or p.get("ticket") or 0)
        except Exception:
            pid = 0
        if pid <= 0:
            continue
        row = live_close_settlement.load_recovery_position_row(pid)
        meta = dict((row or {}).get("recovery_meta") or {})
        plan = dict(meta.get("entry_protection_plan") or {})
        if plan.get("schema_version") != _live_service()._ENTRY_PROTECTION_PLAN_SCHEMA:
            # 恢复仓/计划持久化失败导致的裸仓: 用 preflight 同款回撤距离
            # (atr 缺失时 price*2%/3%)补一份 plan, 让下方修复机制在冷却
            # 约束下自动挂保护; 只处理 broker 侧确无 SL 的仓位。
            if row and _live_service()._float_payload_value(p, "sl", "stop_loss", "stopLoss") <= 0:
                direction = int(_live_service()._direction_from_position(p) or 0)
                entry_price = float(p.get("open_price") or current_price or 0.0)
                if direction and entry_price > 0:
                    sl_dist = entry_price * 0.02
                    tp_dist = entry_price * 0.03
                    recovery_plan = _live_service()._entry_protection_plan_payload(
                        position_id=pid,
                        direction=direction,
                        entry_price=entry_price,
                        target_stop_loss=(
                            entry_price - sl_dist if direction > 0 else entry_price + sl_dist
                        ),
                        target_take_profit=(
                            entry_price + tp_dist if direction > 0 else entry_price - tp_dist
                        ),
                        requested_volume=float(p.get("volume") or 0.0),
                        actual_api_volume=float(p.get("volume") or 0.0),
                        tick=int(tick or 0),
                        status="pending",
                        source="recovered_no_protection",
                    )
                    try:
                        live_close_settlement.merge_recovery_position_meta(pid, {"entry_protection_plan": recovery_plan})
                        plan = dict(recovery_plan)
                    except Exception as exc:
                        logger.error(
                            "[live] recovered protection plan persist failed pos={}: {}",
                            pid,
                            exc,
                        )
            if plan.get("schema_version") != _live_service()._ENTRY_PROTECTION_PLAN_SCHEMA:
                continue
        target_sl = float(plan.get("target_stop_loss") or 0.0)
        target_tp = float(plan.get("target_take_profit") or 0.0)
        if target_sl <= 0 and target_tp <= 0:
            continue
        last_attempt_ts = float(plan.get("last_attempt_ts") or 0.0)
        if last_attempt_ts > 0 and now_ts - last_attempt_ts < _live_service()._ENTRY_PROTECTION_REPAIR_COOLDOWN_SECONDS:
            continue
        direction = int(plan.get("direction") or _live_service()._direction_from_position(p) or 0)
        current_sl = _live_service()._float_payload_value(p, "sl", "stop_loss", "stopLoss")
        current_tp = _live_service()._float_payload_value(p, "tp", "take_profit", "takeProfit")
        needs_sl = False
        if target_sl > 0:
            if current_sl <= 0:
                needs_sl = True
            elif direction > 0 and target_sl > current_sl + 0.01:
                needs_sl = True
            elif direction < 0 and target_sl < current_sl - 0.01:
                needs_sl = True
        needs_tp = bool(target_tp > 0 and current_tp <= 0)
        if not needs_sl and not needs_tp:
            if str(plan.get("status") or "") != "applied":
                try:
                    update_entry_protection_plan_status(
                        pid,
                        status="applied",
                        applied_sl=current_sl,
                        applied_tp=current_tp,
                    )
                except Exception as exc:
                    logger.debug("[live] entry protection applied-state update failed pos={}: {}", pid, exc)
            continue
        anchor = _live_service()._runtime_config_anchor()
        candidates.append(
            ProtectionCandidate(
                source=_live_service()._ENTRY_PROTECTION_REPAIR_SOURCE,
                action="repair_entry_protection",
                priority=10,
                position_id=pid,
                risk_action="tighten_position",
                controls={
                    "target_stop_loss": round(target_sl, 2) if target_sl > 0 else 0.0,
                    "target_take_profit": round(target_tp, 2) if target_tp > 0 else 0.0,
                    "close_reason": _live_service()._ENTRY_PROTECTION_REPAIR_SOURCE,
                    "protection_mode": "entry_sltp_repair",
                },
                evidence={
                    "tick": int(tick or 0),
                    "current_price": round(float(current_price or 0.0), 2),
                    "current_sl": round(float(current_sl or 0.0), 2),
                    "current_tp": round(float(current_tp or 0.0), 2),
                    "target_sl": round(float(target_sl or 0.0), 2),
                    "target_tp": round(float(target_tp or 0.0), 2),
                    "needs_sl": needs_sl,
                    "needs_tp": needs_tp,
                    "plan_status": str(plan.get("status") or ""),
                    "plan_attempts": int(plan.get("attempts") or 0),
                    "confidence": 1.0,
                },
                reason="entry_protection_missing_on_broker",
                position=dict(p),
                config_version=int(anchor.get("config_version") or 0),
                config_hash=str(anchor.get("config_hash") or ""),
            )
        )
    return candidates


def _log_protection_execution_payloads(
    *,
    position: dict[str, Any],
    verdict_payload: dict[str, Any],
    cfg: Any,
    tick: int,
    result_payloads: dict[str, Any],
    acct: dict | None,
    log_position_event: bool = True,
) -> None:
    if log_position_event and result_payloads.get("position_event_type"):
        live_supervision_runtime.log_supervisor_position_event(
            position=position,
            event_type=result_payloads["position_event_type"],
            details=result_payloads["position_event_details"],
        )
    live_supervision_runtime.log_supervisor_trace(
        position=position,
        verdict=verdict_payload,
        cfg=cfg,
        tick=tick,
        **result_payloads["trace_fields"],
        acct=acct,
    )


def _handle_protection_execution_skip(
    *,
    candidate: ProtectionCandidate,
    position: dict[str, Any],
    verdict_payload: dict[str, Any],
    risk_verdict: dict[str, Any],
    decision_id: str,
    candidate_payload: dict[str, Any],
    sl_plan: dict[str, Any],
    cfg: Any,
    tick: int,
    log,
    acct: dict | None,
) -> bool:
    result_payloads = _lifecycle_build_protection_execution_result_payloads(
        result="skipped",
        source=candidate.source,
        action=candidate.action,
        reason=candidate.reason,
        risk_action=candidate.risk_action,
        risk_verdict=risk_verdict,
        decision_id=decision_id,
        candidate_payload=candidate_payload,
        sl_plan=sl_plan,
        controls=candidate.controls,
    )
    _log_protection_execution_payloads(
        position=position,
        verdict_payload=verdict_payload,
        cfg=cfg,
        tick=tick,
        result_payloads=result_payloads,
        acct=acct,
    )
    log(f"tick {tick}: protection {candidate.source} SKIP pos={candidate.position_id} reason={sl_plan.get('reason')}")
    return True


def _mark_entry_protection_plan_after_execution(
    *,
    candidate: ProtectionCandidate,
    pid: int,
    status: str,
    attempted: bool,
    applied_sl: float = 0.0,
    applied_tp: float = 0.0,
    error: str = "",
) -> None:
    if candidate.source != _live_service()._ENTRY_PROTECTION_REPAIR_SOURCE:
        return
    try:
        update_entry_protection_plan_status(
            pid,
            status=status,
            attempted=attempted,
            applied_sl=applied_sl,
            applied_tp=applied_tp,
            error=error,
        )
    except Exception as exc:
        logger.debug("[live] entry protection {} update failed pos={}: {}", status, pid, exc)


def _handle_protection_execution_applied(
    *,
    candidate: ProtectionCandidate,
    position: dict[str, Any],
    verdict_payload: dict[str, Any],
    risk_verdict: dict[str, Any],
    decision_id: str,
    candidate_payload: dict[str, Any],
    sl_plan: dict[str, Any],
    target_sl: float,
    planned_sl: float,
    current_tp: float,
    cfg: Any,
    tick: int,
    log,
    acct: dict | None,
) -> bool:
    pid = int(candidate.position_id or 0)
    _live_service()._track_local_sl_tp(pid, sl=planned_sl, tp=current_tp)
    _mark_entry_protection_plan_after_execution(
        candidate=candidate,
        pid=pid,
        status="applied",
        attempted=True,
        applied_sl=planned_sl,
        applied_tp=current_tp,
    )
    _live_service()._remember_protection_state(
        position,
        verdict_payload,
        source=candidate.source,
        action_applied=candidate.action,
        broker="ctrader",
        strategy_name=_live_service()._current_loop_strategy_name(),
    )
    result_payloads = _lifecycle_build_protection_execution_result_payloads(
        result="applied",
        source=candidate.source,
        action=candidate.action,
        reason=candidate.reason,
        risk_action=candidate.risk_action,
        risk_verdict=risk_verdict,
        decision_id=decision_id,
        candidate_payload=candidate_payload,
        sl_plan=sl_plan,
        controls=candidate.controls,
        target_stop_loss_original=target_sl,
        target_stop_loss_sent=planned_sl,
        target_take_profit_sent=current_tp,
    )
    _log_protection_execution_payloads(
        position=position,
        verdict_payload=verdict_payload,
        cfg=cfg,
        tick=tick,
        result_payloads=result_payloads,
        acct=acct,
    )
    log(f"tick {tick}: protection {candidate.source} pos={pid} sl->{planned_sl:.2f} tp->{current_tp:.2f}")
    return True


def _handle_protection_execution_failed(
    *,
    candidate: ProtectionCandidate,
    position: dict[str, Any],
    verdict_payload: dict[str, Any],
    risk_verdict: dict[str, Any],
    decision_id: str,
    candidate_payload: dict[str, Any],
    sl_plan: dict[str, Any],
    reason: str,
    cfg: Any,
    tick: int,
    log,
    acct: dict | None,
) -> bool:
    pid = int(candidate.position_id or 0)
    _mark_entry_protection_plan_after_execution(
        candidate=candidate,
        pid=pid,
        status="failed",
        attempted=True,
        error=reason,
    )
    result_payloads = _lifecycle_build_protection_execution_result_payloads(
        result="failed",
        source=candidate.source,
        action=candidate.action,
        reason=candidate.reason,
        risk_action=candidate.risk_action,
        risk_verdict=risk_verdict,
        decision_id=decision_id,
        candidate_payload=candidate_payload,
        sl_plan=sl_plan,
        controls=candidate.controls,
        failure_reason=reason,
    )
    _log_protection_execution_payloads(
        position=position,
        verdict_payload=verdict_payload,
        cfg=cfg,
        tick=tick,
        result_payloads=result_payloads,
        acct=acct,
    )
    log(f"tick {tick}: protection {candidate.source} AMEND FAILED pos={pid}: {reason}")
    return True


def _prepare_protection_candidate_execution(
    *,
    candidate: ProtectionCandidate,
    bridge: Any,
    cfg: Any,
    tick: int,
    acct: dict | None,
) -> dict[str, Any] | None:
    position = dict(candidate.position or {})
    pid = int(candidate.position_id or 0)
    if pid <= 0 or not position:
        return None
    verdict_payload = _live_service()._candidate_verdict(candidate)
    close_context = _live_service()._build_close_position_risk_context(
        position_id=pid,
        close_reason=str(candidate.controls.get("close_reason") or candidate.source),
        mode="live",
        broker="ctrader",
        symbol=str(position.get("symbol") or "XAUUSD+"),
        position=position,
        cfg=cfg,
        broker_schedule=_live_service()._broker_schedule_from_bridge(bridge),
    )
    risk_context = _lifecycle_build_protection_candidate_risk_context_from_candidate(
        close_context=close_context,
        position=position,
        candidate=candidate,
        loop_running=bool(live_state_get("loop_running", True)),
        bridge_connected=bool(getattr(bridge, "is_connected", False)),
    )
    risk_verdict = _live_service()._evaluate_risk_reduction_policy(candidate.risk_action, risk_context).to_dict()
    decision_id = live_supervision_runtime.log_supervisor_decision(
        position=position,
        verdict=verdict_payload,
        risk_verdict=risk_verdict,
        acct=acct,
        cfg=cfg,
        event_type=candidate.source,
        tick=tick,
    )
    return {
        "position": position,
        "pid": pid,
        "verdict_payload": verdict_payload,
        "risk_verdict": risk_verdict,
        "decision_id": decision_id,
        "candidate_payload": asdict(candidate),
    }


def execute_protection_candidate(
    candidate: ProtectionCandidate,
    *,
    bridge,
    cfg,
    tick: int,
    log,
    acct: dict | None = None,
) -> bool:
    # AWE trailing is historical evidence only. Keep an explicit guard at
    # the last generic candidate boundary so a stale caller cannot create a
    # new decision/trace or reach RiskPolicy/broker execution.
    if candidate.source == "legacy_awe_trailing":
        log(f"tick {tick}: retired protection candidate ignored pos={candidate.position_id}")
        return False
    prepared = _prepare_protection_candidate_execution(
        candidate=candidate,
        bridge=bridge,
        cfg=cfg,
        tick=tick,
        acct=acct,
    )
    if not prepared:
        return False
    position = prepared["position"]
    pid = int(prepared["pid"])
    verdict_payload = prepared["verdict_payload"]
    risk_verdict = prepared["risk_verdict"]
    decision_id = prepared["decision_id"]
    candidate_payload = prepared["candidate_payload"]
    if not risk_verdict.get("allowed", False):
        result_payloads = _lifecycle_build_protection_execution_result_payloads(
            result="risk_rejected",
            source=candidate.source,
            action=candidate.action,
            reason=candidate.reason,
            risk_action=candidate.risk_action,
            risk_verdict=risk_verdict,
            decision_id=decision_id,
            candidate_payload=candidate_payload,
        )
        _log_protection_execution_payloads(
            position=position,
            verdict_payload=verdict_payload,
            cfg=cfg,
            tick=tick,
            result_payloads=result_payloads,
            acct=acct,
            log_position_event=False,
        )
        return True

    quote = bridge.get_spot_quote() if hasattr(bridge, "get_spot_quote") else {}
    execution_plan = _lifecycle_build_protection_execution_plan(
        position=position,
        controls=candidate.controls,
        source=candidate.source,
        entry_protection_repair_source=_live_service()._ENTRY_PROTECTION_REPAIR_SOURCE,
        quote=quote,
    )
    target_sl = float(execution_plan.get("target_sl") or 0.0)
    current_tp = float(execution_plan.get("current_tp") or 0.0)
    planned_sl = float(execution_plan.get("planned_sl") or 0.0)
    sl_plan = execution_plan.get("sl_plan") or {}
    if not sl_plan["allowed"]:
        return _handle_protection_execution_skip(
            candidate=candidate,
            position=position,
            verdict_payload=verdict_payload,
            risk_verdict=risk_verdict,
            decision_id=decision_id,
            candidate_payload=candidate_payload,
            sl_plan=sl_plan,
            cfg=cfg,
            tick=tick,
            log=log,
            acct=acct,
        )

    try:
        amend_res = bridge.amend_position_sltp(pid, sl=planned_sl, tp=current_tp)
    except Exception as exc:
        amend_res = type("AmendResult", (), {"success": False, "comment": str(exc)})()
    if getattr(amend_res, "success", False):
        projection = _explicit_position_reconcile(bridge)
        verification = _verify_position_protection_projection(
            projection,
            position_id=pid,
            expected_stop_loss=planned_sl,
            expected_take_profit=current_tp,
            precision=int(position.get("digits", 2) or 2),
        )
        if bool(verification.get("ok")):
            _live_service()._publish_fresh_position_reconcile(projection, broker="ctrader")
            return _handle_protection_execution_applied(
                candidate=candidate,
                position=position,
                verdict_payload=verdict_payload,
                risk_verdict=risk_verdict,
                decision_id=decision_id,
                candidate_payload=candidate_payload,
                sl_plan=sl_plan,
                target_sl=target_sl,
                planned_sl=planned_sl,
                current_tp=current_tp,
                cfg=cfg,
                tick=tick,
                log=log,
                acct=acct,
            )

        projection_reason = str(
            verification.get("reason") or "position_reconcile_failed"
        )
        failure_reason = f"amend_projection_unverified:{projection_reason}"
        live_close_settlement.record_risk_reduction_aux_failure(
            "protection_amend_projection_unverified",
            position_id=pid,
            action=candidate.action,
            error=failure_reason,
            payload={
                "source": candidate.source,
                "verification": verification,
            },
        )
        live_safety_watchdog.persist_safety_fail_closed(
            blockers=("amend_projection_unverified",),
            source="protection_amend",
            error=failure_reason,
        )
        return _handle_protection_execution_failed(
            candidate=candidate,
            position=position,
            verdict_payload=verdict_payload,
            risk_verdict=risk_verdict,
            decision_id=decision_id,
            candidate_payload=candidate_payload,
            sl_plan=sl_plan,
            reason=failure_reason,
            cfg=cfg,
            tick=tick,
            log=log,
            acct=acct,
        )
    reason = str(getattr(amend_res, "comment", "") or getattr(amend_res, "error", "") or "amend_failed")
    return _handle_protection_execution_failed(
        candidate=candidate,
        position=position,
        verdict_payload=verdict_payload,
        risk_verdict=risk_verdict,
        decision_id=decision_id,
        candidate_payload=candidate_payload,
        sl_plan=sl_plan,
        reason=reason,
        cfg=cfg,
        tick=tick,
        log=log,
        acct=acct,
    )


def is_deterministic_market_closed_rejection(reason: str) -> bool:
    text = str(reason or "").upper()
    return any(pattern in text for pattern in _live_service()._MARKET_CLOSED_ERROR_PATTERNS)


def market_closed_deferral_active(recovery_meta: Mapping[str, Any], now_ts: float) -> bool:
    """True when a recent deterministic market-closed rejection is suppressing retries."""

    meta = dict(recovery_meta or {})
    if str(meta.get(_live_service().MARKET_CLOSED_DEFER_REASON_KEY) or "") != "market_closed_pending":
        return False
    try:
        last_ts = float(meta.get(_live_service().MARKET_CLOSED_DEFER_TS_KEY, 0.0) or 0.0)
        elapsed = float(now_ts) - last_ts
    except (TypeError, ValueError):
        return False
    return last_ts > 0 and 0.0 <= elapsed < _live_service().MARKET_CLOSED_DEFER_HEARTBEAT_SECONDS


def _defer_market_closed_holding_timeout(
    *,
    position: dict,
    pid: int,
    cfg,
    tick: int,
    now_ts: float,
    holding_seconds: float,
    max_holding_seconds: float,
    market_open_holding: float,
) -> None:
    """Trace a timeout deferral once per closed-market episode.

    The verdict stays "close when the market reopens"; only the futile
    per-tick repetition is suppressed.  A single trace is emitted when the
    deferral starts, then again only if the wall-clock holding time grows by
    more than an hour (a bounded heartbeat) — never one row per tick.
    """

    verdict_payload = _lifecycle_build_holding_timeout_verdict_payload(
        position_id=pid,
        decision_ts=now_ts,
        holding_seconds=holding_seconds,
        max_holding_seconds=max_holding_seconds,
    )
    meta = dict(
        (_live_service()._load_recovery_row_for_risk_reduction(pid, operation="timeout_defer") or {}).get(
            "recovery_meta"
        )
        or {}
    )
    if market_closed_deferral_active(meta, now_ts):
        return
    execution = {
        "close_deferred": True,
        "defer_reason": "market_closed_pending",
        "wall_clock_holding_seconds": round(holding_seconds, 3),
        "market_open_holding_seconds": round(market_open_holding, 3),
        "max_holding_seconds": round(max_holding_seconds, 3),
        "applied_controls": {"close_reason": "holding_timeout"},
        "duplicate_audit": False,
    }
    live_supervision_runtime.log_supervisor_trace(
        position=position,
        verdict=verdict_payload,
        cfg=cfg,
        tick=tick,
        stage="execution_deferred",
        outcome="skipped",
        risk_action="close_position",
        execution_status="deferred",
        execution_reason="market_closed_pending",
        execution=execution,
    )
    try:
        live_close_settlement.merge_recovery_position_meta(
            pid,
            {
                _live_service().MARKET_CLOSED_DEFER_REASON_KEY: "market_closed_pending",
                _live_service().MARKET_CLOSED_DEFER_TS_KEY: now_ts,
                "market_closed_defer_wall_holding_seconds": round(holding_seconds, 3),
                "market_closed_defer_open_holding_seconds": round(market_open_holding, 3),
            },
        )
    except Exception as exc:
        live_close_settlement.record_risk_reduction_aux_failure(
            "risk_reduction_state_persist_failed",
            position_id=pid,
            action="holding_timeout_defer",
            error=exc,
        )


def enforce_holding_timeout(
    bridge,
    pos: list,
    *,
    cfg,
    tick: int,
    log,
    decision_ts: float | None = None,
    candidate_recorder=None,
) -> set[int]:
    handled: set[int] = set()
    max_holding_bars = int(getattr(cfg, "risk_max_holding_bars", 0) or 0)
    if max_holding_bars <= 0:
        return handled

    now_ts = float(decision_ts if decision_ts is not None else time.time())
    for p in pos or []:
        try:
            pid = int(p.get("position_id") or p.get("ticket") or 0)
        except Exception:
            pid = 0
        if pid <= 0:
            continue

        close_context = _live_service()._build_close_position_risk_context(
            position_id=pid,
            close_reason="holding_timeout",
            mode="live",
            broker="ctrader",
            symbol=str(p.get("symbol") or "XAUUSD+"),
            position=p,
            cfg=cfg,
            decision_ts=now_ts,
            broker_schedule=_live_service()._broker_schedule_from_bridge(bridge),
        )
        max_holding_seconds = float(close_context.get("max_holding_seconds", 0.0) or 0.0)
        holding_seconds = float(close_context.get("holding_seconds", 0.0) or 0.0)

        # A previously recorded market-closed deferral/rejection stays
        # suppressed until its hourly heartbeat falls due; the position keeps
        # its broker-side SL/TP protection meanwhile.
        defer_meta = dict(
            (
                _live_service()._load_recovery_row_for_risk_reduction(
                    pid, operation="timeout_defer"
                )
                or {}
            ).get("recovery_meta")
            or {}
        )
        if market_closed_deferral_active(defer_meta, now_ts):
            handled.add(pid)
            continue

        # Timeout is a wall-clock budget, but the close must be executable to
        # be a protection.  A position whose wall-clock holding time crossed
        # the limit only because the market was closed for part of the window
        # is deferred, not closed: every attempt during the closure is
        # deterministically rejected (MARKET_CLOSED) and would otherwise
        # retry every tick until reopen.
        market_budget = dict(close_context.get("market_time_budget") or {})
        entry_ts_for_budget = float(close_context.get("entry_ts", 0.0) or 0.0)
        market_open_holding = float(
            market_budget.get("market_open_holding_seconds", 0.0) or 0.0
        )
        if not market_budget and entry_ts_for_budget > 0:
            market_open_holding = _lifecycle_market_open_seconds_between(
                entry_ts_for_budget,
                now_ts,
                symbol=str(p.get("symbol") or "XAUUSD+"),
                broker_schedule=_live_service()._broker_schedule_from_bridge(bridge),
            )
        if (
            market_budget.get("market_closed_pending")
            and market_open_holding < max_holding_seconds
        ):
            _defer_market_closed_holding_timeout(
                position=dict(p),
                pid=pid,
                cfg=cfg,
                tick=tick,
                now_ts=now_ts,
                holding_seconds=holding_seconds,
                max_holding_seconds=max_holding_seconds,
                market_open_holding=market_open_holding,
            )
            handled.add(pid)
            continue

        if not _lifecycle_holding_timeout_is_expired(close_context):
            continue

        if candidate_recorder is not None:
            candidate_recorder(
                safety_candidate(
                    action="timeout",
                    position_id=pid,
                    source="holding_timeout",
                    controls={"close_reason": "holding_timeout"},
                )
            )

        close_verdict = _live_service()._evaluate_risk_reduction_policy("close_position", close_context)
        verdict_payload = _lifecycle_build_holding_timeout_verdict_payload(
            position_id=pid,
            decision_ts=now_ts,
            holding_seconds=holding_seconds,
            max_holding_seconds=max_holding_seconds,
        )
        decision_id = live_supervision_runtime.log_supervisor_decision(
            position=dict(p),
            verdict=verdict_payload,
            risk_verdict=close_verdict.to_dict(),
            acct=None,
            cfg=cfg,
            event_type="holding_timeout",
            tick=tick,
        )
        if not close_verdict.allowed:
            logger.warning("[live] holding timeout close blocked pos={} reason={}", pid, close_verdict.reason)
            live_supervision_runtime.log_supervisor_trace(
                position=dict(p),
                verdict=verdict_payload,
                cfg=cfg,
                tick=tick,
                **_lifecycle_build_holding_timeout_result_trace_fields(
                    result="risk_rejected",
                    decision_id=decision_id,
                    risk_verdict=close_verdict.to_dict(),
                    execution_reason=close_verdict.reason,
                ),
            )
            handled.add(pid)
            continue
        try:
            # ``pos`` is the broker snapshot consumed by the safety cycle.
            # Pass its API volume so a transient auxiliary reconcile failure
            # cannot suppress the close-only timeout escape hatch.
            result = bridge.close_position(
                pid,
                volume=float(_live_service()._position_api_volume(p) or 0.0),
            )
        except Exception as exc:
            logger.warning("[live] holding timeout close exception pos={}: {}", pid, exc)
            live_supervision_runtime.log_supervisor_trace(
                position=dict(p),
                verdict=verdict_payload,
                cfg=cfg,
                tick=tick,
                **_lifecycle_build_holding_timeout_result_trace_fields(
                    result="exception",
                    decision_id=decision_id,
                    risk_verdict=close_verdict.to_dict(),
                    execution_reason=str(exc),
                ),
            )
            handled.add(pid)
            continue
        if getattr(result, "success", False):
            live_close_settlement.remember_close_reason(pid, "holding_timeout")
            live_close_settlement.remember_close_verdict(pid, close_verdict)
            handled.add(pid)
            live_supervision_runtime.log_supervisor_trace(
                position=dict(p),
                verdict=verdict_payload,
                cfg=cfg,
                tick=tick,
                **_lifecycle_build_holding_timeout_result_trace_fields(
                    result="applied",
                    decision_id=decision_id,
                    risk_verdict=close_verdict.to_dict(),
                ),
            )
            log(
                f"tick {tick}: holding timeout close sent pos={pid} "
                f"held={holding_seconds:.0f}s limit={max_holding_seconds:.0f}s"
            )
        else:
            handled.add(pid)
            failure_reason = str(
                getattr(result, "comment", "") or getattr(result, "error", "") or "close_failed"
            )
            error_code = str(getattr(result, "error_code", "") or "")
            rejection_text = f"{error_code} {failure_reason}"
            if is_deterministic_market_closed_rejection(rejection_text):
                try:
                    live_close_settlement.merge_recovery_position_meta(
                        pid,
                        {
                            _live_service().MARKET_CLOSED_DEFER_REASON_KEY: "market_closed_pending",
                            _live_service().MARKET_CLOSED_DEFER_TS_KEY: now_ts,
                            "market_closed_close_rejection": rejection_text[:300],
                        },
                    )
                except Exception as exc:
                    live_close_settlement.record_risk_reduction_aux_failure(
                        "risk_reduction_state_persist_failed",
                        position_id=pid,
                        action="holding_timeout_market_closed_rejection",
                        error=exc,
                    )
            live_supervision_runtime.log_supervisor_trace(
                position=dict(p),
                verdict=verdict_payload,
                cfg=cfg,
                tick=tick,
                **_lifecycle_build_holding_timeout_result_trace_fields(
                    result="failed",
                    decision_id=decision_id,
                    risk_verdict=close_verdict.to_dict(),
                    execution_reason=str(getattr(result, "comment", "") or getattr(result, "error", "") or "close_failed"),
                ),
            )
    return handled


def consume_pending_close_kwargs(kwargs: dict) -> dict:
    """Store pre-update hook: apply pending-close bookkeeping kwargs."""
    pending_add = kwargs.pop('session_pending_close_add', None)
    pending_remove = kwargs.pop('session_pending_close_remove', None)
    if pending_add is not None or pending_remove:
        track_pending_close_ids(pending_add, pending_remove)
    return kwargs
