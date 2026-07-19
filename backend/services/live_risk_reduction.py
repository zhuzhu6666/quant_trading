"""Fail-safe context and policy helpers for live risk-reducing actions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from risk.policy_service import RiskVerdict


_RISK_REDUCING_ACTIONS = {
    "close_position",
    "reduce_position",
    "tighten_position",
}


@dataclass(frozen=True)
class RiskReductionRuntime:
    append_safety_outbox: Any
    logger_error: Any
    logger_warning: Any
    now: Any
    config_factory: Any
    position_open_timestamp: Any
    lookup_open_decision_context: Any
    temporal_context_for_trade: Any
    build_close_context_payload: Any
    load_recovery_position_row: Any
    lookup_entry_decision_id: Any
    risk_policy: Any


def build_close_position_risk_context(
    *,
    position_id: int,
    close_reason: str,
    runtime: RiskReductionRuntime,
    mode: str = "live",
    broker: str = "",
    symbol: str = "",
    position: Any | None = None,
    cfg: Any = None,
    decision_ts: float | None = None,
) -> dict[str, Any]:
    """Build close context with broker position time as primary truth."""

    if cfg is None:
        try:
            cfg = runtime.config_factory()
        except Exception:
            cfg = None
    now_ts = float(decision_ts or runtime.now())
    broker_entry_ts = runtime.position_open_timestamp(position)
    open_meta: dict[str, Any] = {}
    try:
        open_meta = dict(
            runtime.lookup_open_decision_context(int(position_id)) or {}
        )
    except Exception as exc:
        _record_close_context_enrichment_failure(
            runtime=runtime,
            position_id=position_id,
            close_reason=close_reason,
            broker=broker,
            symbol=symbol,
            broker_entry_ts=broker_entry_ts,
            error=exc,
        )

    entry_ts = float(broker_entry_ts or 0.0) or float(
        open_meta.get("entry_ts", 0.0) or 0.0
    )
    entry_ts_source = (
        "broker_position"
        if float(broker_entry_ts or 0.0) > 0
        else str(open_meta.get("source") or "")
    )
    timeframe = str(
        open_meta.get("timeframe")
        or getattr(cfg, "timeframe", "M5")
        or "M5"
    )
    temporal_context = runtime.temporal_context_for_trade(
        decision_ts=now_ts,
        timeframe=timeframe,
    )
    max_holding_bars = int(
        getattr(cfg, "risk_max_holding_bars", 0) or 0
    )
    return runtime.build_close_context_payload(
        position_id=position_id,
        close_reason=close_reason,
        mode=mode,
        broker=broker,
        symbol=symbol,
        entry_ts=entry_ts,
        entry_ts_source=entry_ts_source,
        temporal_context=temporal_context,
        max_holding_bars=max_holding_bars,
    )


def record_risk_reduction_aux_failure(
    event_type: str,
    *,
    runtime: RiskReductionRuntime,
    position_id: int = 0,
    action: str = "",
    error: Exception | str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Persist auxiliary failures without changing a broker action result."""

    error_text = (
        f"{type(error).__name__}: {error}"
        if isinstance(error, Exception)
        else str(error or "")
    )
    try:
        runtime.append_safety_outbox(
            event_type=event_type,
            correlation_id=str(position_id or ""),
            payload={
                "position_id": int(position_id or 0),
                "action": str(action or ""),
                **dict(payload or {}),
            },
            error=error_text,
        )
    except Exception as outbox_exc:
        runtime.logger_error(
            "[live] risk-reduction auxiliary failure and safety outbox "
            "failure: event=%s error=%s outbox=%s",
            event_type,
            error_text,
            outbox_exc,
        )


def load_recovery_row_for_risk_reduction(
    position_id: int,
    *,
    operation: str,
    runtime: RiskReductionRuntime,
) -> dict[str, Any]:
    try:
        return dict(runtime.load_recovery_position_row(int(position_id)) or {})
    except Exception as exc:
        record_risk_reduction_aux_failure(
            "risk_reduction_pg_enrichment_failed",
            position_id=position_id,
            action=operation,
            error=exc,
            runtime=runtime,
        )
        return {}


def lookup_entry_context_for_risk_reduction(
    position_id: int,
    *,
    operation: str,
    runtime: RiskReductionRuntime,
) -> dict[str, Any]:
    try:
        return dict(
            runtime.lookup_open_decision_context(int(position_id)) or {}
        )
    except Exception as exc:
        record_risk_reduction_aux_failure(
            "risk_reduction_entry_context_failed",
            position_id=position_id,
            action=operation,
            error=exc,
            runtime=runtime,
        )
        return {}


def lookup_entry_decision_for_risk_reduction(
    position_id: int,
    *,
    operation: str,
    runtime: RiskReductionRuntime,
) -> str:
    try:
        return str(runtime.lookup_entry_decision_id(int(position_id)) or "")
    except Exception as exc:
        record_risk_reduction_aux_failure(
            "risk_reduction_entry_decision_failed",
            position_id=position_id,
            action=operation,
            error=exc,
            runtime=runtime,
        )
        return ""


def evaluate_risk_reduction_policy(
    action: str,
    context: dict[str, Any],
    *,
    runtime: RiskReductionRuntime,
) -> RiskVerdict:
    """Continue close/reduce/tighten when policy infrastructure fails."""

    normalized = str(action or "").strip().lower()
    if normalized not in _RISK_REDUCING_ACTIONS:
        return runtime.risk_policy.evaluate(normalized, context)
    try:
        return runtime.risk_policy.evaluate(normalized, context)
    except Exception as exc:
        position_id = int(context.get("position_id") or 0)
        record_risk_reduction_aux_failure(
            "risk_reduction_policy_unavailable",
            position_id=position_id,
            action=normalized,
            error=exc,
            payload={
                "close_reason": str(context.get("close_reason") or "")
            },
            runtime=runtime,
        )
        return RiskVerdict(
            allowed=True,
            reason="risk_policy_unavailable_risk_reduction_continues",
            severity="warning",
            required_mode="risk_reduction_only",
            audit_payload={
                "action": normalized,
                "source": "risk_reduction_fail_safe",
                "position_id": position_id,
                "policy_error": f"{type(exc).__name__}: {exc}",
            },
        )


def _record_close_context_enrichment_failure(
    *,
    runtime: RiskReductionRuntime,
    position_id: int,
    close_reason: str,
    broker: str,
    symbol: str,
    broker_entry_ts: float,
    error: Exception,
) -> None:
    try:
        runtime.append_safety_outbox(
            event_type="close_risk_context_enrichment_failed",
            payload={
                "position_id": int(position_id),
                "close_reason": str(close_reason or ""),
                "broker": str(broker or ""),
                "symbol": str(symbol or ""),
                "broker_entry_ts": float(broker_entry_ts or 0.0),
            },
            error=f"{type(error).__name__}: {error}",
        )
    except Exception as outbox_exc:
        runtime.logger_error(
            "[live] close risk context PG failure and safety outbox "
            "failure: pg=%s outbox=%s",
            error,
            outbox_exc,
        )
    runtime.logger_warning(
        "[live] close risk context PG enrichment unavailable position=%s: %s",
        position_id,
        error,
    )
