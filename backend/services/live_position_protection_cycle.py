"""Legacy-authoritative position protection orchestration outside the live façade."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class PositionProtectionCycleRuntime:
    update_trailing_stops: Callable[..., Any]
    enforce_holding_timeout: Callable[..., Any]
    entry_protection_repair_candidates: Callable[..., Any]
    log_candidate_superseded: Callable[..., Any]
    execute_candidate: Callable[..., Any]
    run_position_supervision: Callable[..., Any]
    protection_candidate_to_safety: Callable[[Any], Any]
    candidate_supersede_reason: Callable[..., str]
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
    """Run timeout, entry repair, supervisor, and trailing in fixed priority."""

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

    trailing_candidates: list[Any] = []
    if atr_price > 0:
        try:
            trailing_candidates = runtime.update_trailing_stops(
                bridge,
                positions,
                current_price,
                pipeline,
                atr_price,
                tick,
                log,
            )
        except Exception as exc:
            record_stage_error("trailing_candidate_collection", exc)

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
    protected_pids = (
        set(timeout_handled) | set(entry_repair_applied) | set(supervisor_handled)
    )
    trailing_applied: set[int] = set()
    trailing_superseded: set[int] = set()
    for candidate in sorted(trailing_candidates, key=lambda item: item.priority):
        if str(getattr(cfg, "autonomy_mode", "") or "").strip().lower() in {
            "demo_autonomous",
            "demo_nursery",
        }:
            trailing_superseded.add(candidate.position_id)
            runtime.log_candidate_superseded(
                candidate,
                cfg=cfg,
                tick=tick,
                reason="demo_adaptive_observation",
                acct=account,
            )
            record_superseded(
                runtime.protection_candidate_to_safety(candidate),
                priority=50,
                reason="demo_adaptive_observation",
            )
            continue
        supersede_reason = runtime.candidate_supersede_reason(
            position_id=candidate.position_id,
            timeout_handled=set(timeout_handled),
            protected_position_ids=protected_pids,
        )
        if supersede_reason:
            trailing_superseded.add(candidate.position_id)
            runtime.log_candidate_superseded(
                candidate,
                cfg=cfg,
                tick=tick,
                reason=supersede_reason,
                acct=account,
            )
            record_superseded(
                runtime.protection_candidate_to_safety(candidate),
                priority=50,
                reason=supersede_reason,
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
                trailing_applied.add(candidate.position_id)
                protected_pids.add(candidate.position_id)
                record_selected(
                    runtime.protection_candidate_to_safety(candidate),
                    priority=50,
                )
        except Exception as exc:
            record_stage_error(
                "trailing_execution",
                exc,
                position_id=int(candidate.position_id or 0),
            )

    result = runtime.build_cycle_result(
        timeout_handled=set(timeout_handled),
        entry_repair_applied=entry_repair_applied,
        supervisor_handled=set(supervisor_handled),
        trailing_applied=trailing_applied,
        trailing_superseded=trailing_superseded,
    )
    if stage_errors:
        result["stage_errors"] = stage_errors
    selected_candidates.sort(
        key=lambda item: (item.position_id, item.action, item.fingerprint)
    )
    result["safety_candidates"] = [asdict(item) for item in selected_candidates]
    result["safety_arbitration"] = arbitration
    return result
