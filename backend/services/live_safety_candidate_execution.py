"""Dispatch Safety Plane candidates through risk-reducing executors only."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SafetyCandidateExecutionRuntime:
    enforce_holding_timeout: Any
    entry_protection_repair_source: str
    runtime_config_anchor: Any
    protection_candidate_cls: Any
    execute_protection_candidate: Any
    evaluate_position_supervisor: Any
    build_safety_candidate: Any
    run_position_supervision: Any


def execute_live_safety_candidate(
    candidate: Any,
    *,
    bridge: Any,
    positions: list[dict[str, Any]],
    cfg: Any,
    account: dict[str, Any],
    pipeline: dict[str, Any],
    tick: int,
    log: Any,
    decision_ts: float,
    runtime: SafetyCandidateExecutionRuntime,
) -> dict[str, Any]:
    """Revalidate and dispatch one close/reduce/tighten candidate."""

    position_id = int(candidate.position_id or 0)
    position = next(
        (
            dict(item)
            for item in positions
            if int(item.get("position_id") or item.get("ticket") or 0)
            == position_id
        ),
        None,
    )
    if position is None:
        return {"ok": False, "status": "position_missing"}

    if candidate.action == "timeout":
        handled = runtime.enforce_holding_timeout(
            bridge,
            [position],
            cfg=cfg,
            tick=tick,
            log=log,
            decision_ts=decision_ts,
        )
        return {
            "ok": position_id in handled,
            "status": (
                "dispatched"
                if position_id in handled
                else "candidate_no_longer_due"
            ),
        }

    if candidate.action == "repair_entry_protection":
        return _execute_protection_candidate(
            candidate,
            position=position,
            position_id=position_id,
            bridge=bridge,
            cfg=cfg,
            account=account,
            tick=tick,
            log=log,
            runtime=runtime,
        )

    if candidate.action in {"close", "reduce", "tighten"}:
        return _execute_supervisor_candidate(
            candidate,
            position=position,
            position_id=position_id,
            bridge=bridge,
            positions=positions,
            cfg=cfg,
            account=account,
            pipeline=pipeline,
            tick=tick,
            log=log,
            decision_ts=decision_ts,
            runtime=runtime,
        )

    return {"ok": False, "status": "unsupported_safety_action"}


def _execute_protection_candidate(
    candidate: Any,
    *,
    position: dict[str, Any],
    position_id: int,
    bridge: Any,
    cfg: Any,
    account: dict[str, Any],
    tick: int,
    log: Any,
    runtime: SafetyCandidateExecutionRuntime,
) -> dict[str, Any]:
    source = runtime.entry_protection_repair_source
    anchor = runtime.runtime_config_anchor()
    protection = runtime.protection_candidate_cls(
        source=source,
        action="repair_entry_protection",
        priority=20,
        position_id=position_id,
        risk_action="tighten_position",
        controls=dict(candidate.controls or {}),
        reason=source,
        position=position,
        config_version=int(anchor.get("config_version") or 0),
        config_hash=str(anchor.get("config_hash") or ""),
    )
    applied = runtime.execute_protection_candidate(
        protection,
        bridge=bridge,
        cfg=cfg,
        tick=tick,
        log=log,
        acct=account,
    )
    return {
        "ok": bool(applied),
        "status": "dispatched" if applied else "not_applied",
    }


def _execute_supervisor_candidate(
    candidate: Any,
    *,
    position: dict[str, Any],
    position_id: int,
    bridge: Any,
    positions: list[dict[str, Any]],
    cfg: Any,
    account: dict[str, Any],
    pipeline: dict[str, Any],
    tick: int,
    log: Any,
    decision_ts: float,
    runtime: SafetyCandidateExecutionRuntime,
) -> dict[str, Any]:
    verdict = runtime.evaluate_position_supervisor(
        position,
        cfg=cfg,
        acct=account,
        now_ts=decision_ts,
        positions=positions,
        persist=False,
    )
    observed_action = str(verdict.get("action") or "hold").strip().lower()
    if observed_action not in {"close", "reduce", "tighten"}:
        return {
            "ok": False,
            "status": "candidate_changed_before_execution",
            "expected_fingerprint": candidate.fingerprint,
            "observed_action": observed_action,
        }
    refreshed = runtime.build_safety_candidate(
        action=observed_action,
        position_id=position_id,
        source=f"supervisor_{observed_action}",
        controls=dict(verdict.get("recommended_controls") or {}),
    )
    if refreshed.fingerprint != candidate.fingerprint:
        return {
            "ok": False,
            "status": "candidate_changed_before_execution",
            "expected_fingerprint": candidate.fingerprint,
            "observed_fingerprint": refreshed.fingerprint,
        }

    recorded: list[Any] = []
    handled = runtime.run_position_supervision(
        bridge,
        [position],
        cfg=cfg,
        acct=account,
        tick=tick,
        log=log,
        decision_ts=decision_ts,
        planned_verdicts={position_id: dict(verdict)},
        candidate_recorder=recorded.append,
        record_partial_close_execution=(
            getattr(pipeline.get("attribution"), "record_partial_close", None)
        ),
    )
    exact = any(
        item.fingerprint == candidate.fingerprint for item in recorded
    )
    return {
        "ok": position_id in handled and exact,
        "status": (
            "dispatched"
            if position_id in handled and exact
            else "not_applied"
        ),
    }
