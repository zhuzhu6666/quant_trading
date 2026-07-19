"""Open-risk context assembly outside the live-service façade."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class OpenRiskContextRuntime:
    state_get: Any
    collect_runtime_health: Any
    temporal_context_for_trade: Any
    active_supervisor_reentry_block: Any
    recent_review_reentry_block: Any
    pending_supervisor_reentry_block: Any
    build_entry_cluster_context: Any
    active_entry_quality_policy: Any
    entry_quality_gate: Any
    build_payload: Any
    tracked_total_api_volume: Any
    active_event_window_policy: Any
    active_entry_cluster_policy: Any
    max_abs_entry_score: Any
    now: Any


def build_open_trade_risk_context(
    *,
    cfg: Any,
    bridge: Any,
    account: dict[str, Any],
    positions: list[Any],
    requested_api_volume: float,
    signal_score: float,
    runtime: OpenRiskContextRuntime,
    symbol: str = "XAUUSD",
    direction: int = 0,
    current_price: float = 0.0,
    atr_price: float = 0.0,
    event_sizing_context: dict[str, Any] | None = None,
    event_filter_context: dict[str, Any] | None = None,
    decision_quality_context: dict[str, Any] | None = None,
    decision_ts: float | None = None,
) -> dict[str, Any]:
    risk_snapshot = runtime.state_get("risk", {}, clone=True) or {}
    loop_running = bool(runtime.state_get("loop_running", True))
    bridge_connected = bool(getattr(bridge, "is_connected", False))
    now_ts = float(runtime.now())
    timeframe = str(getattr(cfg, "timeframe", "M5") or "M5")
    runtime_health_context = runtime.collect_runtime_health(
        timeframe=timeframe,
        now_ts=now_ts,
        account_updated_at=float(
            runtime.state_get("account_updated_at", 0.0) or 0.0
        ),
        positions_updated_at=float(
            runtime.state_get("positions_updated_at", 0.0) or 0.0
        ),
    )
    temporal_context = runtime.temporal_context_for_trade(
        decision_ts=float(decision_ts or now_ts),
        evaluated_at_ts=now_ts,
        timeframe=timeframe,
        session_last_trade_ts=float(
            runtime.state_get("session_last_trade_ts", 0.0) or 0.0
        ),
        loop_started_at=float(
            runtime.state_get("loop_started_at", 0.0) or 0.0
        ),
    )
    active_supervisor_block = runtime.active_supervisor_reentry_block(
        symbol=symbol,
        direction=direction,
    )
    retrospective_block = runtime.recent_review_reentry_block(
        symbol=symbol,
        direction=direction,
        now_ts=now_ts,
    )
    pending_supervisor_block = runtime.pending_supervisor_reentry_block(
        positions or [],
        symbol=symbol,
        direction=direction,
        cfg=cfg,
    )
    supervisor_reentry_block = (
        pending_supervisor_block
        or active_supervisor_block
        or retrospective_block
    )
    entry_cluster_context = runtime.build_entry_cluster_context(
        positions_before=positions or [],
        direction=direction,
        symbol=symbol,
        now_ts=now_ts,
        new_position_id=0,
        new_api_volume=0.0,
    )
    timeframe_seconds = float(
        temporal_context.get("timeframe_seconds", 0.0) or 0.0
    )
    same_direction_cooldown_seconds = max(
        60.0,
        float(int(getattr(cfg, "risk_cooldown_bars", 3) or 3))
        * (timeframe_seconds or 300.0),
    )
    decision_quality = dict(decision_quality_context or {})
    entry_quality_gate = runtime.entry_quality_gate(
        policy=runtime.active_entry_quality_policy(now_ts=now_ts),
        decision_quality=decision_quality,
        signal_score=float(signal_score or 0.0),
    )
    decision_freshness = (
        runtime.state_get("decision_bar_freshness", {}, clone=True) or {}
    )

    return runtime.build_payload(
        cfg=cfg,
        acct=account,
        positions=positions,
        requested_api_volume=requested_api_volume,
        signal_score=signal_score,
        symbol=symbol,
        direction=direction,
        current_price=current_price,
        atr_price=atr_price,
        risk_snapshot=risk_snapshot,
        session_state={
            "pnl": runtime.state_get("session_pnl", 0.0),
            "start_balance": runtime.state_get("session_start_balance", 0.0),
            "trades": runtime.state_get("session_trades", 0),
            "consecutive_losses": runtime.state_get(
                "session_consecutive_loss", 0
            ),
            "drawdown_pct": runtime.state_get(
                "session_max_drawdown_pct", 0.0
            ),
            "circuit_breaker": runtime.state_get("circuit_breaker", False),
        },
        total_api_volume=runtime.tracked_total_api_volume(positions or []),
        event_sizing_context=event_sizing_context,
        event_filter_context=event_filter_context,
        event_window_learning_policy=runtime.active_event_window_policy(
            now_ts=now_ts
        ),
        entry_quality_gate=entry_quality_gate,
        entry_cluster_context=entry_cluster_context,
        entry_cluster_learning_policy=runtime.active_entry_cluster_policy(
            now_ts=now_ts
        ),
        same_direction_cooldown_seconds=same_direction_cooldown_seconds,
        max_abs_entry_score=runtime.max_abs_entry_score(positions or []),
        loop_running=loop_running,
        bridge_connected=bridge_connected,
        data_lag_seconds=float(
            runtime_health_context.get("data_lag_seconds", 0.0) or 0.0
        ),
        runtime_health=runtime_health_context.get("runtime_health", {}) or {},
        temporal_context=temporal_context,
        decision_freshness=decision_freshness,
        supervisor_reentry_block=supervisor_reentry_block,
    )
